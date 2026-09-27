"""Full-market universe — every listed US common stock, no name missed.

Pulls the complete NYSE/NASDAQ/AMEX symbol directories (~10k symbols),
then runs a tiered funnel so the whole market is screened daily while
Opus research goes to the strongest names in EVERY cap tier:

  Stage 1  all listed common stocks (structural junk excluded: warrants,
           rights, units, preferreds, test issues)
  Stage 2  tradability floor: price >= $1, 60d avg dollar volume >= $500k
  Stage 3  quant score every survivor (momentum/trend/RSI/rel-strength)
  Stage 4  market-cap tiers on the top names -> guaranteed research slots
           per tier (large >= $10B, mid $2-10B, small < $2B)

The scan is cached for SCAN_CACHE_HOURS so one full-market sweep per day
feeds every cycle. Failures degrade gracefully chunk by chunk — a bad
batch never kills the scan.
"""
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

import numpy as np
import pandas as pd
import yfinance as yf

import config
from firm import state

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# Structural non-equity instruments (not "small caps" — not common stock at all)
_BAD_NAME_PARTS = ("warrant", "right", " unit", "units ", "preferred",
                   "depositary", "notes due", "subordinated", "trust preferred")

TIER_BOUNDS = {"large": 10e9, "mid": 2e9}  # >=10B large, >=2B mid, else small


def _parse_listing(text: str, symbol_col: int, etf_col: int, test_col: int,
                   want_etfs: bool = False) -> list[str]:
    """want_etfs=False -> common stocks only (the validated scan input,
    unchanged). want_etfs=True -> the ETF rows this parser used to DISCARD
    (operator 2026-07-16: Public trades the full ETF landscape, not the
    hardcoded 20 — the raw material was already in these files)."""
    out = []
    lines = text.strip().splitlines()
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) <= max(symbol_col, etf_col, test_col) or "File Creation" in line:
            continue
        sym, etf, test = parts[symbol_col].strip(), parts[etf_col].strip(), parts[test_col].strip()
        name = parts[1].lower()
        if test == "Y" or not sym:
            continue
        if (etf == "Y") != want_etfs:
            continue
        if not want_etfs and any(b in name for b in _BAD_NAME_PARTS):
            continue
        if "$" in sym or len(sym) > 5:        # preferred/when-issued share classes
            continue
        out.append(sym.replace(".", "-"))     # Yahoo format (BRK.B -> BRK-B)
    return out


def fetch_all_us_symbols(max_age_days: float = 1.0) -> list[str]:
    """Every US-listed common stock symbol. Re-pulled DAILY (was weekly, which made
    any stock listed in the last week invisible to the 'no name missed' scan —
    change-audit verify 2026-06-15). The exchange directory files are tiny + server-
    cached, so a 1-day TTL is cheap and lets a new listing enter the next morning."""
    cache = state.load("universe_symbols", {})
    if cache.get("symbols") and cache.get("asof"):
        try:
            age_days = (datetime.now(timezone.utc)
                        - datetime.fromisoformat(cache["asof"])).total_seconds() / 86400
            if 0 <= age_days < max_age_days:
                return cache["symbols"]
        except (ValueError, TypeError):
            pass
    symbols: set[str] = set()
    try:
        with urllib.request.urlopen(NASDAQ_LISTED, timeout=30) as r:
            symbols |= set(_parse_listing(r.read().decode("utf-8", "replace"), 0, 6, 3))
        with urllib.request.urlopen(OTHER_LISTED, timeout=30) as r:
            symbols |= set(_parse_listing(r.read().decode("utf-8", "replace"), 0, 4, 6))
    except Exception as e:
        state.journal("universe_fetch_error", error=str(e))
        if cache.get("symbols"):
            return cache["symbols"]          # stale beats empty
        return list(config.STOCK_UNIVERSE)   # last-resort fallback
    out = sorted(symbols)
    _prev = state.load("universe_symbols", {}) or {}
    state.save("universe_symbols", {"asof": datetime.now(timezone.utc).isoformat(),
                                    "count": len(out), "symbols": out,
                                    # preserve the ETF slice (2026-07-16) — the
                                    # two fetchers share one cache file
                                    "etfs": _prev.get("etfs") or []})
    state.journal("universe_refreshed", count=len(out))
    return out


