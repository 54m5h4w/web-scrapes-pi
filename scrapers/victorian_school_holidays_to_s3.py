import json
import os
import re
from datetime import datetime, timedelta, date

import requests
from bs4 import BeautifulSoup

from common.aws import get_s3_client
from common.logging_utils import get_logger
from common.s3_paths import utc_iso, build_raw_key, build_latest_key, build_log_key
from common.schema import build_record, build_dataset_payload, build_run_log


# =========================
# CONFIG
# =========================

URL = "https://www.vic.gov.au/school-term-dates-and-holidays-victoria"

S3_BUCKET = os.getenv("S3_BUCKET", "event-scrape-data")

DATASET = "victorian-school-holidays"
ACCESS_LEVEL = "public"
ALLOWED_ROLES = ["staff", "supervisor", "manager", "admin"]
RECORD_TYPE = "public_event"
SCRAPER_NAME = "victorian-school-holidays-v1"
SOURCE_NAME = "vic.gov.au"
SOURCE_URL = URL

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

LOCATION_NAME = "Victoria, Australia"
LOCATION_OBJECT = {
    "code": "VIC",
    "search_text": "Victoria Australia",
    "latitude": None,
    "longitude": None,
}


# =========================
# LOGGING / AWS
# =========================

logger = get_logger(__name__)
s3 = get_s3_client(logger=logger)


# =========================
# HELPERS
# =========================

