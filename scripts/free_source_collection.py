"""Orchestrate bounded free-feed collection. Called by refresh_research CLI."""
from __future__ import annotations

import calendar
import hashlib
import json
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx

from firm.research_automation import collection_lock, universe_plan, health
from firm.research_transport import SourceClient, BudgetExceeded

# Government-origin series only. Do not silently add licensed ICE/S&P/CBOE data.
EXTRA_SERIES = {
    'DGS3MO':('3-month Treasury yield percent',10),
    'DGS30':('30-year Treasury yield percent',10),
    'DFII10':('10-year inflation-indexed Treasury real yield percent',10),
    'PCEPI':('personal consumption expenditure price index',75),
    'PCEPILFE':('core personal consumption expenditure price index',75),
    'GDPC1':('real gross domestic product billions chained 2017 dollars seasonally adjusted annual rate',150),
    'PAYEMS':('total nonfarm payroll employment thousands seasonally adjusted',75),
    'ICSA':('initial unemployment insurance claims seasonally adjusted',21),
    'INDPRO':('industrial production index 2017 equals 100 seasonally adjusted',75),
    'RSAFS':('advance retail and food services sales millions dollars seasonally adjusted',75),
    'HOUST':('new privately owned housing starts thousands annual rate seasonally adjusted',75),
    'WALCL':('Federal Reserve total assets millions dollars Wednesday level',21),
    'M2SL':('M2 money stock billions dollars seasonally adjusted',75),
    'DTWEXBGS':('nominal broad US dollar index January 2006 equals 100',14),
}
BLS_SERIES = {
    'LNS14000000':('unemployment rate percent seasonally adjusted','UNRATE',1.0),
    'CES0000000001':('total nonfarm payrolls thousands seasonally adjusted','PAYEMS',1.0),
    'CUSR0000SA0':('consumer price index all urban consumers seasonally adjusted','CPIAUCSL',1.0),
}
FED_FEEDS = ('https://www.federalreserve.gov/feeds/press_monetary.xml',
             'https://www.federalreserve.gov/feeds/speeches.xml')


def cik_lookup(client, ua, observed):
    from scripts import refresh_research as feeds
    url='https://www.sec.gov/files/company_tickers.json'
    response=client.get(url,headers={'User-Agent':ua})
    obj=response.json()
    rows=obj.values() if isinstance(obj,dict) else []
    out=dict(feeds.UNIVERSE)
    for row in rows:
        if not isinstance(row,dict):continue
        t=row.get('ticker');c=row.get('cik_str')
        if isinstance(t,str) and isinstance(c,int) and not isinstance(c,bool) and 0<c<10**10:
            from firm.research_automation import symbols
            if symbols([t]):out[t.upper()]=c
    if len(out)<=len(feeds.UNIVERSE):raise ValueError('SEC symbol directory empty or invalid')
    return out


def bls_record(client, series):
    from scripts.refresh_research import _record
    url='https://api.bls.gov/publicAPI/v1/timeseries/data/'+series
    response=client.get(url)
    obj=response.json()
    if obj.get('status')!='REQUEST_SUCCEEDED':raise ValueError('BLS request did not succeed')
    result=obj.get('Results',{})
    if isinstance(result,list):result=result[0] if result else {}
    matches=[r for r in result.get('series',[]) if r.get('seriesID')==series]
    if len(matches)!=1:raise ValueError('BLS series identity mismatch')
    observed=response.extensions['research_observed_at']
    out=[]
    for row in matches[0].get('data',[]):
        period=row.get('period','')
        if not (len(period)==3 and period[0]=='M' and period[1:].isdigit()):continue
        month=int(period[1:])
        if not 1<=month<=12:continue # M13 annual aggregate must NOT be treated as a month.
        year=int(row['year']);date=f'{year:04d}-{month:02d}-01'
        if date>observed[:10]:continue
        raw=str(row.get('value','')).strip()
        if raw.upper() in ('','-','--','N/A','NA','(NA)'):continue
        try:value=float(raw)
        except (ValueError,TypeError):raise ValueError('BLS non-numeric observation') from None
        if not math.isfinite(value):continue
        out.append({'date':date,'value':value,'footnotes':row.get('footnotes',[])})
    points=sorted(out,key=lambda r:r['date'])[-36:]
    if not points:raise ValueError('No eligible BLS monthly data')
    label,fred,_=BLS_SERIES[series]
    return _record('macro',url,f'BLS {series} {label}',
        f'BLS original statistical series {series}, {label}. Monthly observation dates are not release dates. '
        'Preliminary footnotes retained. This and its FRED mirror are ONE underlying source, not two independent confirmations.\n'
        +json.dumps(points,sort_keys=True),observed,topic='bls:'+series,max_age=75,
        effective=points[-1]['date']+'T00:00:00+00:00',observations=points,
        mirror_series=fred,lineage_group='BLS:'+series,source_tier='primary_government',
        raw_response_sha256=hashlib.sha256(response.content).hexdigest())


