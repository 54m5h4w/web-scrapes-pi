import json
import math
import os
import re
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3
import pdfplumber
import pandas as pd
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext


# =========================
# CONFIG
# =========================

S3_BUCKET = os.getenv("S3_BUCKET", "event-scrape-data")
AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-2")
AWS_PROFILE = os.getenv("AWS_PROFILE", "").strip()

DATASET = "mcec-swmg-events"
ACCESS_LEVEL = "public"
ALLOWED_ROLES = ["staff", "supervisor", "manager", "admin"]
RECORD_TYPE = "public_event"
SCRAPER_NAME = "mcec-swmg-pdf-v1"
SOURCE_NAME = "SW Members Group PDF"
SOURCE_URL = None
FILTER_LABEL = "MCEC"

LOCATION_SEARCH_TEXT = "Melbourne Convention and Exhibition Centre South Wharf Melbourne VIC Australia"

SITE_HEADINGS = {"MCEC Expansion", "Exhibition Centre", "Convention Centre"}
DATE_RE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})$",
    re.IGNORECASE,
)
CATERING_CONFIRMED_TERMS = [
    "SERVED",
    "FUNCTION",
    "CARTS",
    "PLATTERS",
    "CLASSIC BEVERAGE",
    "PACKAGE BEVERAGE",
    "DESSERT",
    "DINNER",
    "ENTREE",
]
EVENT_HDR_RE = re.compile(r"^(.*)\s+(\d[\d,]*)\s+PAX\s+(\d[\d,]*)\s+PAX$", re.IGNORECASE)
TIME_RE = re.compile(r"(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})")


# =========================
# AWS HELPERS
# =========================

def get_boto3_session():
    if AWS_PROFILE:
        return boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    return boto3.Session(region_name=AWS_REGION)


def get_s3_client():
    session = get_boto3_session()
    return session.client("s3")


def get_caller_arn() -> str:
    try:
        session = get_boto3_session()
        sts = session.client("sts")
        return sts.get_caller_identity().get("Arn", "unknown")
    except Exception:
        return "unknown"


# =========================
# GENERIC HELPERS
# =========================

def utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def utc_stamp() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")


def build_raw_key(access_level: str, dataset: str) -> str:
    return f"{access_level}/raw/{dataset}/{utc_stamp()}.json"


def build_latest_key(access_level: str, dataset: str) -> str:
    return f"{access_level}/latest/{dataset}.json"


def build_log_key(access_level: str, dataset: str) -> str:
    return f"{access_level}/logs/{dataset}/{utc_stamp()}.json"


def day_name_from_date_str(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")


def clean_str(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, float) and math.isnan(x):
        return ""
    try:
        if pd.isna(x):
            return ""
    except Exception:
        pass
    return str(x)


def upload_json_to_s3(key: str, payload: dict, s3_client) -> None:
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def build_location_object(location_name: str) -> dict:
    code = "MCEC"
    search_text = f"{location_name} Melbourne Convention and Exhibition Centre South Wharf Melbourne VIC Australia".strip()
    return {
        "code": code,
        "search_text": re.sub(r"\s+", " ", search_text),
        "latitude": None,
        "longitude": None,
    }


def build_record(
    *,
    title: str,
    date: str,
    start_time: str | None,
    end_time: str | None,
    location_name: str,
    categories: list[str],
    audience_type: list[str],
    filter: str,
    source: str,
    source_url: str | None,
    record_type: str,
    scraper: str,
    notes: str,
    access_level: str,
    dataset: str,
    allowed_roles: list[str],
) -> dict:
    return {
        "title": title,
        "date": date,
        "day_name": day_name_from_date_str(date),
        "start_time": start_time or None,
        "end_time": end_time or None,
        "location_name": location_name,
        "location": build_location_object(location_name),
        "categories": categories,
        "audience_type": audience_type,
        "filter": filter,
        "source": source,
        "source_url": source_url,
        "type": record_type,
        "scraper": scraper,
        "notes": notes,
        "access": {
            "level": access_level,
            "dataset": dataset,
            "allowed_roles": allowed_roles,
        },
    }


def build_dataset_payload(records: list[dict]) -> dict:
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
        "records": records,
    }


