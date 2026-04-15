import csv
import io
import json
import os
import re
from datetime import datetime

import requests

from common.aws import get_s3_client
from common.logging_utils import get_logger
from common.s3_paths import utc_iso, build_raw_key, build_latest_key, build_log_key
from common.schema import build_record, build_dataset_payload, build_run_log


# =========================
# CONFIG
# =========================

S3_BUCKET = os.getenv("S3_BUCKET", "event-scrape-data")

PIPELINE_CSV_URL = os.getenv(
    "PIPELINE_CSV_URL",
    "https://docs.google.com/spreadsheets/d/1tW7xtBvAmEdXBNMLt39dSHRAEs3Goc4Wz4VFeKcFt2k/export?format=csv&gid=825762720",
)

DATASET = "pipeline-internal-events"
ACCESS_LEVEL = "internal"
ALLOWED_ROLES = ["supervisor", "manager", "admin"]
RECORD_TYPE = "internal_event"
SCRAPER_NAME = "pipeline-internal-events-v1"
SOURCE_NAME = "E3.0"
SOURCE_URL = None

HTTP_TIMEOUT_SECONDS = int(os.getenv("PIPELINE_HTTP_TIMEOUT_SECONDS", "60"))
CONFIRMED_STATUS = "CONFIRMED"

VENUE_NAME_MAP = {
    "BP": "BangPop",
    "HATF": "Henry and the Fox",
    "P5": "Plus 5",
    "BBB": "Billie's Bites & Bar",
    "CM": "Common Man",
    "OTH": "Other",
}

VENUE_CODE_MAP = {
    "BP": "BP",
    "HATF": "HATF",
    "P5": "P5",
    "BBB": "BBB",
    "CM": "CM",
    "OTH": "OTH",
}


# =========================
# LOGGING / AWS
# =========================

logger = get_logger(__name__)
s3 = get_s3_client(logger=logger)


# =========================
# HELPERS
# =========================

