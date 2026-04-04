import json
import os
from datetime import datetime, date, timedelta

import requests

from common.aws import get_s3_client
from common.logging_utils import get_logger
from common.s3_paths import utc_iso, build_latest_key, build_log_key, build_raw_key
from common.schema import build_dataset_payload, build_record, build_run_log


# =========================
# CONFIG
# =========================

S3_BUCKET = os.getenv("S3_BUCKET", "event-scrape-data")

DATASET = "melbournecb-conferences"
ACCESS_LEVEL = "public"
ALLOWED_ROLES = ["staff", "supervisor", "manager", "admin"]
RECORD_TYPE = "public_event"
SCRAPER_NAME = "melbournecb-conferences-v1"
SOURCE_NAME = "Melbourne Convention Bureau"
SOURCE_URL = "https://www.melbournecb.com.au/conferences"

MCB_API_URL = "https://www.melbournecb.com.au/api/feature/content/mcbeventlistings"

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; melbournecb-conferences/1.0)",
    "Accept": "application/json",
    "Referer": SOURCE_URL,
}

DEFAULT_AUDIENCE = ["Public", "Conference"]
DEFAULT_LOCATION_NAME = "Melbourne"
DEFAULT_LOCATION_OBJECT = {
    "code": "MELBOURNE",
    "search_text": "Melbourne Victoria Australia",
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

def day_name_from_date_str(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")


def is_today_or_future(yyyy_mm_dd: str) -> bool:
    try:
        return datetime.strptime(yyyy_mm_dd, "%Y-%m-%d").date() >= date.today()
    except ValueError:
        return False


def ddmmyyyy_to_iso(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    try:
        return datetime.strptime(s, "%d-%m-%Y").date().isoformat()
    except ValueError:
        return ""


def expand_ddmmyyyy_range_to_days(from_ddmmyyyy: str, to_ddmmyyyy: str) -> list[str]:
    start_iso = ddmmyyyy_to_iso(from_ddmmyyyy)
    end_iso = ddmmyyyy_to_iso(to_ddmmyyyy)

    if not start_iso:
        return []

    if not end_iso:
        return [start_iso]

    start_d = datetime.strptime(start_iso, "%Y-%m-%d").date()
    end_d = datetime.strptime(end_iso, "%Y-%m-%d").date()

    if end_d < start_d:
        start_d, end_d = end_d, start_d

    out = []
    d = start_d
    while d <= end_d:
        out.append(d.isoformat())
        d += timedelta(days=1)

    return out


# =========================
# FETCH
# =========================

def fetch_conferences() -> list[dict]:
    r = requests.get(MCB_API_URL, headers=REQUEST_HEADERS, timeout=30)
    r.raise_for_status()

    data = r.json()
    listings = data.get("listings", [])

    if not isinstance(listings, list):
        return []

    return listings


# =========================
# TRANSFORM
# =========================

def build_records(listings: list[dict]) -> list[dict]:
    records = []

    for it in listings:
        if not isinstance(it, dict):
            continue

        title = (it.get("Event") or "").strip()
        venue = (it.get("Venue") or "").strip()
        category = (it.get("Category") or "").strip()
        web_url = (it.get("WebUrl") or "").strip()

        from_raw = (it.get("FromDate") or "").strip()
        to_raw = (it.get("ToDate") or "").strip()

        days = expand_ddmmyyyy_range_to_days(from_raw, to_raw)

        for d in days:
            if not is_today_or_future(d):
                continue

            record = build_record(
                title=title,
                date=d,
                day_name=day_name_from_date_str(d),
                start_time=None,
                end_time=None,
                location_name=venue or DEFAULT_LOCATION_NAME,
                location=DEFAULT_LOCATION_OBJECT,
                categories=[category] if category else ["Conference"],
                audience_type=DEFAULT_AUDIENCE,
                source=SOURCE_NAME,
                source_url=web_url or SOURCE_URL,
                record_type=RECORD_TYPE,
                scraper=SCRAPER_NAME,
                notes="",
                access_level=ACCESS_LEVEL,
                dataset=DATASET,
                allowed_roles=ALLOWED_ROLES,
            )

            records.append(record)

    records.sort(key=lambda r: (r["date"], r["title"]))
    logger.info(f"Built {len(records)} conference records")

    return records


# =========================
# PAYLOAD
# =========================

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
# S3
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

def main():
    started_at = utc_iso()
    records_uploaded = 0
    raw_key = None
    latest_key = None

    logger.info("Starting MelbourneCB conferences scraper")

    try:
        listings = fetch_conferences()
        logger.info(f"Fetched {len(listings)} listings")

        records = build_records(listings)
        payload = build_payload(records)

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
            employees_fetched=len(listings),
            records_uploaded=records_uploaded,
            raw_key=raw_key,
            latest_key=latest_key,
            error=None,
        )

        log_key = upload_run_log_to_s3(run_log)

        print(json.dumps({
            "status": "ok",
            "records_uploaded": records_uploaded,
            "bucket": S3_BUCKET,
            "raw_key": raw_key,
            "latest_key": latest_key,
            "log_key": log_key,
        }, indent=2))

    except Exception as e:
        finished_at = utc_iso()
        logger.exception("Scraper failed")

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
            employees_fetched=0,
            records_uploaded=records_uploaded,
            raw_key=raw_key,
            latest_key=latest_key,
            error=str(e),
        )

        upload_run_log_to_s3(run_log)
        raise


if __name__ == "__main__":
    main()