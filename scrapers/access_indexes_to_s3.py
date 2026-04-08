import json
import os
from datetime import date, datetime
from typing import Any
import boto3
import time

from common.aws import get_s3_client
from common.logging_utils import get_logger
from common.s3_paths import utc_iso, build_latest_key, build_log_key, build_raw_key


# =========================
# CONFIG
# =========================

S3_BUCKET = os.getenv("S3_BUCKET", "event-scrape-data")

DATASET = "access-indexes"
SCRAPER_NAME = "access-indexes-v1"
SOURCE_NAME = "Aggregator"
SOURCE_URL = None
CLOUDFRONT_DISTRIBUTION_ID = os.getenv("CLOUDFRONT_DISTRIBUTION_ID", "E6R69V6ZSXCW1").strip()

TARGET_ACCESS_LEVELS = ["public", "restricted"]
EXCLUDED_LATEST_FILENAMES = {
    "public-events-index.json",
    "restricted-events-index.json",
    "access-indexes.json",
    "master.json",
}

# Optional overrides:
# export AGGREGATOR_INCLUDE_PUBLIC_DATASETS=mcg-events,mcec-events,eventbrite-events
# export AGGREGATOR_INCLUDE_RESTRICTED_DATASETS=staff-time-off
INCLUDE_BY_ACCESS = {
    "public": {
        value.strip()
        for value in os.getenv("AGGREGATOR_INCLUDE_PUBLIC_DATASETS", "").split(",")
        if value.strip()
    },
    "restricted": {
        value.strip()
        for value in os.getenv("AGGREGATOR_INCLUDE_RESTRICTED_DATASETS", "").split(",")
        if value.strip()
    },
}

# Future-only applies to event-like data. For restricted data such as time-off,
# leave this false so the restricted master includes all records unless the dataset
# itself already filters them.
FUTURE_ONLY_BY_ACCESS = {
    "public": True,
    "restricted": False,
}

