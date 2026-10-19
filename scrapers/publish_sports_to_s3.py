#!/usr/bin/env python3
"""Safe publisher for SportsDB-only FTA collector. No S3 writes unless --upload is set."""
import argparse, json, os, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
from botocore.exceptions import ClientError

HERE=Path(__file__).resolve().parent
DEFAULT_BUCKET='event-scrape-data'

def fixture_key(record):
    # Stable across changes to fixture times; separate broadcasts remain separate listings.
    return (str(record.get('match_id') or ''),
            str(record.get('channel') or '').casefold().strip())


def apply_new_flags(records, previous, timestamp):
    """Preserve first-seen timestamps; bootstrap prior records as NOT new."""
    prior = {fixture_key(r): r for r in previous if isinstance(r, dict)}
    from datetime import timedelta
    now = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    new_count = 0
    for r in records:
        p = prior.get(fixture_key(r))
        first = p.get('first_seen_at') if p else None
        if p and not first:
            first = timestamp  # existing records are bootstrap, not new
        if not p:
            first = timestamp
        r['first_seen_at'] = first
        try:
            age = now - datetime.fromisoformat(first.replace('Z', '+00:00'))
            within_seven_days = timedelta(0) <= age < timedelta(days=7)
        except (TypeError, ValueError):
            within_seven_days = False
        r['is_new'] = (p is None or p.get('is_new') is True) and within_seven_days
        new_count += int(r['is_new'])
    return new_count


def read_previous(s3, bucket, key):
    try:
        result = s3.get_object(Bucket=bucket, Key=key)
        value = json.loads(result['Body'].read())
        if not isinstance(value, dict) or not isinstance(value.get('fixtures'), list):
            raise RuntimeError('Existing Sports S3 latest has invalid format. Refusing overwrite.')
        return value['fixtures']
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
            return []
        raise


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--upload',action='store_true',help='Actually upload to S3')
    p.add_argument('--days',type=int,default=30)
    p.add_argument('--xml',default='https://xmltv.net/xml_files/Melbourne.xml')
    p.add_argument('--bucket',default=os.getenv('S3_BUCKET',DEFAULT_BUCKET))
    p.add_argument('--min-fixtures',type=int,default=1)
    args=p.parse_args()
    output=HERE.parent/'data'/'sports-tv.json';output.parent.mkdir(exist_ok=True)
    cmd=[sys.executable,str(HERE/'sports_tv_to_s3.py'),'--xml',args.xml,'--days',str(args.days),'--local-only','--output',str(output)]
    subprocess.run(cmd,check=True)
    data=json.loads(output.read_text(encoding='utf-8'))
    records=data.get('fixtures',[])
    if not isinstance(records,list) or len(records)<args.min_fixtures:
        raise SystemExit(f'Refusing to publish: only {len(records) if isinstance(records,list) else 0} fixtures; minimum {args.min_fixtures}. Existing S3 latest unchanged.')
    if data.get('warnings'):
        raise SystemExit('Refusing to publish because API warnings occurred: '+str(data['warnings']))
    print(f'Validated {len(records)} matched fixtures')
    if not args.upload:
        print('LOCAL TEST ONLY. No S3 uploads (add --upload after review).')
        return
    import boto3
    s3=boto3.client('s3')
    stamp=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')
    base='internal/sports'
    previous=read_previous(s3,args.bucket,f'{base}/latest.json')
    if previous:
        new_count=apply_new_flags(records,previous,datetime.now(timezone.utc).isoformat())
    else:
        # First upload is baseline; do not claim that every existing listing is new.
        baseline=datetime.now(timezone.utc).isoformat()
        for r in records:
            r['first_seen_at']=baseline
            r['is_new']=False
        new_count=0
    data['new_fixture_count']=new_count
    data['schema_version']=19
    payload=json.dumps(data,ensure_ascii=False,indent=2).encode('utf-8')
    keys=[f'{base}/raw/{stamp}.json',f'{base}/latest.json']
    for key in keys:
        s3.put_object(Bucket=args.bucket,Key=key,Body=payload,ContentType='application/json',CacheControl='no-store')
        print('Uploaded',f's3://{args.bucket}/{key}')
    log={'timestamp_utc':stamp,'status':'ok','fixture_count':len(records),'new_fixture_count':new_count,'source_counts':data.get('source_counts',{})}
    s3.put_object(Bucket=args.bucket,Key=f'{base}/logs/{stamp}.json',Body=json.dumps(log).encode(),ContentType='application/json')
    print(f'New listings flagged: {new_count}. Latest feed updated.')

if __name__=='__main__':main()
