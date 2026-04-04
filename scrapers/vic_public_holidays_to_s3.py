import json
import os
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from common.aws import get_s3_client
from common.logging_utils import get_logger
from common.s3_paths import utc_iso, build_latest_key, build_log_key, build_raw_key
from common.schema import build_dataset_payload, build_record, build_run_log


# =========================
# CONFIG
# =========================

BASE_URL = "https://business.vic.gov.au/business-information/public-holidays/victorian-public-holidays-{year}"
SOURCE_NAME = "Business Victoria"
SOURCE_URL = "https://business.vic.gov.au/business-information/public-holidays"
S3_BUCKET = os.getenv("S3_BUCKET", "event-scrape-data")

DATASET = "vic-public-holidays"
ACCESS_LEVEL = "public"
ALLOWED_ROLES = ["staff", "supervisor", "manager", "admin"]
RECORD_TYPE = "public_holiday"
SCRAPER_NAME = "vic-public-holidays-v1"

LOCATION_NAME = "Victoria"
LOCATION_OBJECT = {
    "code": "VIC",
    "search_text": "Victoria Australia",
    "latitude": None,
    "longitude": None,
}

DEFAULT_CATEGORY = "Public Holiday"
DEFAULT_AUDIENCE = ["Public"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Connection": "keep-alive",
}

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


# =========================
# LOGGING / AWS
# =========================

logger = get_logger(__name__)
s3 = get_s3_client(logger=logger)


# =========================
# HELPERS
# =========================

def clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def strip_sup(tag) -> None:
    for sup in tag.find_all("sup"):
        sup.decompose()


