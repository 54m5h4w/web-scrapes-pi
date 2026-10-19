#!/usr/bin/env python3
"""Safe publisher for V14 collector. No S3 writes unless --upload is set."""
import argparse, json, os, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

HERE=Path(__file__).resolve().parent
DEFAULT_BUCKET='event-scrape-data'

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--upload',action='store_true',help='Actually upload to S3')
    p.add_argument('--days',type=int,default=7)
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
    payload=json.dumps(data,ensure_ascii=False,indent=2).encode('utf-8')
    base='internal/sports'
    keys=[f'{base}/raw/{stamp}.json',f'{base}/latest.json']
    for key in keys:
        s3.put_object(Bucket=args.bucket,Key=key,Body=payload,ContentType='application/json',CacheControl='no-store')
        print('Uploaded',f's3://{args.bucket}/{key}')
    log={'timestamp_utc':stamp,'status':'ok','fixture_count':len(records),'source_counts':data.get('source_counts',{})}
    s3.put_object(Bucket=args.bucket,Key=f'{base}/logs/{stamp}.json',Body=json.dumps(log).encode(),ContentType='application/json')
    print('UI note: /api/sports/latest requires a separate authenticated backend proxy; do not expose the S3 object through a public URL.')

if __name__=='__main__':main()
