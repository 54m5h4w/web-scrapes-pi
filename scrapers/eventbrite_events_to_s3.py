import json
import os
import re
import time
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit, urlunsplit

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

URL = "https://www.eventbrite.com.au/d/australia--melbourne/all-events/"
BASE_SOURCE_URL = URL
S3_BUCKET = os.getenv("S3_BUCKET", "event-scrape-data")

DATASET = "eventbrite-events"
ACCESS_LEVEL = "public"
ALLOWED_ROLES = ["staff", "supervisor", "manager", "admin"]
RECORD_TYPE = "public_event"
SCRAPER_NAME = "eventbrite-events-v1"
SOURCE_NAME = "Eventbrite"
SOURCE_URL = URL
FILTER_LABEL = "Eventbrite"

LOCATION_NAME = "Eventbrite Melbourne"
LOCATION_OBJECT = {
    "code": "EVENTBRITE_MELB",
    "search_text": "South Wharf Docklands Melbourne VIC Australia Crown Melbourne Mission to Seafarers Victoria Marvel Stadium Eventbrite",
    "latitude": None,
    "longitude": None,
}

NEIGHBOURHOOD_TARGETS = ["Southbank", "Docklands"]

# Exclude our own South Wharf venue events from this public Eventbrite feed.
EXCLUDED_VENUE_SUBSTRINGS = [
    "bangpop",
    "bang pop",
    "plus 5",
    "plus5",
]

ALLOWED_EXACT_LOCATIONS = {
    "south wharf",
}

ALLOWED_VENUE_SUBSTRINGS = [
    "crown melbourne",
    "the mission to seafarers victoria",
    "marvel stadium",
]

MELBOURNE_TZ = ZoneInfo("Australia/Melbourne")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)

MAX_SEARCH_PAGES = int(os.getenv("EVENTBRITE_MAX_SEARCH_PAGES", "50"))
LOAD_MORE_CLICKS = int(os.getenv("EVENTBRITE_LOAD_MORE_CLICKS", "12"))
NEXT_PAGE_SLEEP_SECONDS = float(os.getenv("EVENTBRITE_NEXT_PAGE_SLEEP_SECONDS", "3.0"))
PAGE_READY_SLEEP_SECONDS = float(os.getenv("EVENTBRITE_PAGE_READY_SLEEP_SECONDS", "2.0"))
CARD_STABILISE_TIMEOUT = float(os.getenv("EVENTBRITE_CARD_STABILISE_TIMEOUT", "12"))
CARD_STABILISE_POLL = float(os.getenv("EVENTBRITE_CARD_STABILISE_POLL", "0.6"))
CARD_STABILISE_ROUNDS = int(os.getenv("EVENTBRITE_CARD_STABILISE_ROUNDS", "3"))
AVAILABILITY_SCROLL_PASSES = int(os.getenv("EVENTBRITE_AVAILABILITY_SCROLL_PASSES", "8"))
ENABLE_RECURRING_EXPANSION = os.getenv("EVENTBRITE_EXPAND_RECURRING", "true").strip().lower() in {
    "1", "true", "yes", "y"
}

BADGES = {
    "sales end soon",
    "almost full",
    "just added",
    "happening soon",
    "few tickets left",
    "selling fast",
    "free",
    "online event",
    "donation",
}

MONTHS = {
    "jan": 1, "janu": 1, "january": 1,
    "feb": 2, "febr": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "octo": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "dece": 12, "december": 12,
}

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


# =========================
# LOGGING / AWS
# =========================

logger = get_logger(__name__)
s3 = get_s3_client(logger=logger)


# =========================
# HELPERS
# =========================

