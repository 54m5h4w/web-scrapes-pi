import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from common.aws import get_s3_client
from common.logging_utils import get_logger
from common.s3_paths import utc_iso, build_latest_key, build_log_key, build_raw_key
from common.schema import build_dataset_payload, build_record, build_run_log


# =========================
# CONFIG
# =========================

API_URL = "https://ignition.ticketek.com.au/fanxsearch/api/search"
S3_BUCKET = os.getenv("S3_BUCKET", "event-scrape-data")
TICKETEK_API_KEY = os.getenv("TICKETEK_API_KEY", "VK5eOlJ1ef6bo4NqwYrDjawoNa3jtrNb1wZuYsb1")
TICKETEK_VISITOR_ID = os.getenv("TICKETEK_VISITOR_ID", "158067605.1769904857")

DATASET = "ticketek-plenary-events"
ACCESS_LEVEL = "public"
ALLOWED_ROLES = ["staff", "supervisor", "manager", "admin"]
RECORD_TYPE = "public_event"
SCRAPER_NAME = "ticketek-plenary-events-v1"
SOURCE_NAME = "Ticketek"
SOURCE_URL = "https://premier.ticketek.com.au/"

SEARCH_TERM = os.getenv("TICKETEK_SEARCH_TERM", "Plenary")
DEFAULT_LOCATION_NAME = "Plenary Theatre - South Wharf"
LOCATION_OBJECT = {
    "code": "PLENARY",
    "search_text": "Plenary Theatre South Wharf Melbourne VIC Australia",
    "latitude": None,
    "longitude": None,
}
DEFAULT_AUDIENCE = ["Public"]
DEFAULT_CATEGORY = "Ticketed Event"
REQUEST_TIMEOUT = int(os.getenv("TICKETEK_TIMEOUT_SECONDS", "60"))

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-AU,en-NZ;q=0.9,en-GB;q=0.8,en-US;q=0.7,en;q=0.6",
    "cache-control": "no-cache",
    "content-type": "application/json",
    "origin": "https://premier.ticketek.com.au",
    "pragma": "no-cache",
    "priority": "u=1, i",
    "referer": "https://premier.ticketek.com.au/",
    "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "x-api-key": TICKETEK_API_KEY,
}

BASE_PAYLOAD = {
    "filter": {},
    "searchTerm": SEARCH_TERM,
    "paging": {"pageSize": 20},
    "visitorId": TICKETEK_VISITOR_ID,
}


# =========================
# LOGGING / AWS
# =========================

logger = get_logger(__name__)
s3 = get_s3_client(logger=logger)


# =========================
# MODELS
# =========================

@dataclass
class TicketekEvent:
    id: str
    title: str
    subtitle: str
    date_time_localized: str
    venue_name: str
    venue_code: str
    state: str
    url: str
    show_code: str
    show_event_count: int


# =========================
# HELPERS
# =========================


