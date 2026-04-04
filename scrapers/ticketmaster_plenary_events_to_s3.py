import os
import json
import re
from datetime import datetime

import pandas as pd
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from common.aws import get_s3_client
from common.logging_utils import get_logger
from common.s3_paths import utc_iso, build_raw_key, build_latest_key, build_log_key
from common.schema import build_record, build_dataset_payload, build_run_log
from common.selenium_utils import build_chrome_driver


# =========================
# CONFIG
# =========================

URL = "https://www.ticketmaster.com.au/melbourne-convention-and-exhibition-centre-plenary-tickets-south-wharf/venue/155672"
S3_BUCKET = os.getenv("S3_BUCKET", "event-scrape-data")

DATASET = "ticketmaster-plenary-events"
ACCESS_LEVEL = "public"
ALLOWED_ROLES = ["staff", "supervisor", "manager", "admin"]
RECORD_TYPE = "public_event"
SCRAPER_NAME = "ticketmaster-plenary-events-v1"
SOURCE_NAME = "Ticketmaster"
SOURCE_URL = URL

LOCATION_NAME = "Plenary, Melbourne Convention and Exhibition Centre"
LOCATION_OBJECT = {
    "code": "PLENARY",
    "search_text": "Plenary Melbourne Convention and Exhibition Centre South Wharf VIC Australia",
    "latitude": None,
    "longitude": None,
}

DEFAULT_AUDIENCE = ["Public"]
DEFAULT_CATEGORY = "Ticketmaster"

PAGE_LOAD_SLEEP_SECONDS = float(os.getenv("TICKETMASTER_PAGE_LOAD_SLEEP_SECONDS", "1.2"))
MAX_SCROLLS = int(os.getenv("TICKETMASTER_MAX_SCROLLS", "25"))


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


def to_date_iso(text: str) -> str | None:
    if not text:
        return None
    try:
        dt = pd.to_datetime(text.strip(), dayfirst=True, errors="coerce")
        if pd.isna(dt):
            return None
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def to_time_hhmm(text: str) -> str | None:
    if not text:
        return None

    t = clean(text).lower()

    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t)
    if not m:
        return None

    hh = int(m.group(1))
    mm = int(m.group(2) or "00")
    ap = m.group(3)

    if ap == "pm" and hh != 12:
        hh += 12
    if ap == "am" and hh == 12:
        hh = 0

    return f"{hh:02d}:{mm:02d}"


def try_click_cookie_buttons(driver) -> None:
    candidates = [
        (By.CSS_SELECTOR, "button#onetrust-accept-btn-handler"),
        (By.CSS_SELECTOR, "button[aria-label='Accept All Cookies']"),
        (By.XPATH, "//button[contains(., 'Accept')]"),
        (By.XPATH, "//button[contains(., 'I agree')]"),
    ]
    for by, sel in candidates:
        try:
            btn = driver.find_element(by, sel)
            if btn.is_displayed() and btn.is_enabled():
                btn.click()
                return
        except Exception:
            pass


def scroll_to_load_all_events(driver, max_scrolls: int = 25) -> None:
    last_h = 0
    stable_hits = 0

    for _ in range(max_scrolls):
        h = driver.execute_script("return document.body.scrollHeight")
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        WebDriverWait(driver, 5).until(lambda d: True)
        new_h = driver.execute_script("return document.body.scrollHeight")

        if new_h == last_h:
            stable_hits += 1
            if stable_hits >= 2:
                break
        else:
            stable_hits = 0

        last_h = new_h


# =========================
# SCRAPING
# =========================

def fetch_event_cards():
    driver = build_chrome_driver()
    wait = WebDriverWait(driver, 30)

    try:
        logger.info(f"Opening {URL}")
        driver.get(URL)

        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        try_click_cookie_buttons(driver)

        wait.until(
            lambda d: d.find_elements(By.CSS_SELECTOR, 'ul[data-testid="eventList"] > li')
            or d.find_elements(By.CSS_SELECTOR, '[data-testid="event-list-item"]')
        )

        scroll_to_load_all_events(driver, max_scrolls=MAX_SCROLLS)

        cards = driver.find_elements(By.CSS_SELECTOR, 'ul[data-testid="eventList"] > li')
        if not cards:
            cards = driver.find_elements(By.CSS_SELECTOR, '[data-testid="event-list-item"]')

        logger.info(f"Found {len(cards)} event cards")
        return driver, cards

    except Exception:
        try:
            driver.quit()
        except Exception:
            pass
        raise