def fed_records(client,url):
    from scripts.refresh_research import _record
    response=client.get(url)
    if b'<!DOCTYPE' in response.content.upper() or b'<!ENTITY' in response.content.upper():
        raise ValueError('Unsafe XML declaration')
    root=ET.fromstring(response.content)
    observed=response.extensions['research_observed_at']
    when=datetime.fromisoformat(observed)
    out=[]
    for item in root.findall('.//item'):
        title=(item.findtext('title') or '').strip()
        link=(item.findtext('link') or '').strip()
        raw=item.findtext('pubDate') or ''
        if urlparse(link).scheme!='https' or urlparse(link).hostname!='www.federalreserve.gov':continue
        try:pub=parsedate_to_datetime(raw)
        except (ValueError,TypeError):continue
        if pub.tzinfo is None or pub>when or not title:continue
        # Feed-supplied summaries only, never recursively crawl arbitrary links.
        desc=' '.join((item.findtext('description') or '').split())[:1800]
        for desk in ('macro','sentiment'):
            out.append(_record(desk,link,'Federal Reserve: '+title,
                'Federal Reserve official announcement. Publication is a policy/catalyst event, '
                'not an automatic bullish or bearish investment signal.\n'+title+'\n'+desc,
                observed,topic='fed:'+link+':'+desk,published=pub.isoformat(),effective=pub.isoformat(),
                max_age=60,scope='market',source_tier='primary_government',
                lineage_group=link,raw_response_sha256=hashlib.sha256(response.content).hexdigest()))
        if len(out)>=12:break
    if not out:raise ValueError('No dated Federal Reserve feed entries')
    return out


def reconciliation(records):
    """Check BLS versus its FRED mirror, NOT independent source corroboration.

    Warnings do not silently choose a vendor; revisions/vintage timing may differ.
    """
    latest={r.get('topic_id'):r for r in records}
    out=[]
    for series,(_,mirror,scale) in BLS_SERIES.items():
        a=latest.get('bls:'+series); b=latest.get('fred:'+mirror)
        if not a or not b:continue
        x={r['date']:r['value'] for r in a['observations']}
        y={r['date']:r['value'] for r in b['observations']}
        dates=sorted(set(x)&set(y))
        if not dates:continue
        date=dates[-1];diff=abs(x[date]*scale-y[date])
        tolerance=max(.001,abs(y[date])*.00001)
        out.append({'series':series,'mirror':mirror,'date':date,'difference':round(diff,6),
                    'status':'match' if diff<=tolerance else 'vintage_or_value_mismatch',
                    'independent_sources':False})
    return out