DEFAULT_ALLOWED_ROLES_BY_ACCESS = {
    "public": ["staff", "supervisor", "manager", "admin"],
    "restricted": ["manager", "admin"],
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
    return value if isinstance(value, list) else []



def sort_key(record: dict) -> tuple:
    return (
        record.get("date") or "",
        record.get("start_time") or "",
        record.get("title") or "",
        record.get("location_name") or "",
    )



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



def get_json_from_s3(key: str) -> dict:
    response = s3.get_object(Bucket=S3_BUCKET, Key=key)
    body = response["Body"].read().decode("utf-8")
    return json.loads(body)



def list_latest_keys_for_access(access_level: str) -> list[str]:
    prefix = f"{access_level}/latest/"
    include = INCLUDE_BY_ACCESS.get(access_level, set())
    keys: list[str] = []
    continuation_token = None

    while True:
        kwargs = {
            "Bucket": S3_BUCKET,
            "Prefix": prefix,
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

            dataset_name = filename[:-5]
            if include and dataset_name not in include:
                continue

            keys.append(key)

        if not response.get("IsTruncated"):
            break
        continuation_token = response.get("NextContinuationToken")

    keys.sort()
    return keys



def record_passes_access_filter(record: dict, payload: dict, access_level: str) -> bool:
    record_access = record.get("access", {}) if isinstance(record.get("access"), dict) else {}
    resolved_level = record_access.get("level") or payload.get("access_level") or access_level
    return resolved_level == access_level



def record_passes_time_filter(record: dict, access_level: str) -> bool:
    if not FUTURE_ONLY_BY_ACCESS.get(access_level, False):
        return True

    # Keep records with no date only if you later decide to surface undated items.
    # For now public UI is future-dated only.
    return is_today_or_future(record.get("date"))



def normalise_record(record: dict, payload: dict, source_key: str, access_level: str) -> dict | None:
    if not isinstance(record, dict):
        return None

    if not record_passes_access_filter(record, payload, access_level):
        return None

    if not record_passes_time_filter(record, access_level):
        return None

    dataset = payload.get("dataset") or "unknown-dataset"
    source = record.get("source") or payload.get("source") or SOURCE_NAME
    record_access = record.get("access", {}) if isinstance(record.get("access"), dict) else {}
    allowed_roles = safe_list(record_access.get("allowed_roles")) or safe_list(payload.get("allowed_roles")) or DEFAULT_ALLOWED_ROLES_BY_ACCESS.get(access_level, [])
    resolved_access_level = record_access.get("level") or payload.get("access_level") or access_level

    return {
        "title": record.get("title"),
        "date": record.get("date"),
        "day": record.get("day") or record.get("day_name"),
        "start_time": record.get("start_time"),
        "end_time": record.get("end_time"),
        "location_name": record.get("location_name"),
        "location": record.get("location"),
        "categories": safe_list(record.get("categories")),
        "audience_type": safe_list(record.get("audience_type")),
        "filter": record.get("filter"),
        "source": source,
        "source_url": record.get("source_url"),
        "type": record.get("type") or payload.get("type"),
        "scraper": record.get("scraper") or payload.get("scraper"),
        "notes": record.get("notes"),
        "dataset": dataset,
        "dataset_label": dataset_to_label(dataset),
        "dataset_scraped_at": payload.get("scraped_at"),
        "latest_key": source_key,
        "access": {
            "level": resolved_access_level,
            "dataset": dataset,
            "allowed_roles": allowed_roles,
        },
    }



def build_master_payload(access_level: str, records: list[dict], datasets: list[dict], source_keys: list[str]) -> dict:
    return {
        "dataset": f"{access_level}-events-index",
        "source": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "type": "aggregated_index",
        "scraper": SCRAPER_NAME,
        "access_level": access_level,
        "allowed_roles": DEFAULT_ALLOWED_ROLES_BY_ACCESS.get(access_level, []),
        "scraped_at": utc_iso(),
        "record_count": len(records),
        "dataset_count": len(datasets),
        "datasets": datasets,
        "source_keys": source_keys,
        "records": records,
    }



def build_datasets_payload(access_level: str, datasets: list[dict]) -> dict:
    return {
        "dataset": f"{access_level}-datasets-index",
        "source": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "type": "dataset_index",
        "scraper": SCRAPER_NAME,
        "access_level": access_level,
        "allowed_roles": DEFAULT_ALLOWED_ROLES_BY_ACCESS.get(access_level, []),
        "scraped_at": utc_iso(),
        "dataset_count": len(datasets),
        "datasets": datasets,
    }



def build_run_log(*, started_at: str, finished_at: str, status: str, access_summaries: list[dict], error: str | None) -> dict:
    return {
        "scraper": SCRAPER_NAME,
        "dataset": DATASET,
        "source": SOURCE_NAME,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "s3_bucket": S3_BUCKET,
        "access_summaries": access_summaries,
        "error": error,
    }
    
def invalidate_cloudfront(paths: list[str] | None = None) -> dict | None:
    if not CLOUDFRONT_DISTRIBUTION_ID:
        logger.warning("No CLOUDFRONT_DISTRIBUTION_ID set — skipping CloudFront invalidation")
        return None

    if not paths:
        paths = [
            "/public/index/*",
            "/restricted/index/*",
        ]

    logger.info(
        "Creating CloudFront invalidation | distribution_id=%s paths=%s",
        CLOUDFRONT_DISTRIBUTION_ID,
        paths,
    )

    cf = boto3.client("cloudfront")

    response = cf.create_invalidation(
        DistributionId=CLOUDFRONT_DISTRIBUTION_ID,
        InvalidationBatch={
            "Paths": {
                "Quantity": len(paths),
                "Items": paths,
            },
            "CallerReference": str(time.time()),
        },
    )

    invalidation = response.get("Invalidation", {})
    logger.info(
        "CloudFront invalidation created | id=%s status=%s",
        invalidation.get("Id"),
        invalidation.get("Status"),
    )
    return invalidation


# =========================
# AGGREGATION
# =========================


def aggregate_access_level(access_level: str) -> tuple[list[dict], list[dict], list[str]]:
    latest_keys = list_latest_keys_for_access(access_level)
    logger.info("Found %s latest dataset files for %s", len(latest_keys), access_level)

    all_records: list[dict] = []
    dataset_summaries: list[dict] = []
    seen = set()

    for key in latest_keys:
        logger.info("Reading %s", key)
        payload = get_json_from_s3(key)
        dataset = payload.get("dataset") or key.rsplit("/", 1)[-1].replace(".json", "")
        records = safe_list(payload.get("records"))
        allowed_roles = safe_list(payload.get("allowed_roles")) or DEFAULT_ALLOWED_ROLES_BY_ACCESS.get(access_level, [])

        kept_for_dataset = 0
        for record in records:
            normalised = normalise_record(record, payload, key, access_level)
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



def write_access_outputs(access_level: str, master_payload: dict, datasets_payload: dict) -> dict:
    dataset_name = f"{access_level}-events-index"
    raw_key = build_raw_key(access_level, dataset_name)
    latest_key = build_latest_key(access_level, dataset_name)
    master_key = f"{access_level}/index/master.json"
    datasets_key = f"{access_level}/index/datasets.json"

    upload_json_to_s3(raw_key, master_payload)
    upload_json_to_s3(latest_key, master_payload)
    upload_json_to_s3(master_key, master_payload)
    upload_json_to_s3(datasets_key, datasets_payload)

    return {
        "raw_key": raw_key,
        "latest_key": latest_key,
        "master_key": master_key,
        "datasets_key": datasets_key,
    }


# =========================
# MAIN
# =========================


def main() -> None:
    started_at = utc_iso()
    summaries: list[dict] = []

    logger.info("Starting access index aggregator")

    try:
        for access_level in TARGET_ACCESS_LEVELS:
            records, datasets, latest_keys = aggregate_access_level(access_level)
            master_payload = build_master_payload(access_level, records, datasets, latest_keys)
            datasets_payload = build_datasets_payload(access_level, datasets)
            written = write_access_outputs(access_level, master_payload, datasets_payload)

            summaries.append(
                {
                    "access_level": access_level,
                    "latest_files_found": len(latest_keys),
                    "datasets_aggregated": len(datasets),
                    "records_uploaded": len(records),
                    **written,
                }
            )

            logger.info(
                "%s complete | latest_files=%s datasets=%s records=%s",
                access_level,
                len(latest_keys),
                len(datasets),
                len(records),
            )

        finished_at = utc_iso()
        log_key = build_log_key("internal", DATASET)
        run_log = build_run_log(
            started_at=started_at,
            finished_at=finished_at,
            status="ok",
            access_summaries=summaries,
            error=None,
        )
        upload_json_to_s3(log_key, run_log)

        invalidation = invalidate_cloudfront()

        print(json.dumps({
            "status": "ok",
            "bucket": S3_BUCKET,
            "access_summaries": summaries,
            "log_key": log_key,
            "cloudfront_distribution_id": CLOUDFRONT_DISTRIBUTION_ID,
            "cloudfront_invalidation": invalidation,
        }, indent=2, default=str))

    except Exception as exc:
        finished_at = utc_iso()
        logger.exception("Access index aggregator failed")

        try:
            log_key = build_log_key("internal", DATASET)
            run_log = build_run_log(
                started_at=started_at,
                finished_at=finished_at,
                status="error",
                access_summaries=summaries,
                error=str(exc),
            )
            upload_json_to_s3(log_key, run_log)
        except Exception:
            logger.exception("Failed to write access aggregator error log")

        raise


if __name__ == "__main__":
    main()