def clean_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def day_name_from_date_str(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")


def normalise_title(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"&", "and", s)
    s = re.sub(r"[’']", "", s)
    s = re.sub(r"[^a-z0-9\s\-:]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def to_24h(t: str | None) -> str | None:
    if not t or str(t).strip().lower() in {"n/a", "na", "none", "null"}:
        return None

    s = " ".join(str(t).lower().strip().split())
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", s)
    if not m:
        return None

    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    ap = m.group(3)

    if hh == 12:
        hh = 0
    if ap == "pm":
        hh += 12

    return f"{hh:02d}:{mm:02d}"


def parse_location_venue(line: str) -> tuple[str, str]:
    line = line or ""
    parts = [p.strip() for p in line.split("·") if p.strip()]
    if len(parts) >= 2:
        return parts[0], " · ".join(parts[1:])
    return (line.strip() if line.strip() else "n/a"), "n/a"


def keep_event(location: str, venue: str) -> bool:
    loc = (location or "").strip().lower()
    v = (venue or "").strip().lower()
    combined = f"{loc} {v}"

    # Explicitly remove BangPop and Plus 5, even when Eventbrite labels
    # their broader location as South Wharf.
    if any(needle in combined for needle in EXCLUDED_VENUE_SUBSTRINGS):
        return False

    if loc in ALLOWED_EXACT_LOCATIONS:
        return True

    return any(needle in v for needle in ALLOWED_VENUE_SUBSTRINGS)


def build_location_object(location_name: str) -> dict:
    text = (location_name or "").strip()
    lowered = text.lower()

    if "crown melbourne" in lowered:
        return {
            "code": "CROWN_MELBOURNE",
            "search_text": "Crown Melbourne Southbank Melbourne VIC Australia",
            "latitude": None,
            "longitude": None,
        }

    if "mission to seafarers victoria" in lowered:
        return {
            "code": "SEAFARERS_VIC",
            "search_text": "The Mission to Seafarers Victoria Docklands Melbourne VIC Australia",
            "latitude": None,
            "longitude": None,
        }

    if "marvel stadium" in lowered:
        return {
            "code": "MARVEL",
            "search_text": "Marvel Stadium Docklands Melbourne VIC Australia",
            "latitude": None,
            "longitude": None,
        }

    if "melbourne convention and exhibition centre" in lowered or "mcec" in lowered:
        return {
            "code": "MCEC",
            "search_text": "Melbourne Convention and Exhibition Centre South Wharf Melbourne VIC Australia",
            "latitude": None,
            "longitude": None,
        }

    return {
        "code": "SOUTH_WHARF",
        "search_text": "South Wharf Melbourne VIC Australia",
        "latitude": None,
        "longitude": None,
    }


def build_categories(location_name: str) -> list[str]:
    categories = ["Eventbrite"]
    lowered = (location_name or "").strip().lower()

    if "melbourne convention and exhibition centre" in lowered or "mcec" in lowered:
        categories.append("MCEC")

    if "marvel stadium" in lowered:
        categories.append("Marvel")

    return categories


def split_date_time_and_more(line: str) -> tuple[str, str, bool]:
    s = " ".join((line or "").split())
    re_occurring = bool(re.search(r"\+\s*\d+\s+more\b", s, re.I))
    s2 = re.sub(r"\s*\+\s*\d+\s+more\b.*$", "", s, flags=re.I).strip()

    m = re.search(r"(.*?)(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*$", s2, re.I)
    if not m:
        return (s2 if s2 else "n/a"), "n/a", re_occurring

    date_part = (m.group(1) or "").rstrip(", ").strip()
    time_part = (m.group(2) or "").strip().lower()
    return (date_part if date_part else "n/a"), time_part, re_occurring


def melbourne_today() -> date:
    return datetime.now(MELBOURNE_TZ).date()


def next_weekday(d: date, wd: int) -> date:
    return d + timedelta(days=(wd - d.weekday()) % 7)


def resolve_event_date(day_num: int, month_num: int, explicit_year: int | None = None) -> date | None:
    """Resolve an upcoming Eventbrite day/month to a safe calendar date.

    If Eventbrite supplies a year, use it. If the year is omitted, only roll
    into next year when the month itself has wrapped (for example December ->
    January). A past day in the current month is rejected rather than being
    incorrectly turned into the same day next year.
    """
    today = melbourne_today()

    try:
        year = explicit_year if explicit_year is not None else (
            today.year + 1 if month_num < today.month else today.year
        )
        candidate = date(year, month_num, day_num)
        return candidate if candidate >= today else None
    except ValueError:
        return None


def parse_date_part_to_iso(date_part: str) -> str | None:
    if not date_part or date_part.strip().lower() == "n/a":
        return None

    s = " ".join(date_part.strip().lower().split())
    s = re.sub(r"\bat\b.*$", "", s).strip().rstrip(",")
    today = melbourne_today()

    if s == "today":
        return today.isoformat()
    if s == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if s in WEEKDAYS:
        return next_weekday(today, WEEKDAYS[s]).isoformat()

    # Remove an optional weekday prefix, e.g. "Mon, 17 Aug 2026".
    s2 = re.sub(r"^[a-z]{3,9},?\s+", "", s).strip()
    m = re.match(r"^(\d{1,2})\s+([a-z]{3,9})(?:[\s,]+(\d{4}))?\b", s2)
    if not m:
        return None

    day_num = int(m.group(1))
    mon = MONTHS.get(m.group(2).lower())
    explicit_year = int(m.group(3)) if m.group(3) else None
    if not mon:
        return None

    resolved = resolve_event_date(day_num, mon, explicit_year)
    return resolved.isoformat() if resolved else None


def safe_click(driver, elem) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
    time.sleep(0.15)
    driver.execute_script("arguments[0].click();", elem)


def wait_for_card_count_to_stabilise(driver) -> int:
    end = time.time() + CARD_STABILISE_TIMEOUT
    last = -1
    stable = 0

    while time.time() < end:
        count = len(driver.find_elements(By.CSS_SELECTOR, "div.event-card"))
        if count == last and count > 0:
            stable += 1
        else:
            stable = 0
        last = count
        if stable >= CARD_STABILISE_ROUNDS:
            break
        time.sleep(CARD_STABILISE_POLL)

    return max(last, 0)


def find_next_search_button(driver):
    candidates = driver.find_elements(By.XPATH, "//button[contains(translate(@aria-label,'NEXT','next'),'next')]")
    for elem in candidates:
        try:
            if elem.is_displayed() and elem.is_enabled() and elem.get_attribute("aria-disabled") != "true":
                return elem
        except Exception:
            continue

    candidates = driver.find_elements(By.XPATH, "//a[contains(translate(@aria-label,'NEXT','next'),'next')]")
    for elem in candidates:
        try:
            if elem.is_displayed() and elem.is_enabled() and elem.get_attribute("aria-disabled") != "true":
                return elem
        except Exception:
            continue

    return None


def find_neighbourhood_checkboxes(driver, wait, target_names: list[str]) -> list:
    wait.until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, '[data-testid="filter-section__neighbourhood"]')
        )
    )
    time.sleep(0.8)

    def _try_selector(selector: str):
        try:
            return driver.find_element(By.CSS_SELECTOR, selector)
        except Exception:
            return None

    def _try_xpath(xpath: str):
        try:
            return driver.find_element(By.XPATH, xpath)
        except Exception:
            return None

    try:
        view_more = wait.until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//div[@id='view-more-neighbourhood']"
                "//button[@data-testid='read-more-toggle' and @aria-controls='view-more-neighbourhood']",
            ))
        )
        if view_more.is_displayed() and view_more.is_enabled():
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", view_more)
            time.sleep(0.2)
            driver.execute_script("arguments[0].click();", view_more)
            time.sleep(1.0)
    except Exception:
        pass

    found = []

    for name in target_names:
        candidates = [
            _try_selector(f'input[data-testid="filter-display-{name}"]'),
            _try_xpath(f"//input[contains(@data-testid,'{name}')]"),
            _try_xpath(f"//label[contains(., '{name}')]//input"),
            _try_xpath(f"//span[contains(., '{name}')]/ancestor::label//input"),
        ]

        elem = next((c for c in candidates if c is not None), None)
        if elem is not None:
            found.append(elem)

    if not found:
        raise RuntimeError(
            f"Could not find any target neighbourhood checkboxes: {', '.join(target_names)}"
        )

    return found


