import json
import os
from datetime import datetime, date
from typing import Any

from common.aws import get_s3_client
from common.logging_utils import get_logger
from common.s3_paths import utc_iso, build_raw_key, build_latest_key, build_log_key


# =========================
# CONFIG
# =========================

S3_BUCKET = os.getenv("S3_BUCKET", "event-scrape-data")

DATASET = "public-events-index"
ACCESS_LEVEL = "public"
ALLOWED_ROLES = ["staff", "supervisor", "manager", "admin"]
RECORD_TYPE = "public_event_index"
SCRAPER_NAME = "public-events-index-v1"
SOURCE_NAME = "Aggregator"
SOURCE_URL = None

PUBLIC_LATEST_PREFIX = "public/latest/"
INDEX_MASTER_KEY = "public/index/master.json"
INDEX_DATASETS_KEY = "public/index/datasets.json"
EXCLUDED_LATEST_FILENAMES = {
    f"{DATASET}.json",
    "public-events.json",
}

# Optional override if you want to aggregate only selected public datasets.
# Example:
#   export AGGREGATOR_INCLUDE_DATASETS=mcg-events,mcec-events,eventbrite-events
INCLUDE_DATASETS = {
    value.strip()
    for value in os.getenv("AGGREGATOR_INCLUDE_DATASETS", "").split(",")
    if value.strip()
}


# =========================
# LOGGING / AWS
# =========================

logger = get_logger(__name__)
s3 = get_s3_client(logger=logger)


# =========================
# HELPERS
# =========================


def dataset_to_label(dataset: str) -> str:
    parts = [p for p in (dataset or "").split("-") if p]
    if not parts:
        return "Unknown Dataset"
    return " ".join(part.upper() if len(part) <= 4 else part.capitalize() for part in parts)



def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None



def is_today_or_future(value: str | None) -> bool:
    parsed = parse_iso_date(value)
    return bool(parsed and parsed >= date.today())



def safe_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    return []



def dedupe_key(record: dict) -> tuple:
    return (
        (record.get("dataset") or "").strip().lower(),
        (record.get("title") or "").strip().lower(),
        (record.get("date") or "").strip(),
        (record.get("start_time") or "").strip(),
        (record.get("end_time") or "").strip(),
        (record.get("location_name") or "").strip().lower(),
        (record.get("source_url") or "").strip(),
    )



def sort_key(record: dict) -> tuple:
    return (
        record.get("date") or "",
        record.get("start_time") or "",
        record.get("title") or "",
        record.get("location_name") or "",
    )



def get_json_from_s3(key: str) -> dict:
    response = s3.get_object(Bucket=S3_BUCKET, Key=key)
    body = response["Body"].read().decode("utf-8")
    return json.loads(body)



def list_public_latest_keys() -> list[str]:
    keys: list[str] = []
    continuation_token = None

    while True:
        kwargs = {
            "Bucket": S3_BUCKET,
            "Prefix": PUBLIC_LATEST_PREFIX,
            "MaxKeys": 1000,
        }
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token

        response = s3.list_objects_v2(**kwargs)
        for item in response.get("Contents", []):
            key = item.get("Key") or ""
            if not key.endswith(".json"):
                continue

            filename = key.rsplit("/", 1)[-1]
            if filename in EXCLUDED_LATEST_FILENAMES:
                continue

            dataset_name = filename[:-5]  # strip .json
            if INCLUDE_DATASETS and dataset_name not in INCLUDE_DATASETS:
                continue

            keys.append(key)

        if not response.get("IsTruncated"):
            break
        continuation_token = response.get("NextContinuationToken")

    keys.sort()
    return keys