def fetch_all_us_etfs(max_age_days: float = 1.0) -> list[str]:
    """Every US-listed ETF symbol (the rows fetch_all_us_symbols filters out).
    Same daily-TTL cache file, separate key — one download serves both."""
    cache = state.load("universe_symbols", {})
    # Freshness reads the ETF slice's OWN stamp (etfs_asof), falling back to
    # the legacy shared asof. (2026-08-14 IG follow-up): the shared asof
    # belongs to the stocks-side scan and the old setdefault below never
    # renewed it after an ETF refresh — so once it crossed the TTL, EVERY
    # call refetched both NASDAQ symbol files and journaled
    # universe_etfs_refreshed (observed: 4,061 events in one day, one every
    # 1-2s during allocation), densifying the journal the coverage-guarded
    # windows read.
    _etf_ts = cache.get("etfs_asof") or cache.get("asof")
    if cache.get("etfs") and _etf_ts:
        try:
            age_days = (datetime.now(timezone.utc)
                        - datetime.fromisoformat(_etf_ts)).total_seconds() / 86400
            if 0 <= age_days < max_age_days:
                return cache["etfs"]
        except (ValueError, TypeError):
            pass
    # NEGATIVE CACHE (2026-09-01, fix brief 19 class). A failed directory
    # fetch returned the stale list WITHOUT stamping anything, so the very next
    # call — allocation._is_leveraged runs one per candidate, per health sweep,
    # per seat review — re-fetched, re-failed (2 x 30 s timeouts) and
    # re-journaled: 2,562 universe_etf_fetch_error rows in 7 days, 71% of the
    # journal's retention, which is what blinded four journal-windowed health
    # checks, plus a 21 s stall per call inside the trading cycle. After a
    # failure, serve the cached list for ETF_DIRECTORY_RETRY_MIN, journal ONCE
    # per retry window, and count the failures on the cache record.
    _failed_at = cache.get("etfs_fetch_failed_at")
    if cache.get("etfs") and _failed_at:
        try:
            _fail_age_min = (datetime.now(timezone.utc)
                             - datetime.fromisoformat(_failed_at)).total_seconds() / 60
            if 0 <= _fail_age_min < float(getattr(config, "ETF_DIRECTORY_RETRY_MIN", 60.0)):
                return cache["etfs"]
        except (ValueError, TypeError):
            pass
    etfs: set[str] = set()
    _etf_names_cache: dict[str, str] = {}

    def _harvest(text: str, symbol_col: int, etf_col: int, test_col: int) -> None:
        for line in text.strip().splitlines()[1:]:
            parts = line.split("|")
            if len(parts) <= max(symbol_col, etf_col, test_col) \
                    or "File Creation" in line:
                continue
            sym = parts[symbol_col].strip()
            if (parts[etf_col].strip() != "Y" or parts[test_col].strip() == "Y"
                    or not sym or "$" in sym or len(sym) > 5):
                continue
            sym = sym.replace(".", "-")
            etfs.add(sym)
            _etf_names_cache[sym] = parts[1].strip()

    try:
        with urllib.request.urlopen(NASDAQ_LISTED, timeout=30) as r:
            _harvest(r.read().decode("utf-8", "replace"), 0, 6, 3)
        with urllib.request.urlopen(OTHER_LISTED, timeout=30) as r:
            _harvest(r.read().decode("utf-8", "replace"), 0, 4, 6)
    except Exception as e:
        _fails = int(cache.get("etfs_fetch_fail_n") or 0) + 1
        state.journal("universe_etf_fetch_error", error=str(e)[:120],
                      consecutive=_fails,
                      retry_in_min=float(getattr(config, "ETF_DIRECTORY_RETRY_MIN", 60.0)))
        try:
            cache = state.load("universe_symbols", {}) or {}
            cache["etfs_fetch_failed_at"] = datetime.now(timezone.utc).isoformat()
            cache["etfs_fetch_fail_n"] = _fails
            state.save("universe_symbols", cache)
        except Exception:
            pass
        return cache.get("etfs") or list(config.ETF_UNIVERSE)
    out = sorted(etfs)
    cache = state.load("universe_symbols", {})   # re-read: stocks may be fresher
    cache["etfs"] = out
    cache["etf_names"] = _etf_names_cache
    cache.pop("etfs_fetch_failed_at", None)      # a success clears the negative cache
    cache.pop("etfs_fetch_fail_n", None)
    # Stamp the ETF slice's own freshness UNCONDITIONALLY; leave the shared
    # asof to the stocks-side writer. setdefault here was the refetch-loop
    # bug (2026-08-14): it preserved a stale stocks stamp, so a successful
    # ETF refresh never counted as fresh.
    cache["etfs_asof"] = datetime.now(timezone.utc).isoformat()
    cache.setdefault("asof", datetime.now(timezone.utc).isoformat())
    state.save("universe_symbols", cache)
    state.journal("universe_etfs_refreshed", count=len(out))
    return out


