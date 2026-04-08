import json
import os
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from common.aws import get_s3_client
from common.logging_utils import get_logger
from common.s3_paths import utc_iso, build_latest_key, build_log_key, build_raw_key
from common.schema import build_dataset_payload, build_record, build_run_log
from common.selenium_utils import build_chrome_driver


# =========================
# CONFIG
# =========================

BASE = "https://www.mcg.org.au"
URL = f"{BASE}/events#calendar"
UA = "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
S3_BUCKET = os.getenv("S3_BUCKET", "event-scrape-data")

DATASET = "mcg-events"
ACCESS_LEVEL = "public"
ALLOWED_ROLES = ["staff", "supervisor", "manager", "admin"]
RECORD_TYPE = "public_event"
SCRAPER_NAME = "mcg-events-v1"
SOURCE_NAME = "MCG"
SOURCE_URL = URL
FILTER_LABEL = "MCG"

LOCATION_NAME = "MCG"
LOCATION_OBJECT = {
    "code": "MCG",
    "search_text": "Melbourne Cricket Ground East Melbourne VIC Australia",
    "latitude": None,
    "longitude": None,
}

DEFAULT_AUDIENCE = ["Public"]
DEFAULT_CATEGORY = "Public Event"

PAGE_LOAD_SLEEP_SECONDS = float(os.getenv("MCG_PAGE_LOAD_SLEEP_SECONDS", "1.2"))

MONTH_MAP = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


# =========================
# LOGGING / AWS
# =========================

logger = get_logger(__name__)
s3 = get_s3_client(logger=logger)


# =========================
# HELPERS
# =========================

def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def day_name_from_date_str(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")


def _to_24h_single(token: str) -> str | None:
    token = token.strip().lower().replace(" ", "").replace(".", ":")
    for fmt in ("%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(token, fmt).strftime("%H:%M")
        except ValueError:
            pass
    return None


def parse_time_range(text: str) -> tuple[str | None, str | None]:
    if not text:
        return None, None

    tokens = re.findall(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", text.lower(), flags=re.I)
    if not tokens:
        return None, None

    start = _to_24h_single(tokens[0])
    end = _to_24h_single(tokens[1]) if len(tokens) >= 2 else None
    return start, end


def add_minutes_hhmm(hhmm: str | None, minutes: int) -> str | None:
    if not hhmm:
        return None
    try:
        dt = datetime.strptime(hhmm, "%H:%M")
        return (dt + timedelta(minutes=minutes)).strftime("%H:%M")
    except ValueError:
        return None


def parse_month_year(month_div) -> tuple[int | None, int | None]:
    mid = month_div.get("id", "")
    if mid and "-" in mid:
        m_str, y_str = mid.split("-", 1)
        month_num = MONTH_MAP.get(m_str.strip().lower())
        try:
            year_num = int(y_str.strip())
        except ValueError:
            year_num = None
        return month_num, year_num

    m_span = month_div.select_one(".event-calendar-month-month")
    y_span = month_div.select_one(".event-calendar-month-year")
    month_num = MONTH_MAP.get(clean(m_span.get_text()).lower()) if m_span else None
    try:
        year_num = int(clean(y_span.get_text())) if y_span else None
    except ValueError:
        year_num = None
    return month_num, year_num


def build_notes(time_text: str, aria_label: str | None = None) -> str:
    parts = []
    if time_text:
        parts.append(f"Displayed time: {time_text}")
    if aria_label and aria_label != time_text:
        parts.append(f"Aria label: {aria_label}")
    return " || ".join(parts)


# =========================
# SCRAPING
# =========================

def fetch_events_page_html() -> str:
    driver = build_chrome_driver(user_agent=UA)
    wait = WebDriverWait(driver, 30)

    try:
        logger.info(f"Opening {URL}")
        driver.get(URL)

        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".events-wrapper")))

        scrolled = False
        for sel in ("#calendar", ".event-calendar-month-wrapper", ".events-wrapper"):
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                driver.execute_script(
                    "arguments[0].scrollIntoView({behavior:'instant', block:'center'});",
                    el,
                )
                scrolled = True
                break
            except Exception:
                continue

        if not scrolled:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.6);")

        wait.until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "div.event-calendar-month")
            or d.find_elements(By.CSS_SELECTOR, "a.event-calendar-card")
        )

        time.sleep(PAGE_LOAD_SLEEP_SECONDS)
        return driver.page_source
    finally:
        driver.quit()


def parse_calendar_from_html(html: str) -> tuple[list[dict], int]:
    soup = BeautifulSoup(html, "html.parser")
    records = []
    skipped_no_category = 0

    for month_div in soup.select("div.event-calendar-month"):
        month_num, year_num = parse_month_year(month_div)
        if not month_num or not year_num:
            continue

        for day_group in month_div.select(".daily-event-group"):
            day_num_el = day_group.select_one(".event-day-number")
            if not day_num_el:
                continue

            try:
                day_num = int(clean(day_num_el.get_text()))
            except ValueError:
                continue

            event_date = f"{year_num:04d}-{month_num:02d}-{day_num:02d}"

            for card in day_group.select("a.event-calendar-card"):
                title_el = card.select_one(".event-calendar-card-title")
                title = clean(title_el.get_text()) if title_el else clean(card.get("aria-label", ""))
                if not title:
                    continue

                if "australian sports museum" in title.lower():
                    continue

                badge = card.select_one(".event-calendar-card-tag .badge")
                category = clean(badge.get_text()) if badge else ""
                if not category:
                    skipped_no_category += 1
                    continue

                time_el = card.select_one(".event-calendar-card-details")
                time_text = clean(time_el.get_text()) if time_el else ""

                aria_label = clean(card.get("aria-label", ""))
                if not time_text:
                    time_text = aria_label

                start_time, end_time = parse_time_range(time_text)
                if category.upper() == "AFL" and start_time and not end_time:
                    end_time = add_minutes_hhmm(start_time, 150)

                href = card.get("href", "")
                source_url = href if href.startswith("http") else urljoin(BASE, href)

                records.append(
                    {
                        "title": title,
                        "date": event_date,
                        "start_time": start_time,
                        "end_time": end_time,
                        "category": category,
                        "source_url": source_url,
                        "notes": build_notes(time_text, aria_label),
                    }
                )

    seen = set()
    deduped = []
    for record in records:
        key = (record["source_url"], record["date"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)

    return deduped, skipped_no_category


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
            categories=[row["category"]] if row.get("category") else [DEFAULT_CATEGORY],
            audience_type=DEFAULT_AUDIENCE,
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
    skipped_no_category = 0
    raw_key = None
    latest_key = None

    logger.info("Starting MCG events scraper")

    try:
        html = fetch_events_page_html()
        parsed_rows, skipped_no_category = parse_calendar_from_html(html)
        cards_found = len(parsed_rows)
        logger.info(f"Parsed {cards_found} event cards after filtering")

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
        run_log["skipped_no_category"] = skipped_no_category

        log_key = upload_run_log_to_s3(run_log)

        logger.info(f"Upload complete: {latest_key}")
        logger.info(f"Run log written to: {log_key}")

        print(json.dumps({
            "status": "ok",
            "cards_found": cards_found,
            "records_uploaded": records_uploaded,
            "skipped_no_category": skipped_no_category,
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
            run_log["skipped_no_category"] = skipped_no_category
            log_key = upload_run_log_to_s3(run_log)
            logger.info(f"Error log written to: {log_key}")
        except Exception:
            logger.exception("Failed to write error log to S3")

        raise


if __name__ == "__main__":
    main()
