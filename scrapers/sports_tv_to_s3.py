#!/usr/bin/env python3
"""TheSportsDB-only Australian broadcast feed. No XMLTV calls or invented TV times.

Uses premium /api/v2/json/filter/tv/country/australia. Only sports that
TheSportsDB lists as Australian TV broadcasts with Australian TV
channel labels are included. TV availability is provisional, not guaranteed.
"""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import urllib.request
from zoneinfo import ZoneInfo

MEL = ZoneInfo('Australia/Melbourne')
API_URL = 'https://www.thesportsdb.com/api/v2/json/filter/tv/country/australia'
BLOCKED = re.compile(r'\b(?:sydney|brisbane|perth|adelaide|darwin|hobart|tasmania|queensland|western australia|south australia|new south wales|nsw|wa|sa|qld)\b', re.I)
MELBOURNE = re.compile(r'\b(?:melbourne|victoria|vic)\b', re.I)
NETWORKS = re.compile(r'^(?:(?:7(?:mate|two|flix|plus)?|seven)|(?:9(?:go!?|gem|life|rush)?|nine)|(?:10|ten)(?:\s+(?:bold|peach|shake|drama))?|sbs(?:\s*\d+)?|abc(?:\s*\w+)?)\b', re.I)
REPLAY = re.compile(r'\b(?:replay|repeat|highlights?|recap|review|best of|previously shown|delayed coverage)\b', re.I)


def get(row, *names):
    for n in names:
        v = row.get(n)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ''


