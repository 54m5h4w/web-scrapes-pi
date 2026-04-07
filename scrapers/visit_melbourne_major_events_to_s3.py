import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
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

BASE = "https://www.visitmelbourne.com"
URL = f"{BASE}/whats-on/major-events"
S3_BUCKET = os.getenv("S3_BUCKET", "event-scrape-data")

DATASET = "visit-melbourne-major-events"
ACCESS_LEVEL = "public"
ALLOWED_ROLES = ["staff", "supervisor", "manager", "admin"]
RECORD_TYPE = "public_event"
SCRAPER_NAME = "visit-melbourne-major-events-v1"
SOURCE_NAME = "Visit Melbourne"
SOURCE_URL = URL

LOCATION_FALLBACK = {
    "code": "MELBOURNE",
    "search_text": "Melbourne Victoria Australia",
    "latitude": None,
    "longitude": None,
}
DEFAULT_AUDIENCE = ["Public"]
DEFAULT_CATEGORY = "Major Event"
DEFAULT_LOCATION_NAME = "Melbourne"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)

PAGE_WAIT_SECONDS = int(os.getenv("VISIT_MELBOURNE_PAGE_WAIT_SECONDS", "25"))
MAX_MORE_CLICKS = int(os.getenv("VISIT_MELBOURNE_MAX_MORE_CLICKS", "30"))
POST_CLICK_PAUSE = float(os.getenv("VISIT_MELBOURNE_POST_CLICK_PAUSE", "1.2"))
DEBUG_HTML_DIR = Path(os.getenv("VISIT_MELBOURNE_DEBUG_DIR", "debug_dumps"))


# =========================
# LOGGING / AWS
# =========================

logger = get_logger(__name__)
s3 = get_s3_client(logger=logger)


# =========================
# HELPERS
# =========================


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


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


def day_name_from_date_str(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")


def split_category_location(town_text: str) -> tuple[str, str]:
    town_text = clean(town_text)
    if "᛫" in town_text:
        left, right = town_text.split("᛫", 1)
        return left.strip(" ·\u00b7 "), right.strip(" ·\u00b7 ")
    if "·" in town_text:
        left, right = town_text.split("·", 1)
        return left.strip(" ·\u00b7 "), right.strip(" ·\u00b7 ")
    return town_text, ""


def strip_price(date_text: str) -> str:
    parts = re.split(r"\s*[·\u00b7]\s*", clean(date_text))
    return parts[0].strip() if parts else clean(date_text)


def parse_date_range(date_only: str) -> tuple[str | None, str | None]:
    s = clean(date_only)
    if not s:
        return None, None

    try:
        d = datetime.strptime(s, "%d %b %Y")
        iso = d.strftime("%Y-%m-%d")
        return iso, iso
    except ValueError:
        pass

    m = re.match(r"^(\d{1,2})\s*-\s*(\d{1,2})\s*([A-Za-z]{3,9})\s*(\d{4})$", s)
    if m:
        d1, d2, mon, yr = m.groups()
        mon_num = MONTH_MAP.get(mon.lower())
        if mon_num:
            try:
                start = datetime(int(yr), mon_num, int(d1)).strftime("%Y-%m-%d")
                end = datetime(int(yr), mon_num, int(d2)).strftime("%Y-%m-%d")
                return start, end
            except ValueError:
                return None, None

    m = re.match(r"^(\d{1,2})\s*([A-Za-z]{3,9})\s*-\s*(\d{1,2})\s*([A-Za-z]{3,9})\s*(\d{4})$", s)
    if m:
        d1, mon1, d2, mon2, yr = m.groups()
        mon1_num = MONTH_MAP.get(mon1.lower())
        mon2_num = MONTH_MAP.get(mon2.lower())
        if mon1_num and mon2_num:
            try:
                start = datetime(int(yr), mon1_num, int(d1)).strftime("%Y-%m-%d")
                end = datetime(int(yr), mon2_num, int(d2)).strftime("%Y-%m-%d")
                return start, end
            except ValueError:
                return None, None

    return None, None


def build_location_object(location_name: str) -> dict:
    search_text = clean(location_name) or DEFAULT_LOCATION_NAME
    return {
        "code": re.sub(r"[^A-Z0-9]+", "-", search_text.upper()).strip("-") or "MELBOURNE",
        "search_text": f"{search_text} Victoria Australia",
        "latitude": None,
        "longitude": None,
    }


def build_notes(date_only: str, end_date: str | None, image_url: str | None) -> str:
    parts = []
    if date_only:
        parts.append(f"Displayed date: {date_only}")
    if end_date:
        parts.append(f"Event end date: {end_date}")
    if image_url:
        parts.append(f"Image: {image_url}")
    return " || ".join(parts)


# =========================
# SELENIUM SCRAPING
# =========================


def dismiss_common_banners(driver) -> None:
    xpaths = [
        "//button[contains(.,'Accept')]",
        "//button[contains(.,'I agree')]",
        "//button[contains(.,'Allow all')]",
        "//button[contains(.,'OK')]",
        "//button[contains(.,'Got it')]",
        "//button[contains(.,'Continue')]",
        "//a[contains(.,'Accept')]",
        "//a[contains(.,'Continue')]",
    ]

    for xp in xpaths:
        try:
            btn = WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.XPATH, xp)))
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(0.5)
            return
        except TimeoutException:
            continue
        except Exception:
            continue


