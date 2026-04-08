import json
import os
import time
from datetime import datetime, date
from urllib.parse import urlparse

import requests
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException

from common.aws import get_s3_client
from common.logging_utils import get_logger
from common.s3_paths import utc_iso, build_latest_key, build_log_key, build_raw_key
from common.schema import build_dataset_payload, build_record, build_run_log
from common.selenium_utils import build_chrome_driver


# =========================
# CONFIG
# =========================

URL = "https://www.mcec.com.au/whats-on"
BUTTON_SELECTOR = "button[aria-label^='Load more']"
UA = "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

S3_BUCKET = os.getenv("S3_BUCKET", "event-scrape-data")

DATASET = "mcec-events"
ACCESS_LEVEL = "public"
ALLOWED_ROLES = ["staff", "supervisor", "manager", "admin"]
RECORD_TYPE = "public_event"
SCRAPER_NAME = "mcec-whats-on-v1"
SOURCE_NAME = "MCEC"
SOURCE_URL = URL
FILTER_LABEL = "MCEC"

LOCATION_NAME = "MCEC"
LOCATION_OBJECT = {
    "code": "MCEC",
    "search_text": "Melbourne Convention and Exhibition Centre South Wharf VIC Australia",
    "latitude": None,
    "longitude": None,
}

DEFAULT_AUDIENCE = ["Public"]
DEFAULT_CATEGORY = "Public Event"

INITIAL_WAIT_SECONDS = float(os.getenv("MCEC_INITIAL_WAIT_SECONDS", "3"))
LOAD_MORE_SLEEP_SECONDS = float(os.getenv("MCEC_LOAD_MORE_SLEEP_SECONDS", "1.2"))
BUTTON_WAIT_SECONDS = int(os.getenv("MCEC_BUTTON_WAIT_SECONDS", "3"))


# =========================
# LOGGING / AWS
# =========================

logger = get_logger(__name__)
s3 = get_s3_client(logger=logger)


# =========================
# HELPERS
# =========================

def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def fmt_dt(dtx: datetime) -> str:
    return dtx.strftime("%Y-%m-%dT%H:%M:%S")


def expand_daterange_to_days(start_str: str, end_str: str) -> list[dict]:
    start_dt = parse_dt(start_str)
    end_dt = parse_dt(end_str)

    start_time = start_dt.time()
    end_time = end_dt.time()

    day = start_dt.date()
    last_day = end_dt.date()

    out = []
    while day <= last_day:
        out.append(
            {
                "startDate": fmt_dt(datetime.combine(day, start_time)),
                "endDate": fmt_dt(datetime.combine(day, end_time)),
            }
        )
        day = day.fromordinal(day.toordinal() + 1)

    return out


def parse_iso(dt_str: str):
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str)
    except ValueError:
        if len(dt_str) == 16:
            return datetime.fromisoformat(dt_str + ":00")
        return None


def date_str(dt_str: str) -> str:
    dtp = parse_iso(dt_str)
    return dtp.strftime("%Y-%m-%d") if dtp else ""


def time_str(dt_str: str) -> str | None:
    dtp = parse_iso(dt_str)
    if not dtp:
        return None
    return dtp.strftime("%H:%M")


def is_today_or_future(yyyy_mm_dd: str) -> bool:
    if not yyyy_mm_dd:
        return False
    try:
        return datetime.strptime(yyyy_mm_dd, "%Y-%m-%d").date() >= date.today()
    except ValueError:
        return False


def day_name_from_date_str(date_str_value: str) -> str:
    return datetime.strptime(date_str_value, "%Y-%m-%d").strftime("%A")