def build_run_log(
    *,
    started_at: str,
    finished_at: str,
    status: str,
    records_uploaded: int,
    raw_key: str | None,
    latest_key: str | None,
    selected_files: list[str],
    local_output_path: str | None,
    error: str | None = None,
) -> dict:
    return {
        "scraper": SCRAPER_NAME,
        "dataset": DATASET,
        "record_type": RECORD_TYPE,
        "source": SOURCE_NAME,
        "access_level": ACCESS_LEVEL,
        "allowed_roles": ALLOWED_ROLES,
        "s3_bucket": S3_BUCKET,
        "aws_region": AWS_REGION,
        "aws_profile": AWS_PROFILE or None,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "records_uploaded": records_uploaded,
        "raw_key": raw_key,
        "latest_key": latest_key,
        "selected_files": selected_files,
        "local_output_path": local_output_path,
        "error": error,
    }


# =========================
# PDF PARSER LOGIC
# =========================

def parse_date(line: str) -> str | None:
    line = (line or "").strip()
    m = DATE_RE.match(line)
    if not m:
        return None

    _, day, mon, year = m.groups()
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(f"{day} {mon} {year}", fmt).date().isoformat()
        except Exception:
            pass
    return None


def is_footer_or_note(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return True
    if s.startswith(("COP018 (MCEC) Page", "South Wharf Members Group", "From 01/")):
        return True
    if s.startswith("This report should be used as a guide only"):
        return True
    if s.startswith("•"):
        return True
    if s in ("Description Time Space", "Estimated", "Event Attendance", "Estimated Day Attendance"):
        return True
    return False


def time_key(t: str):
    try:
        return datetime.strptime(t, "%H:%M").time()
    except Exception:
        return datetime.strptime("00:00", "%H:%M").time()


def norm_key(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).upper()


def fmt_sched_with_space(r: pd.Series) -> str:
    item = clean_str(r.get("schedule_item")).strip()
    if not item:
        return ""
    space = clean_str(r.get("space")).strip()
    time_part = f"{clean_str(r.get('start_time')).strip()}-{clean_str(r.get('end_time')).strip()}"
    return f"{item} ({time_part} | {space})" if space else f"{item} ({time_part})"


def extract_lines_page(page) -> list[str]:
    text = page.extract_text() or ""
    if text.strip():
        lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
        return [ln for ln in lines if ln]

    words = page.extract_words() or []
    if not words:
        return []

    words.sort(key=lambda w: (round(w["top"], 1), w["x0"]))
    lines = []
    current_y = None
    buffer = []

    for w in words:
        y = round(w["top"], 1)
        if current_y is None or abs(y - current_y) <= 2.0:
            buffer.append(w["text"])
            current_y = y if current_y is None else current_y
        else:
            lines.append(" ".join(buffer).strip())
            buffer = [w["text"]]
            current_y = y

    if buffer:
        lines.append(" ".join(buffer).strip())

    return [re.sub(r"\s+", " ", ln).strip() for ln in lines if ln.strip()]


def extract_rows_from_pdf(pdf_path: Path) -> pd.DataFrame:
    rows = []
    current_date = None
    current_site = None
    last_known_site = None
    current_event_header = None
    current_event_att = None
    current_day_att = None
    buffer_desc = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            lines = extract_lines_page(page)

            for line in lines:
                if is_footer_or_note(line):
                    continue

                d = parse_date(line)
                if d:
                    current_date = d
                    current_site = None
                    current_event_header = None
                    current_event_att = None
                    current_day_att = None
                    buffer_desc = []
                    continue

                clean_line = re.sub(r"[^A-Za-z ]", "", line).strip()

                matched_site = None
                for site in SITE_HEADINGS:
                    if site in clean_line:
                        matched_site = site
                        break

                if matched_site:
                    current_site = matched_site
                    last_known_site = matched_site
                    current_event_header = None
                    current_event_att = None
                    current_day_att = None
                    buffer_desc = []
                    continue

                m_evt = EVENT_HDR_RE.match(line)
                if m_evt and not TIME_RE.search(line):
                    hdr, day_pax, event_pax = m_evt.groups()
                    current_event_header = hdr.strip()
                    current_day_att = int(day_pax.replace(",", ""))
                    current_event_att = int(event_pax.replace(",", ""))
                    buffer_desc = []
                    continue

                m_time = TIME_RE.search(line)
                if m_time:
                    start, end = m_time.group(1), m_time.group(2)
                    pre = line[: m_time.start()].strip()
                    post = line[m_time.end() :].strip()

                    desc_parts = []
                    if buffer_desc:
                        desc_parts.append(" ".join(buffer_desc).strip())
                    if pre:
                        desc_parts.append(pre)

                    desc = " ".join([p for p in desc_parts if p]).strip()
                    buffer_desc = []

                    rows.append(
                        {
                            "date": current_date,
                            "site": current_site or last_known_site,
                            "event_header": current_event_header,
                            "schedule_item": desc,
                            "start_time": start,
                            "end_time": end,
                            "space": post,
                            "estimated_event_attendance": current_event_att,
                            "estimated_day_attendance": current_day_att,
                        }
                    )
                    continue

                if current_event_header:
                    buffer_desc.append(line)

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df = df[df["date"].notna() & df["event_header"].notna()].copy()
    df = df[df["estimated_day_attendance"].fillna(0) > 0].copy()
    df["start_time_sort"] = df["start_time"].apply(time_key)
    df.sort_values(["date", "start_time_sort", "site", "event_header"], inplace=True)
    df.drop(columns=["start_time_sort"], inplace=True)
    return df


def extract_unique_spaces_from_schedule(schedule_items: str) -> str:
    if not schedule_items or pd.isna(schedule_items):
        return ""

    spaces = []
    entries = [e.strip() for e in schedule_items.split(";") if e.strip()]
    for e in entries:
        m = re.search(r"\|\s*(.*?)\s*\)$", e)
        if m:
            space = m.group(1).strip()
            if space:
                spaces.append(space)

    unique_spaces = list(dict.fromkeys(spaces))
    return ", ".join(unique_spaces)


def compute_catering_note(schedule_items: str, event_space: str, event_header: str) -> str:
    sched_up = (schedule_items or "").upper()
    for term in CATERING_CONFIRMED_TERMS:
        if term.upper() in sched_up:
            return "catering HIGHLY likely / confirmed"

    ev_space = clean_str(event_space).strip()
    if ev_space:
        entries = [e.strip() for e in (schedule_items or "").split(";") if e.strip()]
        for e in entries:
            if re.match(r"^LUNCH\b", e.strip(), flags=re.IGNORECASE):
                m = re.search(r"\|\s*(.*?)\s*\)$", e)
                lunch_space = m.group(1).strip() if m else ""
                if lunch_space and lunch_space != ev_space:
                    return "catering highly likely"

    hdr_up = (event_header or "").upper()
    if ("EXHIBITION" in hdr_up) or ("CONCERT/TICKETED EVENTS" in hdr_up):
        return "catering unlikely"

    return "catering undefined"


def header_base_name(event_header: str) -> str:
    s = (event_header or "").strip()
    if not s:
        return ""

    idx_dash = s.find(" - ")
    m_num = re.search(r"\d", s)
    idx_num = m_num.start() if m_num else -1
    cut_points = [i for i in [idx_dash, idx_num] if i != -1]
    if not cut_points:
        return s
    return s[: min(cut_points)].strip()


def header_categories(event_header: str) -> list[str]:
    s = (event_header or "").strip()
    if not s:
        return ["MCEC Event"]
    if " - " not in s:
        return [s]
    return [p.strip() for p in s.split(" - ") if p.strip()]


def header_audience_type(event_header: str) -> list[str]:
    s = (event_header or "").strip()
    parts = [p.strip() for p in s.split(" - ") if p.strip()]
    if len(parts) >= 2:
        return [parts[1]]
    base = header_base_name(s)
    return [base] if base else ["Public"]


def reduce_to_event_level(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()
    df["schedule_item_norm"] = df["schedule_item"].apply(norm_key)
    df["is_guests_arrive"] = df["schedule_item_norm"].eq("GUESTS ARRIVE")
    df["is_guests_depart"] = df["schedule_item_norm"].eq("GUESTS DEPART")
    df["is_show_open"] = df["schedule_item_norm"].eq("SHOW OPEN")

    df["guests_arrive_space"] = df.apply(lambda r: r["space"] if r["is_guests_arrive"] else "", axis=1)
    df["show_open_space"] = df.apply(lambda r: r["space"] if r["is_show_open"] else "", axis=1)
    df["guests_depart_space"] = df.apply(lambda r: r["space"] if r["is_guests_depart"] else "", axis=1)

    df["guests_arrive"] = df.apply(lambda r: r["start_time"] if r["is_guests_arrive"] else "", axis=1)
    df["guests_depart"] = df.apply(lambda r: r["end_time"] if r["is_guests_depart"] else "", axis=1)
    df["show_open_start"] = df.apply(lambda r: r["start_time"] if r["is_show_open"] else "", axis=1)
    df["show_open_end"] = df.apply(lambda r: r["end_time"] if r["is_show_open"] else "", axis=1)

    df["sched_entry"] = df.apply(
        lambda r: "" if (r["is_guests_arrive"] or r["is_guests_depart"] or r["is_show_open"])
        else fmt_sched_with_space(r),
        axis=1,
    )

    group_cols = ["date", "site", "event_header", "estimated_event_attendance", "estimated_day_attendance"]
    agg = (
        df.groupby(group_cols, dropna=False)
        .agg(
            schedule_items=("sched_entry", lambda s: "; ".join([x for x in s if x])),
            guests_arrive=("guests_arrive", lambda s: next((x for x in s if x), "")),
            guests_depart=("guests_depart", lambda s: next((x for x in s if x), "")),
            show_open_start=("show_open_start", lambda s: next((x for x in s if x), "")),
            show_open_end=("show_open_end", lambda s: next((x for x in s if x), "")),
            guests_depart_space=("guests_depart_space", lambda s: next((x for x in s if str(x).strip()), "")),
            guests_arrive_space=("guests_arrive_space", lambda s: next((x for x in s if str(x).strip()), "")),
            show_open_space=("show_open_space", lambda s: next((x for x in s if str(x).strip()), "")),
        )
        .reset_index()
    )

    agg["event_start_time"] = agg["guests_arrive"].astype(str)
    mask = agg["event_start_time"].str.strip().eq("")
    agg.loc[mask, "event_start_time"] = agg.loc[mask, "show_open_start"].astype(str)

    agg["event_end_time"] = agg["guests_depart"].astype(str)
    mask = agg["event_end_time"].str.strip().eq("")
    agg.loc[mask, "event_end_time"] = agg.loc[mask, "show_open_end"].astype(str)

    agg["event_start_time"] = agg["event_start_time"].str.strip()
    agg["event_end_time"] = agg["event_end_time"].str.strip()
    agg.loc[agg["event_start_time"].eq(""), "event_start_time"] = None
    agg.loc[agg["event_end_time"].eq(""), "event_end_time"] = None

    agg["event_space"] = agg["guests_arrive_space"].astype(str)
    mask = agg["event_space"].str.strip().eq("")
    agg.loc[mask, "event_space"] = agg.loc[mask, "show_open_space"].astype(str)
    mask = agg["event_space"].str.strip().eq("")
    agg.loc[mask, "event_space"] = agg.loc[mask, "guests_depart_space"].astype(str)
    agg["event_space"] = agg["event_space"].str.strip()

    mask = agg["event_space"].str.strip().eq("")
    agg.loc[mask, "event_space"] = agg.loc[mask, "schedule_items"].apply(extract_unique_spaces_from_schedule)

    agg["catering_provided"] = agg.apply(
        lambda r: compute_catering_note(r.get("schedule_items", ""), r.get("event_space", ""), r.get("event_header", "")),
        axis=1,
    )
    return agg


def build_location_name(site: str, event_space: str) -> str:
    site = clean_str(site).strip()
    event_space = clean_str(event_space).strip()

    if site == "MCEC Expansion":
        return f"MCEC Expansion - {event_space}" if event_space else "MCEC Expansion"
    if site == "Exhibition Centre":
        return f"MCEC Exhibition Centre - {event_space}" if event_space else "MCEC Exhibition Centre"
    if site == "Convention Centre":
        return f"MCEC Convention Centre - {event_space}" if event_space else "MCEC Convention Centre"
    if site:
        return f"MCEC {site} - {event_space}" if event_space else f"MCEC {site}"
    return f"MCEC - {event_space}" if event_space else "MCEC"


def swmg_pdf_to_records(pdf_path: Path) -> list[dict]:
    df_rows = extract_rows_from_pdf(pdf_path)
    if df_rows.empty:
        return []

    df_event = reduce_to_event_level(df_rows)
    if df_event.empty:
        return []

    records = []
    for _, r in df_event.iterrows():
        estimated_day_attendance = int(r.get("estimated_day_attendance") or 0)
        title = f"{header_base_name(r.get('event_header', ''))} {estimated_day_attendance}pax".strip()
        location_name = build_location_name(r.get("site"), r.get("event_space"))

        notes_parts = [
            clean_str(r.get("catering_provided")).strip(),
            f"source_pdf={pdf_path.name}",
        ]
        schedule_items = clean_str(r.get("schedule_items")).strip()
        if schedule_items:
            notes_parts.append(f"schedule={schedule_items}")
        notes = " || ".join([p for p in notes_parts if p])

        records.append(
            build_record(
                title=title,
                date=clean_str(r.get("date")).strip(),
                start_time=clean_str(r.get("event_start_time")).strip() or None,
                end_time=clean_str(r.get("event_end_time")).strip() or None,
                location_name=location_name,
                categories=header_categories(clean_str(r.get("event_header"))),
                audience_type=header_audience_type(clean_str(r.get("event_header"))),
                filter=FILTER_LABEL,
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

    records.sort(key=lambda x: (x["date"], x["start_time"] or "", x["title"], x["location_name"]))
    return records


def dedupe_records(records: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for r in records:
        key = (
            r.get("date"),
            r.get("start_time"),
            r.get("end_time"),
            (r.get("title") or "").lower().strip(),
            (r.get("location_name") or "").lower().strip(),
            (r.get("source") or "").lower().strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# =========================
# GUI APP
# =========================

@dataclass
class ProcessResult:
    record_count: int
    local_output_path: str
    raw_key: str
    latest_key: str
    log_key: str


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MCEC SWMG PDF Uploader")
        self.root.geometry("860x620")

        self.selected_files: list[str] = []
        self.output_dir = Path.cwd() / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._build_ui()

    def _build_ui(self):
        frame = tk.Frame(self.root, padx=16, pady=16)
        frame.pack(fill="both", expand=True)

        title = tk.Label(frame, text="MCEC SWMG PDF → Separate JSON → S3", font=("Segoe UI", 16, "bold"))
        title.pack(anchor="w")

        subtitle = tk.Label(
            frame,
            text="Select one or more SWMG PDFs. This creates a separate mcec-swmg-events dataset and does not merge into any existing JSON.",
            justify="left",
            wraplength=800,
        )
        subtitle.pack(anchor="w", pady=(6, 14))

        btn_row = tk.Frame(frame)
        btn_row.pack(fill="x", pady=(0, 10))

        tk.Button(btn_row, text="Select PDF Files", command=self.select_files, width=18).pack(side="left")
        tk.Button(btn_row, text="Process + Upload to S3", command=self.process_files, width=20).pack(side="left", padx=8)
        tk.Button(btn_row, text="Clear", command=self.clear_files, width=10).pack(side="left")

        self.files_label = tk.Label(frame, text="No files selected", justify="left", anchor="w")
        self.files_label.pack(fill="x", pady=(0, 10))

        profile_text = AWS_PROFILE if AWS_PROFILE else "(default boto3 credentials)"
        settings_text = f"Dataset: {DATASET}    Bucket: {S3_BUCKET}    Region: {AWS_REGION}    Profile: {profile_text}"
        tk.Label(frame, text=settings_text, fg="#555555").pack(anchor="w", pady=(0, 10))

        self.log_box = scrolledtext.ScrolledText(frame, height=26, font=("Consolas", 10))
        self.log_box.pack(fill="both", expand=True)
        self.log("Ready.")
        self.log(f"Local output folder: {self.output_dir}")
        self.log(f"AWS profile: {profile_text}")
        self.log(f"Caller ARN: {get_caller_arn()}")

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_box.insert("end", f"[{ts}] {msg}\n")
        self.log_box.see("end")
        self.root.update_idletasks()

    def select_files(self):
        files = filedialog.askopenfilenames(
            title="Select SWMG PDF files",
            filetypes=[("PDF files", "*.pdf")],
        )
        if files:
            self.selected_files = list(files)
            self.files_label.config(text="Selected:\n" + "\n".join(self.selected_files))
            self.log(f"Selected {len(self.selected_files)} file(s).")

    def clear_files(self):
        self.selected_files = []
        self.files_label.config(text="No files selected")
        self.log("Cleared selected files.")

    def process_files(self):
        if not self.selected_files:
            messagebox.showwarning("No files selected", "Please select one or more PDF files first.")
            return

        started_at = utc_iso()
        raw_key = None
        latest_key = None
        local_output_path = None
        records_uploaded = 0

        try:
            self.log("Starting processing...")
            all_records = []

            for file_path in self.selected_files:
                pdf_path = Path(file_path)
                self.log(f"Parsing: {pdf_path.name}")
                records = swmg_pdf_to_records(pdf_path)
                self.log(f"  Extracted {len(records)} records from {pdf_path.name}")
                all_records.extend(records)

            all_records = dedupe_records(all_records)
            self.log(f"Combined deduped record count: {len(all_records)}")

            payload = build_dataset_payload(all_records)

            timestamp = utc_stamp()
            local_output = self.output_dir / f"{DATASET}_{timestamp}.json"
            local_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            local_output_path = str(local_output)
            self.log(f"Wrote local JSON: {local_output}")

            s3 = get_s3_client()
            raw_key = build_raw_key(ACCESS_LEVEL, DATASET)
            latest_key = build_latest_key(ACCESS_LEVEL, DATASET)
            upload_json_to_s3(raw_key, payload, s3)
            upload_json_to_s3(latest_key, payload, s3)
            records_uploaded = payload["record_count"]
            self.log(f"Uploaded raw JSON to s3://{S3_BUCKET}/{raw_key}")
            self.log(f"Uploaded latest JSON to s3://{S3_BUCKET}/{latest_key}")

            finished_at = utc_iso()
            run_log = build_run_log(
                started_at=started_at,
                finished_at=finished_at,
                status="ok",
                records_uploaded=records_uploaded,
                raw_key=raw_key,
                latest_key=latest_key,
                selected_files=self.selected_files,
                local_output_path=local_output_path,
                error=None,
            )
            log_key = build_log_key(ACCESS_LEVEL, DATASET)
            upload_json_to_s3(log_key, run_log, s3)
            self.log(f"Uploaded log JSON to s3://{S3_BUCKET}/{log_key}")

            messagebox.showinfo(
                "Complete",
                f"Uploaded {records_uploaded} records.\n\nLocal file:\n{local_output_path}\n\nLatest S3 key:\n{latest_key}",
            )

        except Exception as exc:
            self.log("ERROR: Processing failed.")
            self.log(str(exc))
            self.log(traceback.format_exc())

            try:
                s3 = get_s3_client()
                finished_at = utc_iso()
                run_log = build_run_log(
                    started_at=started_at,
                    finished_at=finished_at,
                    status="error",
                    records_uploaded=records_uploaded,
                    raw_key=raw_key,
                    latest_key=latest_key,
                    selected_files=self.selected_files,
                    local_output_path=local_output_path,
                    error=str(exc),
                )
                log_key = build_log_key(ACCESS_LEVEL, DATASET)
                upload_json_to_s3(log_key, run_log, s3)
                self.log(f"Uploaded error log to s3://{S3_BUCKET}/{log_key}")
            except Exception as log_exc:
                self.log(f"Also failed to upload error log: {log_exc}")

            messagebox.showerror("Failed", f"Processing failed.\n\n{exc}")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()