def etf_names() -> dict[str, str]:
    """{symbol: listing name} for ETFs — the leverage/inverse classifier's
    input (allocation.py seats must be 1x products only)."""
    fetch_all_us_etfs()
    return (state.load("universe_symbols", {}) or {}).get("etf_names") or {}


def _chunk_metrics(closes: pd.DataFrame, volumes: pd.DataFrame,
                   bench_3m: float, seen: set[str]) -> dict[str, dict]:
    """Technicals + liquidity for one downloaded chunk.
    Adds every symbol that returned real data to `seen` so the retry pass
    can re-fetch only symbols the provider actually failed on."""
    out = {}
    for t in closes.columns:
        s = closes[t].dropna()
        if len(s) >= 5:
            seen.add(t)            # data arrived; absence later = filtered, not missed
        if len(s) < 120:
            continue
        px = float(s.iloc[-1])
        if px < config.MIN_PRICE:
            continue
        v = volumes[t].dropna() if t in volumes.columns else pd.Series(dtype=float)
        # MEDIAN, not mean (2026-06-10 audit): one squeeze day dominates a
        # 60d mean — the floor must reflect the liquidity you can actually
        # exit into on a normal day
        dollar_vol = float((v.tail(60) * s.tail(60)).median()) if len(v) >= 20 else 0.0
        if dollar_vol < config.MIN_AVG_DOLLAR_VOL:
            continue

        def ret(days: int) -> float:
            return float(s.iloc[-1] / s.iloc[-1 - days] - 1) * 100 if len(s) > days else 0.0

        delta = s.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = (100 - 100 / (1 + rs)).dropna()
        sma50 = float(s.rolling(50).mean().iloc[-1])
        sma200 = float(s.rolling(200).mean().iloc[-1]) if len(s) >= 200 else sma50
        rets = s.pct_change().dropna()
        out[t] = {
            "price": px,
            "ret_1m": round(ret(21), 2), "ret_3m": round(ret(63), 2),
            "ret_6m": round(ret(126), 2),
            "ret_12m": round(ret(252), 2) if len(s) > 252 else round(ret(len(s) - 2), 2),
            **({"ret_12m_partial": True} if len(s) <= 252 else {}),
            "rsi14": round(float(rsi.iloc[-1]), 1) if len(rsi) else 50.0,
            "above_sma50": bool(px > sma50), "above_sma200": bool(px > sma200),
            "vol_ann_pct": round(float(rets.tail(63).std() * np.sqrt(252)) * 100, 1),
            "max_dd_2y_pct": round(float(((s / s.cummax()) - 1).min()) * 100, 1),
            "rel_strength_3m": round(ret(63) - bench_3m, 2),
            "avg_dollar_vol": round(dollar_vol),
        }
    return out


def _market_caps(tickers: list[str]) -> dict[str, float]:
    def one(t: str) -> tuple[str, float]:
        try:
            mc = yf.Ticker(t).fast_info["market_cap"]
            return t, float(mc) if mc else 0.0
        except Exception:
            return t, 0.0
    out = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for t, mc in pool.map(one, tickers):
            out[t] = mc
    # One retry round for rate-limit casualties — every stock counts
    misses = [t for t, mc in out.items() if mc <= 0]
    if misses:
        time.sleep(5)
        with ThreadPoolExecutor(max_workers=4) as pool:
            for t, mc in pool.map(one, misses):
                if mc > 0:
                    out[t] = mc
    return out


_SCAN_LOCK = __import__("threading").Lock()


def scan_market(force_refresh: bool = False) -> dict:
    """Full-market scan; cached so one sweep per day serves every cycle.
    Serialized — two threads must never hammer the provider with concurrent
    10k-symbol sweeps."""
    with _SCAN_LOCK:
        return _scan_market_locked(force_refresh)


def _scan_degraded_verdict(n_tradable: int, n_listed: int, cache: dict) -> tuple:
    """A scan returning too few tradable names is a data-PROVIDER throttle, not a
    real market. Returns (degraded, keep_prior, floor): keep_prior is True only when
    a fuller prior scan exists to fall back on (don't poison the cache with a thin
    snapshot — operator 2026-06-24, a yfinance throttle gave 918/5609 vs ~3836)."""
    floor = max(config.SCAN_MIN_TRADABLE,
                int(config.SCAN_MIN_TRADABLE_FRAC * max(1, n_listed)))
    degraded = n_tradable < floor
    prior_n = int((cache or {}).get("symbols_tradable") or 0)
    keep_prior = bool(degraded and prior_n > n_tradable and (cache or {}).get("candidates"))
    return degraded, keep_prior, floor