def day_name_from_date_str(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")



def sort_key(ev: TicketekEvent) -> tuple[str, str, str]:
    return (ev.date_time_localized or "", ev.title or "", ev.id or "")



def iso_to_date_time(iso_str: str) -> tuple[str | None, str | None]:
    if not iso_str:
        return None, None
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.date().isoformat(), dt.strftime("%H:%M")
    except Exception:
        return None, None



def build_notes(ev: TicketekEvent) -> str:
    parts = [
        f"subtitle={ev.subtitle}" if ev.subtitle else "",
        f"venue={ev.venue_name}" if ev.venue_name else "",
        f"venueCode={ev.venue_code}" if ev.venue_code else "",
        f"showCode={ev.show_code}" if ev.show_code else "",
        f"performanceId={ev.id}" if ev.id else "",
    ]
    return " | ".join(part for part in parts if part)


# =========================
# API
# =========================


def payload_for_search_term(search_term: str, page_size: int = 100) -> dict[str, Any]:
    payload = dict(BASE_PAYLOAD)
    payload["searchTerm"] = search_term
    payload["paging"] = {"pageSize": page_size}
    return payload



def post_search(payload: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(
        API_URL,
        headers=HEADERS,
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )

    text = response.text
    response.raise_for_status()

    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Ticketek response was not valid JSON. First 400 chars: {text[:400]}") from exc

    if isinstance(data, dict) and data.get("message") == "Forbidden":
        raise RuntimeError(
            "Ticketek returned Forbidden. Refresh TICKETEK_VISITOR_ID and confirm headers are still valid."
        )

    if not isinstance(data, dict):
        raise RuntimeError(f"Expected Ticketek JSON object, got {type(data)}")

    return data



def extract_events(data: dict[str, Any]) -> list[TicketekEvent]:
    events = data.get("events", [])
    if not isinstance(events, list):
        return []

    parsed: list[TicketekEvent] = []
    for item in events:
        if not isinstance(item, dict):
            continue

        venue = item.get("venue") or {}
        link = item.get("link") or {}
        show = item.get("show") or {}

        parsed.append(
            TicketekEvent(
                id=str(item.get("id", "")).strip(),
                title=str(item.get("title", "")).strip(),
                subtitle=str(item.get("subtitle", "")).strip(),
                date_time_localized=str(item.get("dateTimeLocalized", "")).strip(),
                venue_name=str(venue.get("name", "")).strip(),
                venue_code=str(venue.get("venueCode", "")).strip(),
                state=str(venue.get("state", "")).strip(),
                url=str((link.get("uri") if isinstance(link, dict) else "") or "").strip(),
                show_code=str((show.get("showCode") if isinstance(show, dict) else "") or "").strip(),
                show_event_count=int((show.get("eventCount") if isinstance(show, dict) else 0) or 0),
            )
        )

    return [event for event in parsed if event.title and event.url]



def fetch_primary_events(query: str = SEARCH_TERM) -> list[TicketekEvent]:
    data = post_search(payload_for_search_term(query, page_size=20))
    events = extract_events(data)
    events.sort(key=sort_key)
    return events



def fetch_all_sessions_for_show(show_code: str, venue_code: str) -> list[TicketekEvent]:
    data = post_search(payload_for_search_term(show_code, page_size=200))
    events = extract_events(data)

    top_results = data.get("topResults")
    if isinstance(top_results, dict):
        extra = top_results.get("events")
        if isinstance(extra, list):
            events.extend(extract_events({"events": extra}))

    needle = f"/events/{show_code}/venues/{venue_code}/performances/"
    filtered = [event for event in events if needle in (event.url or "")]
    filtered.sort(key=sort_key)
    return filtered


# =========================
# TRANSFORM
# =========================


def build_ticketek_records(events: list[TicketekEvent]) -> list[dict]:
    records: list[dict] = []
    seen = set()

    for event in events:
        event_date, start_time = iso_to_date_time(event.date_time_localized)
        if not event_date or not start_time:
            logger.info(f"Skipping record with invalid date/time: title={event.title!r} raw={event.date_time_localized!r}")
            continue

        dedupe_key = (
            event.title.lower().strip(),
            event_date,
            start_time,
            DEFAULT_LOCATION_NAME.lower().strip(),
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        record = build_record(
            title=event.title,
            date=event_date,
            day_name=day_name_from_date_str(event_date),
            start_time=start_time,
            end_time=None,
            location_name=DEFAULT_LOCATION_NAME,
            location=LOCATION_OBJECT,
            categories=[DEFAULT_CATEGORY],
            audience_type=DEFAULT_AUDIENCE,
            source=SOURCE_NAME,
            source_url=event.url,
            record_type=RECORD_TYPE,
            scraper=SCRAPER_NAME,
            notes=build_notes(event),
            access_level=ACCESS_LEVEL,
            dataset=DATASET,
            allowed_roles=ALLOWED_ROLES,
        )
        records.append(record)

    records.sort(key=lambda r: (r["date"], r["start_time"] or "", r["title"]))
    logger.info(f"Built {len(records)} Ticketek records")
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
    primary_events_found = 0
    performances_found = 0
    records_uploaded = 0
    raw_key = None
    latest_key = None

    logger.info("Starting Ticketek Plenary scraper")

    try:
        primary = fetch_primary_events(SEARCH_TERM)
        primary_events_found = len(primary)
        logger.info(f"Fetched {primary_events_found} primary Ticketek events")

        all_performances: list[TicketekEvent] = []
        expanded_cache: dict[tuple[str, str], list[TicketekEvent]] = {}

        for event in primary:
            if event.show_event_count > 1 and event.show_code and event.venue_code:
                cache_key = (event.show_code, event.venue_code)
                if cache_key not in expanded_cache:
                    expanded_cache[cache_key] = fetch_all_sessions_for_show(event.show_code, event.venue_code)
                    logger.info(
                        "Expanded show_code=%s venue_code=%s sessions_found=%s",
                        event.show_code,
                        event.venue_code,
                        len(expanded_cache[cache_key]),
                    )

                sessions = expanded_cache[cache_key]
                if sessions:
                    all_performances.extend(sessions)
                else:
                    logger.info(f"Expansion empty, keeping original performance for {event.title!r}")
                    all_performances.append(event)
            else:
                all_performances.append(event)

        all_performances.sort(key=sort_key)
        performances_found = len(all_performances)
        logger.info(f"Total Ticketek performances before record mapping: {performances_found}")

        records = build_ticketek_records(all_performances)
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
            employees_fetched=primary_events_found,
            records_uploaded=records_uploaded,
            raw_key=raw_key,
            latest_key=latest_key,
            error=None,
        )
        run_log["primary_events_found"] = primary_events_found
        run_log["performances_found"] = performances_found

        log_key = upload_run_log_to_s3(run_log)

        logger.info(f"Upload complete: {latest_key}")
        logger.info(f"Run log written to: {log_key}")

        print(json.dumps({
            "status": "ok",
            "primary_events_found": primary_events_found,
            "performances_found": performances_found,
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
                employees_fetched=primary_events_found,
                records_uploaded=records_uploaded,
                raw_key=raw_key,
                latest_key=latest_key,
                error=str(exc),
            )
            run_log["primary_events_found"] = primary_events_found
            run_log["performances_found"] = performances_found
            log_key = upload_run_log_to_s3(run_log)
            logger.info(f"Error log written to: {log_key}")
        except Exception:
            logger.exception("Failed to write error log to S3")

        raise


if __name__ == "__main__":
    main()