def apply_neighborhood_filter(driver, wait) -> str:
    """
    Apply multiple neighbourhood filters and verify results changed before continuing.
    Returns a comma-separated label of filters actually targeted.
    """
    checkboxes = find_neighbourhood_checkboxes(driver, wait, NEIGHBOURHOOD_TARGETS)

    def _is_checked(elem) -> bool:
        try:
            return (
                elem.get_attribute("checked") is not None
                or elem.is_selected()
                or (elem.get_attribute("aria-checked") or "").lower() == "true"
            )
        except Exception:
            return False

    before_cards = len(driver.find_elements(By.CSS_SELECTOR, "div.event-card"))
    before_url = driver.current_url

    applied_names = []

    for checkbox in checkboxes:
        filter_name = (
            checkbox.get_attribute("data-testid")
            or checkbox.get_attribute("value")
            or "neighbourhood"
        )

        if not _is_checked(checkbox):
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", checkbox)
            time.sleep(0.2)
            driver.execute_script("arguments[0].click();", checkbox)
            wait.until(lambda d, cb=checkbox: _is_checked(cb))

        applied_names.append(filter_name)
        time.sleep(0.8)

    try:
        wait.until(
            lambda d: (
                d.current_url != before_url
                or len(d.find_elements(By.CSS_SELECTOR, "div.event-card")) != before_cards
            )
        )
    except Exception:
        time.sleep(2.5)

    time.sleep(2.0)
    return ", ".join(applied_names)


def load_more_results(driver, max_clicks: int = LOAD_MORE_CLICKS) -> int:
    clicks = 0
    for _ in range(max_clicks):
        try:
            btn = driver.find_element(By.XPATH, "//button[contains(., 'Load more')]")
        except Exception:
            break

        try:
            driver.execute_script("arguments[0].click();", btn)
            clicks += 1
            time.sleep(1.2)
        except Exception:
            break
    return clicks