def collect(root:Path, *, priority=(), runtime=False, cache=None, request_budget=180,time_budget=480):
    from scripts import refresh_research as feeds
    from firm.research_provenance import validate_research
    if not (1<=request_budget<=300 and 1<=time_budget<=900):
        raise ValueError('Collection budgets outside hard bounds')
    started=datetime.now(timezone.utc).isoformat()
    plan=universe_plan(priority,include_state=runtime)
    cache=Path(cache or root.parent/'research_http_cache')
    with collection_lock(cache) as acquired:
        if not acquired:return {'status':'busy','records_collected':0,'errors':[],'models_trained':0}
        records=[];errors=[];success=[];skipped=[]
        def attempt(label,fn):
            try:
                value=fn();batch=value if isinstance(value,list) else [value]
                # Validate ALL rows before this source batch is eligible for persistence.
                received=datetime.now(timezone.utc).isoformat()
                normalized=[]
                for r in batch:
                    r=dict(r);r['source_observed_at']=r['observed_at']
                    r['observed_at']=received;r.pop('id',None)
                    normalized.append(validate_research(r))
                batch=normalized
                records.extend(batch);success.append(label)
            except (ValueError,KeyError,TypeError,IndexError,OverflowError,httpx.HTTPError,ET.ParseError,BudgetExceeded) as exc:
                errors.append({'source':label,'error':type(exc).__name__,
                               'detail':(str(exc)[:160] if isinstance(exc,ValueError)
                                         and str(exc).startswith(('BLS ','No eligible BLS'))
                                         else 'Source failed validation or retrieval; prior good snapshots retained')})
        ua=feeds.contact()
        with SourceClient(cache,max_requests=request_budget,seconds=time_budget) as client:
            mapping=dict(feeds.UNIVERSE)
            if ua:
                try:mapping=cik_lookup(client,ua,started)
                except (ValueError,KeyError,TypeError,httpx.HTTPError,BudgetExceeded):
                    errors.append({'source':'SEC symbol directory','error':'DirectoryUnavailable','detail':'Known CIKs retained; unknowns deferred'})
            # Price/filing priority changes by job type; same finite shared budget.
            def prices():
                try:
                    _,benchmark,_=feeds.chart(client,'SPY',started)
                except (ValueError,KeyError,TypeError,IndexError,httpx.HTTPError,BudgetExceeded):
                    errors.append({'source':'Yahoo SPY','error':'BenchmarkUnavailable'});return
                for ticker in plan['prices']:
                    def one(t=ticker):
                        url,series,digest=feeds.chart(client,t,started)
                        return feeds.price_records(t,series,benchmark,datetime.now(timezone.utc).isoformat(),url,digest)
                    attempt('Yahoo '+ticker,one)
            if runtime:prices()
            for ticker in plan['sec']:
                cik=mapping.get(ticker)
                if not ua or not cik:
                    skipped.append({'ticker':ticker,'source':'SEC','reason':'missing_contact_or_cik'});continue
                attempt('SEC facts '+ticker,lambda t=ticker,c=cik:feeds.sec_records(client,t,c,started,ua))
                attempt('SEC filings '+ticker,lambda t=ticker,c=cik:feeds.filing_records(client,t,c,started,ua))
            feeds.SERIES.update({s:v[0] for s,v in EXTRA_SERIES.items()})
            for series in feeds.SERIES:
                attempt('FRED '+series,lambda s=series:feeds.fred_record(client,s,started))
            for series in BLS_SERIES:
                attempt('BLS '+series,lambda s=series:bls_record(client,s))
            for url in FED_FEEDS:attempt('Federal Reserve '+url.rsplit('/',1)[-1],lambda u=url:fed_records(client,u))
            if not runtime:prices()
            stats=dict(client.counts);events=client.events
        checks=reconciliation(records)
        mismatches=[c for c in checks if c['status']!='match']
        for r in records:
            if any(r.get('topic_id') in ('bls:'+c['series'],'fred:'+c['mirror']) for c in mismatches):
                r['quality_warnings']=['BLS/FRED mirror mismatch: check release vintage before using figures']
        added=feeds.persist(records,root)
        finished=datetime.now(timezone.utc).isoformat()
        report={'started_at':started,'observed_at':finished,'status':'healthy' if not errors and not skipped and not mismatches else 'degraded',
            'added':added,'records_collected':len(records),'successful_sources':success,'errors':errors,'skipped':skipped,
            'universe':plan,'transport':stats,'source_checks':events,'reconciliation':checks,
            'health':health(root,asof=finished,wanted=plan['sec']),
            'models_trained':0,'llm_calls':0,'broker_calls':0,
            'limits':{'requests':request_budget,'seconds':time_budget},
            'provider_terms':'Government-origin allowlist; Yahoo best-effort derived research only; no raw price-history redistribution'}
        # Bound private response cache storage; snapshots are kept point-in-time.
        metas=sorted(cache.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        total=0
        for p in metas:
            body=p.with_suffix('.body')
            total+=p.stat().st_size+(body.stat().st_size if body.exists() else 0)
            if total>256_000_000: p.unlink(missing_ok=True);body.unlink(missing_ok=True)
        print(json.dumps({k:v for k,v in report.items() if k not in ('source_checks','health')},indent=2))
        return report
