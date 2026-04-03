import json
import os
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.chrome.service import Service

from common.aws import get_s3_client
from common.logging_utils import get_logger
from common.s3_paths import utc_iso, build_latest_key, build_log_key, build_raw_key
from common.schema import build_dataset_payload, build_record, build_run_log


# =========================
# CONFIG
# =========================

BASE = "https://www.marvelstadium.com.au"
URL = f"{BASE}/events"
S3_BUCKET = os.getenv("S3_BUCKET", "event-scrape-data")

DATASET = "marvel-events"
ACCESS_LEVEL = "public"
ALLOWED_ROLES = ["staff", "supervisor", "manager", "admin"]
RECORD_TYPE = "public_event"
SCRAPER_NAME = "marvel-events-v1"
SOURCE_NAME = "Marvel Stadium"
SOURCE_URL = URL

LOCATION_NAME = "Marvel Stadium"
LOCATION_OBJECT = {
    "code": "MARVEL",
    "search_text": "Marvel Stadium Docklands Melbourne VIC Australia",
    "latitude": None,
    "longitude": None,
}

DEFAULT_AUDIENCE = ["Public"]
DEFAULT_CATEGORY = "Public Event"

# Optional tuning
MAX_LOAD_MORE_CLICKS = int(os.getenv("MARVEL_MAX_LOAD_MORE_CLICKS", "50"))
PAGE_LOAD_SLEEP_SECONDS = float(os.getenv("MARVEL_PAGE_LOAD_SLEEP_SECONDS", "3"))
RENDER_SLEEP_SECONDS = float(os.getenv("MARVEL_RENDER_SLEEP_SECONDS", "1.2"))
SELENIUM_HEADLESS = os.getenv("SELENIUM_HEADLESS", "true").strip().lower() in {"1", "true", "yes", "y"}


# =========================
# LOGGING / AWS
# =========================

logger = get_logger(__name__)
s3 = get_s3_client(logger=logger)


# =========================
# SELENIUM
# =========================

LOAD_MORE_XPATHS = [
    "//button[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'load more')]",
    "//a[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'load more')]",
    "//*[self::button or self::a][contains(@class,'load') and contains(@class,'more')]",
]


