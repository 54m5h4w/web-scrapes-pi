import csv
import io
import json
import os
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import requests

from common.aws import get_s3_client
from common.logging_utils import get_logger
from common.s3_paths import utc_iso, build_raw_key, build_latest_key, build_log_key
from common.schema import build_record, build_dataset_payload, build_run_log

try:
    from common.locations import get_location_name, build_location_object
except Exception:
    get_location_name = None
    build_location_object = None


# =========================
# CONFIG
# =========================

S3_BUCKET = os.getenv("S3_BUCKET", "event-scrape-data")

# Preferred: paste the Google Sheets CSV export URL directly into env
MANUAL_EVENTS_CSV_URL = "https://docs.google.com/spreadsheets/d/1PU7yKlL9N1qypTtK9ABNfCRx-FFgTo96iKFaOQ-Pq4g/export?format=csv&gid=2138812343"
GOOGLE_SHEET_GID = os.getenv("MANUAL_EVENTS_SHEET_GID", "0").strip()

DATASET = "manual-events"
ACCESS_LEVEL = "public"
ALLOWED_ROLES = ["staff", "supervisor", "manager", "admin"]
RECORD_TYPE = "public_event"
SCRAPER_NAME = "manual-events-v1"
SOURCE_NAME = "Manual"
SOURCE_URL = "https://docs.google.com/spreadsheets/d/1PU7yKlL9N1qypTtK9ABNfCRx-FFgTo96iKFaOQ-Pq4g/edit#gid=2138812343"
DEFAULT_AUDIENCE = ["Public"]
DEFAULT_CATEGORY = "Manual Event"
DEFAULT_LOCATION_CODE = os.getenv("MANUAL_EVENTS_DEFAULT_LOCATION_CODE", "").strip().upper()
HTTP_TIMEOUT_SECONDS = int(os.getenv("MANUAL_EVENTS_HTTP_TIMEOUT_SECONDS", "60"))


# =========================
# LOGGING / AWS
# =========================

logger = get_logger(__name__)
s3 = get_s3_client(logger=logger)


# =========================
# HELPERS
# =========================

def build_csv_url() -> str:
    if MANUAL_EVENTS_CSV_URL:
        return MANUAL_EVENTS_CSV_URL

    if GOOGLE_SHEET_ID:
        return (
            f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/export"
            f"?format=csv&gid={GOOGLE_SHEET_GID}"
        )

    raise RuntimeError(
        "Missing manual sheet config. Set MANUAL_EVENTS_CSV_URL "
        "or MANUAL_EVENTS_SHEET_ID (+ optional MANUAL_EVENTS_SHEET_GID)."
    )


def clean(value) -> str:
    return (value or "").strip()


def normalise_header(header: str) -> str:
    h = clean(header).lower()
    h = h.replace(" ", "_")
    return h


