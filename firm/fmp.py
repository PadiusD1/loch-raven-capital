"""Financial Modeling Prep client — clean, dated fundamentals + confirmed
earnings, the accuracy upgrade over yfinance's screening-grade .info.

Design contract (2026-06-13, operator wired FMP for accuracy):
  - KEY-GATED: available() is False without a key -> every caller falls back
    to yfinance, so the fund is identical to before until a key is set.
  - FAIL-SOFT: any network/parse/HTTP error returns None; the caller falls
    back. A data-vendor hiccup must never break a trading cycle.
  - CACHED: one call per (endpoint, symbol) per hour — stays inside the free
    tier (~250/day) for ~12 names/cycle.
  - DEFENSIVE MAPPING: FMP field names vary slightly across plans/versions,
    so each metric tries several known key names. selftest() reports the live
    mapping for a one-time verification the first time a key is present.
"""
import json
import time
import urllib.parse
import urllib.request

import config

_cache: dict[str, tuple[float, object]] = {}
_CACHE_TTL_S = 3600.0
# Free tier is 200 calls/day. Stop at a margin so a cache miss or a manual
# selftest can never push the account over its quota — past the budget every
# caller transparently falls back to yfinance (operator: "only 200/day, be
# mindful"). Cache hits do NOT count.
DAILY_BUDGET = 180


def available() -> bool:
    return bool(config.FMP_API_KEY)


def _budget_left() -> int:
    from firm import state
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    u = state.load("fmp_usage", {})
    if u.get("date") != today:
        return DAILY_BUDGET
    return DAILY_BUDGET - int(u.get("count", 0))


def _spend(n: int = 1) -> None:
    from firm import state
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    u = state.load("fmp_usage", {})
    if u.get("date") != today:
        u = {"date": today, "count": 0}
    u["count"] = int(u.get("count", 0)) + n
    state.save("fmp_usage", u)


def _blocked_fields() -> set:
    """Fields the weekly vendor audit auto-disabled after sustained drift vs
    the cross-source check — FMP stops emitting them so the data_engine merge
    keeps yfinance's value instead. Self-healing: a rotted FMP field degrades
    to the second source rather than poisoning scores. Auto-cleared by the
    audit when the field re-agrees for VENDOR_AUDIT_STRIKES_TO_CLEAR weeks."""
    try:
        from firm import state
        return set(state.load("fmp_blocklist", {}).get("fields", []))
    except Exception:
        return set()


def _get(path: str, **params) -> object | None:
    """GET /stable/{path}; cached hourly; budget-guarded; None on any error
    (caller falls back to yfinance)."""
    if not config.FMP_API_KEY:
        return None
    params["apikey"] = config.FMP_API_KEY
    key = f"{path}?{urllib.parse.urlencode(sorted(params.items()))}"
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]            # cache hit — free, doesn't touch the quota
    if _budget_left() <= 0:
        return None              # quota guard — fall back rather than overspend
    url = f"{config.FMP_BASE}/{path}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "loch-raven/1.0"})
        with urllib.request.urlopen(req, timeout=config.FMP_TIMEOUT_S) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    _spend(1)
    if len(_cache) > 400:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:200]:
            _cache.pop(k, None)
    _cache[key] = (now, data)
    return data


def _first(d: dict, *names):
    """First present, finite value among candidate FMP field names."""
    for n in names:
        v = d.get(n)
        if isinstance(v, (int, float)) and v == v:
            return v
        if isinstance(v, str) and v:
            return v
    return None


