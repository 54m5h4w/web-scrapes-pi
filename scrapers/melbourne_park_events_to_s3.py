import json
import os
import re
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

from common.aws import get_s3_client
from common.logging_utils import get_logger
from common.s3_paths import utc_iso, build_latest_key, build_log_key, build_raw_key
from common.schema import build_dataset_payload, build_record, build_run_log


# =========================
# CONFIG
# =========================

BASE_URL = "https://www.melbournepark.com.au/events/"
HTTP_HEADERS = {
    "User-Agent": os.getenv(
        "MP_USER_AGENT",
        "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://www.melbournepark.com.au/",
}
REQUEST_TIMEOUT = int(os.getenv("MP_REQUEST_TIMEOUT", "30"))
MAX_PAGES = int(os.getenv("MP_MAX_PAGES", "20"))
S3_BUCKET = os.getenv("S3_BUCKET", "event-scrape-data")

DATASET = "melbourne-park-events"
ACCESS_LEVEL = "public"
ALLOWED_ROLES = ["staff", "supervisor", "manager", "admin"]
RECORD_TYPE = "public_event"
SCRAPER_NAME = "melbourne-park-events-v1"
SOURCE_NAME = "Melbourne Park"
SOURCE_URL = BASE_URL
FILTER_LABEL = "Melbourne Park"


DEFAULT_AUDIENCE = ["Public"]
DEFAULT_LOCATION_SEARCH = "Melbourne Park Melbourne VIC Australia"
DEFAULT_CATEGORY = "Public Event"
WEEKDAYS = r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"


# =========================
# LOGGING / AWS
# =========================

logger = get_logger(__name__)
s3 = get_s3_client(logger=logger)


# =========================
# HELPERS
# =========================

def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def day_name_from_date_str(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")


def strip_weekday(value: str) -> str:
    return re.sub(rf"^{WEEKDAYS}\s+", "", value.strip(), flags=re.I).strip()


def normalise_category(category: str) -> str:
    value = clean_text(category)
    if value.lower() == "football":
        return "Soccer"
    return value or DEFAULT_CATEGORY


def parse_time_24h(time_text: str) -> tuple[str | None, str | None]:
    """
    Returns (start_24h, end_24h) as HH:MM or (None, None) if unknown.
    Supports examples like:
      6:00PM to 9:00PM
      6pm - 9pm
      18:00 to 21:00
      MULTIPLE TIMES
    """
    value = clean_text(time_text)
    if not value:
        return None, None
    if "multiple times" in value.lower():
        return None, None

    value = value.replace("–", "-").replace("—", "-")
    value = re.sub(r"\bto\b", "-", value, flags=re.I)

    tokens = re.findall(r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)|(\d{1,2}:\d{2})", value, flags=re.I)
    parts = [(left or right).strip() for left, right in tokens if (left or right)]

    if not parts:
        return None, None

    def to_24(token: str) -> str | None:
        token = token.strip().lower().replace(" ", "")

        if re.fullmatch(r"\d{1,2}:\d{2}", token):
            hour_str, minute_str = token.split(":")
            hour = int(hour_str)
            minute = int(minute_str)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{hour:02d}:{minute:02d}"
            return None

        match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)", token)
        if not match:
            return None

        hour = int(match.group(1))
        minute = int(match.group(2) or "00")
        am_pm = match.group(3)

        if hour == 12:
            hour = 0
        if am_pm == "pm":
            hour += 12

        return f"{hour:02d}:{minute:02d}"

    start_time = to_24(parts[0]) if len(parts) >= 1 else None
    end_time = to_24(parts[1]) if len(parts) >= 2 else None
    return start_time, end_time


def expand_date_range(date_text: str) -> list[str]:
    """
    Returns a list of ISO dates (YYYY-MM-DD).

    Examples:
      Saturday 21 to Sunday 22 March 2026 -> [2026-03-21, 2026-03-22]
      Saturday 21 March 2026 -> [2026-03-21]
      21 March 2026 -> [2026-03-21]
    """
    value = clean_text(date_text).replace("–", "-").replace("—", "-")
    if not value:
        return []

    if " to " in value:
        left, right = value.split(" to ", 1)
        left = strip_weekday(left)
        right = strip_weekday(right)

        try:
            end_date = datetime.strptime(right, "%d %B %Y").date()
        except ValueError:
            return []

        match = re.search(r"(\d{1,2})", left)
        if not match:
            return []

        start_day = int(match.group(1))

        try:
            start_date = end_date.replace(day=start_day)
        except ValueError:
            return []

        dates = []
        current = start_date
        while current <= end_date:
            dates.append(current.isoformat())
            current += timedelta(days=1)
        return dates

    single_value = strip_weekday(value)
    try:
        return [datetime.strptime(single_value, "%d %B %Y").date().isoformat()]
    except ValueError:
        return []


def build_location_object(location_name: str) -> dict:
    name = clean_text(location_name) or "Melbourne Park"
    return {
        "code": "MELBOURNE_PARK",
        "search_text": f"{name} {DEFAULT_LOCATION_SEARCH}" if name.lower() != "melbourne park" else DEFAULT_LOCATION_SEARCH,
        "latitude": None,
        "longitude": None,
    }


# =========================
# SCRAPING
# =========================

def fetch_page(page: int) -> str:
    url = BASE_URL if page == 1 else f"{BASE_URL}?sf_paged={page}"

    session = requests.Session()
    session.headers.update(HTTP_HEADERS)

    response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)

    logger.info(
        "Melbourne Park fetch | page=%s status=%s url=%s content_type=%s",
        page,
        response.status_code,
        response.url,
        response.headers.get("content-type", ""),
    )

    if response.status_code == 403:
        snippet = response.text[:500].replace("\n", " ")
        raise RuntimeError(f"Melbourne Park returned 403 Forbidden. Body starts: {snippet}")

    response.raise_for_status()
    return response.text


