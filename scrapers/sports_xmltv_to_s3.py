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
def main():
    p=argparse.ArgumentParser();p.add_argument('--xml',default=EPG);p.add_argument('--days',type=int,default=30);p.add_argument('--upload',action='store_true');p.add_argument('--bucket',default=os.getenv('S3_BUCKET','event-scrape-data'));p.add_argument('--sports-json',default='data/sports-tv.json');a=p.parse_args()
    if not 1<=a.days<=31:raise SystemExit('days must be 1..31')
    root=ET.fromstring(load_xml(a.xml));names={};lcn={}
    for c in root.findall('channel'):
        cid=c.get('id');names[cid]=[e.text or '' for e in c.findall('display-name')];lcn[cid]=c.findtext('lcn') or ''
    # Match SportsDB TV logos by exact channel or conservative channel family.
    logos={};famlogos={};sports_path=Path(a.sports_json)
    if sports_path.is_file():
        sports=json.loads(sports_path.read_text(encoding='utf-8'))
        for rec in sports.get('fixtures',[]):
            ch=normal(rec.get('channel'));logo=rec.get('strLogo') or rec.get('channel_logo')
            if ch and logo and logo.startswith('https://'):
                logos[ch]=logo;famlogos.setdefault(channel_family(ch),logo)
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
        identity=(family,when.isoformat(),normal(title),normal(subtitle))
        if identity in seen:continue
        seen.add(identity)
        stop=parse_date(node.get('stop'))
        key=hashlib.sha256('|'.join([family,normal(title),normal(subtitle)]).encode()).hexdigest()[:24]
        desc=(node.findtext('desc') or '').strip()
        row={'xml_id':key,'match_id':None,'teams':title,'epg_subtitle':subtitle,'description':desc[:700], 'strSport':'Sports','sport':'Sports','channel':ch,'channel_family':family,'strLogo':logos.get(normal(ch)) or famlogos.get(family),'channel_logo':logos.get(normal(ch)) or famlogos.get(family),'match_start_local':when.isoformat(),'coverage_start_local':when.isoformat(),'coverage_end_local':stop.isoformat() if stop else None,'source':'XMLTV Melbourne','broadcast_type':'xmltv_sports_programme','category':cats,'competition':''}
        out.append(row)
    out.sort(key=lambda x:(x['match_start_local'],x['channel'],x['teams']))
    payload={'version':1,'generated_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'source':'XMLTV Melbourne','programmes':out,'count':len(out),'notice':'Sports-category TV programmes, not all confirmed live events; omission keys persist by programme, subtitle and channel.'}
    target=Path('data/sports-xmltv.json');target.parent.mkdir(exist_ok=True);target.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf8')
    print('XMLTV sports programmes:',len(out),'local file:',target,'logo matches:',sum(bool(x['strLogo']) for x in out))
    if a.upload:
        if not out:raise SystemExit('Refusing to publish empty XMLTV sports list')
        import boto3
        boto3.client('s3').put_object(Bucket=a.bucket,Key='internal/sports/xmltv-latest.json',Body=json.dumps(payload,ensure_ascii=False).encode(),ContentType='application/json',CacheControl='no-store')
        print('Uploaded: s3://'+a.bucket+'/internal/sports/xmltv-latest.json')
    else:print('LOCAL ONLY - no S3 writes')
if __name__=='__main__':main()
