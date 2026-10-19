#!/usr/bin/env python3
"""XMLTV Melbourne coverage-first + TheSportsDB fixture enrichment.
Never manufactures kick-off times; uncertain matches stay unverified.
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

MEL = ZoneInfo("Australia/Melbourne")
UTC = dt.timezone.utc
EPG_URL = "https://xmltv.net/xml_files/Melbourne.xml"
SPORTS = {
    "rugby league": "Rugby", "nrl": "Rugby", "a-league": "Soccer",
    "football": "Soccer", "soccer": "Soccer", "australian championship": "Soccer", "afl": "Australian Football",
    "cricket": "Cricket", "basketball": "Basketball", "nbl": "Basketball",
    "nfl": "American Football", "american football": "American Football",
    "tennis": "Tennis", "moto": "Motorsport", "grand prix": "Motorsport",
}
NON_LIVE = ("highlights", "replay", "encore", "best moments", "best of", "review",
            "sports tonight", "offsiders", "pre-game", "post-game", "speedweek", "agenda setters", "weekly kick off", "panel", "discussion", "recap", "roundup", "round-up",
            "ifish", "escape fishing", "horse racing preview", "fishing", "ifish", "sport report")
CATEGORIES = {"sports", "sport"}
CHANNEL_EQUIV = [
    ("7mate", "7mate"), ("7two", "7TWO"), ("7flix", "7flix"), ("7", "7"),
    ("9gem", "9Gem"), ("9go", "9Go!"), ("9life", "9Life"), ("9", "9"),
    ("10", "10"), ("abc", "ABC"), ("sbs", "SBS"), ("nitv", "NITV")
]
BLOCKS = {"news", "drama", "comedy", "special interest", "children", "documentary"}
TEAM_NOISE = {"fc","women","men","rlfc","rugby","league","football","the","club"}
def parse_time(value):
    return dt.datetime.strptime(value.strip(), "%Y%m%d%H%M%S %z").astimezone(MEL)
def normalized(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
def name_tokens(s):
    return {x for x in normalized(s).split() if len(x)>2 and x not in TEAM_NOISE}
def channel_family(name):
    n=normalized(name).replace(" ", "")
    if "+1" in (name or ""): return None
    for prefix, family in CHANNEL_EQUIV:
        if n.startswith(prefix):return family
    return None
def read_xml(source):
    if source.startswith("https://"):
        request = urllib.request.Request(
        source,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()
    return Path(source).read_bytes()
def broadcast_type(title, subtitle, description, previously_shown=False):
    """Classify evidence from EPG only. A fixture match is not proof of live TV."""
    heading=normalized(' '.join((title,subtitle)))
    details=normalized(description)
    if re.search(r"\b(highlights?|replays?|encore|repeat|rerun|best moments|best of)\b",heading) or re.search(r"\b(highlights?|replays?|previously recorded|recorded coverage)\b",details):
        return 'highlights_or_replay', 'epg_text'
    if previously_shown:
        return 'replay', 'xmltv_previously_shown'
    if re.search(r"\b(pre game|post game|preview|sports tonight|offsiders|panel|discussion|review show|speedweek|agenda setters|weekly kick off|fishing|ifish)\b",heading) or re.search(r"\b(comprehensive motorsport show|sports magazine|covers many forms of|discussion programme|talking points)\b",details):
        return 'studio_or_preview', 'epg_title'
    if re.search(r"\b(live)\b",heading) or re.search(r"\b(live coverage|live broadcast|broadcast live)\b",details):
        return 'live_labelled', 'epg_text'
    return 'unknown', 'not_specified'

def programmes_from_xml(payload, start_date=None, days=7):
    root=ET.fromstring(payload)
    channels={}
    for c in root.findall("channel"):
        channels[c.attrib["id"]]=(c.findtext("display-name") or c.attrib["id"], c.findtext("lcn") or "")
    out=[]; seen=set()
    for elem in root.findall("programme"):
        rawchan=elem.get("channel","")
        ch,lcn=channels.get(rawchan,(rawchan,""))
        family=channel_family(ch)
        if not family:continue
        begin=elem.get("start"); finish=elem.get("stop")
        if not begin or not finish:continue
        try: start,end=parse_time(begin),parse_time(finish)
        except ValueError:continue
        if start_date and not start_date<=start.date()<start_date+dt.timedelta(days=days):continue
        if not (7 <= start.hour < 22):continue
        title=(elem.findtext("title") or "").strip()
        subtitle=(elem.findtext("sub-title") or "").strip()
        desc=(elem.findtext("desc") or "").strip()
        categories=[(e.text or "").strip() for e in elem.findall("category")]
        if not any(x.lower() in CATEGORIES for x in categories):continue
        if not title or title.lower() in {"tba","to be advised"}:continue
        key=(family,start.isoformat(),normalized(title),normalized(subtitle))
        if key in seen:continue
        seen.add(key)
        btype,reason=broadcast_type(title,subtitle,desc,elem.find("previously-shown") is not None)
        kind="replay_or_other" if btype in ("highlights_or_replay","replay","studio_or_preview") else "sports_programme"
        sport=next((v for k,v in SPORTS.items() if k in normalized(title+" "+subtitle)),None)
        out.append({"epg_title":title,"epg_subtitle":subtitle,"description":desc,
            "sport_hint":sport,"channel":ch,"channel_family":family,"lcn":lcn,
            "coverage_start_local":start.isoformat(),"coverage_end_local":end.isoformat(),
            "category":categories,"programme_type":kind,
            "broadcast_type":btype,"broadcast_type_evidence":reason,
            "match_start_local":None,"match_id":None,"teams":None,
            "match_status":"not_checked","source":"XMLTV Melbourne"})
    return sorted(out,key=lambda x:(x["coverage_start_local"],x["channel_family"]))
def api_rows(payload):
    for k in ("events","event","results"):
        if isinstance(payload.get(k),list):return payload[k]
    return []
def sportsdb_event_time(event):
    timestamp=event.get("strTimestamp") or event.get("strTimeStamp")
    if timestamp:
        d=dt.datetime.fromisoformat(timestamp.replace("Z","+00:00"))
        if d.tzinfo is None:d=d.replace(tzinfo=UTC)
        return d.astimezone(MEL)
    date,t=event.get("dateEvent"),event.get("strTime")
    if not date or not t:return None
    try:return dt.datetime.fromisoformat(f"{date}T{t}").replace(tzinfo=UTC).astimezone(MEL)
    except ValueError:return None
def call_sportsdb(date,sport,key):
    params=urllib.parse.urlencode({"d":date.isoformat(),"s":sport})
    url=f"https://www.thesportsdb.com/api/v1/json/{urllib.parse.quote(key,safe='')}/eventsday.php?{params}"
    with urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"MelbourneSports/12"}),timeout=35) as response:
        return api_rows(json.load(response))
def candidate_teams(row):
    # Subtitle may contain "Avondale FC V Broadmeadow Magic FC: Round 2".
    subtitle=re.split(r":| - Round",row["epg_subtitle"])[0]
    parts=re.split(r"\s+(?:vs?\.?|versus)\s+",subtitle,flags=re.I)
    if len(parts)!=2:return None
    return parts[0].strip(),parts[1].strip()
def match_fixture(row,fixtures):
    teams=candidate_teams(row)
    if not teams:return None,"no_team_pair_in_xml"
    start=dt.datetime.fromisoformat(row["coverage_start_local"])
    end=dt.datetime.fromisoformat(row["coverage_end_local"])
    candidates=[]
    for e in fixtures:
        home=e.get("strHomeTeam") or ""
        away=e.get("strAwayTeam") or ""
        if not home or not away:continue
        exact=(name_tokens(teams[0]) & name_tokens(home) and name_tokens(teams[1]) & name_tokens(away))
        reverse=(name_tokens(teams[1]) & name_tokens(home) and name_tokens(teams[0]) & name_tokens(away))
        if not (exact or reverse):continue
        st=sportsdb_event_time(e)
        if not st:continue
        if not start-dt.timedelta(minutes=20)<=st<=end+dt.timedelta(minutes=10):continue
        candidates.append((e,st))
    ids={str(e.get("idEvent")) for e,_ in candidates}
    if len(ids)!=1:return None,("ambiguous" if ids else "fixture_not_found")
    e,st=candidates[0]
    return {"id":e.get("idEvent"),"start":st.isoformat(),
            "home":e.get("strHomeTeam"),"away":e.get("strAwayTeam"),
            "competition":e.get("strLeague"),"sport":e.get("strSport") or row.get("sport_hint")}, "matched_unique_teams_and_slot"
def enrich(rows,key,fetch=call_sportsdb):
    cache={};warnings=[]
    for row in rows:
        if row["programme_type"]!="sports_programme":
            row["match_status"]="excluded_from_live";continue
        if not candidate_teams(row):
            row["match_status"]="no_team_pair_in_xml";continue
        sport=row["sport_hint"]
        if not sport:
            row["match_status"]="sport_not_identified";continue
        date=dt.datetime.fromisoformat(row["coverage_start_local"]).date()
        pool=[]
        for d in (date-dt.timedelta(days=1),date,date+dt.timedelta(days=1)):
            keycache=(d.isoformat(),sport)
            if keycache not in cache:
                try:cache[keycache]=fetch(d,sport,key)
                except Exception as e:
                    cache[keycache]=[]
                    warnings.append(f"SportsDB lookup {d} {sport} failed: {type(e).__name__}: {e}")
            pool+=cache[keycache]
        matched,status=match_fixture(row,pool)
        row["match_status"]=status
        if matched:
            row["match_id"]=matched["id"]
            row["match_start_local"]=matched["start"]
            row["teams"]=matched["home"]+" vs "+matched["away"]
            row["competition"]=matched["competition"]
            row["sport"]=matched["sport"]
            row["broadcast_type"]="live_fixture_matched"
            row["broadcast_type_evidence"]="sportsdb_unique_fixture_in_epg_slot"
    return warnings,len(cache)
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--xml",default=EPG_URL,help="Local Melbourne.xml path or https URL")
    p.add_argument("--date",help="YYYY-MM-DD start date")
    p.add_argument("--days",type=int,default=7)
    p.add_argument("--local-only",action="store_true")
    p.add_argument("--epg-only",action="store_true",help="No SportsDB calls; test EPG parsing")
    p.add_argument("--output",default="data/sports-tv.json")
    args=p.parse_args()
    if not 1<=args.days<=14:p.error("--days must be 1..14")
    date=dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(MEL).date()
    rows=programmes_from_xml(read_xml(args.xml),date,args.days)
    warnings=[]; queries=0
    if not args.epg_only:
        key=os.environ.get("SPORTSDB_API_KEY","").strip()
        if not key:p.error("SPORTSDB_API_KEY missing; set env var or use --epg-only")
        warnings,queries=enrich(rows,key)
    matched=[r for r in rows if r["match_id"] and r["programme_type"]=="sports_programme" and r["broadcast_type"] not in ("replay","highlights_or_replay","studio_or_preview")]
    for r in matched:
        if r.get("sport")=="Rugby" and "rugby league" in (r.get("epg_title","")+" "+r.get("competition","")).lower(): r["sport"]="Rugby League"
        elif r.get("sport")=="Soccer":r["sport"]="Football"
    review=[r for r in rows if r not in matched]
    result={"schema_version":14,"source":"XMLTV Melbourne + TheSportsDB",
        "timezone":"Australia/Melbourne","date_from":date.isoformat(),"days":args.days,
        "source_counts":{"epg_sports_programmes":len(rows),
            "fixture_matches":len(matched),
            "excluded_for_review":len(review),
            "sportsdb_requests":queries,"match_status":dict(Counter(r["match_status"] for r in rows)),
            "broadcast_types":dict(Counter(r["broadcast_type"] for r in rows))},
        "fixtures":matched,"programmes":matched,"review":review,"warnings":warnings,
        "verification_note":"Coverage times from XMLTV; game start only from TheSportsDB. Live-labelled is based on EPG text only; likely-live is an inference; EPG classification and matching are provisional; commercial screening rights not verified."}
    target=Path(args.output);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding="utf-8")
    review_path=target.with_name(target.stem+"-review.json")
    review_path.write_text(json.dumps({"source":"XMLTV", "review":review,"warnings":warnings},indent=2,ensure_ascii=False),encoding="utf-8")
    print("Saved matched schedule",target,"and review",review_path)
    print("Counts:",json.dumps(result["source_counts"]))
    for w in warnings:print("WARNING",w)
if __name__=="__main__":main()