def parse_cards(cards) -> tuple[list[dict], int]:
    events = []
    skipped_cancelled = 0
    seen = set()

    for li in cards:
        # =========================
        # SKIP CANCELLED EVENTS
        # =========================
        try:
            badge_texts = [
                el.text.strip().lower()
                for el in li.find_elements(By.CSS_SELECTOR, 'span[class*="Badge__Label"]')
                if el.text.strip()
            ]
            if "cancelled" in badge_texts:
                skipped_cancelled += 1
                continue
        except Exception:
            pass

        card_text = clean(li.text)

        # =========================
        # DATE
        # =========================
        date_text = ""
        try:
            date_candidates = li.find_elements(By.CSS_SELECTOR, ".VisuallyHidden-sc-8buqks-0 span")
            for el in date_candidates:
                txt = clean(el.text)
                if re.search(r"\b\d{1,2}\s+[A-Za-z]+\s+\d{4}\b", txt):
                    date_text = txt
                    break
        except Exception:
            pass

        # =========================
        # TIME
        # =========================
        time_text = ""
        try:
            time_candidates = [
                "span.sc-6055f2eb-1 span",
                "span[class*='sc-6055f2eb-1'] span",
                "span[aria-hidden='true'] span",
            ]
            for sel in time_candidates:
                try:
                    txt = clean(li.find_element(By.CSS_SELECTOR, sel).text)
                    if re.search(r"\b\d{1,2}(?::\d{2})?\s*(am|pm)\b", txt, re.I):
                        time_text = txt
                        break
                except Exception:
                    pass

            if not time_text:
                m = re.search(r"\b\d{1,2}(?::\d{2})?\s*(am|pm)\b", card_text, re.I)
                time_text = m.group(0) if m else ""
        except Exception:
            pass

        # =========================
        # TITLE
        # =========================
        title = ""
        title_selectors = [
            "span.sc-f8b674f0-4",
            "span[class*='sc-f8b674f0-4']",
            'a[data-testid="event-list-link"] span.VisuallyHidden-sc-8buqks-0',
            'a[data-testid="event-list-link"] h3',
        ]

        for sel in title_selectors:
            try:
                txt = clean(li.find_element(By.CSS_SELECTOR, sel).text)
                if txt:
                    if "," in txt and re.search(r"\b(am|pm)\b", txt.lower()):
                        title = clean(txt.split(",")[0])
                    else:
                        title = txt
                    if title:
                        break
            except Exception:
                pass

        if not title:
            lines = [x.strip() for x in (li.text or "").splitlines() if x.strip()]
            for line in reversed(lines):
                low = line.lower()
                if "cancelled" in low:
                    continue
                if "low availability" in low:
                    continue
                if re.search(r"\b\d{1,2}(?::\d{2})?\s*(am|pm)\b", low):
                    continue
                if low in {"wed", "thu", "fri", "sat", "sun", "mon", "tue"}:
                    continue
                if re.fullmatch(r"[A-Z]{3}", line):
                    continue
                if re.fullmatch(r"\d{1,2}", line):
                    continue
                title = line
                break

        # =========================
        # LINK
        # =========================
        link = ""
        try:
            link = li.find_element(
                By.CSS_SELECTOR,
                'a[data-testid="event-list-link"]'
            ).get_attribute("href") or ""
        except Exception:
            pass

        # =========================
        # NORMALISE / FILTER
        # =========================
        event_date = to_date_iso(date_text)
        start_time = to_time_hhmm(time_text)

        if event_date:
            dt = pd.to_datetime(event_date, errors="coerce")
            if not pd.isna(dt) and dt.date() < datetime.today().date():
                continue

        if not title and not event_date:
            continue

        dedupe_key = (title or "", event_date or "", start_time or "", link or "")
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        notes_parts = []
        if link:
            notes_parts.append(link)
        if card_text:
            notes_parts.append(f"Card text: {card_text}")

        events.append({
            "title": title or "Untitled",
            "date": event_date,
            "start_time": start_time,
            "end_time": None,
            "location_name": LOCATION_NAME,
            "location": LOCATION_OBJECT,
            "categories": [DEFAULT_CATEGORY],
            "audience_type": DEFAULT_AUDIENCE,
            "source": SOURCE_NAME,
            "source_url": link or SOURCE_URL,
            "notes": " || ".join(notes_parts),
        })

    return events, skipped_cancelled


# =========================
# RECORD BUILDING
# =========================

def build_event_records(parsed_rows: list[dict]) -> list[dict]:
    records = []

    for row in parsed_rows:
        record = build_record(
            title=row["title"],
            date=row["date"],
            day_name=day_name_from_date_str(row["date"]) if row["date"] else None,
            start_time=row["start_time"],
            end_time=row["end_time"],
            location_name=row["location_name"],
            location=row["location"],
            categories=row["categories"],
            audience_type=row["audience_type"],
            source=row["source"],
            source_url=row["source_url"],
            record_type=RECORD_TYPE,
            scraper=SCRAPER_NAME,
            notes=row["notes"],
            access_level=ACCESS_LEVEL,
            dataset=DATASET,
            allowed_roles=ALLOWED_ROLES,
        )
        records.append(record)

    records.sort(key=lambda r: (r["date"] or "", r["start_time"] or "", r["title"]))
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
    skipped_cancelled = 0
    raw_key = None
    latest_key = None
    driver = None

    logger.info("Starting Ticketmaster Plenary scraper")

    try:
        driver, cards = fetch_event_cards()
        cards_found = len(cards)

        parsed_rows, skipped_cancelled = parse_cards(cards)
        logger.info(f"Parsed {len(parsed_rows)} events after filtering")
        logger.info(f"Skipped {skipped_cancelled} cancelled events")

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
        run_log["skipped_cancelled"] = skipped_cancelled

        log_key = upload_run_log_to_s3(run_log)

        logger.info(f"Upload complete: {latest_key}")
        logger.info(f"Run log written to: {log_key}")

        print(json.dumps({
            "status": "ok",
            "cards_found": cards_found,
            "records_uploaded": records_uploaded,
            "skipped_cancelled": skipped_cancelled,
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
            run_log["skipped_cancelled"] = skipped_cancelled
            log_key = upload_run_log_to_s3(run_log)
            logger.info(f"Error log written to: {log_key}")
        except Exception:
            logger.exception("Failed to write error log to S3")

        raise

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()