def clean(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalise_header(header: str) -> str:
    return re.sub(r"\s+", " ", clean(header))


def day_name_from_date_str(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")


def parse_date_to_iso(value: str) -> str:
    value = clean(value)
    if not value:
        raise ValueError("Missing Event Date")

    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Unsupported Event Date format: {value}") from exc


def parse_time_range(value: str) -> tuple[str | None, str | None]:
    value = clean(value)
    if not value:
        return None, None

    value = value.replace("–", " to ").replace("—", " to ")
    parts = re.split(r"\s+to\s+|\s*-\s*", value, maxsplit=1, flags=re.I)

    if len(parts) == 1:
        start = parse_time_to_hhmm(parts[0])
        return start, None

    start = parse_time_to_hhmm(parts[0])
    end = parse_time_to_hhmm(parts[1])
    return start, end


def parse_time_to_hhmm(value: str) -> str | None:
    value = clean(value)
    if not value:
        return None

    for fmt in ("%I:%M%p", "%I%p", "%H:%M", "%H:%M:%S", "%I:%M %p"):
        try:
            return datetime.strptime(value.replace(" ", ""), fmt).strftime("%H:%M")
        except ValueError:
            continue

    try:
        return datetime.strptime(value, "%I:%M %p").strftime("%H:%M")
    except ValueError as exc:
        raise ValueError(f"Unsupported TIME format: {value}") from exc


def parse_pax(value: str) -> str:
    raw = clean(value)
    if not raw:
        return ""

    raw = raw.replace(",", "")
    try:
        pax_num = int(float(raw))
        return str(pax_num)
    except ValueError:
        return raw


def clean_source_url(value: str) -> str | None:
    value = clean(value)
    if not value or value.lower() == "view event":
        return None
    return value


def resolve_venue_code(raw_venue: str) -> str:
    venue = clean(raw_venue).upper()
    if not venue:
        return "UNKNOWN"

    if venue in VENUE_CODE_MAP:
        return VENUE_CODE_MAP[venue]

    tokens = re.findall(r"[A-Z0-9]+", venue)
    for token in tokens:
        if token in VENUE_CODE_MAP:
            return VENUE_CODE_MAP[token]

    return "UNKNOWN"


def resolve_location_name(raw_venue: str, raw_space: str) -> str:
    venue_code = resolve_venue_code(raw_venue)
    venue_name = VENUE_NAME_MAP.get(venue_code, clean(raw_venue) or "Unknown Venue")
    space = clean(raw_space)

    if space:
        return f"{venue_name} - {space}"
    return venue_name


def build_location_object(raw_venue: str, raw_space: str) -> dict:
    venue_code = resolve_venue_code(raw_venue)
    venue_name = VENUE_NAME_MAP.get(venue_code, clean(raw_venue) or "Unknown Venue")
    space = clean(raw_space)

    search_parts = [venue_name, space, "Melbourne", "Victoria", "Australia"]
    search_text = " ".join(part for part in search_parts if part)

    return {
        "code": venue_code,
        "search_text": search_text,
        "latitude": None,
        "longitude": None,
    }


def build_title(name: str, pax: str) -> str:
    name = clean(name)
    pax = clean(pax)

    if pax:
        return f"{name} {pax}PAX"
    return name


def build_filter(raw_venue: str) -> str:
    venue_code = resolve_venue_code(raw_venue)
    return f"{venue_code} Internal Event"


def build_notes(row: dict) -> str:
    parts = [
        clean(row.get("REF")),
        clean(row.get("STYLE")),
        clean(row.get("FOOD")),
        clean(row.get("BEVERAGE")),
        clean(row.get("SPEND")),
    ]
    return " | ".join(part for part in parts if part and part != "-")


def is_confirmed_status(value: str) -> bool:
    return clean(value).upper() == CONFIRMED_STATUS


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

def fetch_csv_text() -> str:
    logger.info("Fetching pipeline CSV: %s", PIPELINE_CSV_URL)

    response = requests.get(PIPELINE_CSV_URL, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()

    return response.content.decode("utf-8-sig")


def parse_csv_rows(csv_text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        raise RuntimeError("CSV has no header row")

    reader.fieldnames = [normalise_header(h) for h in reader.fieldnames]

    required = {"Event Date", "NAME", "VENUE", "PAX", "TIME", "STATUS"}
    missing = sorted(required - set(reader.fieldnames))
    if missing:
        raise RuntimeError(f"CSV missing required columns: {', '.join(missing)}")

    rows = []
    for raw_row in reader:
        row = {normalise_header(k): clean(v) for k, v in raw_row.items() if k is not None}
        if not any(row.values()):
            continue
        rows.append(row)

    return rows


# =========================
# RECORD BUILDING
# =========================

def build_event_records(parsed_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    records = []
    rejected_rows = []
    seen = set()

    for idx, row in enumerate(parsed_rows, start=2):
        try:
            if not is_confirmed_status(row.get("STATUS")):
                continue

            title_name = clean(row.get("NAME"))
            if not title_name:
                raise ValueError("Missing NAME")

            pax = parse_pax(row.get("PAX"))
            title = build_title(title_name, pax)

            event_date = parse_date_to_iso(row.get("Event Date"))
            start_time, end_time = parse_time_range(row.get("TIME"))

            location_name = resolve_location_name(row.get("VENUE"), row.get("SPACE"))
            location = build_location_object(row.get("VENUE"), row.get("SPACE"))
            source_url = clean_source_url(row.get("E3.0 LINK / EB LINK"))

            dedupe_key = (
                title.lower(),
                event_date,
                start_time or "",
                end_time or "",
                location.get("code") or "",
                source_url or "",
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            venue_code = resolve_venue_code(row.get("VENUE"))

            record = build_record(
                title=title,
                date=event_date,
                day_name=day_name_from_date_str(event_date),
                start_time=start_time,
                end_time=end_time,
                location_name=location_name,
                location=location,
                categories=["Internal Event"],
                audience_type=["Internal", "Functions"],
                filter=build_filter(row.get("VENUE")),
                source=SOURCE_NAME,
                source_url=source_url,
                record_type=RECORD_TYPE,
                scraper=SCRAPER_NAME,
                notes=build_notes(row),
                access_level=ACCESS_LEVEL,
                dataset=DATASET,
                allowed_roles=ALLOWED_ROLES,
                allowed_venues=[venue_code],
            )

            record["venue"] = venue_code
            records.append(record)

        except Exception as exc:
            rejected_rows.append(
                {
                    "sheet_row": idx,
                    "row": row,
                    "error": str(exc),
                }
            )

    records.sort(key=lambda r: (r["date"], r["start_time"] or "", r["title"]))
    logger.info("Built %s confirmed internal event records", len(records))

    if rejected_rows:
        logger.warning("Rejected %s row(s) during parsing", len(rejected_rows))

    return records, rejected_rows


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
    raw_key = None
    latest_key = None

    logger.info("Starting pipeline internal events scraper")

    try:
        csv_text = fetch_csv_text()
        parsed_rows = parse_csv_rows(csv_text)
        rows_fetched = len(parsed_rows)

        records, rejected_rows = build_event_records(parsed_rows)
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
            employees_fetched=rows_fetched,
            records_uploaded=records_uploaded,
            raw_key=raw_key,
            latest_key=latest_key,
            error=None if not rejected_rows else f"Rejected rows: {len(rejected_rows)}",
        )
        upload_run_log_to_s3(run_log)

        logger.info(
            "Pipeline internal events scraper complete | rows_fetched=%s | records_uploaded=%s",
            rows_fetched,
            records_uploaded,
        )

    except Exception as exc:
        finished_at = utc_iso()
        logger.exception("Pipeline internal events scraper failed")

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
        upload_run_log_to_s3(run_log)
        raise


if __name__ == "__main__":
    main()