def extract_events_on_current_page(driver) -> list[dict]:
    events: list[dict] = []
    cards = driver.find_elements(By.CSS_SELECTOR, "div.event-card")

    for card in cards:
        try:
            raw_card_text = card.text or ""
        except Exception:
            raw_card_text = ""

        try:
            raw = card.find_element(By.CSS_SELECTOR, "section.event-card-details").text or ""
        except Exception:
            continue

        lines = [line.strip() for line in raw.split("\n") if line.strip()]
        while lines and lines[0].lower() in BADGES and len(lines) >= 2:
            lines = lines[1:]

        if len(lines) < 3:
            continue

        title = lines[0].strip()
        date_part, time_part, re_occurring = split_date_time_and_more(lines[1])
        location, venue = parse_location_venue(lines[2])

        try:
            link = card.find_element(By.CSS_SELECTOR, "a.event-card-link").get_attribute("href") or ""
            link = clean_url(link.strip()) if link else None
        except Exception:
            link = None

        events.append(
            {
                "title": title,
                "date_part": date_part,
                "time_part": time_part,
                "re_occurring": bool(re_occurring),
                "location": location,
                "venue": venue,
                "link": link,
                "raw_dt_line": lines[1].strip(),
                "raw_card_text": raw_card_text,
            }
        )

    return events


def scrape_search_results() -> tuple[list[dict], dict]:
    driver = build_chrome_driver(user_agent=USER_AGENT)
    wait = WebDriverWait(driver, 40)

    scraped_cards: list[dict] = []
    stats = {
        "pages_scraped": 0,
        "load_more_clicks": 0,
        "cards_seen": 0,
        "page_signatures_repeated": 0,
        "filter_applied": "",
    }

    try:
        logger.info(f"Opening {URL}")
        driver.get(URL)
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(PAGE_READY_SLEEP_SECONDS)

        applied_filter = apply_neighborhood_filter(driver, wait)
        stats["filter_applied"] = applied_filter
        logger.info(f"Applied neighbourhood filter: {applied_filter}")

        page_num = 1
        seen_signatures = set()

        while True:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.event-card")))

            lm_clicks = load_more_results(driver)
            stats["load_more_clicks"] += lm_clicks

            final_card_count = wait_for_card_count_to_stabilise(driver)
            page_events = extract_events_on_current_page(driver)
            scraped_cards.extend(page_events)

            signature = tuple(
                sorted(
                    (
                        (item.get("title") or "").strip().lower(),
                        (item.get("date_part") or "").strip().lower(),
                        (item.get("location") or "").strip().lower(),
                        (item.get("venue") or "").strip().lower(),
                    )
                    for item in page_events[:20]
                )
            )

            stats["pages_scraped"] += 1
            stats["cards_seen"] += final_card_count
            logger.info(
                f"Eventbrite page {page_num}: cards={final_card_count}, extracted={len(page_events)}, load_more_clicks={lm_clicks}"
            )

            if signature in seen_signatures:
                stats["page_signatures_repeated"] += 1
                logger.warning("Detected repeated Eventbrite result page signature; stopping pagination")
                break
            seen_signatures.add(signature)

            if page_num >= MAX_SEARCH_PAGES:
                logger.warning(f"Reached EVENTBRITE_MAX_SEARCH_PAGES={MAX_SEARCH_PAGES}; stopping pagination")
                break

            time.sleep(NEXT_PAGE_SLEEP_SECONDS)
            nxt = find_next_search_button(driver)
            if not nxt:
                logger.info("No next page button found; pagination complete")
                break

            try:
                stale_anchor = driver.find_element(By.CSS_SELECTOR, "div.event-card")
            except Exception:
                stale_anchor = None

            safe_click(driver, nxt)
            if stale_anchor is not None:
                try:
                    wait.until(EC.staleness_of(stale_anchor))
                except Exception:
                    pass

            time.sleep(1.5)
            page_num += 1

        return scraped_cards, stats
    finally:
        driver.quit()