def clean_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def extract_event_fields(data: dict) -> dict:
    pp = data.get("pageProps")
    if not isinstance(pp, dict):
        return {}

    title = pp.get("title") or ""
    event_website = pp.get("eventWebsite") or ""
    event_date_type = pp.get("eventDateType") or ""
    entry = pp.get("entry") or ""

    categories = [
        c.get("title")
        for c in (pp.get("categories") or [])
        if isinstance(c, dict) and c.get("title")
    ]

    audience_type = ""
    aud = pp.get("audienceType") or {}
    if isinstance(aud, dict):
        audience_type = aud.get("title") or ""

    raw_event_dates = pp.get("eventDates") or []
    event_dates = []
    if isinstance(raw_event_dates, list):
        for d in raw_event_dates:
            if isinstance(d, dict):
                event_dates.append(
                    {
                        "startDate": d.get("startDate") or "",
                        "endDate": d.get("endDate") or "",
                    }
                )

    if event_date_type == "DateRange" and event_dates:
        start_str = event_dates[0].get("startDate")
        end_str = event_dates[0].get("endDate")
        if start_str and end_str:
            event_dates = expand_daterange_to_days(start_str, end_str)

    return {
        "title": title,
        "eventWebsite": event_website,
        "eventDateType": event_date_type,
        "eventDates": event_dates,
        "categories": categories,
        "audienceType": audience_type,
        "entry": entry,
    }


# =========================
# SCRAPING
# =========================

def fetch_mcec_events() -> tuple[list[dict], int, int]:
    driver = build_chrome_driver(user_agent=UA)
    wait = WebDriverWait(driver, 30)

    click_count = 0
    json_url_count = 0

    try:
        logger.info(f"Opening {URL}")
        driver.get(URL)

        logger.info("Waiting for page to load")
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, BUTTON_SELECTOR)))
        time.sleep(INITIAL_WAIT_SECONDS)

        logger.info("Clicking Load more until exhausted")
        while True:
            try:
                btn = WebDriverWait(driver, BUTTON_WAIT_SECONDS).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, BUTTON_SELECTOR))
                )
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.4)
                driver.execute_script("arguments[0].click();", btn)
                click_count += 1
                logger.info(f"Clicked Load more ({click_count})")
                time.sleep(LOAD_MORE_SLEEP_SECONDS)
            except TimeoutException:
                logger.info("Load more button no longer visible")
                break
            except StaleElementReferenceException:
                continue

        resource_urls = driver.execute_script(
            "return performance.getEntriesByType('resource').map(e => e.name);"
        ) or []

        json_like = []
        seen_urls = set()
        for url in resource_urls:
            lower_url = (url or "").lower()
            if not url or url in seen_urls:
                continue
            if (
                ".json" in lower_url
                or "/api/" in lower_url
                or "graphql" in lower_url
                or "wp-json" in lower_url
                or "ajax" in lower_url
            ):
                json_like.append(url)
                seen_urls.add(url)

        matches = [u for u in json_like if "whats-on" in (u or "").lower()]
        filtered_urls = []
        for url in matches:
            path = urlparse(url).path.lower()
            if path.endswith("/whats-on.json") or path.endswith("whats-on.json"):
                continue
            filtered_urls.append(url)

        json_url_count = len(filtered_urls)
        logger.info(f"Found {json_url_count} candidate JSON resource URLs")

        session = requests.Session()
        headers = {
            "User-Agent": driver.execute_script("return navigator.userAgent;"),
            "Referer": URL,
            "Accept": "application/json, text/plain, */*",
        }

        for cookie in driver.get_cookies():
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"))

        combined = []
        seen_keys = set()

        for url in filtered_urls:
            try:
                response = session.get(url, headers=headers, timeout=30)
                response.raise_for_status()
                data = response.json()

                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = None
                    for key in ("results", "items", "data", "events"):
                        if key in data and isinstance(data[key], list):
                            items = data[key]
                            break
                    if items is None:
                        items = [data]
                else:
                    continue

                for item in items:
                    if not isinstance(item, dict):
                        continue

                    extracted = extract_event_fields(item)
                    if not extracted:
                        continue

                    dedupe_key = extracted.get("eventWebsite") or json.dumps(extracted, sort_keys=True)
                    if dedupe_key in seen_keys:
                        continue

                    seen_keys.add(dedupe_key)
                    combined.append(extracted)

            except Exception as exc:
                logger.warning(f"Skipping resource URL {url}: {type(exc).__name__}: {exc}")

        parsed_rows = []
        for ev in combined:
            title = clean_text(ev.get("title"))
            website = clean_text(ev.get("eventWebsite"))
            audience = clean_text(ev.get("audienceType"))
            entry = clean_text(ev.get("entry"))

            categories = ev.get("categories") or []
            categories = [clean_text(c) for c in categories if clean_text(c)]

            notes_parts = []
            if entry:
                notes_parts.append(f"Entry: {entry}")
            if website:
                notes_parts.append(f"Website: {website}")

            event_date_type = clean_text(ev.get("eventDateType"))
            dates = ev.get("eventDates") or []
            if not isinstance(dates, list):
                dates = []

            def add_row(start_dt: str, end_dt: str):
                dstr = date_str(start_dt)
                if not is_today_or_future(dstr):
                    return

                parsed_rows.append(
                    {
                        "title": title,
                        "date": dstr,
                        "start_time": time_str(start_dt),
                        "end_time": time_str(end_dt),
                        "categories": categories,
                        "audience_type": [audience] if audience else DEFAULT_AUDIENCE,
                        "source_url": website or SOURCE_URL,
                        "notes": " || ".join(notes_parts),
                    }
                )

            if event_date_type in ("DateRange", "MultipleDays", "MultipleDates"):
                for d in dates:
                    if isinstance(d, dict):
                        add_row(d.get("startDate") or "", d.get("endDate") or "")
            else:
                if dates and isinstance(dates[0], dict):
                    d0 = dates[0]
                    add_row(d0.get("startDate") or "", d0.get("endDate") or "")
                else:
                    add_row("", "")

        deduped = []
        seen = set()
        for row in parsed_rows:
            key = (
                row["title"],
                row["date"],
                row["start_time"] or "",
                row["source_url"],
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)

        deduped.sort(key=lambda r: (r["date"], r["start_time"] or "", r["title"]))
        return deduped, click_count, json_url_count

    finally:
        driver.quit()


