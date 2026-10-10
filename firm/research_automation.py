"""Free-source planning, health and bounded local-cycle refresh.

The collector reads symbols from the LOCAL book, never publishes that book,
never calls an LLM/broker, and never mutates trading settings or model weights.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SYMBOL = re.compile(r'^[A-Z][A-Z0-9.-]{0,9}$')
_LOCK = threading.Lock()


def symbols(values):
    return list(dict.fromkeys(v.upper() for v in values if isinstance(v, str) and SYMBOL.fullmatch(v.upper())))


def universe_plan(priority=(), *, include_state=False, when=None):
    import config
    from scripts.refresh_research import UNIVERSE
    held, pending, watch = [], [], []
    if include_state:
        # Never interpret committed demo snapshots as the user's actual book.
        for path in sorted(Path(config.STATE_DIR).glob('sleeve_*.json')):
            try:
                obj = json.loads(path.read_text())
                for symbol, row in (obj.get('positions') or {}).items():
                    held.extend([row.get('underlying'), row.get('symbol'), symbol])
            except (ValueError, OSError, AttributeError, TypeError): continue
        try:
            obj=json.loads((Path(config.STATE_DIR)/'watchlist.json').read_text())
            rows=obj.get('items', obj) if isinstance(obj, dict) else {}
            if isinstance(rows, dict):
                for key,row in rows.items():
                    if isinstance(row, dict): watch.extend([row.get('ticker'), key])
        except (OSError, ValueError, TypeError): pass
    core = symbols([getattr(config,'BENCHMARK','SPY'),getattr(config,'CASH_ETF','BIL')]
        + list(getattr(config,'CORE_ELIGIBLE',[])) + list(getattr(config,'ROTATION_UNIVERSE',[]))
        + list(getattr(config,'ETF_UNIVERSE',[])))
    pinned = symbols(held + list(priority))
    stocks = symbols(list(UNIVERSE) + list(getattr(config,'STOCK_UNIVERSE',[])))
    # Rotate only the exploratory tail. Held/candidate names always come first.
    day = (when or datetime.now(timezone.utc)).date().toordinal()
    if stocks:
        offset=(day * 11) % len(stocks)
        stocks=stocks[offset:]+stocks[:offset]
    ordered=symbols(pinned + list(UNIVERSE) + watch[:12] + stocks)
    equities=[s for s in ordered if s not in core]
    sec_limit=max(7,min(int(getattr(config,'RESEARCH_SEC_SYMBOL_LIMIT',24)),40))
    price_limit=max(32,min(int(getattr(config,'RESEARCH_PRICE_SYMBOL_LIMIT',64)),100))
    price=symbols([getattr(config,'BENCHMARK','SPY')] + pinned + core + equities)[:price_limit]
    return {'sec':equities[:sec_limit], 'prices':price, 'priority':pinned,
            'deferred':sorted(set(equities[sec_limit:]) | (set(pinned)-set(price))),
            'rotation_policy':'held/candidates first; broad stock tail rotates daily',
            'private_state_read':bool(include_state)}


@contextmanager
def collection_lock(root: Path):
    """OS-released cross-process lock; no stale-lock deletion or race on rename."""
    root.mkdir(parents=True,exist_ok=True)
    handle=(root/'collection.lock').open('a+b')
    handle.seek(0); handle.write(b'0'); handle.flush(); handle.seek(0)
    acquired=False
    try:
        if os.name == 'nt':
            import msvcrt
            try: msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1); acquired=True
            except OSError: pass
        else:
            import fcntl
            try: fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB); acquired=True
            except OSError: pass
        yield acquired
    finally:
        if acquired:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(handle,fcntl.LOCK_UN)
        handle.close()


def health(root=None, *, asof=None, wanted=()):
    from firm.research_rag import load_records
    from firm.research_provenance import decision_time, DESKS
    now=decision_time(asof) if asof else datetime.now(timezone.utc)
    latest={}
    for r in load_records(root):
        if decision_time(r['observed_at']) > now: continue
        key=(r['desk'],r.get('topic_id') or r['source_url'])
        if key not in latest or r['observed_at'] > latest[key]['observed_at']:latest[key]=r
    desk={d:{'fresh':0,'stale':0,'methodology':0} for d in sorted(DESKS)}
    covered={}
    for r in latest.values():
        if r.get('scope')=='methodology': desk[r['desk']]['methodology']+=1;continue
        age=(now-decision_time(r['asof'])).total_seconds()/86400
        stale=age>float(r.get('max_age_days',120))
        desk[r['desk']]['stale' if stale else 'fresh']+=1
        for t in r.get('tickers',[]):
            if not stale: covered.setdefault(t,set()).add(r['desk'])
    gaps={t:sorted({'fundamental','sentiment','technical','quant'}-covered.get(t,set())) for t in symbols(wanted)}
    return {'asof':now.isoformat(),'latest_topics':len(latest),'desks':desk,
            'missing_or_stale_company_evidence':{t:v for t,v in gaps.items() if v},
            'no_training_or_trading':True}


def prepare_cycle(priority=()):
    """Bounded acquisition BEFORE research workers; failures leave old data intact.

    Uses a subprocess so the caller can enforce a hard wall-clock timeout.
    No source HTTP happens inside analyst/PM prompts, exit monitors or orders.
    """
    import config
    if not getattr(config,'RESEARCH_AUTO_REFRESH',False):return {'status':'disabled'}
    with _LOCK:
        from firm.research_rag import _root
        root=_root()
        marker=Path(config.STATE_DIR)/'research_cycle_refresh.json'
        requested=symbols(priority)
        now=datetime.now(timezone.utc)
        try:
            previous=json.loads(marker.read_text())
            age=(now-datetime.fromisoformat(previous['attempted_at'])).total_seconds()
            if 0<=age<300 or (0<=age<4*3600 and set(requested)<=set(previous.get('symbols',[])) and previous.get('ok')):
                return {'status':'cached_attempt','health':health(root,wanted=requested)}
        except (ValueError,KeyError,TypeError,OSError):pass
        marker.parent.mkdir(parents=True,exist_ok=True)
        stamp={'attempted_at':now.isoformat(),'symbols':requested,'ok':False}
        marker.write_text(json.dumps(stamp))
        try:
            command=[sys.executable,'-m','scripts.refresh_research','--output',str(root),
                     '--cache',str(Path(config.STATE_DIR)/'research_http_cache'),
                     '--report',str(Path(config.STATE_DIR)/'research_refresh_report.json'),
                     '--symbols',','.join(requested),'--runtime','--time-budget','90','--request-budget','100']
            result=subprocess.run(command,cwd=config.BASE_DIR,timeout=105,capture_output=True,text=True)
            stamp['returncode']=result.returncode
            # A usable partial collection must not be reported as fully healthy.
            report_path=Path(config.STATE_DIR)/'research_refresh_report.json'
            try:
                report=json.loads(report_path.read_text())
                if datetime.fromisoformat(report['started_at']) < now:
                    raise ValueError('Stale child report')
                stamp['source_status']=report.get('status','unknown')
                stamp['source_errors']=len(report.get('errors',[]))
                stamp['source_skips']=len(report.get('skipped',[]))
                stamp['ok']=result.returncode==0 and stamp['source_status']=='healthy'
            except (ValueError,KeyError,TypeError,OSError):
                stamp['source_status']='missing_or_stale_report'
                stamp['ok']=False
        except (OSError,subprocess.TimeoutExpired) as exc:
            stamp['error']=type(exc).__name__
        marker.write_text(json.dumps(stamp))
        return {'status':'refreshed' if stamp['ok'] else 'degraded',
                'source_status':stamp.get('source_status','failed'),
                'source_errors':stamp.get('source_errors',0),
                'source_skips':stamp.get('source_skips',0),
                'health':health(root,wanted=requested)}
