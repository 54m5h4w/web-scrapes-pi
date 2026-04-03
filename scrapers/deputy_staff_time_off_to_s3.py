import os
import json
import time
import requests

from datetime import datetime, date, timedelta

from common.aws import get_s3_client
from common.logging_utils import get_logger
from common.s3_paths import utc_iso, build_raw_key, build_latest_key, build_log_key
from common.schema import build_record, build_dataset_payload, build_run_log
from common.locations import get_location_name, build_location_object


# =========================
# CONFIG
# =========================

INSTALL = os.getenv("DEPUTY_INSTALL", "hospitalityone.au")
API_KEY = os.getenv("DEPUTY_API_KEY")
S3_BUCKET = os.getenv("S3_BUCKET", "event-scrape-data")

DATASET = "staff-time-off"
ACCESS_LEVEL = "restricted"
ALLOWED_ROLES = ["manager", "admin"]
RECORD_TYPE = "staff_leave"
SCRAPER_NAME = "deputy-staff-leave-v1"
SOURCE_NAME = "Deputy"
SOURCE_URL = None

EMPLOYEE_QUERY_URL = f"https://{INSTALL}.deputy.com/api/v1/resource/Employee/QUERY"
LEAVE_QUERY_URL = f"https://{INSTALL}.deputy.com/api/v1/resource/Leave/QUERY"

COMPANY_IDS = {10, 14, 16}
EXCLUDED_ROLES = {50, 144, 145}

EXCLUDED_NAMES = {
    ("terry", "panayiotou"),
    ("malcolm", "williams"),
    ("jonathan", "mooney"),
}

if not API_KEY:
    raise RuntimeError("Missing DEPUTY_API_KEY environment variable.")

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "DeputyAPI/1.0 (+python-requests)",
}


# =========================
# LOGGING / AWS
# =========================

logger = get_logger(__name__)
s3 = get_s3_client(logger=logger)


# =========================
# HELPERS
# =========================

def iso_to_date(iso_str: str) -> date:
    return datetime.fromisoformat(iso_str).date()

def daterange_inclusive(d1: date, d2: date):
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)