def wait_for_tile_grid(driver) -> None:
    WebDriverWait(driver, PAGE_WAIT_SECONDS).until(
        lambda d: (
            len(d.find_elements(By.CSS_SELECTOR, "div.summary-item.is-type-product")) > 0
            or len(d.find_elements(By.CSS_SELECTOR, "div.summary-item")) > 0
            or len(d.find_elements(By.CSS_SELECTOR, "a.title[href]")) > 0
            or "summary-items-container" in (d.page_source or "")
            or "summary-item" in (d.page_source or "")
        )
    )

def click_more_until_gone(driver, max_clicks: int = 30) -> int:
    clicks = 0
    xpaths = [
        "//button[contains(@class,'cta') and contains(@class,'is-pent') and contains(.,'More')]",
        "//button[contains(.,'More')]",
    ]

    while clicks < max_clicks:
        btn = None
        for xp in xpaths:
            try:
                btn = WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.XPATH, xp)))
                if btn:
                    break
            except TimeoutException:
                continue

        if not btn:
            break

        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.2)
            driver.execute_script("arguments[0].click();", btn)
            clicks += 1
            time.sleep(POST_CLICK_PAUSE)
        except StaleElementReferenceException:
            continue
        except Exception:
            break

    return clicks


def dump_debug(driver, label: str) -> None:
    DEBUG_HTML_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")

    html_path = DEBUG_HTML_DIR / f"{label}_{ts}.html"
    png_path = DEBUG_HTML_DIR / f"{label}_{ts}.png"

    try:
        html_path.write_text(driver.page_source, encoding="utf-8")
    except Exception:
        pass

    try:
        driver.save_screenshot(str(png_path))
    except Exception:
        pass

    logger.info(f"Debug HTML: {html_path.resolve()}")
    logger.info(f"Debug PNG: {png_path.resolve()}")
    logger.info(f"Current URL: {driver.current_url}")
    logger.info(f"Page title: {driver.title}")


def fetch_events_page_html() -> tuple[str, int]:
    driver = build_chrome_driver(user_agent=USER_AGENT)

    try:
        logger.info(f"Opening {URL}")
        driver.get(URL)
        time.sleep(3)

        dismiss_common_banners(driver)
        time.sleep(1.5)

        # force some page movement so lazy content hydrates
        for pos in (0.25, 0.5, 0.8, 0.0):
            try:
                driver.execute_script(
                    f"window.scrollTo(0, document.body.scrollHeight * {pos});"
                )
                time.sleep(1.2)
            except Exception:
                pass

        try:
            WebDriverWait(driver, PAGE_WAIT_SECONDS).until(
                lambda d: (
                    len(d.find_elements(By.CSS_SELECTOR, "div.summary-item.is-type-product")) > 0
                    or len(d.find_elements(By.CSS_SELECTOR, "div.summary-item")) > 0
                    or len(d.find_elements(By.CSS_SELECTOR, "a.title[href]")) > 0
                    or "summary-items-container" in (d.page_source or "")
                    or "summary-item" in (d.page_source or "")
                )
            )
        except TimeoutException:
            dump_debug(driver, "no_tile_grid")
            raise

        clicks = click_more_until_gone(driver, max_clicks=MAX_MORE_CLICKS)

        if clicks:
            time.sleep(2)

        return driver.page_source, clicks
    finally:
        driver.quit()

# =========================
# PARSING
# =========================