def dedupe_scraped_cards(scraped_cards: list[dict]) -> tuple[list[dict], int]:
    deduped: list[dict] = []
    seen = set()

    for row in scraped_cards:
        title_norm = normalise_title(row.get("title", ""))
        key = (
            title_norm,
            row.get("date_part", ""),
            row.get("time_part", ""),
            row.get("location", ""),
            row.get("venue", ""),
            row.get("link") or "",
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    return deduped, len(scraped_cards) - len(deduped)


def compile_base_rows(scraped_cards: list[dict]) -> tuple[list[dict], dict]:
    stats = {
        "filtered_out": 0,
        "date_parse_failed": 0,
        "duplicates": 0,
        "recurring_cards": 0,
    }

    records: list[dict] = []
    seen_keys = set()

    for item in scraped_cards:
        title = (item.get("title") or "").strip()
        title_norm = normalise_title(title)
        if not title_norm:
            stats["filtered_out"] += 1
            continue

        location = item.get("location", "n/a")
        venue = item.get("venue", "n/a")
        if not keep_event(location, venue):
            stats["filtered_out"] += 1
            continue

        iso_date = parse_date_part_to_iso(item.get("date_part", "n/a"))
        if not iso_date:
            stats["date_parse_failed"] += 1
            continue

        start_time = to_24h(item.get("time_part"))
        source_url = item.get("link")
        dedupe_key = (title_norm, iso_date, start_time or "", source_url or "")
        if dedupe_key in seen_keys:
            stats["duplicates"] += 1
            continue
        seen_keys.add(dedupe_key)

        combined_location = f"{venue} - {location}" if venue != "n/a" and location != "n/a" else (venue or location)

        notes_parts = []
        if source_url:
            notes_parts.append(f"eventbrite: {source_url}")
        if item.get("re_occurring"):
            notes_parts.append("re_occurring: true")
            stats["recurring_cards"] += 1
        if item.get("raw_dt_line"):
            notes_parts.append(f"raw: {item['raw_dt_line']}")

        records.append(
            {
                "title": title,
                "date": iso_date,
                "start_time": start_time,
                "end_time": None,
                "re_occurring": bool(item.get("re_occurring")),
                "location_name": combined_location,
                "source_url": source_url,
                "notes": " | ".join(notes_parts),
            }
        )

    records.sort(key=lambda r: (r["date"], r["start_time"] or "", r["title"]))
    return records, stats


def extract_eventbrite_link_from_notes(notes: str) -> str:
    if not notes:
        return ""
    match = re.search(r"(https?://\S+)", notes)
    return match.group(1).strip() if match else ""


def parse_month_label(month_label: str) -> tuple[int | None, int | None]:
    txt = " ".join((month_label or "").split()).strip()
    match = re.match(r"^([A-Za-z]+)(?:\s+(\d{4}))?$", txt)
    if not match:
        return None, None

    mon = MONTHS.get(match.group(1).lower())
    explicit_year = int(match.group(2)) if match.group(2) else None
    return explicit_year, mon


def parse_slot_range_to_24h(slot_text: str) -> tuple[str | None, str | None]:
    txt = (slot_text or "").strip().lower()
    parts = re.split(r"\s*[-–]\s*", txt)
    if len(parts) != 2:
        return None, None
    return to_24h(parts[0].strip()), to_24h(parts[1].strip())


def explode_time_slots(raw_slots: list[str]) -> list[str]:
    time_range_re = re.compile(
        r"(\d{1,2}:\d{2}\s*[ap]m)\s*-\s*(\d{1,2}:\d{2}\s*[ap]m)",
        re.IGNORECASE,
    )
    out = []
    for raw in raw_slots or []:
        if not raw:
            continue
        parts = [re.sub(r"\s+", " ", p).strip() for p in str(raw).splitlines() if p.strip()]
        for part in parts:
            matches = time_range_re.findall(part)
            if len(matches) <= 1:
                out.append(part)
            else:
                for a, b in matches:
                    out.append(f"{a} - {b}")

    seen = set()
    deduped = []
    for part in out:
        if part not in seen:
            seen.add(part)
            deduped.append(part)
    return deduped


def click_check_availability(driver, wait) -> bool:
    time.sleep(0.8)

    try:
        btn = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "button[data-testid='conversion-bar-checkout-button']"))
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.3)
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(0.8)
        return True
    except Exception:
        pass

    try:
        btn = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((
                By.XPATH,
                "//button[.//*[self::div or self::span][contains(translate(normalize-space(.),"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'check availability')]]",
            ))
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.3)
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(0.8)
        return True
    except Exception:
        return False


