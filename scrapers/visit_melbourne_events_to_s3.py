import json
import os
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin
import time

from selenium.webdriver.support.ui import WebDriverWait

from common.aws import get_s3_client
from common.logging_utils import get_logger
from common.s3_paths import utc_iso, build_latest_key, build_log_key, build_raw_key
from common.schema import build_dataset_payload, build_record, build_run_log
from common.selenium_utils import build_chrome_driver


# =========================
# CONFIG
# =========================

BASE = "https://www.visitmelbourne.com"
LANDING_URL = os.getenv("VISIT_MELBOURNE_LANDING_URL", f"{BASE}/whats-on")
API_URL = os.getenv(
    "VISIT_MELBOURNE_API_URL",
    "https://www.visitmelbourne.com/api/feature/content/summarydisplay/search?Id=0485a22de13747bdadabc7cdf3922f7a&Maximum=50&Minimum=1&Sort=Random&DefaultSortKey=eca415fc6d634ef59d1497604262cc4f&ShowRegion=False&UseExternalProductLink=False&Seed=1022291849&Timestamp=638645622610000000&hash=63F8362311B7DEC429CBFC95ED83907A&numItems=12&startAt=0&facets=91d59066d5e2449ebd683bbcfed3a61b",
)
S3_BUCKET = os.getenv("S3_BUCKET", "event-scrape-data")

DATASET = "visit-melbourne-events"
ACCESS_LEVEL = "public"
ALLOWED_ROLES = ["staff", "supervisor", "manager", "admin"]
RECORD_TYPE = "public_event"
SCRAPER_NAME = "visit-melbourne-events-v1"
SOURCE_NAME = "Visit Melbourne"
SOURCE_URL = LANDING_URL

DEFAULT_AUDIENCE = ["Public"]
REQUEST_TIMEOUT_SECONDS = int(os.getenv("VISIT_MELBOURNE_WAIT_SECONDS", "45"))

UA = os.getenv(
    "VISIT_MELBOURNE_USER_AGENT",
    "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
)


# =========================
# LOGGING / AWS
# =========================

logger = get_logger(__name__)
s3 = get_s3_client(logger=logger)


# =========================
# HELPERS
# =========================

