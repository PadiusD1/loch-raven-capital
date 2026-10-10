"""Validate research metadata and availability, never certify truth by URL alone."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

DESKS = frozenset(('fundamental', 'technical', 'sentiment', 'macro', 'quant'))
REQUIRED = ('desk', 'source_url', 'published_at', 'observed_at', 'asof', 'title', 'body')


def decision_time(value: str) -> datetime:
    """Parse the engine's UTC format, timezone-aware ISO, or start of an NY date.

    Date-only means START of the day, not end of day: intraday releases must
    never become available to an earlier decision by padding with 23:59.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Missing decision timestamp')
    value = value.strip()
    if len(value) == 10:
        dt = datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=ZoneInfo('America/New_York'))
    elif value.endswith(' UTC'):
        dt = datetime.strptime(value, '%Y-%m-%d %H:%M UTC').replace(tzinfo=timezone.utc)
    else:
        dt = _time(value)
    return dt.astimezone(timezone.utc)


def _time(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError('Missing timestamp')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Research timestamps must include timezone')
    return dt.astimezone(timezone.utc)


def validate_research(record: dict, decision_at: str | None = None) -> dict:
    if not isinstance(record, dict):
        raise ValueError('Research record must be an object')
    missing = [k for k in REQUIRED if not record.get(k)]
    if missing:
        raise ValueError('Missing fields: ' + ', '.join(missing))
    if record['desk'] not in DESKS:
        raise ValueError('Unknown research desk')
    for key in ('title', 'body', 'source_url'):
        if not isinstance(record[key], str):
            raise ValueError(f'{key} must be text')
    url = urlparse(record['source_url'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password:
        raise ValueError('Source must be an HTTPS URL without credentials')
    if len(record['body'].strip()) < 20 or len(record['body']) > 100_000:
        raise ValueError('Research body outside size bounds')
    published, observed, effective = (_time(record[k]) for k in ('published_at', 'observed_at', 'asof'))
    if published > observed or effective > observed:
        raise ValueError('Research cannot be observed before publication or effective date')
    if decision_at and max(published, observed, effective) > decision_time(decision_at):
        raise ValueError('Research unavailable at decision time')
    digest = hashlib.sha256(record['body'].encode()).hexdigest()
    if record.get('content_sha256') and record['content_sha256'] != digest:
        raise ValueError('Research content hash mismatch')
    tickers = record.get('tickers', [])
    if not isinstance(tickers, list) or any(not isinstance(t, str) for t in tickers):
        raise ValueError('tickers must be a list of strings')
    if any(not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}', t.upper()) for t in tickers):
        raise ValueError('Invalid ticker symbol')
    age = record.get('max_age_days', 120)
    if isinstance(age,bool) or not isinstance(age,(float,int)) or not math.isfinite(age) or age < 0:
        raise ValueError('Invalid freshness limit')
    ident = record.get('id') or hashlib.sha256(json.dumps(
        [record['desk'], record['source_url'], observed.isoformat(), digest]).encode()).hexdigest()[:24]
    if not isinstance(ident, str) or not ident or len(ident) > 160:
        raise ValueError('Invalid research identifier')
    return {**record, 'id': ident, 'tickers': [t.upper() for t in tickers],
            'published_at': published.isoformat(), 'observed_at': observed.isoformat(),
            'asof': effective.isoformat(), 'source_host': url.hostname.lower(),
            'content_sha256': digest, 'provenance_checked': True, 'fact_checked': False}