def get_recurring_availability_dates(driver, event_url: str) -> dict:
    wait = WebDriverWait(driver, 60)

    def detect_checkout_ui() -> str:
        if driver.find_elements(By.CSS_SELECTOR, "div[data-testid='scrollable-calendar-container'], div[data-testid='month']"):
            return "calendar_grid"
        if driver.find_elements(By.XPATH, "//*[contains(normalize-space(.), 'Date and time')]"):
            if driver.find_elements(By.XPATH, "//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'am') or contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'pm')]"):
                return "date_tiles"
        if driver.find_elements(By.CSS_SELECTOR, "[data-testid='time-slot-container'], ul[class*='TimeSlotList'], div[class*='TimeSlotContainer']"):
            return "time_slots"
        return "unknown"

    def find_checkout_context_anywhere() -> str:
        def has_ui_markers() -> bool:
            return bool(
                driver.find_elements(By.CSS_SELECTOR, "div[data-testid='scrollable-calendar-container'], div[data-testid='month']")
                or driver.find_elements(By.XPATH, "//*[contains(normalize-space(.), 'Date and time')]")
                or driver.find_elements(By.CSS_SELECTOR, "[data-testid='time-slot-container'], ul[class*='TimeSlotList'], div[class*='TimeSlotContainer']")
            )

        driver.switch_to.default_content()
        if has_ui_markers():
            return "TOP"

        for _ in range(12):
            driver.switch_to.default_content()
            frames = driver.find_elements(By.TAG_NAME, "iframe")
            frame_meta = []
            for i in range(len(frames)):
                try:
                    src = frames[i].get_attribute("src") or ""
                except Exception:
                    src = ""
                frame_meta.append((i, src))

            preferred = [x for x in frame_meta if "checkout-external" in x[1]]
            scan_list = preferred + [x for x in frame_meta if x not in preferred]

            for index, _src in scan_list:
                try:
                    driver.switch_to.default_content()
                    fresh_frames = driver.find_elements(By.TAG_NAME, "iframe")
                    if index >= len(fresh_frames):
                        continue
                    driver.switch_to.frame(fresh_frames[index])
                    if has_ui_markers():
                        return f"iframe[{index}]"
                except Exception:
                    continue
            time.sleep(0.6)

        driver.switch_to.default_content()
        raise RuntimeError("checkout UI not detected")

    def parse_time_slots_same_day() -> dict:
        slots = []
        for elem in driver.find_elements(By.XPATH, "//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'am') or contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'pm')]"):
            txt = (elem.text or "").strip()
            if " - " in txt and len(txt) <= 40:
                slots.append(txt)
        return {"time_slots": sorted(set(slots)), "mode": "time_slots"}

    def parse_date_tiles() -> dict:
        month_nodes = []
        for elem in driver.find_elements(By.XPATH, "//*[self::p or self::div][normalize-space(.)]"):
            txt = " ".join((elem.text or "").strip().split())
            explicit_year, month_num = parse_month_label(txt)
            if month_num:
                month_nodes.append((elem, explicit_year, month_num))

        results = []
        for month_node, explicit_year, month_num in month_nodes:

            container = month_node
            for _ in range(4):
                try:
                    container = container.find_element(By.XPATH, "./..")
                except Exception:
                    break

            time_like = container.find_elements(
                By.XPATH,
                ".//*[self::p or self::div][contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'am') or contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'pm')]",
            )
            for t in time_like:
                ttxt = (t.text or "").strip()
                m = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)", ttxt.lower())
                if not m:
                    continue
                t24 = to_24h(f"{m.group(1)}:{m.group(2)} {m.group(3)}")
                if not t24:
                    continue
                hh, mm = map(int, t24.split(":"))

                day_num = None
                tile_root = t
                try:
                    for _ in range(6):
                        tile_root = tile_root.find_element(By.XPATH, "./..")
                        day_candidates = tile_root.find_elements(
                            By.XPATH,
                            ".//*[normalize-space(text()) and string-length(normalize-space(text()))<=2]",
                        )
                        for candidate in day_candidates:
                            dtxt = (candidate.text or "").strip()
                            if dtxt.isdigit():
                                value = int(dtxt)
                                if 1 <= value <= 31:
                                    day_num = value
                                    break
                        if day_num:
                            break
                except Exception:
                    pass

                if not day_num:
                    continue

                resolved_date = resolve_event_date(day_num, month_num, explicit_year)
                if not resolved_date:
                    continue

                dt = datetime(resolved_date.year, resolved_date.month, resolved_date.day, hh, mm)
                results.append(dt.strftime("%Y-%m-%dT%H:%M"))

        seen = set()
        out = []
        for value in results:
            if value not in seen:
                seen.add(value)
                out.append(value)
        return {"available_datetimes": out, "mode": "date_tiles"}

    def parse_calendar_grid() -> dict:
        results = defaultdict(list)

        def get_calendar_container():
            containers = driver.find_elements(By.CSS_SELECTOR, "div[data-testid='scrollable-calendar-container']")
            return containers[0] if containers else None

        def scrape_visible_months() -> dict:
            out = defaultdict(list)
            months = driver.find_elements(By.CSS_SELECTOR, "div[data-testid='month']")
            for month_el in months:
                try:
                    month_label = month_el.find_element(By.CSS_SELECTOR, "p[class*='monthName']").text.strip()
                except Exception:
                    month_label = "n/a"

                try:
                    day_nodes = month_el.find_elements(
                        By.XPATH,
                        ".//li[contains(@class,'availableDateCell') and contains(@class,'enabled')]//p[contains(@class,'dateText')]",
                    )
                except Exception:
                    continue

                parsed = []
                for node in day_nodes:
                    txt = ((node.get_attribute("textContent") or "").strip())
                    if txt.isdigit():
                        parsed.append(int(txt))

                if month_label != "n/a" and parsed:
                    out[month_label].extend(parsed)
            return out

        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "div[data-testid='month']")) > 0)
        chunk = scrape_visible_months()
        for month_label, days in chunk.items():
            results[month_label].extend(days)

        prev_month_count = len(driver.find_elements(By.CSS_SELECTOR, "div[data-testid='month']"))
        for _ in range(AVAILABILITY_SCROLL_PASSES):
            scroll_targets = driver.find_elements(By.CSS_SELECTOR, "div[class*='ScrollableCalendar_scrollableList']")
            scroll_target = scroll_targets[0] if scroll_targets else get_calendar_container()
            if not scroll_target:
                break
            driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollTop + arguments[0].clientHeight * 0.9;",
                scroll_target,
            )
            time.sleep(0.8)

            new_month_count = len(driver.find_elements(By.CSS_SELECTOR, "div[data-testid='month']"))
            chunk = scrape_visible_months()
            for month_label, days in chunk.items():
                results[month_label].extend(days)

            if new_month_count == prev_month_count:
                break
            prev_month_count = new_month_count

        final = {k: sorted(set(v)) for k, v in results.items() if k and k != "n/a"}
        final["mode"] = "calendar_grid"
        return final

    logger.info(f"Checking recurring availability: {event_url}")
    driver.get(event_url)
    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    time.sleep(1.2)

    if not click_check_availability(driver, wait):
        return {"n/a": []}

    time.sleep(0.8)

    try:
        find_checkout_context_anywhere()
    except Exception:
        return {"n/a": []}

    ui = detect_checkout_ui()
    if ui == "time_slots":
        return parse_time_slots_same_day()
    if ui == "date_tiles":
        return parse_date_tiles()
    if ui == "calendar_grid":
        return parse_calendar_grid()
    return {"n/a": []}