def unwrap(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ('tv', 'tvevents', 'filter', 'events', 'data'):
            v = payload.get(key)
            if isinstance(v, list):
                return v
        arrays = [v for v in payload.values() if isinstance(v, list)]
        return arrays[0] if len(arrays) == 1 else []
    return []


def fetch(key):
    req = urllib.request.Request(API_URL, headers={
        'X-API-KEY': key, 'Accept': 'application/json',
        'User-Agent': 'MelbourneSportsCollector/1.0',
    })
    with urllib.request.urlopen(req, timeout=45) as resp:
        payload = json.load(resp)
    records = unwrap(payload)
    if not isinstance(records, list):
        raise RuntimeError('Unexpected SportsDB TV response')
    return records


def time_local(row):
    """SportsDB fixture timestamp is UTC; dateEvent + strTime also UTC."""
    stamp = get(row, 'strTimestamp', 'strTimeStamp', 'dateTime', 'strDateTime')
    if not stamp:
        date = get(row, 'dateEvent', 'date')
        clock = get(row, 'strTime', 'time')
        if date and clock:
            stamp = date + 'T' + clock
        elif date:
            stamp = date + 'T00:00:00'
        else:
            return None
    try:
        parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(MEL)
    except ValueError:
        return None


def assess_channel(label):
    label = label.strip()
    if not label:
        return 'missing_channel'
    if BLOCKED.search(label):
        return 'interstate_channel'
    if MELBOURNE.search(label):
        return 'melbourne_labelled'
    if NETWORKS.search(label):
        return 'national_network_unverified'
    return 'unknown_channel_region'


def convert(row, today, until, include_unknown_channels=False):
    if not isinstance(row, dict):
        return None, 'malformed_record'
    channel = get(row, 'strChannel', 'strTVChannel', 'channel', 'strTV')
    channel_status = assess_channel(channel)
    if channel_status in ('missing_channel', 'interstate_channel'):
        return None, channel_status
    start = time_local(row)
    if start is None:
        return None, 'missing_fixture_datetime'
    if not today <= start.date() < until:
        return None, 'outside_window'
    # Only exclude recordings when explicitly labelled. Future-dated != proof of live broadcast.
    title = get(row, 'strEvent', 'strMatch', 'event', 'title')
    description = get(row, 'strDescriptionEN', 'strDescription')
    status = get(row, 'strStatus', 'strTVStatus')
    if REPLAY.search(' '.join((title, description, status))):
        return None, 'recorded_or_highlights'
    match_id = get(row, 'idEvent', 'eventId', 'id_event')
    if not match_id:
        return None, 'missing_event_id'
    home = get(row, 'strHomeTeam', 'homeTeam')
    away = get(row, 'strAwayTeam', 'awayTeam')
    teams = ' vs '.join(x for x in (home, away) if x)
    if not teams:
        teams = title or 'Sporting event'
    sport = get(row, 'strSport', 'sport') or 'Sport'
    competition = get(row, 'strLeague', 'league')
    return {
        'match_id': match_id,
        'teams': teams,
        'sport': sport,  # Exact strSport, no renaming
        'strSport': sport,
        'event_thumb': get(row, 'strEventThumb'),
        'strEventThumb': get(row, 'strEventThumb'),
        'channel_logo': get(row, 'strLogo'),
        'strLogo': get(row, 'strLogo'),
        'competition': competition,
        'channel': channel,
        'channel_family': channel,
        'match_start_local': start.isoformat(),
        'coverage_start_local': None,
        'coverage_end_local': None,
        'broadcast_type': 'sportsdb_scheduled_tv_assignment',
        'match_status': 'sportsdb_tv_assigned',
        'tv_region_status': channel_status,
        'tv_confirmation': 'SportsDB listing; verify broadcaster rights and live coverage',
        'epg_title': None,
        'source': 'TheSportsDB',
        'sportsdb_event': title,
        'strEvent': title,
    }, 'included'


def build(raw, date_from, days, include_unknown_channels=False):
    until = date_from + timedelta(days=days)
    fixtures, review, statuses = [], [], Counter()
    seen = set()
    for row in raw:
        record, status = convert(row, date_from, until, include_unknown_channels)
        statuses[status] += 1
        if record:
            k = (record['match_id'], record['channel'].casefold(), record['match_start_local'])
            if k in seen:
                statuses['duplicate'] += 1
                continue
            seen.add(k)
            fixtures.append(record)
        elif status != 'outside_window':
            # Review deliberately contains only non-sensitive scheduling fields.
            review.append({'reason': status,
                           'event': get(row, 'strEvent', 'event', 'title') if isinstance(row,dict) else '',
                           'channel': get(row, 'strChannel', 'channel') if isinstance(row,dict) else '',
                           'event_id': get(row, 'idEvent') if isinstance(row,dict) else ''})
    fixtures.sort(key=lambda r: (r['match_start_local'], r['sport'], r['teams']))
    return fixtures, review, statuses


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=9)
    ap.add_argument('--date', help='YYYY-MM-DD start date in Melbourne')
    ap.add_argument('--local-only', action='store_true')
    ap.add_argument('--output', default='data/sports-tv.json')
    ap.add_argument('--xml', help='Ignored: SportsDB-only mode; kept for existing publisher compatibility')
    ap.add_argument('--include-unknown-channels', action='store_true', help='Include TV channels not recognised as Melbourne/national; review first')
    args = ap.parse_args()
    if not 1 <= args.days <= 30:
        ap.error('--days must be 1..30')
    key = os.environ.get('SPORTSDB_API_KEY', '').strip()
    if not key:
        ap.error('SPORTSDB_API_KEY must be configured')
    start = datetime.fromisoformat(args.date).date() if args.date else datetime.now(MEL).date()
    raw = fetch(key)
    if not raw:
        raise SystemExit('SportsDB returned no TV assignments; refusing to create an empty publishable feed')
    fixtures, review, statuses = build(raw, start, args.days, args.include_unknown_channels)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    result = {
        'schema_version': 16,
        'source': 'TheSportsDB premium Australian TV assignments only',
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'timezone': 'Australia/Melbourne',
        'date_from': start.isoformat(), 'days': args.days,
        'source_counts': {
            'sportsdb_tv_records': len(raw),
            'fixture_matches': len(fixtures),
            'excluded_for_review': len(review),
            'match_status': dict(statuses),
        },
        'fixtures': fixtures, 'programmes': fixtures, 'review': review, 'warnings': [],
        'verification_note': 'No XMLTV. Times are SportsDB fixture start, NOT broadcaster coverage. Country TV feed may be incomplete. Channel availability and live coverage not verified.',
    }
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    review_path = output.with_name(output.stem + '-review.json')
    review_path.write_text(json.dumps({'source': result['source'], 'review': review}, indent=2, ensure_ascii=False), encoding='utf-8')
    print('Saved SportsDB-only schedule', output, 'and review', review_path)
    print('Counts:', json.dumps(result['source_counts']))
    print('No XMLTV used. TV channels and live status are provisional.')

if __name__ == '__main__':
    main()