def normalise_record(record: dict, payload: dict, source_key: str) -> dict | None:
    if not isinstance(record, dict):
        return None

    event_date = record.get("date")
    if not is_today_or_future(event_date):
        return None

    dataset = payload.get("dataset") or "unknown-dataset"
    source = record.get("source") or payload.get("source") or SOURCE_NAME
    allowed_roles = safe_list(record.get("access", {}).get("allowed_roles")) or safe_list(payload.get("allowed_roles")) or ALLOWED_ROLES
    access_level = record.get("access", {}).get("level") or payload.get("access_level") or ACCESS_LEVEL

    return {
        "title": record.get("title"),
        "date": event_date,
        "day": record.get("day") or record.get("day_name"),
        "start_time": record.get("start_time"),
        "end_time": record.get("end_time"),
        "location_name": record.get("location_name"),
        "location": record.get("location"),
        "categories": safe_list(record.get("categories")),
        "audience_type": safe_list(record.get("audience_type")),
        "source": source,
        "source_url": record.get("source_url"),
        "type": record.get("type") or payload.get("type") or RECORD_TYPE,
        "scraper": record.get("scraper") or payload.get("scraper"),
        "notes": record.get("notes"),
        "dataset": dataset,
        "dataset_label": dataset_to_label(dataset),
        "dataset_scraped_at": payload.get("scraped_at"),
        "latest_key": source_key,
        "access": {
            "level": access_level,
            "dataset": dataset,
            "allowed_roles": allowed_roles,
        },
    }



def build_master_payload(records: list[dict], datasets: list[dict], source_keys: list[str]) -> dict:
    return {
        "dataset": DATASET,
        "source": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "type": RECORD_TYPE,
        "scraper": SCRAPER_NAME,
        "access_level": ACCESS_LEVEL,
        "allowed_roles": ALLOWED_ROLES,
        "scraped_at": utc_iso(),
        "record_count": len(records),
        "dataset_count": len(datasets),
        "datasets": datasets,
        "source_keys": source_keys,
        "records": records,
    }



def build_datasets_payload(datasets: list[dict]) -> dict:
    return {
        "dataset": f"{DATASET}-datasets",
        "source": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "type": "dataset_index",
        "scraper": SCRAPER_NAME,
        "access_level": ACCESS_LEVEL,
        "allowed_roles": ALLOWED_ROLES,
        "scraped_at": utc_iso(),
        "dataset_count": len(datasets),
        "datasets": datasets,
    }



def build_run_log(
    *,
    started_at: str,
    finished_at: str,
    status: str,
    latest_files_found: int,
    datasets_aggregated: int,
    records_uploaded: int,
    raw_key: str | None,
    latest_key: str | None,
    index_master_key: str | None,
    index_datasets_key: str | None,
    error: str | None,
) -> dict:
    return {
        "scraper": SCRAPER_NAME,
        "dataset": DATASET,
        "record_type": RECORD_TYPE,
        "source": SOURCE_NAME,
        "access_level": ACCESS_LEVEL,
        "allowed_roles": ALLOWED_ROLES,
        "s3_bucket": S3_BUCKET,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "latest_files_found": latest_files_found,
        "datasets_aggregated": datasets_aggregated,
        "records_uploaded": records_uploaded,
        "raw_key": raw_key,
        "latest_key": latest_key,
        "index_master_key": index_master_key,
        "index_datasets_key": index_datasets_key,
        "error": error,
    }


# =========================
# AGGREGATION
# =========================


def aggregate_public_datasets() -> tuple[list[dict], list[dict], list[str]]:
    latest_keys = list_public_latest_keys()
    logger.info(f"Found {len(latest_keys)} public latest dataset files")

    all_records: list[dict] = []
    dataset_summaries: list[dict] = []
    seen = set()

    for key in latest_keys:
        logger.info(f"Reading {key}")
        payload = get_json_from_s3(key)

        dataset = payload.get("dataset") or key.rsplit("/", 1)[-1].replace(".json", "")
        records = safe_list(payload.get("records"))
        access_level = payload.get("access_level") or ACCESS_LEVEL
        allowed_roles = safe_list(payload.get("allowed_roles")) or ALLOWED_ROLES

        kept_for_dataset = 0
        for record in records:
            normalised = normalise_record(record, payload, key)
            if not normalised:
                continue

            key_tuple = dedupe_key(normalised)
            if key_tuple in seen:
                continue
            seen.add(key_tuple)

            all_records.append(normalised)
            kept_for_dataset += 1

        dataset_summaries.append(
            {
                "key": dataset,
                "label": dataset_to_label(dataset),
                "source": payload.get("source") or SOURCE_NAME,
                "source_url": payload.get("source_url"),
                "access_level": access_level,
                "allowed_roles": allowed_roles,
                "scraped_at": payload.get("scraped_at"),
                "latest_key": key,
                "record_count": kept_for_dataset,
            }
        )

    all_records.sort(key=sort_key)
    dataset_summaries.sort(key=lambda d: (d.get("label") or d.get("key") or ""))

    return all_records, dataset_summaries, latest_keys


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



