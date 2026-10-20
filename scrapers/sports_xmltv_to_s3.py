#!/usr/bin/env python3
"""XMLTV Melbourne Sports-labelled programmes to separate S3 feed, no SportsDB fixture gate."""
import argparse,datetime as dt,hashlib,json,os,re,urllib.request,xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo
TZ=ZoneInfo('Australia/Melbourne')
EPG='https://xmltv.net/xml_files/Melbourne.xml'
def normal(s):return re.sub(r'\s+',' ',(s or '').strip()).casefold()
def channel_family(s):
    t=normal(s)
    if 'racing.com' in t or 'racing com' in t:return ''
    if t.startswith('nitv'):return 'nitv'
    if re.search(r'\bchannel\s*31\b',t):return 'channel 31'
    if re.search(r'\b(?:7mate|7two|7flix)\b',t):return re.search(r'\b(7mate|7two|7flix)\b',t).group(1)
    if re.search(r'\b(?:9go!?|9gem|9life)\b',t):return re.search(r'\b(9go!?|9gem|9life)\b',t).group(1).replace('!','')
    if t.startswith('sbs2') or 'viceland' in t:return 'sbs viceland'
    if t.startswith('sbs'):return 'sbs'
    if re.match(r'^7\b',t):return '7'
    if re.match(r'^9\b',t):return '9'
    if re.match(r'^10\b',t):return '10'
    if t.startswith('abc'):return 'abc'
    return ''
def parse_date(s):
    m=re.match(r'^(\d{14})(?:\s*([+-]\d{4}))?',s or '')
    if not m:return None
    d=dt.datetime.strptime(m.group(1),'%Y%m%d%H%M%S')
    if m.group(2):
        v=m.group(2);off=(int(v[1:3])*60+int(v[3:]))*(1 if v[0]=='+' else -1)
        d=d.replace(tzinfo=dt.timezone(dt.timedelta(minutes=off)))
    else:d=d.replace(tzinfo=TZ)
    return d.astimezone(TZ)