def build_driver() -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    if SELENIUM_HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1600,2200")
    options.add_argument("--disable-gpu")
    options.add_argument("--lang=en-AU")
    options.binary_location = "/usr/bin/chromium"

    service = Service("/usr/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)


# =========================
# HELPERS
# =========================


def day_name_from_date_str(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")



def parse_date_and_time(summary: str) -> tuple[str | None, str | None]:
    """
    Marvel summary examples:
      "Thursday 26 February, 6:00 pm"
      "Thursday 26 February, 6 pm"
      "26 February, 6:00 pm"
      "Thursday 26 February 2026, 6:00 pm"
    """
    if not summary:
        return None, None

    s = " ".join(summary.split())

    time_24 = None
    tm = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", s, re.I)
    if tm:
        hour = int(tm.group(1))
        minute = int(tm.group(2) or 0)
        ampm = tm.group(3).lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        time_24 = f"{hour:02d}:{minute:02d}"

    dm = re.search(
        r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)(?:\s+(20\d{2}))?\b",
        s,
        re.I,
    )
    if not dm:
        return None, time_24

    day_num = int(dm.group(1))
    month_name = dm.group(2)
    year_text = dm.group(3)
    today = date.today()

    if year_text:
        year_num = int(year_text)
    else:
        year_num = today.year
        try:
            candidate = dateparser.parse(f"{day_num} {month_name} {year_num}", dayfirst=True).date()
            if candidate < today:
                year_num += 1
        except Exception:
            pass

    try:
        dt = dateparser.parse(f"{day_num} {month_name} {year_num}", dayfirst=True)
        if not dt:
            return None, time_24
        return dt.strftime("%Y-%m-%d"), time_24
    except Exception:
        return None, time_24



def infer_end_time(tag: str, start_time: str | None) -> str | None:
    if not start_time:
        return None

    tag_upper = (tag or "").strip().upper()
    durations = {
        "AFL": timedelta(hours=2, minutes=30),
    }
    duration = durations.get(tag_upper)
    if not duration:
        return None

    try:
        start_dt = datetime.strptime(start_time, "%H:%M")
        return (start_dt + duration).strftime("%H:%M")
    except Exception:
        return None



def build_notes(event: dict) -> str:
    parts = []
    if event.get("summary"):
        parts.append(f"Summary: {event['summary']}")
    if event.get("outbound_links"):
        parts.append("Outbound links: " + " | ".join(event["outbound_links"]))
    return " || ".join(parts)


# =========================
# SCRAPING
# =========================


def click_load_more_until_gone(driver: webdriver.Chrome, max_clicks: int = MAX_LOAD_MORE_CLICKS) -> int:
    clicks = 0
    while clicks < max_clicks:
        found = None
        for xpath in LOAD_MORE_XPATHS:
            try:
                found = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                if found:
                    break
            except TimeoutException:
                continue

        if not found:
            logger.info("No load-more control found; finished expanding page")
            return clicks

        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", found)
            time.sleep(0.4)
            driver.execute_script("arguments[0].click();", found)
            clicks += 1
            logger.info(f"Clicked load more ({clicks})")
            time.sleep(RENDER_SLEEP_SECONDS)
        except StaleElementReferenceException:
            continue

    logger.warning(f"Stopped after max_clicks={max_clicks} safety cap")
    return clicks



def fetch_events_page_html() -> tuple[str, int]:
    driver = build_driver()
    try:
        logger.info(f"Opening {URL}")
        driver.get(URL)
        time.sleep(PAGE_LOAD_SLEEP_SECONDS)
        clicks = click_load_more_until_gone(driver)
        html = driver.page_source
        return html, clicks
    finally:
        driver.quit()



def parse_events_from_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    events = []

    for article in soup.select("article.o-promo-pod"):
        internal_a = article.select_one("a.o-promo-pod__absolute-link[href]")
        internal_href = internal_a["href"].strip() if internal_a else ""
        event_page = urljoin(BASE, internal_href) if internal_href else None

        tag_el = article.select_one("span.o-promo-pod__tag")
        heading_el = article.select_one("h3.o-promo-pod__heading")
        summary_el = article.select_one("p.o-promo-pod__summary")

        tag = tag_el.get_text(strip=True) if tag_el else ""
        title = heading_el.get_text(strip=True) if heading_el else ""
        summary = summary_el.get_text(strip=True) if summary_el else ""

        outbound_links = []
        for a in article.select("div.o-promo-pod__arrow-link-container a[href]"):
            href = a["href"].strip()
            if href:
                outbound_links.append(href)

        events.append(
            {
                "tag": tag,
                "title": title,
                "summary": summary,
                "event_page": event_page,
                "outbound_links": outbound_links,
            }
        )

    seen = set()
    unique_events = []
    for event in events:
        key = event["event_page"] or (event["title"] + "||" + event["summary"])
        if key in seen:
            continue
        seen.add(key)
        unique_events.append(event)

    return unique_events


# =========================
# RECORD BUILDING
# =========================


def build_event_records(events: list[dict]) -> tuple[list[dict], int]:
    records = []
    skipped_missing_date = 0

    for event in events:
        title = (event.get("title") or "").strip()
        tag = (event.get("tag") or "").strip()
        summary = (event.get("summary") or "").strip()
        source_url = event.get("event_page") or SOURCE_URL

        event_date, start_time = parse_date_and_time(summary)
        if not event_date:
            skipped_missing_date += 1
            logger.warning(f"Skipping event with no parsed date: {title!r} | summary={summary!r}")
            continue

        end_time = infer_end_time(tag, start_time)
        categories = [tag] if tag else [DEFAULT_CATEGORY]

        record = build_record(
            title=title,
            date=event_date,
            day_name=day_name_from_date_str(event_date),
            start_time=start_time,
            end_time=end_time,
            location_name=LOCATION_NAME,
            location=LOCATION_OBJECT,
            categories=categories,
            audience_type=DEFAULT_AUDIENCE,
            source=SOURCE_NAME,
            source_url=source_url,
            record_type=RECORD_TYPE,
            scraper=SCRAPER_NAME,
            notes=build_notes(event),
            access_level=ACCESS_LEVEL,
            dataset=DATASET,
            allowed_roles=ALLOWED_ROLES,
        )
        records.append(record)

    records.sort(key=lambda r: (r["date"], r["start_time"] or "", r["title"]))
    logger.info(f"Built {len(records)} records; skipped {skipped_missing_date} rows with no parsed date")
    return records, skipped_missing_date



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
    events_found = 0
    records_uploaded = 0
    load_more_clicks = 0
    skipped_missing_date = 0
    raw_key = None
    latest_key = None

    logger.info("Starting Marvel Stadium events scraper")

    try:
        html, load_more_clicks = fetch_events_page_html()
        events = parse_events_from_html(html)
        events_found = len(events)
        logger.info(f"Parsed {events_found} event cards")

        records, skipped_missing_date = build_event_records(events)
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
            employees_fetched=events_found,
            records_uploaded=records_uploaded,
            raw_key=raw_key,
            latest_key=latest_key,
            error=None,
        )
        run_log["cards_found"] = events_found
        run_log["load_more_clicks"] = load_more_clicks
        run_log["skipped_missing_date"] = skipped_missing_date

        log_key = upload_run_log_to_s3(run_log)

        logger.info(f"Upload complete: {latest_key}")
        logger.info(f"Run log written to: {log_key}")

        print(json.dumps({
            "status": "ok",
            "cards_found": events_found,
            "records_uploaded": records_uploaded,
            "skipped_missing_date": skipped_missing_date,
            "load_more_clicks": load_more_clicks,
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
                employees_fetched=events_found,
                records_uploaded=records_uploaded,
                raw_key=raw_key,
                latest_key=latest_key,
                error=str(exc),
            )
            run_log["cards_found"] = events_found
            run_log["load_more_clicks"] = load_more_clicks
            run_log["skipped_missing_date"] = skipped_missing_date
            log_key = upload_run_log_to_s3(run_log)
            logger.info(f"Error log written to: {log_key}")
        except Exception:
            logger.exception("Failed to write error log to S3")

        raise


if __name__ == "__main__":
    main()