def expand_recurring_rows(base_rows: list[dict]) -> tuple[list[dict], dict]:
    stats = {
        "recurring_checked": 0,
        "recurring_expanded": 0,
        "recurring_failed": 0,
    }

    expanded = list(base_rows)
    seen = {
        (
            normalise_title(row.get("title", "")),
            row.get("date", ""),
            row.get("start_time") or "",
            row.get("source_url") or "",
        )
        for row in expanded
    }

    recurring_rows = [row for row in base_rows if row.get("re_occurring") and row.get("source_url")]
    if not recurring_rows:
        return expanded, stats

    driver = build_chrome_driver(user_agent=USER_AGENT)
    try:
        for parent in recurring_rows:
            stats["recurring_checked"] += 1
            source_url = parent.get("source_url")
            if not source_url:
                continue

            try:
                availability = get_recurring_availability_dates(driver, source_url)
            except Exception as exc:
                stats["recurring_failed"] += 1
                logger.warning(f"Recurring availability failed for {source_url}: {exc}")
                continue

            mode = availability.get("mode")
            children = []

            if mode == "calendar_grid":
                for month_label, days in availability.items():
                    if month_label == "mode" or not isinstance(days, list):
                        continue
                    explicit_year, month = parse_month_label(month_label)
                    if not month:
                        continue
                    for day_num in days:
                        resolved_date = resolve_event_date(int(day_num), month, explicit_year)
                        if not resolved_date:
                            continue
                        child = deepcopy(parent)
                        child["date"] = resolved_date.isoformat()
                        child["re_occurring"] = False
                        child["notes"] = ((child.get("notes") or "") + f" | derived_from_recurring: {source_url} | mode: calendar_grid | month_label: {month_label}").strip()
                        children.append(child)

            elif mode == "date_tiles":
                for dt_str in availability.get("available_datetimes", []) or []:
                    try:
                        dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M")
                    except ValueError:
                        continue
                    child = deepcopy(parent)
                    child["date"] = dt.strftime("%Y-%m-%d")
                    child["start_time"] = dt.strftime("%H:%M")
                    child["re_occurring"] = False
                    child["notes"] = ((child.get("notes") or "") + f" | derived_from_recurring: {source_url} | mode: date_tiles").strip()
                    children.append(child)

            elif mode == "time_slots":
                for slot in explode_time_slots(availability.get("time_slots", []) or []):
                    start_time, end_time = parse_slot_range_to_24h(slot)
                    child = deepcopy(parent)
                    child["start_time"] = start_time or child.get("start_time")
                    child["end_time"] = end_time or child.get("end_time")
                    child["re_occurring"] = False
                    child["notes"] = ((child.get("notes") or "") + f" | derived_from_recurring: {source_url} | mode: time_slots | slot: {slot}").strip()
                    children.append(child)

            for child in children:
                key = (
                    normalise_title(child.get("title", "")),
                    child.get("date", ""),
                    child.get("start_time") or "",
                    child.get("source_url") or "",
                )
                if key in seen:
                    continue
                seen.add(key)
                expanded.append(child)
                stats["recurring_expanded"] += 1

    finally:
        driver.quit()

    expanded.sort(key=lambda r: (r["date"], r.get("start_time") or "", r["title"]))
    return expanded, stats


