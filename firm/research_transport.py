"""Bounded, cached HTTP for free research sources. No model or broker calls.

Cache metadata contains no credentials. Observed-at belongs to the bytes, while
checked-at is the time a publisher confirms that the bytes are unchanged.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

HOSTS = frozenset(('data.sec.gov', 'www.sec.gov', 'fred.stlouisfed.org',
                  'query1.finance.yahoo.com', 'www.federalreserve.gov', 'api.bls.gov'))
MAX_BYTES = 15_000_000


class BudgetExceeded(RuntimeError):
    pass


class SourceClient:
    """Sequential public GET transport with time/request limits and TTL cache.

    Failures NEVER renew a cache entry or erase its previous body. HTTP 429
    respects Retry-After; excessive waits trip the budget instead of hammering.
    This transport must be used under the collector's single-writer lock.
    """
    def __init__(self, cache: Path, *, max_requests=180, seconds=480,
                 transport=None, clock=None, monotonic=None, sleep=None):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.deadline = self.monotonic() + max(1, min(float(seconds), 900))
        self.limit = max(1, min(int(max_requests), 300))
        self.http = httpx.Client(timeout=15, follow_redirects=False, transport=transport,
            headers={'User-Agent': 'LochRavenCapitalResearch/2.0'})
        self.counts = Counter()
        self.last = {}
        self.events = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.http.close()

    @staticmethod
    def ttl(url):
        p = urlparse(url)
        if p.path.endswith('company_tickers.json'):
            return 7 * 86400
        if p.hostname == 'data.sec.gov':
            return 8 * 3600 if '/companyfacts/' in p.path else 2 * 3600
        if p.hostname == 'api.bls.gov':
            return 12 * 3600
        if p.hostname == 'www.federalreserve.gov':
            return 2 * 3600
        return 4 * 3600

    def _budget(self, wait=0):
        if self.counts['requests'] >= self.limit or self.monotonic() + wait >= self.deadline:
            raise BudgetExceeded('Research request/time budget exhausted')

    def _cached(self, url):
        key = hashlib.sha256(url.encode()).hexdigest()
        path = self.cache / (key + '.json')
        try:
            row = json.loads(path.read_text())
            content = (self.cache / (key + '.body')).read_bytes()
            if len(content) > MAX_BYTES or hashlib.sha256(content).hexdigest() != row['sha256']:
                return key, None
            if row['url'] != url:
                return key, None
            for k in ('observed_at', 'checked_at'):
                dt = datetime.fromisoformat(row[k])
                if dt.tzinfo is None or dt > self.clock():
                    return key, None
            row['content'] = content
            return key, row
        except (OSError, ValueError, KeyError, TypeError):
            return key, None

    def _response(self, url, row, status):
        self.events.append({'url': url, 'status': status, 'observed_at': row['observed_at'],
                            'checked_at': row['checked_at']})
        return httpx.Response(200, content=row['content'], request=httpx.Request('GET', url),
            headers={'content-type': row.get('content_type', '')},
            extensions={'research_observed_at': row['observed_at'],
                        'research_checked_at': row['checked_at']})

    def _write(self, key, row):
        body = self.cache / (key + '.body')
        tmp = body.with_suffix('.tmp')
        tmp.write_bytes(row['content']); tmp.replace(body)
        meta = self.cache / (key + '.json')
        tmp = meta.with_suffix('.tmp')
        tmp.write_text(json.dumps({k:v for k,v in row.items() if k != 'content'})); tmp.replace(meta)

    def get(self, url, *, headers=None):
        p = urlparse(url)
        if p.scheme != 'https' or p.hostname not in HOSTS or p.username or p.password or p.port not in (None, 443):
            raise ValueError('Source URL not allowlisted')
        key, cached = self._cached(url)
        if cached and (self.clock()-datetime.fromisoformat(cached['checked_at'])).total_seconds() < self.ttl(url):
            self.counts['cache_hits'] += 1
            return self._response(url, cached, 'cache')
        request_headers = dict(headers or {})
        if cached:
            if cached.get('etag'): request_headers['If-None-Match'] = cached['etag']
            if cached.get('last_modified'): request_headers['If-Modified-Since'] = cached['last_modified']
        for attempt in range(3):
            # <=2 requests/sec per host, including retries. SEC documents <=10.
            wait = max(0., 0.5 - (self.monotonic()-self.last.get(p.hostname, -1e9)))
            self._budget(wait)
            if wait: self.sleep(wait)
            self.counts['requests'] += 1
            self.counts['host:'+p.hostname] += 1
            self.last[p.hostname] = self.monotonic()
            try:
                with self.http.stream('GET', url, headers=request_headers,
                                      timeout=min(15., max(.1, self.deadline-self.monotonic()))) as response:
                    status = response.status_code
                    if status == 304 and cached:
                        cached['checked_at'] = self.clock().isoformat()
                        self._write(key, cached); self.counts['not_modified'] += 1
                        return self._response(url, cached, 'not_modified')
                    if status in (429, 500, 502, 503, 504):
                        retry = response.headers.get('Retry-After', '')
                        delay = float(2 ** attempt)
                        if retry:
                            try: delay = max(delay, float(retry))
                            except ValueError:
                                try: delay = max(delay, (parsedate_to_datetime(retry)-self.clock()).total_seconds())
                                except (ValueError, TypeError): pass
                        if not math.isfinite(delay) or delay > 60:
                            raise BudgetExceeded('Publisher requests a longer cooldown; retry next run')
                        if attempt == 2: response.raise_for_status()
                        self._budget(delay); self.sleep(delay)
                        self.counts['retries'] += 1
                        continue
                    if 300 <= status < 400:
                        raise ValueError('Publisher redirect rejected')
                    response.raise_for_status()
                    content = bytearray()
                    for part in response.iter_bytes():
                        content.extend(part)
                        if len(content) > MAX_BYTES: raise ValueError('Source exceeds 15 MB')
                        if self.monotonic() >= self.deadline: raise BudgetExceeded('Read time budget exceeded')
                    now = self.clock().isoformat()
                    digest = hashlib.sha256(content).hexdigest()
                    row = {'url': url, 'content': bytes(content), 'sha256': digest,
                           'observed_at': cached['observed_at'] if cached and cached['sha256']==digest else now,
                           'checked_at': now, 'etag': response.headers.get('ETag'),
                           'last_modified': response.headers.get('Last-Modified'),
                           'content_type': response.headers.get('Content-Type', '')}
                    self._write(key, row)
                    return self._response(url, row, 'download')
            except httpx.TransportError:
                if attempt == 2: raise
                self._budget(2 ** attempt); self.sleep(2 ** attempt)
                self.counts['retries'] += 1
        raise RuntimeError('No valid response')
