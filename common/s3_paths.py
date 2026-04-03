from datetime import datetime, timezone


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_file_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def build_raw_key(access_level: str, dataset: str, timestamp: str | None = None) -> str:
    ts = timestamp or utc_file_ts()
    return f"{access_level}/raw/{dataset}/{ts}.json"


def build_latest_key(access_level: str, dataset: str) -> str:
    return f"{access_level}/latest/{dataset}.json"


def build_log_key(access_level: str, dataset: str, timestamp: str | None = None) -> str:
    ts = timestamp or utc_file_ts()
    return f"{access_level}/logs/{dataset}/{ts}.json"