def day_name_from_date_str(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")


def parse_date_iso(text: str, year: int) -> str:
    """
    "Thursday 1 January" -> "YYYY-01-01"
    "Subject to AFL schedule" -> ""
    """
    t = clean(text).lower()
    if not t or "subject to" in t or "tba" in t:
        return ""

    m = re.search(r"\b(\d{1,2})\s+([a-z]+)\b", t)
    if not m:
        return ""

    day = int(m.group(1))
    month = MONTHS.get(m.group(2))
    if not month:
        return ""

    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def find_holidays_table(soup: BeautifulSoup, year: int):
    table = soup.select_one("div.table-wrap table")
    if table:
        return table

    table = soup.find("table", id=re.compile(r"^table\d+$"))
    if table:
        return table

    for t in soup.find_all("table"):
        th_texts = [clean(th.get_text(" ", strip=True)).lower() for th in t.find_all("th")]
        has_holiday = any(x == "holiday" for x in th_texts)
        has_year = any("date" in x and str(year) in x for x in th_texts)
        if has_holiday and has_year:
            return t

    needle = f"date in {year}"
    for t in soup.find_all("table"):
        txt = clean(t.get_text(" ", strip=True)).lower()
        if "holiday" in txt and needle in txt:
            return t

    return None


def fetch_year_html(year: int) -> str:
    url = BASE_URL.format(year=year)
    resp = requests.get(url, headers=HEADERS, timeout=30)
    logger.info(f"[{year}] HTTP {resp.status_code} | {resp.headers.get('content-type', '')}")

    if resp.status_code == 404:
        raise FileNotFoundError(f"No page for year {year}")

    resp.raise_for_status()
    return resp.text


def year_page_looks_valid(html: str, year: int) -> bool:
    text = clean(BeautifulSoup(html, "html.parser").get_text(" ", strip=True)).lower()
    return (
        "public holidays" in text
        and "victorian public holidays" in text
        and str(year) in text
    )


def get_available_years() -> list[int]:
    """
    Scan a reasonable window and keep only years that resolve to a valid holiday page.
    """
    current_year = datetime.now().year
    candidate_years = range(current_year - 2, current_year + 6)
    valid_years = []

    for year in candidate_years:
        try:
            html = fetch_year_html(year)
            if year_page_looks_valid(html, year):
                valid_years.append(year)
            else:
                logger.info(f"[{year}] Skipped: page did not look like a valid holidays page")
        except FileNotFoundError:
            logger.info(f"[{year}] Skipped: no page found")
        except Exception as exc:
            logger.warning(f"[{year}] Failed during discovery: {exc}")

    return valid_years


def scrape_year(year: int) -> list[dict]:
    html = fetch_year_html(year)
    soup = BeautifulSoup(html, "html.parser")

    table = find_holidays_table(soup, year)
    if not table:
        raise RuntimeError(f"[{year}] Public holidays table not found")

    tbody = table.find("tbody")
    if not tbody:
        raise RuntimeError(f"[{year}] Found table but no <tbody>")

    rows_out = []

    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue

        holiday_td, date_td = tds[0], tds[1]
        strip_sup(holiday_td)
        strip_sup(date_td)

        holiday = clean(holiday_td.get_text(" ", strip=True))
        if not holiday:
            continue

        date_text = clean(date_td.get_text(" | ", strip=True))
        parts = [clean(p) for p in date_text.split("|") if clean(p)]

        for part in parts:
            date_iso = parse_date_iso(part, year)
            if not date_iso:
                continue

            rows_out.append(
                {
                    "title": f"{holiday} Public Holiday",
                    "date": date_iso,
                    "notes": f"Source page year: {year}",
                    "source_url": BASE_URL.format(year=year),
                }
            )

    return rows_out


def dedupe_rows(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []

    for row in rows:
        key = (row["title"], row["date"])
        if key in seen:
            continue
        seen.add(key)
        out.append(row)

    out.sort(key=lambda r: (r["date"], r["title"]))
    return out


# =========================
# RECORD BUILDING
# =========================

def build_holiday_records(parsed_rows: list[dict]) -> list[dict]:
    records = []

    for row in parsed_rows:
        records.append(
            build_record(
                title=row["title"],
                date=row["date"],
                day_name=day_name_from_date_str(row["date"]),
                start_time=None,
                end_time=None,
                location_name=LOCATION_NAME,
                location=LOCATION_OBJECT,
                categories=[DEFAULT_CATEGORY],
                audience_type=DEFAULT_AUDIENCE,
                source=SOURCE_NAME,
                source_url=row["source_url"],
                record_type=RECORD_TYPE,
                scraper=SCRAPER_NAME,
                notes=row.get("notes", ""),
                access_level=ACCESS_LEVEL,
                dataset=DATASET,
                allowed_roles=ALLOWED_ROLES,
            )
        )

    logger.info(f"Built {len(records)} public holiday records")
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
    years_checked = 0
    years_scraped = 0
    rows_found = 0
    records_uploaded = 0
    raw_key = None
    latest_key = None

    logger.info("Starting VIC public holidays scraper")

    try:
        years = get_available_years()
        years_checked = len(range(datetime.now().year - 2, datetime.now().year + 6))
        years_scraped = len(years)

        if not years:
            raise RuntimeError("No valid public holiday year pages found")

        logger.info(f"Discovered valid years: {years}")

        all_rows = []
        for year in years:
            year_rows = scrape_year(year)
            logger.info(f"[{year}] Parsed {len(year_rows)} holiday rows")
            all_rows.extend(year_rows)

        all_rows = dedupe_rows(all_rows)
        rows_found = len(all_rows)

        if not all_rows:
            raise RuntimeError("No public holiday rows scraped from any valid year page")

        records = build_holiday_records(all_rows)
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
            employees_fetched=rows_found,
            records_uploaded=records_uploaded,
            raw_key=raw_key,
            latest_key=latest_key,
            error=None,
        )
        run_log["years_checked"] = years_checked
        run_log["years_scraped"] = years_scraped
        run_log["rows_found"] = rows_found
        run_log["years"] = years

        log_key = upload_run_log_to_s3(run_log)

        logger.info(f"Upload complete: {latest_key}")
        logger.info(f"Run log written to: {log_key}")

        print(json.dumps({
            "status": "ok",
            "years_checked": years_checked,
            "years_scraped": years_scraped,
            "rows_found": rows_found,
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
                employees_fetched=rows_found,
                records_uploaded=records_uploaded,
                raw_key=raw_key,
                latest_key=latest_key,
                error=str(exc),
            )
            run_log["years_checked"] = years_checked
            run_log["years_scraped"] = years_scraped
            run_log["rows_found"] = rows_found

            log_key = upload_run_log_to_s3(run_log)
            logger.info(f"Error log written to: {log_key}")
        except Exception:
            logger.exception("Failed to write error log to S3")

        raise


if __name__ == "__main__":
    main()