# =========================
# RECORD BUILDING
# =========================

def build_event_records(parsed_rows: list[dict]) -> list[dict]:
    records = []

    for row in parsed_rows:
        record = build_record(
            title=row["title"],
            date=row["date"],
            day_name=day_name_from_date_str(row["date"]),
            start_time=row["start_time"],
            end_time=row["end_time"],
            location_name=LOCATION_NAME,
            location=LOCATION_OBJECT,
            categories=row["categories"] if row.get("categories") else [DEFAULT_CATEGORY],
            audience_type=row["audience_type"] if row.get("audience_type") else DEFAULT_AUDIENCE,
            filter=FILTER_LABEL,
            source=SOURCE_NAME,
            source_url=row["source_url"],
            record_type=RECORD_TYPE,
            scraper=SCRAPER_NAME,
            notes=row.get("notes", ""),
            access_level=ACCESS_LEVEL,
            dataset=DATASET,
            allowed_roles=ALLOWED_ROLES,
        )
        records.append(record)

    records.sort(key=lambda r: (r["date"], r["start_time"] or "", r["title"]))
    logger.info(f"Built {len(records)} event records")
    return records


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
    cards_found = 0
    records_uploaded = 0
    click_count = 0
    json_url_count = 0
    raw_key = None
    latest_key = None

    logger.info("Starting MCEC What's On scraper")

    try:
        parsed_rows, click_count, json_url_count = fetch_mcec_events()
        cards_found = len(parsed_rows)
        logger.info(f"Parsed {cards_found} upcoming event rows")

        records = build_event_records(parsed_rows)
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
            employees_fetched=cards_found,
            records_uploaded=records_uploaded,
            raw_key=raw_key,
            latest_key=latest_key,
            error=None,
        )
        run_log["cards_found"] = cards_found
        run_log["load_more_clicks"] = click_count
        run_log["json_url_count"] = json_url_count

        log_key = upload_run_log_to_s3(run_log)

        logger.info(f"Upload complete: {latest_key}")
        logger.info(f"Run log written to: {log_key}")

        print(json.dumps({
            "status": "ok",
            "cards_found": cards_found,
            "records_uploaded": records_uploaded,
            "load_more_clicks": click_count,
            "json_url_count": json_url_count,
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
                employees_fetched=cards_found,
                records_uploaded=records_uploaded,
                raw_key=raw_key,
                latest_key=latest_key,
                error=str(exc),
            )
            run_log["cards_found"] = cards_found
            run_log["load_more_clicks"] = click_count
            run_log["json_url_count"] = json_url_count
            log_key = upload_run_log_to_s3(run_log)
            logger.info(f"Error log written to: {log_key}")
        except Exception:
            logger.exception("Failed to write error log to S3")

        raise


if __name__ == "__main__":
    main()