def parse_tile_grid(html: str) -> list[dict]:
    """
    Parses the Visit Melbourne tile cards.

    Works whether cards sit inside:
      div.summary-items-grid
    or are present without that wrapper.

    Extracts:
    - title
    - url
    - category
    - location
    - date_only
    - event_start_date
    - event_end_date
    - image
    """
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict] = []

    grid = soup.select_one("div.summary-items-grid")

    if grid:
        items = grid.select("div.summary-item.is-type-product")
    else:
        items = soup.select("div.summary-item.is-type-product")

    if not items:
        items = soup.select("div.summary-item")

    for item in items:
        # URL: prefer the title link
        title_a = item.select_one("a.title[href]")
        any_a = item.select_one("a[href]")

        href = ""
        if title_a and title_a.get("href"):
            href = title_a["href"].strip()
        elif any_a and any_a.get("href"):
            href = any_a["href"].strip()

        url = urljoin(BASE, href) if href else ""

        # Title
        h4 = item.select_one("a.title h4")
        if not h4:
            h4 = item.select_one("h4")
        title = h4.get_text(" ", strip=True) if h4 else ""

        # Category + location
        town_el = item.select_one("div.town.small-copy, div.town")
        town_text = town_el.get_text(" ", strip=True) if town_el else ""
        category, location = split_category_location(town_text)

        # Date raw: may include price after separator dot
        date_el = item.select_one("div.date.small-copy span, div.date span, div.date")
        date_raw = date_el.get_text(" ", strip=True) if date_el else ""
        date_only = strip_price(date_raw) if date_raw else ""

        start_iso, end_iso = ("", "")
        if date_only:
            start_iso, end_iso = parse_date_range(date_only)

        # Image
        img = item.select_one("figure.image img, img")
        img_src = ""
        if img:
            img_src = (img.get("data-src") or img.get("src") or "").strip()
            if img_src:
                img_src = urljoin(BASE, img_src)

        if not (title or url):
            continue

        events.append(
            {
                "title": title,
                "category": category,
                "location": location,
                "date_only": date_only,
                "event_start_date": start_iso,
                "event_end_date": end_iso,
                "url": url,
                "image": img_src,
                "source": URL,
            }
        )

    return dedupe(events)


# =========================
# RECORD BUILDING
# =========================


def build_event_records(parsed_rows: list[dict]) -> list[dict]:
    records = []

    for row in parsed_rows:
        event_date = row.get("event_start_date")
        if not event_date:
            continue

        location_name = row.get("location") or DEFAULT_LOCATION_NAME

        record = build_record(
            title=row.get("title", ""),
            date=event_date,
            day_name=day_name_from_date_str(event_date),
            start_time=None,
            end_time=None,
            location_name=location_name,
            location=build_location_object(location_name) if location_name else LOCATION_FALLBACK,
            categories=[row["category"]] if row.get("category") else [DEFAULT_CATEGORY],
            audience_type=DEFAULT_AUDIENCE,
            source=SOURCE_NAME,
            source_url=row.get("url", ""),
            record_type=RECORD_TYPE,
            scraper=SCRAPER_NAME,
            notes=build_notes(
                row.get("date_only", ""),
                row.get("event_end_date"),
                row.get("image"),
            ),
            access_level=ACCESS_LEVEL,
            dataset=DATASET,
            allowed_roles=ALLOWED_ROLES,
        )
        records.append(record)

    records.sort(key=lambda r: (r["date"], r["title"]))
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

def dedupe(events: list[dict]) -> list[dict]:
    seen = set()
    uniq = []
    for e in events:
        key = e.get("url") or (e.get("title", "") + "||" + e.get("date_only", ""))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)
    return uniq

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
    more_clicks = 0
    raw_key = None
    latest_key = None

    logger.info("Starting Visit Melbourne major events scraper")

    try:
        html, more_clicks = fetch_events_page_html()
        parsed_rows = parse_tile_grid(html)
        cards_found = len(parsed_rows)
        logger.info(f"Parsed {cards_found} event cards")

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
        run_log["more_clicks"] = more_clicks

        log_key = upload_run_log_to_s3(run_log)

        logger.info(f"Upload complete: {latest_key}")
        logger.info(f"Run log written to: {log_key}")

        print(json.dumps({
            "status": "ok",
            "cards_found": cards_found,
            "records_uploaded": records_uploaded,
            "more_clicks": more_clicks,
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
            run_log["more_clicks"] = more_clicks
            log_key = upload_run_log_to_s3(run_log)
            logger.info(f"Error log written to: {log_key}")
        except Exception:
            logger.exception("Failed to write error log to S3")

        raise


if __name__ == "__main__":
    main()