def day_name_from_date_str(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")


def parse_date_to_iso(value: str) -> str:
    value = clean(value)
    if not value:
        raise ValueError("Missing date")

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%-d/%-m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Last-resort ISO-ish parse for timestamps like 2026-04-10 00:00:00
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Unsupported date format: {value}")


def parse_time_to_hhmm(value: str) -> str | None:
    value = clean(value)
    if not value:
        return None

    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I%p", "%I:%M%p"):
        try:
            return datetime.strptime(value, fmt).strftime("%H:%M")
        except ValueError:
            pass

    raise ValueError(f"Unsupported time format: {value}")


def parse_list_cell(value: str, default: list[str] | None = None) -> list[str]:
    value = clean(value)
    if not value:
        return list(default or [])
    return [part.strip() for part in value.split(",") if part.strip()]


def extract_sheet_browser_url(csv_url: str) -> str | None:
    """
    Turn a CSV export URL into a nicer sheet URL for source_url when possible.
    """
    try:
        parsed = urlparse(csv_url)
        if "docs.google.com" not in parsed.netloc:
            return None
        parts = parsed.path.split("/")
        # /spreadsheets/d/{id}/export
        if len(parts) >= 4 and parts[1] == "spreadsheets" and parts[2] == "d":
            sheet_id = parts[3]
            qs = parse_qs(parsed.query)
            gid = qs.get("gid", ["0"])[0]
            return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={gid}"
    except Exception:
        return None
    return None


def resolve_location(location_code: str) -> tuple[str, dict]:
    code = clean(location_code).upper() or DEFAULT_LOCATION_CODE

    if code and build_location_object and get_location_name:
        try:
            return get_location_name(code), build_location_object(code)
        except Exception:
            logger.warning(f"Unknown location code '{code}', falling back to generic object")

    fallback_code = code or "MANUAL"
    fallback_name = code or "Manual / Unspecified Location"
    return fallback_name, {
        "code": fallback_code,
        "search_text": fallback_name,
        "latitude": None,
        "longitude": None,
    }


def upload_json_to_s3(key: str, payload: dict) -> None:
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


# =========================
# FETCH / PARSE
# =========================

def fetch_csv_text() -> tuple[str, str]:
    csv_url = build_csv_url()
    logger.info(f"Fetching manual events CSV: {csv_url}")

    response = requests.get(csv_url, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()

    text = response.content.decode("utf-8-sig")
    browser_url = extract_sheet_browser_url(csv_url) or csv_url
    return text, browser_url


def parse_csv_rows(csv_text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        raise RuntimeError("CSV has no header row")

    reader.fieldnames = [normalise_header(h) for h in reader.fieldnames]

    required = {"title", "date"}
    missing = sorted(required - set(reader.fieldnames))
    if missing:
        raise RuntimeError(f"CSV missing required columns: {', '.join(missing)}")

    rows = []
    for raw_row in reader:
        row = {normalise_header(k): clean(v) for k, v in raw_row.items() if k is not None}

        # Skip completely empty rows
        if not any(row.values()):
            continue

        rows.append(row)

    return rows


# =========================
# RECORD BUILDING
# =========================

def build_event_records(parsed_rows: list[dict], sheet_source_url: str) -> tuple[list[dict], list[dict]]:
    records = []
    rejected_rows = []
    seen = set()

    for idx, row in enumerate(parsed_rows, start=2):  # row 1 is header
        try:
            title = clean(row.get("title"))
            if not title:
                raise ValueError("Missing title")

            date_iso = parse_date_to_iso(row.get("date", ""))
            start_time = parse_time_to_hhmm(row.get("start_time", ""))
            end_time = parse_time_to_hhmm(row.get("end_time", ""))

            category = clean(row.get("category")) or DEFAULT_CATEGORY
            audience = parse_list_cell(row.get("audience", ""), default=DEFAULT_AUDIENCE)
            notes = clean(row.get("notes", ""))
            source_url = clean(row.get("source_url")) or sheet_source_url
            location_code = clean(row.get("location_code", ""))

            location_name, location_obj = resolve_location(location_code)

            dedupe_key = (
                title.lower(),
                date_iso,
                start_time or "",
                end_time or "",
                location_obj.get("code", ""),
                source_url,
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            record = build_record(
                title=title,
                date=date_iso,
                day_name=day_name_from_date_str(date_iso),
                start_time=start_time,
                end_time=end_time,
                location_name=location_name,
                location=location_obj,
                categories=[category],
                audience_type=audience,
                source=SOURCE_NAME,
                source_url=source_url,
                record_type=RECORD_TYPE,
                scraper=SCRAPER_NAME,
                notes=notes,
                access_level=ACCESS_LEVEL,
                dataset=DATASET,
                allowed_roles=ALLOWED_ROLES,
            )
            records.append(record)

        except Exception as exc:
            rejected_rows.append({
                "sheet_row": idx,
                "row": row,
                "error": str(exc),
            })

    records.sort(key=lambda r: (r["date"], r["start_time"] or "", r["title"]))
    logger.info(f"Built {len(records)} manual event records")
    if rejected_rows:
        logger.warning(f"Rejected {len(rejected_rows)} row(s) during parsing")

    return records, rejected_rows


def build_payload(records: list[dict], sheet_source_url: str) -> dict:
    return build_dataset_payload(
        dataset=DATASET,
        source=SOURCE_NAME,
        source_url=sheet_source_url,
        record_type=RECORD_TYPE,
        scraper=SCRAPER_NAME,
        access_level=ACCESS_LEVEL,
        allowed_roles=ALLOWED_ROLES,
        records=records,
    )


# =========================
# S3 OUTPUT
# =========================

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
    rows_fetched = 0
    records_uploaded = 0
    rejected_count = 0
    raw_key = None
    latest_key = None
    sheet_source_url = None

    logger.info("Starting manual events scraper")

    try:
        csv_text, sheet_source_url = fetch_csv_text()
        parsed_rows = parse_csv_rows(csv_text)
        rows_fetched = len(parsed_rows)
        logger.info(f"Fetched {rows_fetched} row(s) from manual sheet")

        records, rejected_rows = build_event_records(parsed_rows, sheet_source_url)
        rejected_count = len(rejected_rows)

        payload = build_payload(records, sheet_source_url)

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
            employees_fetched=rows_fetched,
            records_uploaded=records_uploaded,
            raw_key=raw_key,
            latest_key=latest_key,
            error=None,
        )
        run_log["rows_fetched"] = rows_fetched
        run_log["rejected_rows"] = rejected_rows

        log_key = upload_run_log_to_s3(run_log)

        logger.info(f"Upload complete: {latest_key}")
        logger.info(f"Run log written to: {log_key}")

        print(json.dumps({
            "status": "ok",
            "rows_fetched": rows_fetched,
            "records_uploaded": records_uploaded,
            "rejected_count": rejected_count,
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
                employees_fetched=rows_fetched,
                records_uploaded=records_uploaded,
                raw_key=raw_key,
                latest_key=latest_key,
                error=str(exc),
            )
            run_log["rows_fetched"] = rows_fetched
            log_key = upload_run_log_to_s3(run_log)
            logger.info(f"Error log written to: {log_key}")
        except Exception:
            logger.exception("Failed to write error log to S3")

        raise


if __name__ == "__main__":
    main()