def _scan_market_locked(force_refresh: bool) -> dict:
    cache = state.load("market_scan", {})
    if not force_refresh and cache.get("asof"):
        try:
            cached_dt = datetime.fromisoformat(cache["asof"])
            age_h = (datetime.now(timezone.utc) - cached_dt).total_seconds() / 3600
            # SAME ET TRADING DAY only (operator caught 2026-06-15): the 18h TTL
            # alone let a Sunday-night (or prior-day) scan serve today's premarket
            # + 9:45 cycle, so the fund judged a STALE candidate universe and never
            # added today's fresh names. A new trading day must force a new scan.
            same_day = cached_dt.astimezone(_ET).date() == datetime.now(_ET).date()
        except (ValueError, TypeError):
            age_h, same_day = 1e9, False
        if same_day and 0 <= age_h < config.SCAN_CACHE_HOURS:
            return cache

    from firm import data_engine  # late import (circular-safe)
    symbols = fetch_all_us_symbols()
    state.set_status("screening", f"Full-market scan: {len(symbols)} listed stocks", 6)

    bench = data_engine.yf_download(config.BENCHMARK, period="1y", auto_adjust=True,
                                    progress=False)["Close"].squeeze().dropna()
    bench_3m = float(bench.iloc[-1] / bench.iloc[-64] - 1) * 100 if len(bench) > 64 else 0.0

    tech: dict[str, dict] = {}
    seen: set[str] = set()

    def _scan_chunk(chunk: list[str]) -> None:
        try:
            raw = data_engine.yf_download(chunk, period="1y", auto_adjust=True,
                                          progress=False, threads=True)
            if raw is None or raw.empty:
                return
            closes = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
            vols = raw["Volume"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Volume"]]
            tech.update(_chunk_metrics(closes, vols, bench_3m, seen))
        except Exception:
            return

    chunks = [symbols[i:i + config.SCAN_CHUNK]
              for i in range(0, len(symbols), config.SCAN_CHUNK)]
    for i, chunk in enumerate(chunks):
        _scan_chunk(chunk)
        if i % 5 == 0:
            state.set_status("screening",
                             f"Full-market scan: chunk {i + 1}/{len(chunks)}, "
                             f"{len(tech)} tradable so far", 6 + 6 * i / len(chunks))
        time.sleep(0.8)   # gentle on the data provider — no force

    # Retry pass: every stock counts. Symbols with NO data were provider
    # failures (rate limits), not filters — re-fetch them in smaller batches.
    missing = [s for s in symbols if s not in seen]
    if missing:
        state.set_status("screening",
                         f"Full-market scan: retrying {len(missing)} missed symbols", 12)
        time.sleep(10)
        for i in range(0, len(missing), 100):
            _scan_chunk(missing[i:i + 100])
            time.sleep(1.5)
        still_missing = len([s for s in missing if s not in seen])
        state.journal("scan_retry", attempted=len(missing), still_missing=still_missing)

    scores = {t: data_engine.quant_score(m) for t, m in tech.items()}
    # Hundreds of names tie at the score cap — break ties on RISK-ADJUSTED
    # blended momentum (2026-06-10 audit: raw momentum as tie-break just
    # re-selects the most volatile movers; dividing by vol prefers names
    # that earned the same move with less risk)
    ranked = sorted(
        scores,
        key=lambda t: (scores[t],
                       (tech[t]["ret_6m"] + tech[t]["ret_3m"]
                        + tech[t]["rel_strength_3m"])
                       / max(tech[t].get("vol_ann_pct", 30.0), 15.0)),
        reverse=True)

    # Cap-tier the head of the ranking, then fill guaranteed slots per tier.
    # Quality gates first (2026-06-10 audit): a guaranteed slot is a promise
    # of Opus research dollars — parabolic squeezes, death spirals, and
    # nano-caps must never collect on it. The loop just continues down the
    # ranking, so every tier still fills with investable names.
    head = ranked[:config.TIER_POOL]
    caps = _market_caps(head)
    # A failed mcap fetch (0) silently exiles even the #1 ranked name from
    # every tier — make the failure visible so a data outage is diagnosable
    no_cap = [t for t in head if caps.get(t, 0) <= 0]
    if no_cap:
        state.journal("mcap_fetch_failed", n=len(no_cap), sample=no_cap[:10])
    candidates: dict[str, list[str]] = {"large": [], "mid": [], "small": []}
    # Ranked, quality-passed RESERVE per tier (the slots + a backfill bench), so
    # cycle.py can refill a slot the cooldown/momentum-lane vacates with the next
    # FRESH name instead of shrinking the slate (operator 2026-06-23: "day to day
    # exploring MORE names, don't shorten how many get brought up").
    pool_depth = max(int(getattr(config, "SCAN_TIER_POOL_DEPTH", 12)),
                     max(config.TIER_SLOTS.values()))
    tier_pool: dict[str, list[str]] = {"large": [], "mid": [], "small": []}
    gated: list[dict] = []
    for t in head:
        mc = caps.get(t, 0)
        m = tech.get(t, {})
        reason = ""
        if m.get("vol_ann_pct", 0) > config.MAX_VOL_ANN_PCT:
            reason = f"vol {m.get('vol_ann_pct')}% > {config.MAX_VOL_ANN_PCT}%"
        elif m.get("ret_12m", 0) <= config.MIN_RET_12M_PCT:
            reason = f"12m return {m.get('ret_12m')}% (death spiral)"
        tier = ("large" if mc >= TIER_BOUNDS["large"]
                else "mid" if mc >= TIER_BOUNDS["mid"] else "small")
        if not reason and tier == "small":
            if mc < config.SMALL_TIER_MIN_MCAP:
                reason = f"mcap ${mc / 1e6:.0f}M < ${config.SMALL_TIER_MIN_MCAP / 1e6:.0f}M floor"
            elif m.get("price", 0) < config.SMALL_TIER_MIN_PRICE:
                reason = f"price ${m.get('price')} < ${config.SMALL_TIER_MIN_PRICE} floor"
        if reason:
            if len(gated) < 12:
                gated.append({"ticker": t, "tier": tier, "why": reason})
            continue
        if mc <= 0:
            continue                       # failed mcap -> not a guaranteed slot
        if len(candidates[tier]) < config.TIER_SLOTS[tier]:
            candidates[tier].append(t)
        if len(tier_pool[tier]) < pool_depth:
            tier_pool[tier].append(t)      # top slots first, then the backfill bench
        # walk deep enough to fill every tier's RESERVE (not just its slots), so a
        # cooled slot has a fresh name waiting; bounded by pool_depth per tier.
        if all(len(tier_pool[k]) >= pool_depth for k in tier_pool):
            break
    if gated:
        state.journal("candidates_quality_gated", names=gated)
    flat = candidates["large"] + candidates["mid"] + candidates["small"]
    pool_flat = sorted({t for p in tier_pool.values() for t in p} | set(flat))

    top_tech = {t: tech[t] for t in ranked[:300] if t in tech}
    result = {
        "asof": datetime.now(timezone.utc).isoformat(),
        "symbols_listed": len(symbols),
        "symbols_tradable": len(tech),
        "candidates": candidates,
        "candidates_flat": flat,
        "tier_pool": tier_pool,
        "tech": top_tech,
        "quant_scores": {t: scores[t] for t in ranked[:300]},
        "market_caps": {t: caps.get(t, 0) for t in pool_flat},
    }
    # DEGRADED-SCAN GUARD (operator 2026-06-24): a scan returning far fewer tradable
    # names than the market has is a data-PROVIDER throttle, not a real universe.
    # Caching it would make the cycle research a thin, skewed slice. Don't overwrite
    # a fuller prior scan with it — keep the prior + alert, and let a healthy scan
    # replace it once the provider recovers (the same-trading-day TTL forces a retry).
    degraded, keep_prior, floor = _scan_degraded_verdict(len(tech), len(symbols), cache)
    if degraded:
        prior_n = int(cache.get("symbols_tradable") or 0)
        state.journal("scan_degraded", tradable=len(tech), listed=len(symbols),
                      floor=floor, prior_tradable=prior_n, kept_prior=keep_prior)
        try:
            from firm import alerts
            alerts.alert(
                "MARKET SCAN DEGRADED",
                f"Scan returned only {len(tech)} tradable of {len(symbols)} listed "
                f"(floor {floor}) — a data-provider throttle. "
                + (f"Kept the fuller prior scan ({prior_n} tradable); the cycle uses "
                   f"that. Re-scan when the provider recovers."
                   if keep_prior else
                   "No fuller prior to fall back on — the cycle will use this thin "
                   "universe; re-scan when the provider recovers."),
                severity="warning")
        except Exception:
            pass
        if keep_prior:
            return cache          # keep the fuller prior scan; don't poison the cache
    state.save("market_scan", result)
    state.journal("market_scan_done", listed=len(symbols), tradable=len(tech),
                  candidates=candidates, degraded=degraded)
    return result