def day_name_from_date_str(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")


# =========================
# API
# =========================

def employee_query(payload: dict) -> list[dict]:
    r = requests.post(
        EMPLOYEE_QUERY_URL,
        headers=HEADERS,
        json=payload,
        timeout=60,
        allow_redirects=False,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected response type from Deputy: {type(data)}")
    return data

def fetch_all_employees() -> list[dict]:
    all_rows: list[dict] = []
    last_id = 0

    while True:
        payload = {
            "search": {
                "s1": {"field": "Id", "type": "gt", "data": last_id}
            }
        }

        batch = employee_query(payload)
        if not batch:
            break

        all_rows.extend(batch)

        new_last_id = max(int(e["Id"]) for e in batch if "Id" in e)
        if new_last_id <= last_id:
            raise RuntimeError(f"Paging stalled (last_id={last_id}, new_last_id={new_last_id}).")
        last_id = new_last_id

    dedup = {int(e["Id"]): e for e in all_rows if "Id" in e}
    return [dedup[k] for k in sorted(dedup.keys())]

def get_future_leave(employee_id: int) -> list[dict]:
    now_ts = int(time.time())
    payload = {
        "search": {
            "s1": {"field": "Employee", "type": "eq", "data": employee_id},
            "s2": {"field": "Start", "type": "gt", "data": now_ts},
        }
    }
    r = requests.post(
        LEAVE_QUERY_URL,
        headers=HEADERS,
        json=payload,
        timeout=60,
        allow_redirects=False,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


# =========================
# LOCATION
# =========================

def resolve_location_code(employee: dict) -> str:
    first = (employee.get("FirstName") or "").strip().lower()
    last = (employee.get("LastName") or "").strip().lower()

    if (first, last) in {
        ("malcolm", "williams"),
        ("jonathan", "mooney"),
        ("nick", "marriot"),
    }:
        return "ALL"

    company_map = {
        10: "HATF",
        14: "BP",
        16: "P5",
    }

    return company_map.get(employee.get("Company"), "UNKNOWN")


# =========================
# RECORD BUILDING
# =========================

def filtered_staff(employees: list[dict]) -> list[dict]:
    return [
        e for e in employees
        if e.get("Active") is True
        and e.get("Company") in COMPANY_IDS
        and e.get("Role") not in EXCLUDED_ROLES
        and (
            ((e.get("FirstName") or "").strip().lower(),
             (e.get("LastName") or "").strip().lower())
            not in EXCLUDED_NAMES
        )
    ]

def build_leave_records(employees: list[dict]) -> list[dict]:
    rows = []
    seen = set()

    staff = filtered_staff(employees)
    logger.info(f"Filtered to {len(staff)} active staff records")

    for emp in staff:
        emp_id = emp.get("Id")
        if not emp_id:
            continue

        first = (emp.get("FirstName") or "").strip()
        last = (emp.get("LastName") or "").strip()
        full_name = f"{first} {last}".strip()

        location_code = resolve_location_code(emp)
        location_name = get_location_name(location_code)
        location_obj = build_location_object(location_code)

        leave_records = get_future_leave(int(emp_id))

        for lv in leave_records:
            ds = lv.get("DateStart")
            de = lv.get("DateEnd")
            if not ds or not de:
                continue

            start_d = iso_to_date(ds)
            end_d = iso_to_date(de)

            for day in daterange_inclusive(start_d, end_d):
                day_str = day.isoformat()
                key = (int(emp_id), day_str)
                if key in seen:
                    continue
                seen.add(key)

                rows.append(
                    build_record(
                        title=f"{full_name} Annual Leave",
                        date=day_str,
                        day_name=day_name_from_date_str(day_str),
                        start_time=None,
                        end_time=None,
                        location_name=location_name,
                        location=location_obj,
                        categories=["Staff Time Off"],
                        audience_type=["Internal", "Staffing"],
                        source=SOURCE_NAME,
                        source_url=SOURCE_URL,
                        record_type=RECORD_TYPE,
                        scraper=SCRAPER_NAME,
                        notes="",
                        access_level=ACCESS_LEVEL,
                        dataset=DATASET,
                        allowed_roles=ALLOWED_ROLES,
                    )
                )

    rows.sort(key=lambda r: (r["date"], r["title"]))
    logger.info(f"Built {len(rows)} leave records")
    return rows


# =========================
# PAYLOADS
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

def main():
    started_at = utc_iso()
    employees_fetched = 0
    records_uploaded = 0
    raw_key = None
    latest_key = None

    logger.info("Starting Deputy staff time off scraper")

    try:
        employees = fetch_all_employees()
        employees_fetched = len(employees)
        logger.info(f"Fetched {employees_fetched} employees")

        records = build_leave_records(employees)
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
            employees_fetched=employees_fetched,
            records_uploaded=records_uploaded,
            raw_key=raw_key,
            latest_key=latest_key,
            error=None,
        )
        log_key = upload_run_log_to_s3(run_log)

        logger.info(f"Upload complete: {latest_key}")
        logger.info(f"Run log written to: {log_key}")

        print(json.dumps({
            "status": "ok",
            "employees_fetched": employees_fetched,
            "records_uploaded": records_uploaded,
            "bucket": S3_BUCKET,
            "raw_key": raw_key,
            "latest_key": latest_key,
            "log_key": log_key,
        }, indent=2))

    except Exception as e:
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
                employees_fetched=employees_fetched,
                records_uploaded=records_uploaded,
                raw_key=raw_key,
                latest_key=latest_key,
                error=str(e),
            )
            log_key = upload_run_log_to_s3(run_log)
            logger.info(f"Error log written to: {log_key}")
        except Exception:
            logger.exception("Failed to write error log to S3")

        raise


if __name__ == "__main__":
    main()