def load_xml(source):
    if Path(source).is_file():return Path(source).read_bytes()
    req=urllib.request.Request(source,headers={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36','Accept':'*/*'})
    with urllib.request.urlopen(req,timeout=90) as r:return r.read()

def extract_matchup(title, subtitle, description):
    """Prefer XMLTV subtitle, otherwise identify two teams from description.

    Conservative: team-versus-team wording only. Preserve the programme title
    when no unambiguous pairing exists; never infer teams from event titles alone.
    """
    subtitle = re.sub(r"\s+", " ", subtitle or "").strip()
    if subtitle:
        return subtitle
    desc = re.sub(r"\s+", " ", description or "").strip()
    if not desc:
        return title

    # Phrases used by Australian TV guides, including AFLW summaries.
    # Capture stops at punctuation or a coverage phrase, so locations and
    # broadcast commentary don't become part of a team name.
    team = r"(?:the\s+)?[A-Za-z][A-Za-z0-9'’&. -]{1,58}?"
    verb = r"(?:go\s+head[- ]to[- ]head\s+with|go\s+head\s+to\s+head\s+against|take\s+on|clash(?:es)?\s+with|face(?:s)?|meet(?:s)?|vs?\.?|versus)"
    ending = r"(?=\s*(?:,|;|\.|!|$|\blive\b|\bfrom\b|\bat\b|\bin\b|\bon\b|\bthis\b|\bfor\b))"
    pattern = rf"\b({team})\s+{verb}\s+({team}){ending}"
    match = re.search(pattern, desc, re.IGNORECASE)
    if not match:
        # Explicit subject/predicate search handles unprefixed national teams such
        # as "Australia takes on New Zealand in this final ...".
        fallback = (
            r"(?:^|[.!?]\s+)(?:the\s+)?"
            r"([A-Za-z][A-Za-z0-9'’&. -]{1,58}?)\s+"
            r"(?:takes?\s+on|clash(?:es)?\s+with|go(?:es)?\s+head[- ]to[- ]head\s+with)\s+"
            r"(?:the\s+)?([A-Za-z][A-Za-z0-9'’&. -]{1,58}?)"
            r"(?=\s+(?:in\s+this|in\s+the|live\b|from\b|at\b|on\b|for\b)|[,;.!?]|$)"
        )
        match = re.search(fallback, desc, re.IGNORECASE)
    if not match:
        venue_patterns = [
            r'\bfrom\s+(?:the\s+)?([A-Z][A-Za-z0-9’\'& .-]{3,80}?(?:Street Circuit|Raceway|Circuit|Oval|Stadium|Arena))(?=\s*[,.]|\s+on\b|$)',
            r'\bat\s+(?:the\s+)?([A-Z][A-Za-z0-9’\'& .-]{3,80}?(?:Street Circuit|Raceway|Circuit|Oval|Stadium|Arena))(?=\s*[,.]|\s+on\b|$)',
        ]
        for venue_pattern in venue_patterns:
            venue_match = re.search(venue_pattern, desc)
            if venue_match:
                return re.sub(r'\s+', ' ', venue_match.group(1)).strip(' ,.;')
        return title

    def tidy(value):
        value = re.sub(r"^(?:the)\s+", "", value.strip(), flags=re.I)
        return re.sub(r"\s+", " ", value).strip(" ,.:;-")

    left, right = tidy(match.group(1)), tidy(match.group(2))
    # Reject sentence fragments and trivially generic names.
    invalid = {"the", "team", "teams", "game", "match", "live", "today"}
    if not left or not right or left.casefold() in invalid or right.casefold() in invalid:
        return title
    return f"{left} v {right}"


def prefer_detailed_programmes(rows):
    """Choose the most informative programme for an identical TV broadcast slot.

    Distinct identified team matchups in the same slot survive for review;
    venue-only and generic duplicates do not survive alongside team matchups.
    """
    groups = {}
    for row in rows:
        key = (row['channel_family'], row['coverage_start_local'],
               row.get('coverage_end_local'), normal(row['epg_title']))
        groups.setdefault(key, []).append(row)

    def is_matchup(row):
        return bool(re.search(r'\s+v(?:s\.?)?\s+', row.get('teams') or '', re.I))

    def score(row):
        ch = normal(row.get('channel'))
        return (bool(row.get('epg_subtitle')), bool(row.get('description')),
                len(row.get('description') or ''),
                not (' hd ' in (' ' + ch + ' ') or ' h d ' in (' ' + ch + ' ')))

    chosen = []
    for group in groups.values():
        fixtures = [r for r in group if is_matchup(r)]
        if fixtures:
            # Keep distinct named fixtures, but one best HD/SD record per fixture.
            candidate_rows = fixtures
        else:
            # When no team pair exists, pick one detailed venue/generic record.
            candidate_rows = [max(group, key=lambda r: (
                normal(r.get('teams')) != normal(r.get('epg_title')),
                *score(r)))]
        best = {}
        for row in candidate_rows:
            identity = normal(row.get('teams'))
            if identity not in best or score(row) > score(best[identity]):
                best[identity] = row
        chosen.extend(best.values())
    return chosen

def main():
    p=argparse.ArgumentParser();p.add_argument('--xml',default=EPG);p.add_argument('--days',type=int,default=30);p.add_argument('--upload',action='store_true');p.add_argument('--bucket',default=os.getenv('S3_BUCKET','event-scrape-data'));a=p.parse_args()
    if not 1<=a.days<=31:raise SystemExit('days must be 1..31')
    root=ET.fromstring(load_xml(a.xml));names={};lcn={}
    for c in root.findall('channel'):
        cid=c.get('id');names[cid]=[e.text or '' for e in c.findall('display-name')];lcn[cid]=c.findtext('lcn') or ''
    start=dt.datetime.now(TZ).date();end=start+dt.timedelta(days=a.days)
    out=[];seen=set()
    for node in root.findall('programme'):
        cats=[(c.text or '').strip() for c in node.findall('category')]
        if not any(c.casefold() in ('sports','sport') for c in cats):continue
        when=parse_date(node.get('start'))
        if not when or not start<=when.date()<end:continue
        cid=node.get('channel','');labels=names.get(cid,[]);ch=next((s for s in labels if channel_family(s)),(labels or [cid])[0]);family=channel_family(ch)
        if not family or '+1' in normal(ch):continue
        title=(node.findtext('title') or '').strip();subtitle=(node.findtext('sub-title') or '').strip()
        if not title:continue
        # Prefer common non-HD channel where both are in the EPG, dedupe by family + start + title + subtitle.
        # Exact duplicates can be discarded immediately. Different subtitles
        # are evaluated in a second pass to favour identified teams.
        identity=(family,when.isoformat(),normal(title),normal(subtitle))
        if identity in seen:continue
        seen.add(identity)
        stop=parse_date(node.get('stop'))
        key=hashlib.sha256('|'.join([family,normal(title),normal(subtitle)]).encode()).hexdigest()[:24]
        desc=(node.findtext('desc') or '').strip()
        matchup=extract_matchup(title,subtitle,desc)
        row={'xml_id':key,'match_id':None,'teams':matchup,'epg_title':title,'epg_subtitle':subtitle,'description':desc, 'strSport':'Sports','sport':'Sports','channel':ch,'channel_family':family,'strLogo':None,'channel_logo':None,'match_start_local':when.isoformat(),'coverage_start_local':when.isoformat(),'coverage_end_local':stop.isoformat() if stop else None,'source':'XMLTV Melbourne','broadcast_type':'xmltv_sports_programme','category':cats,'competition':''}
        out.append(row)
    out = prefer_detailed_programmes(out)
    out.sort(key=lambda x:(x['match_start_local'],x['channel'],x['teams']))
    payload={'version':1,'generated_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'source':'XMLTV Melbourne','programmes':out,'count':len(out),'notice':'XMLTV Sports-category broadcasts; broadcast start/end times from XMLTV. Title-wide omissions are managed separately.'}
    target=Path('data/sports-xmltv.json');target.parent.mkdir(exist_ok=True);target.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf8')
    print('XMLTV sports programmes:',len(out),'local file:',target,'channel labels:',len(set(x['channel'] for x in out)))
    if a.upload:
        if not out:raise SystemExit('Refusing to publish empty XMLTV sports list')
        import boto3
        boto3.client('s3').put_object(Bucket=a.bucket,Key='internal/sports/xmltv-latest.json',Body=json.dumps(payload,ensure_ascii=False).encode(),ContentType='application/json',CacheControl='no-store')
        print('Uploaded: s3://'+a.bucket+'/internal/sports/xmltv-latest.json')
    else:print('LOCAL ONLY - no S3 writes')
if __name__=='__main__':main()