def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def iso(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def day_name_from_date_str(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")


def daterange(start_d: date, end_d: date):
    cur = start_d
    while cur <= end_d:
        yield cur
        cur += timedelta(days=1)


def parse_first_day_month(text: str, year: int) -> date:
    """
    Extract the first 'DD Month' found in a cell, e.g.
    'Tuesday 27 January (students start Wednesday 28 January...)'
    """
    t = clean(text).lower()
    m = re.search(r"\b(\d{1,2})\s+([a-z]+)\b", t)
    if not m:
        raise ValueError(f"Could not parse a date from: {text!r}")

    day = int(m.group(1))
    month_name = m.group(2)
    month = MONTHS.get(month_name)

    if not month:
        raise ValueError(f"Unknown month {month_name!r} in: {text!r}")

    return date(year, month, day)


def find_term_table_for_year(soup: BeautifulSoup, year: int):
    caption_text = f"{year} Victorian school term dates".lower()

    for table in soup.find_all("table"):
        cap = table.find("caption")
        if cap and clean(cap.get_text(" ", strip=True)).lower() == caption_text:
            return table

    return None


def find_available_term_years(soup: BeautifulSoup) -> list[int]:
    """
    Find all years with captions like:
      '2026 Victorian school term dates'
    """
    years = set()

    for table in soup.find_all("table"):
        cap = table.find("caption")
        if not cap:
            continue

        caption = clean(cap.get_text(" ", strip=True))
        m = re.match(r"^(\d{4})\s+Victorian school term dates$", caption, flags=re.I)
        if m:
            years.add(int(m.group(1)))

    return sorted(years)


def extract_terms(soup: BeautifulSoup, year: int) -> dict[str, tuple[date, date]] | None:
    """
    Returns:
      {
        "Term 1": (start_date, finish_date),
        "Term 2": (...),
        ...
      }
    """
    table = find_term_table_for_year(soup, year)
    if not table:
        return None

    tbody = table.find("tbody")
    if not tbody:
        return None

    term_map: dict[str, tuple[date, date]] = {}

    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue

        term = clean(tds[0].get_text(" ", strip=True))
        start_raw = clean(tds[1].get_text(" ", strip=True))
        finish_raw = clean(tds[2].get_text(" ", strip=True))

        try:
            start_d = parse_first_day_month(start_raw, year)
            finish_d = parse_first_day_month(finish_raw, year)
        except ValueError:
            continue

        term_map[term] = (start_d, finish_d)

    needed = ["Term 1", "Term 2", "Term 3", "Term 4"]
    if not all(k in term_map for k in needed):
        return None

    return term_map


def fetch_term_data() -> tuple[dict[int, dict[str, tuple[date, date]]], list[int], list[int]]:
    current_year = datetime.now().year

    logger.info(f"Fetching Victorian school holiday page: {URL}")
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    logger.info(f"HTTP {resp.status_code} | {resp.headers.get('content-type', '')}")
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    available_years = find_available_term_years(soup)
    target_years = [y for y in available_years if y >= current_year]

    logger.info(f"Available years found on page: {available_years}")
    logger.info(f"Target years from current year onward: {target_years}")

    if not target_years:
        raise RuntimeError(
            f"No school term tables found for current year onward (current_year={current_year})."
        )

    term_data: dict[int, dict[str, tuple[date, date]]] = {}

    for y in target_years:
        td = extract_terms(soup, y)
        if td:
            term_data[y] = td
        else:
            logger.warning(f"Missing or partial term table for {y}")

    return term_data, available_years, target_years


# =========================
# RECORD BUILDING
# =========================

def build_school_holiday_records(term_data: dict[int, dict[str, tuple[date, date]]], target_years: list[int]) -> list[dict]:
    records: list[dict] = []

    for y in target_years:
        if y not in term_data:
            logger.warning(f"Skipping {y}: no term data")
            continue

        t = term_data[y]

        holiday_windows = [
            (
                "Term 1 School Holidays",
                t["Term 1"][1] + timedelta(days=1),
                t["Term 2"][0] - timedelta(days=1),
                "School Holiday",
                f"Derived from {y} Victorian school term dates table.",
            ),
            (
                "Term 2 School Holidays",
                t["Term 2"][1] + timedelta(days=1),
                t["Term 3"][0] - timedelta(days=1),
                "School Holiday",
                f"Derived from {y} Victorian school term dates table.",
            ),
            (
                "Term 3 School Holidays",
                t["Term 3"][1] + timedelta(days=1),
                t["Term 4"][0] - timedelta(days=1),
                "School Holiday",
                f"Derived from {y} Victorian school term dates table.",
            ),
        ]

        for title, start_d, end_d, category, notes in holiday_windows:
            if end_d < start_d:
                continue

            for d in daterange(start_d, end_d):
                day_str = iso(d)
                records.append(
                    build_record(
                        title=title,
                        date=day_str,
                        day_name=day_name_from_date_str(day_str),
                        start_time=None,
                        end_time=None,
                        location_name=LOCATION_NAME,
                        location=LOCATION_OBJECT,
                        categories=[category],
                        audience_type=["Public"],
                        source=SOURCE_NAME,
                        source_url=SOURCE_URL,
                        record_type=RECORD_TYPE,
                        scraper=SCRAPER_NAME,
                        notes=notes,
                        access_level=ACCESS_LEVEL,
                        dataset=DATASET,
                        allowed_roles=ALLOWED_ROLES,
                    )
                )

        # Last School Day = Term 4 finish date
        last_school_day = iso(t["Term 4"][1])
        records.append(
            build_record(
                title="Last School Day",
                date=last_school_day,
                day_name=day_name_from_date_str(last_school_day),
                start_time=None,
                end_time=None,
                location_name=LOCATION_NAME,
                location=LOCATION_OBJECT,
                categories=["School Term"],
                audience_type=["Public"],
                source=SOURCE_NAME,
                source_url=SOURCE_URL,
                record_type=RECORD_TYPE,
                scraper=SCRAPER_NAME,
                notes=f"Final day of Term 4 for {y}, derived from the Victorian school term dates table.",
                access_level=ACCESS_LEVEL,
                dataset=DATASET,
                allowed_roles=ALLOWED_ROLES,
            )
        )

        # Term 1 Begins = next available year's Term 1 start date
        if (y + 1) in term_data:
            term_1_begins = iso(term_data[y + 1]["Term 1"][0])
            records.append(
                build_record(
                    title="Term 1 Begins",
                    date=term_1_begins,
                    day_name=day_name_from_date_str(term_1_begins),
                    start_time=None,
                    end_time=None,
                    location_name=LOCATION_NAME,
                    location=LOCATION_OBJECT,
                    categories=["School Term"],
                    audience_type=["Public"],
                    source=SOURCE_NAME,
                    source_url=SOURCE_URL,
                    record_type=RECORD_TYPE,
                    scraper=SCRAPER_NAME,
                    notes=f"Start of Term 1 for {y + 1}, derived from the Victorian school term dates table.",
                    access_level=ACCESS_LEVEL,
                    dataset=DATASET,
                    allowed_roles=ALLOWED_ROLES,
                )
            )
        else:
            logger.warning(f"Cannot add 'Term 1 Begins' for {y}: missing Term 1 table for {y + 1}")

    records.sort(key=lambda r: (r["date"], r["title"]))
    logger.info(f"Built {len(records)} school holiday records")
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
    years_found = 0
    target_year_count = 0
    records_uploaded = 0
    raw_key = None
    latest_key = None

    logger.info("Starting Victorian school holidays scraper")

    try:
        term_data, available_years, target_years = fetch_term_data()
        years_found = len(available_years)
        target_year_count = len(target_years)

        records = build_school_holiday_records(term_data, target_years)
        if not records:
            raise RuntimeError("No records generated from Victorian school term data.")

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
            employees_fetched=years_found,
            records_uploaded=records_uploaded,
            raw_key=raw_key,
            latest_key=latest_key,
            error=None,
        )
        run_log["years_found"] = years_found
        run_log["target_year_count"] = target_year_count
        run_log["processed_years"] = sorted(term_data.keys())

        log_key = upload_run_log_to_s3(run_log)

        logger.info(f"Upload complete: {latest_key}")
        logger.info(f"Run log written to: {log_key}")

        print(json.dumps({
            "status": "ok",
            "years_found": years_found,
            "target_year_count": target_year_count,
            "processed_years": sorted(term_data.keys()),
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
                employees_fetched=years_found,
                records_uploaded=records_uploaded,
                raw_key=raw_key,
                latest_key=latest_key,
                error=str(exc),
            )
            run_log["years_found"] = years_found
            run_log["target_year_count"] = target_year_count
            log_key = upload_run_log_to_s3(run_log)
            logger.info(f"Error log written to: {log_key}")
        except Exception:
            logger.exception("Failed to write error log to S3")

        raise


if __name__ == "__main__":
    main()