def clean_text(value: str | None) -> str:
    value = value or ""
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def day_name_from_date_str(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")


def absolute_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return urljoin(BASE, url)


def unique_keep_order(values: list[str]) -> list[str]:
    out = []
    seen = set()
    for value in values:
        value = clean_text(value)
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def build_location_object(item: dict) -> dict:
    town = clean_text(item.get("town"))
    lat = item.get("latitude")
    lon = item.get("longitude")

    search_parts = [
        town,
        "Melbourne",
        "Victoria",
        "Australia",
    ]
    search_text = " ".join([p for p in search_parts if p]).strip()

    code_bits = [town.upper().replace(" ", "-")] if town else ["MELBOURNE"]
    code = code_bits[0][:40]

    return {
        "code": code,
        "search_text": search_text,
        "latitude": lat,
        "longitude": lon,
    }


def parse_single_date_piece(text: str, default_year: int | None = None) -> datetime | None:
    text = clean_text(text)

    # Always parse with an explicit year to avoid Python 3.15 warnings/errors.
    if re.fullmatch(r"\d{1,2}\s+[A-Za-z]+", text):
        if default_year is None:
            return None
        text = f"{text} {default_year}"

    formats = [
        "%d %b %Y",
        "%d %B %Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    return None


def expand_event_date_text(text: str) -> list[str]:
    """
    Supports common Visit Melbourne formats such as:
    - 07 - 19 Apr 2026
    - 07 Apr 2026
    - 31 Mar - 02 Apr 2026
    - 31 Dec 2026 - 02 Jan 2027
    """
    text = clean_text(text)
    if not text:
        return []

    m = re.match(r"^(\d{1,2})\s*-\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$", text)
    if m:
        d1, d2, month_name, year = m.groups()
        start = parse_single_date_piece(f"{int(d1):02d} {month_name} {year}")
        end = parse_single_date_piece(f"{int(d2):02d} {month_name} {year}")
        if start and end and start <= end:
            return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((end - start).days + 1)]

    m = re.match(
        r"^(\d{1,2}\s+[A-Za-z]+(?:\s+\d{4})?)\s*-\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})$",
        text,
    )
    if m:
        start_text, end_text = m.groups()
        end_dt = parse_single_date_piece(end_text)
        start_dt = parse_single_date_piece(start_text, default_year=end_dt.year if end_dt else None)
        if start_dt and end_dt:
            if start_dt > end_dt and start_dt.year == end_dt.year:
                start_dt = start_dt.replace(year=start_dt.year - 1)
            if start_dt <= end_dt:
                return [(start_dt + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((end_dt - start_dt).days + 1)]

    dt = parse_single_date_piece(text)
    if dt:
        return [dt.strftime("%Y-%m-%d")]

    return []


def extract_dates(item: dict) -> list[str]:
    raw_dates = item.get("eventDates") or []
    if isinstance(raw_dates, str):
        raw_dates = [raw_dates]

    dates = []
    for raw in raw_dates:
        dates.extend(expand_event_date_text(raw))

    if not dates:
        fallback = clean_text(item.get("eventDate"))
        dates.extend(expand_event_date_text(fallback))

    return unique_keep_order(dates)


def build_notes(item: dict) -> str:
    parts = []

    short_desc = clean_text(item.get("shortDescription"))
    mini_desc = clean_text(item.get("miniDescription"))
    raw_event_date = clean_text(item.get("eventDate"))
    price_range = clean_text(item.get("priceRange") or item.get("formattedPrice"))
    town = clean_text(item.get("town"))
    label = clean_text(item.get("label"))
    book_link = absolute_url(item.get("bookLink"))
    image_src = absolute_url((item.get("image") or {}).get("src"))

    if short_desc:
        parts.append(f"Short description: {short_desc}")
    if mini_desc and mini_desc != short_desc:
        parts.append(f"Mini description: {mini_desc}")
    if raw_event_date:
        parts.append(f"Displayed event date: {raw_event_date}")
    if price_range:
        parts.append(f"Price: {price_range}")
    if town:
        parts.append(f"Town: {town}")
    if label:
        parts.append(f"Label: {label}")
    if book_link:
        parts.append(f"Book link: {book_link}")
    if image_src:
        parts.append(f"Image: {image_src}")

    return " || ".join(parts)


# =========================
# FETCH
# =========================

def fetch_json_via_selenium() -> dict:
    driver = build_chrome_driver(
        user_agent=UA,
        extra_args=[
            "--disable-blink-features=AutomationControlled",
        ],
    )
    wait = WebDriverWait(driver, REQUEST_TIMEOUT_SECONDS)

    try:
        logger.info("Opening API URL directly via browser")
        driver.get(API_URL)

        # Wait for page to load
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")

        time.sleep(2)

        # Get raw body text (this is your JSON)
        body_text = driver.find_element("tag name", "body").text.strip()

        if not body_text:
            raise RuntimeError("Empty response body from API")

        # Cloudflare detection
        lowered = body_text.lower()
        if "just a moment" in lowered or "<html" in lowered:
            raise RuntimeError(f"Blocked by Cloudflare: {body_text[:500]}")

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError:
            raise RuntimeError(f"Response was not valid JSON: {body_text[:500]}")

        logger.info(f"Fetched JSON successfully with {len(data.get('items', []))} items")
        return data

    finally:
        driver.quit()


# =========================
# PARSE / MAP
# =========================

def parse_items(payload: dict) -> list[dict]:
    items = payload.get("items") or []
    if not isinstance(items, list):
        raise RuntimeError(f"Expected 'items' list in payload, got: {type(items)}")
    return items


def build_event_records(items: list[dict]) -> tuple[list[dict], int]:
    records = []
    undated_items = 0
    seen = set()

    for item in items:
        title = clean_text(item.get("title"))
        if not title:
            continue

        dates = extract_dates(item)
        if not dates:
            undated_items += 1
            continue

        categories = unique_keep_order(
            [
                clean_text(item.get("label")),
                *([c for c in (item.get("contentTagsOrCategory") or []) if isinstance(c, str)]),
            ]
        ) or ["Public Event"]

        source_url = absolute_url(item.get("link")) or SOURCE_URL
        location_name = clean_text(item.get("town")) or "Melbourne"
        location = build_location_object(item)
        notes = build_notes(item)

        for event_date in dates:
            key = (item.get("id"), event_date)
            if key in seen:
                continue
            seen.add(key)

            records.append(
                build_record(
                    title=title,
                    date=event_date,
                    day_name=day_name_from_date_str(event_date),
                    start_time=None,
                    end_time=None,
                    location_name=location_name,
                    location=location,
                    categories=categories,
                    audience_type=DEFAULT_AUDIENCE,
                    source=SOURCE_NAME,
                    source_url=source_url,
                    record_type=RECORD_TYPE,
                    scraper=SCRAPER_NAME,
                    notes=notes,
                    access_level=ACCESS_LEVEL,
                    dataset=DATASET,
                    allowed_roles=ALLOWED_ROLES,
                )
            )

    records.sort(key=lambda r: (r["date"], r["title"]))
    logger.info(f"Built {len(records)} event records from {len(items)} source items")
    return records, undated_items


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
    items_fetched = 0
    records_uploaded = 0
    undated_items = 0
    raw_key = None
    latest_key = None

    logger.info("Starting Visit Melbourne events scraper")

    try:
        payload = fetch_json_via_selenium()
        items = parse_items(payload)
        items_fetched = len(items)
        logger.info(f"Fetched {items_fetched} source items")

        records, undated_items = build_event_records(items)
        dataset_payload = build_payload(records)

        logger.info("Uploading dataset to S3")
        result = upload_payload(dataset_payload)
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
            employees_fetched=items_fetched,
            records_uploaded=records_uploaded,
            raw_key=raw_key,
            latest_key=latest_key,
            error=None,
        )
        run_log["items_fetched"] = items_fetched
        run_log["undated_items"] = undated_items

        log_key = upload_run_log_to_s3(run_log)

        logger.info(f"Upload complete: {latest_key}")
        logger.info(f"Run log written to: {log_key}")

        print(json.dumps({
            "status": "ok",
            "items_fetched": items_fetched,
            "records_uploaded": records_uploaded,
            "undated_items": undated_items,
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
                employees_fetched=items_fetched,
                records_uploaded=records_uploaded,
                raw_key=raw_key,
                latest_key=latest_key,
                error=str(exc),
            )
            run_log["items_fetched"] = items_fetched
            run_log["undated_items"] = undated_items
            log_key = upload_run_log_to_s3(run_log)
            logger.info(f"Error log written to: {log_key}")
        except Exception:
            logger.exception("Failed to write error log to S3")

        raise


if __name__ == "__main__":
    main()