def parse_events(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("#eventListing .card")
    rows = []

    for card in cards:
        category_el = card.select_one("p.small.fw-bold.text-uppercase")
        category = clean_text(category_el.get_text(" ", strip=True) if category_el else "")
        category = category.replace("Event on", "").strip()

        title_el = card.select_one("p.fw-bold.fs-4") or card.select_one("h3")
        title = clean_text(title_el.get_text(" ", strip=True) if title_el else "").rstrip(",")

        date_el = card.select_one("p.mb-0.fs-6.fw-bold")
        date_text = clean_text(date_el.get_text(" ", strip=True) if date_el else "")

        time_el = card.select_one("span.text-black.small")
        time_text = clean_text(time_el.get_text(" ", strip=True) if time_el else "")
        time_text = re.sub(r"^at\s+", "", time_text, flags=re.I)

        venue_el = card.select_one("p.mt-auto.text-uppercase.fw-bold.small")
        location_name = clean_text(venue_el.get_text(" ", strip=True) if venue_el else "") or "Melbourne Park"

        link_el = card.select_one("a[href]")
        href = (link_el.get("href") or "").strip() if link_el else ""
        if href.startswith("http"):
            source_url = href
        elif href:
            source_url = f"https://www.melbournepark.com.au{href}"
        else:
            source_url = SOURCE_URL

        if not title or not date_text:
            continue

        date_list = expand_date_range(date_text)
        if not date_list:
            logger.info(f"Skipping event with unparseable date: {title} | {date_text}")
            continue

        start_time, end_time = parse_time_24h(time_text)
        notes_parts = []
        if time_text:
            notes_parts.append(f"Displayed time: {time_text}")
        if date_text:
            notes_parts.append(f"Displayed date: {date_text}")
        notes = " || ".join(notes_parts)

        for event_date in date_list:
            rows.append(
                {
                    "title": title,
                    "date": event_date,
                    "start_time": start_time,
                    "end_time": end_time,
                    "location_name": location_name,
                    "location": build_location_object(location_name),
                    "category": normalise_category(category),
                    "source_url": source_url,
                    "notes": notes,
                }
            )

    return rows


# =========================
# RECORD BUILDING
# =========================

def build_event_records(parsed_rows: list[dict]) -> list[dict]:
    records = []

    for row in parsed_rows:
        records.append(
            build_record(
                title=row["title"],
                date=row["date"],
                day_name=day_name_from_date_str(row["date"]),
                start_time=row["start_time"],
                end_time=row["end_time"],
                location_name=row["location_name"],
                location=row["location"],
                categories=[row["category"]] if row.get("category") else [DEFAULT_CATEGORY],
                audience_type=DEFAULT_AUDIENCE,
                filter=FILTER_LABEL,
                source=SOURCE_NAME,
                source_url=row.get("source_url") or SOURCE_URL,
                record_type=RECORD_TYPE,
                scraper=SCRAPER_NAME,
                notes=row.get("notes", ""),
                access_level=ACCESS_LEVEL,
                dataset=DATASET,
                allowed_roles=ALLOWED_ROLES,
            )
        )

    records.sort(key=lambda r: (r["date"], r["start_time"] or "", r["title"]))
    logger.info(f"Built {len(records)} event records")
    return records


def dedupe_rows(rows: list[dict]) -> list[dict]:
    """
    Deduplicate Melbourne Park rows while preferring the real event page link
    over the generic Melbourne Park events listing URL.

    Current issue:
    - same event/date/location can appear twice
    - one row has the true venue/event URL
    - the other has SOURCE_URL fallback
    - old dedupe used source_url inside the key, so both survived

    This helper:
    - dedupes by title/date/time/location
    - keeps the row with the better source_url
    """

    def norm_text(value: str | None) -> str:
        return clean_text(value).casefold()

    def dedupe_key(row: dict) -> tuple:
        return (
            norm_text(row.get("title")),
            row.get("date") or "",
            row.get("start_time") or "",
            row.get("end_time") or "",
            norm_text(row.get("location_name")),
        )

    def score_row(row: dict) -> tuple:
        """
        Higher score wins.

        Priority:
        1. real event link beats fallback SOURCE_URL
        2. absolute http link beats blank/non-http
        3. longer notes as a light tie-breaker
        """
        source_url = (row.get("source_url") or "").strip()
        is_real_event_link = bool(source_url and source_url != SOURCE_URL)
        is_absolute_http = source_url.startswith("http")
        notes_len = len((row.get("notes") or "").strip())

        return (
            1 if is_real_event_link else 0,
            1 if is_absolute_http else 0,
            notes_len,
        )

    best_by_key: dict[tuple, dict] = {}

    for row in rows:
        key = dedupe_key(row)
        existing = best_by_key.get(key)

        if existing is None:
            best_by_key[key] = row
            continue

        if score_row(row) > score_row(existing):
            best_by_key[key] = row

    deduped = list(best_by_key.values())
    deduped.sort(key=lambda r: (r["date"], r.get("start_time") or "", r["title"]))
    return deduped


def build_payload(records: list[dict]) -> dict:
    return build_dataset_payload(
        dataset=DATASET,
        source=SOURCE_NAME,
        source_url=SOURCE_URL,
        record_type=RECORD_TYPE,
        scraper=SCRAPER_NAME,
        access_level=ACCESS_LEVEL,
        allowed_roles=ALLOWED_ROLES,
        records=records,
    )


# =========================
# S3 OUTPUT
# =========================

def upload_json_to_s3(key: str, payload: dict) -> None:
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def upload_payload(payload: dict) -> dict:
    raw_key = build_raw_key(ACCESS_LEVEL, DATASET)
    latest_key = build_latest_key(ACCESS_LEVEL, DATASET)

    upload_json_to_s3(raw_key, payload)
    upload_json_to_s3(latest_key, payload)

    return {
        "bucket": S3_BUCKET,
        "raw_key": raw_key,
        "latest_key": latest_key,
        "record_count": payload["record_count"],
    }


def upload_run_log_to_s3(run_log: dict) -> str:
    log_key = build_log_key(ACCESS_LEVEL, DATASET)
    upload_json_to_s3(log_key, run_log)
    return log_key


# =========================
# MAIN
# =========================

def main() -> None:
    started_at = utc_iso()
    pages_fetched = 0
    parsed_rows_count = 0
    records_uploaded = 0
    raw_key = None
    latest_key = None

    logger.info("Starting Melbourne Park events scraper")

    try:
        all_rows = []

        for page in range(1, MAX_PAGES + 1):
            logger.info(f"Fetching page {page}")
            html = fetch_page(page)
            pages_fetched += 1

            rows = parse_events(html)
            logger.info(f"Found {len(rows)} parsed event rows on page {page}")

            if not rows:
                break

            all_rows.extend(rows)

        deduped_rows = dedupe_rows(all_rows)
        parsed_rows_count = len(deduped_rows)
        logger.info(f"Parsed {parsed_rows_count} event rows after dedupe")

        records = build_event_records(deduped_rows)
        payload = build_payload(records)

        logger.info("Uploading dataset to S3")
        result = upload_payload(payload)
        records_uploaded = result["record_count"]
        raw_key = result["raw_key"]
        latest_key = result["latest_key"]

        finished_at = utc_iso()
        run_log = build_run_log(
            scraper=SCRAPER_NAME,
            dataset=DATASET,
            record_type=RECORD_TYPE,
            source=SOURCE_NAME,
            access_level=ACCESS_LEVEL,
            allowed_roles=ALLOWED_ROLES,
            s3_bucket=S3_BUCKET,
            started_at=started_at,
            finished_at=finished_at,
            status="ok",
            employees_fetched=parsed_rows_count,
            records_uploaded=records_uploaded,
            raw_key=raw_key,
            latest_key=latest_key,
            error=None,
        )
        run_log["pages_fetched"] = pages_fetched
        run_log["rows_parsed"] = parsed_rows_count

        log_key = upload_run_log_to_s3(run_log)

        logger.info(f"Upload complete: {latest_key}")
        logger.info(f"Run log written to: {log_key}")

        print(json.dumps({
            "status": "ok",
            "pages_fetched": pages_fetched,
            "rows_parsed": parsed_rows_count,
            "records_uploaded": records_uploaded,
            "bucket": S3_BUCKET,
            "raw_key": raw_key,
            "latest_key": latest_key,
            "log_key": log_key,
        }, indent=2))

    except Exception as exc:
        finished_at = utc_iso()
        logger.exception("Scraper failed")

        try:
            run_log = build_run_log(
                scraper=SCRAPER_NAME,
                dataset=DATASET,
                record_type=RECORD_TYPE,
                source=SOURCE_NAME,
                access_level=ACCESS_LEVEL,
                allowed_roles=ALLOWED_ROLES,
                s3_bucket=S3_BUCKET,
                started_at=started_at,
                finished_at=finished_at,
                status="error",
                employees_fetched=parsed_rows_count,
                records_uploaded=records_uploaded,
                raw_key=raw_key,
                latest_key=latest_key,
                error=str(exc),
            )
            run_log["pages_fetched"] = pages_fetched
            run_log["rows_parsed"] = parsed_rows_count
            log_key = upload_run_log_to_s3(run_log)
            logger.info(f"Error log written to: {log_key}")
        except Exception:
            logger.exception("Failed to write error log to S3")

        raise


if __name__ == "__main__":
    main()