def fundamentals(ticker: str, respect_blocklist: bool = True) -> dict | None:
    """Same schema as data_engine.fetch_fundamentals, sourced from FMP TTM
    ratios + key-metrics + profile (fresher and cleaner than yfinance .info),
    plus Piotroski/Altman quality scores yfinance doesn't provide. None if no
    key or FMP returns nothing for this symbol (caller falls back).

    respect_blocklist=True (default) drops any field the weekly vendor audit
    auto-disabled (the data_engine merge then keeps yfinance's value). The audit
    itself calls with respect_blocklist=False to re-test a blocked field for
    recovery."""
    if not available():
        return None
    # LEAN by design (200/day budget): only the two endpoints that carry the
    # accuracy-critical value/quality signal — ratios-ttm (PE/PEG/PS/margins/
    # ROE/leverage) + financial-scores (Piotroski/Altman, unavailable on
    # yfinance). sector/mktcap/fcf/growth are NOT accuracy-sensitive and stay
    # on yfinance via the merge in data_engine — saving 2 calls per name.
    # Field names VERIFIED against the live API 2026-06-13 (not guessed).
    ratios = _get("ratios-ttm", symbol=ticker)
    r = (ratios[0] if isinstance(ratios, list) and ratios else {}) or {}
    if not r:
        return None
    out = {"ticker": ticker, "source": "fmp"}
    out["pe"] = _first(r, "priceToEarningsRatioTTM")   # clean dated TTM, accurate
    # FWD P/E: ratios-ttm has NO forward field (forward needs analyst estimates,
    # not a TTM ratio). Do NOT fall back to trailing — that would mislabel
    # trailing as forward and mislead BOTH value_score AND the committee
    # (verified 2026-06-13: FMP fwd_pe == trailing for every name; NVDA real
    # forward ~20.6 vs trailing 31.2). Emit only if FMP genuinely has it;
    # otherwise the data_engine merge keeps yfinance's REAL forwardPE.
    _fpe = _first(r, "forwardPriceToEarningsRatioTTM")
    if _fpe is not None:
        out["fwd_pe"] = _fpe
    # PEG intentionally NOT sourced from FMP: priceToEarningsGrowthRatioTTM is
    # garbage for cyclicals/loss-makers (verified: Ford 0.03, PFE -3.46, JPM
    # 6.08 vs yfinance's saner 8.48/13.77/1.70). The merge keeps yfinance's
    # trailingPegRatio instead.
    out["ps"] = _first(r, "priceToSalesRatioTTM")
    out["profit_margin"] = _first(r, "netProfitMarginTTM")
    out["op_margin"] = _first(r, "operatingProfitMarginTTM")
    # D/E as a RAW RATIO (e.g. AAPL 0.80, Ford 4.26) — verified accurate vs
    # ground truth. This is the CANONICAL scale: yfinance's percentage form
    # (79.5) is normalized to a ratio in data_engine._fetch_fundamentals_yf, and
    # quality_score's thresholds are ratio-scale (0.3/0.8/2.0). Do not ×100.
    out["debt_to_equity"] = _first(r, "debtToEquityRatioTTM")
    # ROE lives in key-metrics-ttm, not ratios-ttm (verified) — 1 more call,
    # worth it: ROE is a core quality input
    km = _get("key-metrics-ttm", symbol=ticker)
    k = (km[0] if isinstance(km, list) and km else {}) or {}
    out["roe"] = _first(k, "returnOnEquityTTM")
    # book value PER SHARE — sign = stockholders'-equity sign; lets the sanitizer
    # drop ROE as not-meaningful on negative equity (2026-06-16). Same field in
    # both vendors so the merge is apples-to-apples.
    out["book_value"] = _first(k, "bookValuePerShareTTM")
    # Piotroski (0-9) + Altman Z — quality signals yfinance can't give
    scores = _get("financial-scores", symbol=ticker)
    sc = (scores[0] if isinstance(scores, list) and scores else {}) or {}
    out["piotroski"] = _first(sc, "piotroskiScore")
    out["altman_z"] = _first(sc, "altmanZScore")
    if respect_blocklist:
        for bf in _blocked_fields():
            out.pop(bf, None)        # degrade to yfinance for auto-disabled fields
    return out


def _earnings_rows(ticker: str) -> list[dict]:
    data = _get("earnings", symbol=ticker)
    return data if isinstance(data, list) else []


def next_earnings(ticker: str) -> dict | None:
    """Next confirmed earnings: date + session (BMO/AMC from FMP's 'time')."""
    if not available():
        return None
    import datetime as _dt
    rows = _earnings_rows(ticker)
    if not rows:
        return None
    today = _dt.datetime.now(_dt.timezone.utc).date()
    future = []
    for row in rows:
        d = row.get("date")
        if not d:
            continue
        try:
            dd = _dt.date.fromisoformat(str(d)[:10])
        except (ValueError, TypeError):
            continue
        if dd >= today:
            future.append((dd, row))
    if not future:
        return None
    future.sort()
    dd, row = future[0]
    t = str(row.get("time") or "").lower()
    sess = "BMO" if t in ("bmo", "before market open") else \
           "AMC" if t in ("amc", "after market close") else "unknown"
    return {"date": dd.isoformat(), "days_away": (dd - today).days, "session": sess}


def recent_earnings(ticker: str, within_days: int = 8) -> dict | None:
    """Last reported print with ACTUAL EPS vs estimate -> beat/miss."""
    if not available():
        return None
    import datetime as _dt
    rows = _earnings_rows(ticker)
    if not rows:
        return None
    today = _dt.datetime.now(_dt.timezone.utc).date()
    past = []
    for row in rows:
        d = row.get("date")
        if not d:
            continue
        try:
            dd = _dt.date.fromisoformat(str(d)[:10])
        except (ValueError, TypeError):
            continue
        if dd <= today and row.get("epsActual") is not None:
            past.append((dd, row))
    if not past:
        return None
    past.sort(reverse=True)
    dd, row = past[0]
    age = (today - dd).days
    if age > within_days:
        return None
    est = row.get("epsEstimated")
    act = row.get("epsActual")
    sup = None
    if isinstance(est, (int, float)) and isinstance(act, (int, float)) and est:
        sup = (act - est) / abs(est) * 100
    t = str(row.get("time") or "").lower()
    sess = "BMO" if t in ("bmo", "before market open") else \
           "AMC" if t in ("amc", "after market close") else "unknown"
    return {"reported_on": dd.isoformat(), "days_ago": age, "session": sess,
            "eps_estimate": round(est, 4) if isinstance(est, (int, float)) else None,
            "eps_reported": round(act, 4) if isinstance(act, (int, float)) else None,
            "surprise_pct": round(sup, 2) if sup is not None else None,
            "verdict": ("beat" if sup is not None and sup > 1
                        else "miss" if sup is not None and sup < -1
                        else "in-line" if sup is not None else None)}


def selftest(ticker: str = "AAPL") -> dict:
    """One-shot live verification of the field mapping — run once after a key
    is set to confirm FMP's current field names match the mapping above."""
    if not available():
        return {"available": False, "note": "no FMP_API_KEY set"}
    return {"available": True, "fundamentals": fundamentals(ticker),
            "next_earnings": next_earnings(ticker),
            "recent_earnings": recent_earnings(ticker)}