def upload_payloads(master_payload: dict, datasets_payload: dict) -> dict:
    raw_key = build_raw_key(ACCESS_LEVEL, DATASET)
    latest_key = build_latest_key(ACCESS_LEVEL, DATASET)
    log_key = build_log_key(ACCESS_LEVEL, DATASET)

    upload_json_to_s3(raw_key, master_payload)
    upload_json_to_s3(latest_key, master_payload)
    upload_json_to_s3(INDEX_MASTER_KEY, master_payload)
    upload_json_to_s3(INDEX_DATASETS_KEY, datasets_payload)

    return {
        "raw_key": raw_key,
        "latest_key": latest_key,
        "index_master_key": INDEX_MASTER_KEY,
        "index_datasets_key": INDEX_DATASETS_KEY,
        "log_key": log_key,
    }


# =========================
# MAIN
# =========================


def main() -> None:
    started_at = utc_iso()
    latest_files_found = 0
    datasets_aggregated = 0
    records_uploaded = 0
    raw_key = None
    latest_key = None
    index_master_key = None
    index_datasets_key = None

    logger.info("Starting public events index aggregator")

    try:
        records, datasets, latest_keys = aggregate_public_datasets()
        latest_files_found = len(latest_keys)
        datasets_aggregated = len(datasets)
        records_uploaded = len(records)

        master_payload = build_master_payload(records, datasets, latest_keys)
        datasets_payload = build_datasets_payload(datasets)

        result = upload_payloads(master_payload, datasets_payload)
        raw_key = result["raw_key"]
        latest_key = result["latest_key"]
        index_master_key = result["index_master_key"]
        index_datasets_key = result["index_datasets_key"]

        finished_at = utc_iso()
        run_log = build_run_log(
            started_at=started_at,
            finished_at=finished_at,
            status="ok",
            latest_files_found=latest_files_found,
            datasets_aggregated=datasets_aggregated,
            records_uploaded=records_uploaded,
            raw_key=raw_key,
            latest_key=latest_key,
            index_master_key=index_master_key,
            index_datasets_key=index_datasets_key,
            error=None,
        )
        upload_json_to_s3(result["log_key"], run_log)

        logger.info(f"Upload complete: {latest_key}")
        logger.info(f"UI master written to: {index_master_key}")
        logger.info(f"Dataset metadata written to: {index_datasets_key}")

        print(json.dumps({
            "status": "ok",
            "latest_files_found": latest_files_found,
            "datasets_aggregated": datasets_aggregated,
            "records_uploaded": records_uploaded,
            "bucket": S3_BUCKET,
            "raw_key": raw_key,
            "latest_key": latest_key,
            "index_master_key": index_master_key,
            "index_datasets_key": index_datasets_key,
            "log_key": result["log_key"],
        }, indent=2))

    except Exception as exc:
        finished_at = utc_iso()
        logger.exception("Aggregator failed")

        try:
            log_key = build_log_key(ACCESS_LEVEL, DATASET)
            run_log = build_run_log(
                started_at=started_at,
                finished_at=finished_at,
                status="error",
                latest_files_found=latest_files_found,
                datasets_aggregated=datasets_aggregated,
                records_uploaded=records_uploaded,
                raw_key=raw_key,
                latest_key=latest_key,
                index_master_key=index_master_key,
                index_datasets_key=index_datasets_key,
                error=str(exc),
            )
            upload_json_to_s3(log_key, run_log)
        except Exception:
            logger.exception("Failed to write aggregator error log")

        raise


if __name__ == "__main__":
    main()