def build_event_records(rows: list[dict]) -> list[dict]:
    records = []
    for row in rows:
        records.append(
            build_record(
                title=row["title"],
                date=row["date"],
                day_name=day_name_from_date_str(row["date"]),
                start_time=row.get("start_time"),
                end_time=row.get("end_time"),
                location_name=row.get("location_name", LOCATION_NAME),
                location=build_location_object(row.get("location_name", LOCATION_NAME)),
                categories=build_categories(row.get("location_name", LOCATION_NAME)),
                audience_type=["Public"],
                filter=FILTER_LABEL,
                source=SOURCE_NAME,
                source_url=row.get("source_url") or BASE_SOURCE_URL,
                record_type=RECORD_TYPE,
                scraper=SCRAPER_NAME,
                notes=row.get("notes", ""),
                access_level=ACCESS_LEVEL,
                dataset=DATASET,
                allowed_roles=ALLOWED_ROLES,
            )
        )

    records.sort(key=lambda r: (r["date"], r.get("start_time") or "", r["title"]))
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
    cards_scraped = 0
    cards_after_scrape_dedupe = 0
    records_uploaded = 0
    raw_key = None
    latest_key = None

    logger.info("Starting Eventbrite events scraper")

    scrape_stats = {}
    compile_stats = {}
    recurring_stats = {}

    try:
        scraped_cards, scrape_stats = scrape_search_results()
        cards_scraped = len(scraped_cards)
        logger.info(f"Scraped {cards_scraped} raw Eventbrite cards")

        deduped_cards, duplicate_card_count = dedupe_scraped_cards(scraped_cards)
        cards_after_scrape_dedupe = len(deduped_cards)
        logger.info(
            f"Deduped raw cards to {cards_after_scrape_dedupe} unique entries (removed {duplicate_card_count})"
        )

        base_rows, compile_stats = compile_base_rows(deduped_cards)
        logger.info(f"Compiled {len(base_rows)} base rows after business filtering")

        if ENABLE_RECURRING_EXPANSION:
            expanded_rows, recurring_stats = expand_recurring_rows(base_rows)
            logger.info(
                f"Recurring expansion added {recurring_stats.get('recurring_expanded', 0)} rows "
                f"from {recurring_stats.get('recurring_checked', 0)} recurring cards"
            )
        else:
            expanded_rows = base_rows
            recurring_stats = {
                "recurring_checked": 0,
                "recurring_expanded": 0,
                "recurring_failed": 0,
            }
            logger.info("Recurring expansion disabled by EVENTBRITE_EXPAND_RECURRING")

        records = build_event_records(expanded_rows)
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
            employees_fetched=cards_scraped,
            records_uploaded=records_uploaded,
            raw_key=raw_key,
            latest_key=latest_key,
            error=None,
        )
        run_log.update(
            {
                "cards_scraped": cards_scraped,
                "cards_after_scrape_dedupe": cards_after_scrape_dedupe,
                "duplicate_cards_removed": duplicate_card_count,
                **scrape_stats,
                **compile_stats,
                **recurring_stats,
            }
        )
        log_key = upload_run_log_to_s3(run_log)

        logger.info(f"Upload complete: {latest_key}")
        logger.info(f"Run log written to: {log_key}")

        print(
            json.dumps(
                {
                    "status": "ok",
                    "cards_scraped": cards_scraped,
                    "cards_after_scrape_dedupe": cards_after_scrape_dedupe,
                    "records_uploaded": records_uploaded,
                    "bucket": S3_BUCKET,
                    "raw_key": raw_key,
                    "latest_key": latest_key,
                    "log_key": log_key,
                    "scrape_stats": scrape_stats,
                    "compile_stats": compile_stats,
                    "recurring_stats": recurring_stats,
                },
                indent=2,
            )
        )

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
                employees_fetched=cards_scraped,
                records_uploaded=records_uploaded,
                raw_key=raw_key,
                latest_key=latest_key,
                error=str(exc),
            )
            run_log.update(
                {
                    "cards_scraped": cards_scraped,
                    "cards_after_scrape_dedupe": cards_after_scrape_dedupe,
                    **scrape_stats,
                    **compile_stats,
                    **recurring_stats,
                }
            )
            log_key = upload_run_log_to_s3(run_log)
            logger.info(f"Error log written to: {log_key}")
        except Exception:
            logger.exception("Failed to write error log to S3")

        raise


if __name__ == "__main__":
    main()