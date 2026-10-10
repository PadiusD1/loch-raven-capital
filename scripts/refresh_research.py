"""Collect source-linked financial research without an LLM or brokerage access.

python -m scripts.refresh_research --output data/research
SEC contact is read from SEC_USER_AGENT or this repository's public commit
history; it is sent only to SEC, not embedded in data. Other feeds need no key.
Failures are reported independently; a failed source never erases prior data.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import numpy as np

from firm.research_provenance import validate_research
from scripts.fetch_sec_research import extract_facts

UNIVERSE = {'AAPL': 320193, 'MSFT': 789019, 'NVDA': 1045810, 'AMD': 2488,
            'MU': 723125, 'CSCO': 858877, 'AMZN': 1018724}
SERIES = {'DGS2': '2-year Treasury yield percent', 'DGS10': '10-year Treasury yield percent',
          'FEDFUNDS': 'effective federal funds rate percent',
          'UNRATE': 'civilian unemployment rate percent',
          'CPIAUCSL': 'consumer price index all urban consumers seasonally adjusted',
          'T10Y2Y': '10-year minus 2-year Treasury yield spread percentage points'}
from firm.research_transport import HOSTS as ALLOWED_HOSTS


def _get(client, url, *, headers=None):
    from urllib.parse import urlparse
    if urlparse(url).hostname not in ALLOWED_HOSTS:
        raise ValueError('Unapproved source host')
    # No arbitrary redirects, cookies, credentials, user-supplied URLs or HTML execution.
    for attempt in range(3):
        response = client.get(url, headers=headers)
        if response.status_code not in (429, 500, 502, 503, 504):
            response.raise_for_status()
            if len(response.content) > 15_000_000:
                raise ValueError('Source response exceeds 15 MB')
            return response
        if attempt < 2:
            time.sleep(2 ** attempt)
    response.raise_for_status()


def _record(desk, url, title, body, observed, *, topic, tickers=(), effective=None,
            published=None, max_age=120, **extra):
    return validate_research(dict(desk=desk, source_url=url, title=title, body=body,
        observed_at=observed, published_at=published or observed, asof=effective or observed,
        topic_id=topic, tickers=list(tickers), max_age_days=max_age, **extra))


def sec_records(client, ticker, cik, observed, user_agent):
    url = f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json'
    response = _get(client, url, headers={'User-Agent': user_agent})
    observed = response.extensions.get("research_observed_at", observed)
    payload = response.json()
    if int(payload.get('cik', -1)) != cik:
        raise ValueError('SEC entity identity mismatch')
    rows = extract_facts(payload, observed_at=observed)
    grouped = defaultdict(list)
    for row in rows:
        if row['end'] and row['end'] <= observed[:10]:
            grouped[row['metric']].append(row)
    digest = hashlib.sha256(response.content).hexdigest()
    out = []
    for metric, facts in grouped.items():
        title = f'{ticker} {payload.get("entityName", "")} SEC {metric} filed financial facts'
        body = (f'{ticker} {metric}. Source: SEC company-reported XBRL. Values are absolute USD, '
                'unless the explicit unit is shares or USD/shares; never assume millions. Start/end identify duration versus instant facts. Never add overlapping '
                'year-to-date periods or mix original and amended filings.\n' + json.dumps(facts, sort_keys=True))
        out.append(_record('fundamental', url, title, body, observed,
            topic=f'sec:{ticker}:{metric}', tickers=[ticker],
            effective=max(f['end'] for f in facts)+'T00:00:00+00:00',
            facts=facts, raw_response_sha256=digest, extraction='SEC XBRL, no model-generated values',
            publication_precision='observed snapshot; each fact retains its filing date'))
    if not out:
        raise ValueError('No usable SEC facts')
    return out


def filing_records(client, ticker, cik, observed, user_agent):
    url = f'https://data.sec.gov/submissions/CIK{cik:010d}.json'
    response = _get(client, url, headers={'User-Agent': user_agent})
    observed = response.extensions.get("research_observed_at", observed)
    obj = response.json()
    if int(obj.get('cik', -1)) != cik or ticker not in obj.get('tickers', []):
        raise ValueError('SEC submission entity/ticker mismatch')
    recent = obj.get('filings', {}).get('recent', {})
    events = []
    for i, form in enumerate(recent.get('form', [])):
        if form not in ('8-K', '10-Q', '10-K', '8-K/A', '10-Q/A', '10-K/A', '4', '4/A'):
            continue
        accepted = recent.get('acceptanceDateTime', [])[i]
        accepted_time = datetime.fromisoformat(accepted.replace('Z', '+00:00'))
        if accepted_time.tzinfo is None:
            accepted_time = accepted_time.replace(tzinfo=timezone.utc)
        if accepted_time.astimezone(timezone.utc) > datetime.fromisoformat(observed):
            continue
        events.append({'form': form, 'filed': recent['filingDate'][i],
                       'accepted': accepted, 'accession': recent['accessionNumber'][i],
                       'document': recent['primaryDocument'][i],
                       'items': (recent.get('items') or ['']*len(recent['form']))[i]})
        if len(events) >= 12:
            break
    if not events:
        raise ValueError('No SEC filing events')
    body = (f'{ticker} recent primary disclosure/catalyst calendar from SEC submissions. '
            'These are filing metadata, NOT the filing text, a sentiment score, or proof of good/bad news. '
            'Form 4 is an insider report, not proof of a discretionary purchase; amendments and item codes are retained. '
            'Read the referenced document before drawing a directional conclusion.\n' + json.dumps(events))
    return [_record('sentiment', url, f'{ticker} SEC filing disclosure calendar', body, observed,
         topic=f'sec-events:{ticker}', tickers=[ticker], max_age=45, events=events,
         raw_response_sha256=hashlib.sha256(response.content).hexdigest())]


def fred_record(client, series, observed):
    start = '2016-01-01'  # stable URL/cache key; public macro history, not a fitted training set
    url = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}'
    response = _get(client, url)
    observed = response.extensions.get("research_observed_at", observed)
    rows = []
    for row in csv.DictReader(io.StringIO(response.text)):
        date = row.get('DATE') or row.get('observation_date')
        val = row.get(series)
        if not date or date > observed[:10] or val in (None, '', '.'):
            continue
        number = float(val)
        if math.isfinite(number):
            rows.append({'date': date, 'value': number})
    if not rows:
        raise ValueError('No FRED numeric observations')
    rows.sort(key=lambda r: r['date'])
    # Only government-origin series; no S&P/CBOE licensed histories redistributed.
    points = rows[-120:]
    body = (f'{series}: {SERIES[series]}. Latest-vintage observations downloaded from FRED. '
            'Observation dates are NOT publication dates. Available to this system only at observed_at; '
            'do not use this snapshot in earlier backtests.\n' + json.dumps(points))
    return _record('macro', url, f'FRED {series} {SERIES[series]}', body, observed,
        topic=f'fred:{series}', effective=points[-1]['date']+'T00:00:00+00:00',
        max_age=__import__('scripts.free_source_collection',fromlist=['EXTRA_SERIES']).EXTRA_SERIES.get(series,('',60))[1],
        source_tier='government_origin_via_FRED',
        observations=points, vintage='latest_at_observed_at_not_historical_vintage',
        raw_response_sha256=hashlib.sha256(response.content).hexdigest())


def chart(client, ticker, observed):
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2y'
    response = _get(client, url)
    observed = response.extensions.get('research_observed_at', observed)
    result = response.json()['chart']['result'][0]
    if result['meta']['symbol'].upper() != ticker:
        raise ValueError('Price symbol mismatch')
    times = result.get('timestamp', [])
    prices = result['indicators'].get('adjclose', [{}])[0].get('adjclose', [])
    if len(times) != len(prices):
        raise ValueError('Missing adjusted prices')
    from zoneinfo import ZoneInfo
    when=datetime.fromisoformat(observed).astimezone(ZoneInfo('America/New_York'))
    today=when.date().isoformat()
    cutoff=today if (when.hour,when.minute)>=(16,15) else (when.date()-timedelta(days=1)).isoformat()
    rows = {datetime.fromtimestamp(t, timezone.utc).astimezone(ZoneInfo('America/New_York')).date().isoformat(): float(p)
            for t,p in zip(times,prices) if p is not None and not isinstance(p,bool)
            and math.isfinite(float(p)) and p>0
            and datetime.fromtimestamp(t,timezone.utc).astimezone(ZoneInfo('America/New_York')).date().isoformat()<=cutoff}
    if len(rows) < 65:
        raise ValueError('Too few completed adjusted price observations')
    values=dict(sorted(rows.items()))
    # Quarantine gross adjusted-price dislocations; investigate rather than invent a split.
    arr=np.array(list(values.values()))
    if np.any(np.abs(np.diff(arr)/arr[:-1]) > .8):
        raise ValueError('Unconfirmed adjusted-price dislocation above 80 percent')
    return url, values, hashlib.sha256(response.content).hexdigest()


def price_records(ticker, series, benchmark, observed, url, digest):
    dates = sorted(set(series) & set(benchmark))
    if len(dates) < 65:
        raise ValueError('Insufficient matched benchmark dates')
    prices = np.array([series[d] for d in dates], dtype=float)
    spy = np.array([benchmark[d] for d in dates], dtype=float)
    ret, br = np.diff(prices)/prices[:-1], np.diff(spy)/spy[:-1]
    metrics = {'observations': len(prices), 'last_completed_session': dates[-1],
               'last_adjusted_close': round(float(prices[-1]), 6)}
    for n in (21, 63, 126, 252):
        if len(prices) > n:
            metrics[f'return_{n}d_pct'] = round(float(100*(prices[-1]/prices[-n-1]-1)), 4)
            metrics[f'excess_vs_SPY_{n}d_pp'] = round(float(100*(prices[-1]/prices[-n-1]-spy[-1]/spy[-n-1])), 4)
    for n in (50, 200):
        if len(prices) >= n:
            metrics[f'adjusted_sma_{n}'] = round(float(prices[-n:].mean()), 6)
    n = min(252, len(ret))
    sd = float(np.std(ret[-n:], ddof=1))
    variance = float(np.var(br[-n:], ddof=1))
    risk = {'return_observations': n, 'annualized_volatility_pct': round(sd*math.sqrt(252)*100, 4),
            'beta_to_SPY': round(float(np.cov(ret[-n:], br[-n:])[0, 1]/variance), 6) if variance > 0 else None,
            'correlation_to_SPY': round(float(np.corrcoef(ret[-n:], br[-n:])[0, 1]), 6)
                if sd > 0 and variance > 0 else None,
            'max_drawdown_252_sessions_pct': round(float(100*np.min(prices[-252:]/np.maximum.accumulate(prices[-252:])-1)), 4)}
    common = (f'{ticker} versus SPY; derived from aligned completed daily Yahoo adjusted-close observations. '
              'Adjusted close is not a tradable quote. This is descriptive historical data, not a '
              'tested return forecast. The snapshot is only available at observed_at.\n')
    return [_record(desk, url, f'{ticker} {label}', common+json.dumps(values, sort_keys=True), observed,
              topic=f'yahoo:{ticker}:{desk}', tickers=[ticker], effective=__import__('datetime').datetime.fromisoformat(dates[-1]+'T16:15:00').replace(
                  tzinfo=__import__('zoneinfo').ZoneInfo('America/New_York')).astimezone(timezone.utc).isoformat(),
              max_age=7, metrics=values, raw_response_sha256=digest, source_vendor='Yahoo Finance',
              raw_history_redistributed=False)
            for desk, label, values in [('technical', 'price trend momentum relative strength', metrics),
                                        ('quant', 'benchmark beta volatility correlation drawdown risk', risk)]]


def persist(records, root):
    root.mkdir(parents=True, exist_ok=True)
    path = root/'snapshots.jsonl'
    old = [validate_research(json.loads(s)) for s in path.read_text().splitlines() if s.strip()] if path.exists() else []
    latest={}
    for r in old:
        key=(r['desk'],r.get('topic_id'),r['source_url'])
        if key not in latest or r['observed_at']>latest[key]['observed_at']:latest[key]=r
    added=0
    for row in records:
        r=validate_research(row)
        key=(r['desk'],r.get('topic_id'),r['source_url'])
        previous=latest.get(key)
        if previous and r['observed_at']<previous['observed_at']:
            continue  # a cached older response cannot overwrite a newer snapshot
        quality_fields=('quality_warnings','source_tier','lineage_group','max_age_days')
        if (previous and previous['content_sha256']==r['content_sha256']
                and all(previous.get(k)==r.get(k) for k in quality_fields)):
            continue  # unchanged bytes AND quality; warnings themselves are evidence
        old.append(r);latest[key]=r;added+=1
    temp = path.with_suffix('.tmp')
    temp.write_text(''.join(json.dumps(r, sort_keys=True, allow_nan=False)+'\n' for r in old), encoding='utf-8')
    temp.replace(path)
    return added


def contact():
    if os.getenv('SEC_USER_AGENT'):
        return os.environ['SEC_USER_AGENT']
    try:
        emails = subprocess.check_output(['git', 'log', '--all', '--reverse', '--format=%ae'], text=True).splitlines()
        email = next(e for e in emails if re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', e)
                     and not e.endswith('noreply.github.com'))
        return 'LochRavenCapital research '+email
    except (OSError, subprocess.CalledProcessError, StopIteration):
        return ''


def refresh(root: Path, **kwargs) -> dict:
    from scripts.free_source_collection import collect
    return collect(root, **kwargs)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('data/research'))
    parser.add_argument('--report',type=Path,default=Path('data/research_refresh_report.json'))
    parser.add_argument('--cache',type=Path)
    parser.add_argument('--symbols',default='')
    parser.add_argument('--runtime',action='store_true')
    parser.add_argument('--request-budget',type=int,default=180)
    parser.add_argument('--time-budget',type=float,default=480)
    args=parser.parse_args()
    result=refresh(args.output,priority=args.symbols.split(','),runtime=args.runtime,cache=args.cache,
                   request_budget=args.request_budget,time_budget=args.time_budget)
    args.report.parent.mkdir(parents=True,exist_ok=True)
    temp=args.report.with_suffix('.tmp')
    temp.write_text(json.dumps(result,indent=2)+'\n');temp.replace(args.report)
    if not result.get('records_collected') and result.get('status')!='busy':
        raise SystemExit('No source succeeded; old corpus retained and error report written')
