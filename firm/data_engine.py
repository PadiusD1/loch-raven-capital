"""Research-floor data engine.

Pulls price history, technicals, fundamentals, and news for the universe via
yfinance, then runs the quant pre-screen that decides which names earn a full
multi-agent research workup (bounds Opus usage per cycle).
"""
import math
import threading
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import yfinance as yf

import config

_history_cache: dict[str, pd.DataFrame] = {}


_yf_reset_ts = 0.0


def _yf_reset_allowed() -> bool:
    """Floor between yfinance session rebuilds (2026-09-02 full audit,
    data-and-vendors-1). A rebuild is the heavy repair and it was gated only on
    the failure streak, so a vendor-wide outage made EVERY batch rebuild the
    session and retry the whole batch -- measured once as ~3,000 pointless
    retries and a 5-hour unprotected book, because each of those attempts runs
    under the lock the exit sweep needs. Rate-limited, a vendor outage costs one
    rebuild per interval and the fail path returns {} immediately, which every
    caller already handles."""
    global _yf_reset_ts
    import time as _t
    now = _t.time()
    if now - _yf_reset_ts < float(getattr(config, "YF_RESET_MIN_INTERVAL_S", 300.0)):
        return False
    _yf_reset_ts = now
    return True


def prune_yf_threads() -> int:
    """Release the worker Thread objects yfinance leaves behind after every
    download(threads=True).

    yfinance hands each ticker to the `multitasking` library, which appends
    every worker Thread to a module-global list (multitasking.config["TASKS"])
    and never removes it — yfinance never calls wait_for_tasks(). A finished
    threading.Thread object still owns two lock objects (_started and
    _tstate_lock), and on Windows every CPython lock is one kernel SEMAPHORE
    handle, so each ticker of each threaded download costs the process two
    kernel handles FOREVER. Measured 2026-09-01 in a fresh sandboxed process:
    +12 handles per 6-ticker download, +0 with threads=False, and
    multitasking.config["TASKS"] growing 31 -> 49 across the probe. The live
    app.py at 96h uptime held 255,259 leaked semaphores (of 255,556 handles),
    had been paged out to a 14 MB working set, and answered NO HTTP endpoint
    within 30 s while its monitor threads still beat — the "hung-but-listening"
    state behind every keepalive incident in the fund's history. Because
    run_cycle.py decides delegated-vs-headless from an HTTP probe, it is also
    how a scheduled cycle goes headless and stands exit protection down.

    Prunes IN PLACE (the library holds the same list object). Fail-soft: a
    yfinance/multitasking upgrade that moves the list degrades this to a
    no-op, never a crash. Returns the number of dead threads released."""
    try:
        import multitasking
        tasks = multitasking.config.get("TASKS")
        if not isinstance(tasks, list) or not tasks:
            return 0
        alive = [t for t in tasks if t.is_alive()]
        released = len(tasks) - len(alive)
        if released:
            tasks[:] = alive
        return released
    except Exception:
        return 0


def yf_download(*args, **kwargs):
    """The ONLY sanctioned way to call yfinance's download() inside firm/ — see
    prune_yf_threads. Prunes after every call, including one that raises.
    tests/test_firm.py greps firm/ for a bare yfinance download call so a new
    call site cannot quietly reopen the leak."""
    try:
        return yf.download(*args, **kwargs)
    finally:
        prune_yf_threads()


def fetch_history(tickers: list[str], period: str = "2y") -> pd.DataFrame:
    """Daily adjusted closes for a list of tickers, one column per ticker."""
    # UTC cache bucket (2026-06-14 audit A3): every other consumer stamps UTC;
    # a naive-local hour key rolled the cache on a different clock than the UTC
    # freshness logic (rotation lock, spy_return_since), and skewed across DST.
    key = f"{','.join(sorted(tickers))}|{period}|{datetime.now(timezone.utc).strftime('%Y%m%d%H')}"
    if key in _history_cache:
        return _history_cache[key]
    raw = yf_download(tickers, period=period, auto_adjust=True,
                      progress=False, threads=True)
    closes = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
    if isinstance(closes, pd.Series):
        closes = closes.to_frame(name=tickers[0])
    closes = closes.dropna(how="all")
    if len(_history_cache) > 8:
        _history_cache.clear()
    _history_cache[key] = closes
    return closes


# In-process yfinance session recovery (2026-07-17 IWM outage): the LONG-LIVED
# app's shared yfinance session (cookie/crumb/curl session) can go bad under
# rate limiting, after which EVERY download in this process returns nothing for
# hours while a fresh process prices the same names instantly — all three
# sleeves ran unpriceable 4:05pm-midnight on a healthy vendor. Wholesale
# failures are counted, journaled (the old bare `except: return {}` meant the
# outage was undiagnosable after the fact), and after 2 consecutive wholesale
# misses the yfinance singleton is dropped so the next call builds a fresh
# session. Races on these counters across threads are benign (worst case one
# extra reset/journal line).
_yf_fail_streak = 0
_yf_fail_journal_ts = 0.0
_YF_FAIL_JOURNAL_EVERY_S = 900.0


def _reset_yf_session() -> bool:
    """Drop yfinance's YfData singleton so the next call rebuilds the session,
    cookie and crumb from scratch. Guarded attribute-walk: a yfinance upgrade
    that moves these internals degrades this to a no-op, never a crash.

    (2026-08-13 IG audit) The singleton drop alone is a PLACEBO on yfinance
    1.3.0: YfData reloads the PERSISTED cookie from the on-disk store
    (%LOCALAPPDATA%\\py-yfinance — cookies.db plus its SQLite sidecars), so
    the "fresh" session resumed the exact rate-limited identity it was built
    to escape. Delete the persisted cookie files too (best-effort — a locked/
    missing file must never turn recovery into a crash) so the rebuilt session
    mints a fresh identity. The tz-cache sidecar is deliberately left alone."""
    _cleared: list[str] = []
    try:
        import os
        _base = os.environ.get("LOCALAPPDATA") or ""
        _cdir = os.path.join(_base, "py-yfinance")
        if _base and os.path.isdir(_cdir):
            for _fn in os.listdir(_cdir):
                if _fn.startswith("cookie"):
                    try:
                        os.remove(os.path.join(_cdir, _fn))
                        _cleared.append(_fn)
                    except OSError:
                        pass
        if _cleared:
            from firm import state
            state.journal("yf_cookie_store_cleared", files=_cleared)
    except Exception:
        pass
    try:
        import yfinance.data as _yfd
        inst = getattr(_yfd.SingletonMeta, "_instances", None)
        if isinstance(inst, dict):
            inst.pop(_yfd.YfData, None)
            return True
    except Exception:
        pass
    return False


def _yf_batch_closes(tickers: list[str]) -> dict[str, float]:
    raw = yf_download(tickers, period="5d", auto_adjust=True,
                      progress=False, threads=True)
    closes = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
    if isinstance(closes, pd.Series):
        closes = closes.to_frame(name=tickers[0])
    if len(tickers) == 1 and "Close" in closes.columns:
        closes = closes.rename(columns={"Close": tickers[0]})
    out = {}
    for t in closes.columns:
        s = closes[t].dropna()
        if len(s) and float(s.iloc[-1]) > 0:
            out[t] = float(s.iloc[-1])
    return out


def _note_yf_failure(n_tickers: int, err: str) -> None:
    global _yf_fail_streak, _yf_fail_journal_ts
    _yf_fail_streak += 1
    now = datetime.now(timezone.utc).timestamp()
    if now - _yf_fail_journal_ts >= _YF_FAIL_JOURNAL_EVERY_S:
        _yf_fail_journal_ts = now
        try:
            from firm import state
            state.journal("yf_batch_failed", n_tickers=n_tickers,
                          consecutive=_yf_fail_streak, error=err[:160])
        except Exception:
            pass


def batch_last_prices(tickers: list[str]) -> dict[str, float]:
    """Latest prices for many tickers in ONE download (rate-limit friendly)."""
    global _yf_fail_streak
    tickers = sorted(set(t for t in tickers if t))
    if not tickers:
        return {}
    try:
        out = _yf_batch_closes(tickers)
    except Exception as e:
        _note_yf_failure(len(tickers), str(e) or type(e).__name__)
        out = {}
        if _yf_fail_streak >= 2 and _yf_reset_allowed() and _reset_yf_session():
            try:
                out = _yf_batch_closes(tickers)
            except Exception:
                pass
    else:
        if not out:
            # yf can "succeed" with an all-NaN frame under throttling — that is
            # a wholesale miss too, just wearing a 200 status
            _note_yf_failure(len(tickers), "empty result")
            if _yf_fail_streak >= 2 and _yf_reset_allowed() and _reset_yf_session():
                try:
                    out = _yf_batch_closes(tickers)
                except Exception:
                    pass
    if out:
        _yf_fail_streak = 0
    return out


def last_price(ticker: str) -> float:
    px = batch_last_prices([ticker]).get(ticker, 0.0)
    if px > 0:
        return px
    try:
        fi = yf.Ticker(ticker).fast_info
        px = float(fi["last_price"])
        return px if px > 0 and not math.isnan(px) else 0.0
    except Exception:
        return 0.0


# ── Technicals ────────────────────────────────────────────────────────────────

def _rsi(series: pd.Series, length: int = 14) -> float:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(length).mean()
    loss = (-delta.clip(upper=0)).rolling(length).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    val = rsi.dropna()
    return float(val.iloc[-1]) if len(val) else 50.0


def _ret(series: pd.Series, days: int) -> float:
    s = series.dropna()
    if len(s) <= days:
        return 0.0
    return float(s.iloc[-1] / s.iloc[-1 - days] - 1) * 100


def compute_technicals(closes: pd.DataFrame, benchmark: pd.Series) -> dict[str, dict]:
    out: dict[str, dict] = {}
    bench_3m = _ret(benchmark, 63)
    for t in closes.columns:
        s = closes[t].dropna()
        if len(s) < 60:
            continue
        sma50 = float(s.rolling(50).mean().iloc[-1])
        sma200 = float(s.rolling(200).mean().iloc[-1]) if len(s) >= 200 else sma50
        rets = s.pct_change().dropna()
        vol = float(rets.tail(63).std() * np.sqrt(252)) * 100
        peak = s.cummax()
        mdd = float(((s / peak) - 1).min()) * 100
        out[t] = {
            "price": float(s.iloc[-1]),
            "ret_1m": round(_ret(s, 21), 2),
            "ret_3m": round(_ret(s, 63), 2),
            "ret_6m": round(_ret(s, 126), 2),
            "ret_12m": round(_ret(s, 252), 2),
            "rsi14": round(_rsi(s), 1),
            "above_sma50": bool(s.iloc[-1] > sma50),
            "above_sma200": bool(s.iloc[-1] > sma200),
            "vol_ann_pct": round(vol, 1),
            "max_dd_2y_pct": round(mdd, 1),
            "rel_strength_3m": round(_ret(s, 63) - bench_3m, 2),
        }
    return out


# ── Fundamentals & news ───────────────────────────────────────────────────────

_FUND_FIELDS = {
    "trailingPE": "pe", "forwardPE": "fwd_pe", "pegRatio": "peg",
    "priceToSalesTrailing12Months": "ps", "profitMargins": "profit_margin",
    "operatingMargins": "op_margin", "revenueGrowth": "rev_growth",
    "earningsGrowth": "eps_growth", "returnOnEquity": "roe",
    "debtToEquity": "debt_to_equity", "freeCashflow": "fcf",
    "marketCap": "mkt_cap", "sector": "sector", "shortName": "name",
    # book value PER SHARE — its sign IS the stockholders'-equity sign (shares
    # are always positive), so bookValue <= 0 means negative/zero equity, which
    # makes ROE and other equity-denominated ratios not-meaningful (2026-06-16).
    "bookValue": "book_value",
    # FORWARD CONSENSUS + POSITIONING (AUDIT 2026-07-16 infodiet: these fields
    # were already in the same yfinance .info download and simply discarded —
    # the committee judged trailing numbers only, and the sentiment desk's
    # mandate named 'positioning' data the packet never carried).
    "forwardEps": "fwd_eps", "trailingEps": "eps",
    "recommendationMean": "analyst_rec_mean",       # 1=strong buy .. 5=sell
    "numberOfAnalystOpinions": "analyst_count",
    "targetMeanPrice": "analyst_target_mean",
    "shortPercentOfFloat": "short_pct_float",
    "sharesShortPriorMonth": "shares_short_prior",
    "sharesShort": "shares_short",
    "heldPercentInsiders": "insider_held_pct",
    "heldPercentInstitutions": "institution_held_pct",
}


def _fetch_fundamentals_yf(ticker: str) -> dict:
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:
        info = {}
    out = {"ticker": ticker, "source": "yfinance"}
    for src, dst in _FUND_FIELDS.items():
        v = info.get(src)
        if isinstance(v, float):
            v = round(v, 4)
        out[dst] = v
    # yfinance reports debtToEquity as a PERCENTAGE number (AAPL 79.5 = 0.795x,
    # Ford 425.5 = 4.25x). Canonicalize to a RATIO so it matches FMP and how D/E
    # is universally quoted — and so quality_score's ratio-scale thresholds are
    # correct for BOTH vendors (2026-06-13: the un-normalized merge let FMP's
    # raw ratio be read against percentage thresholds, scoring every name as
    # ultra-low-leverage).
    de = out.get("debt_to_equity")
    if isinstance(de, (int, float)):
        out["debt_to_equity"] = round(de / 100, 4)
    return out


def _fund_plausible(field: str, v) -> bool:
    """Is this fundamental value physically possible? Bands are WIDE — only true
    garbage fails (impossible magnitudes, undefined PEG, a units/scale
    regression). Real extremes (AAPL ROE 1.47x, Ford D/E 4.26x) pass."""
    if not isinstance(v, (int, float)) or v != v:        # None / NaN
        return False
    lo, hi = config.FUND_PLAUSIBLE_BOUNDS.get(field, (float("-inf"), float("inf")))
    if v < lo or v > hi:
        return False
    if field in ("pe", "fwd_pe") and 0 < v < 1:          # sub-1 P/E is a vendor glitch
        return False
    return True


# PEG is computed on different growth windows by different vendors, so a bad
# PEG must NOT be cross-substituted — that manufactures a misleading number
# (e.g. a positive PEG from one vendor for a company whose earnings are actually
# shrinking). It's dropped to None and value_score's peg>0 gate simply ignores it.
_DROP_ONLY_FIELDS = {"peg"}


def _vendors_disagree(a: float, b: float, ratio: float = 3.0) -> bool:
    """Two vendor values for the SAME (canonical-scale) field are mutually
    untrustworthy when they have opposite signs or differ by more than `ratio`x
    in magnitude — the yfinance-1788% vs independent-210% ROE split (2026-06-16).
    Both fetch paths canonicalize units (D/E to a ratio, margins to decimals), so
    a >3x gap is a real disagreement, not a units artifact — distrust BOTH rather
    than silently keeping the merge winner."""
    if not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        return False
    # A zero from one vendor can't form a meaningful ratio and is NOT an opposite
    # sign — a break-even name (one vendor rounds margin/ROE to 0.0, the other
    # reports ~2-50%) is a same-side reading, not a scale split (adversarial review
    # 2026-06-17). A genuinely garbage LARGE side is already caught by the
    # plausibility band, so treating "one side is 0" as agreement never lets garbage
    # through — it only stops over-dropping legitimate break-even values.
    if a == 0 or b == 0:
        return False
    if (a > 0) != (b > 0):                               # both nonzero -> true opposite signs
        return True
    hi, lo = max(abs(a), abs(b)), min(abs(a), abs(b))
    return hi / lo > ratio


def _sanitize_fundamentals(merged: dict, yf_data: dict, fmp_data: dict,
                           ticker: str = "") -> dict:
    """Self-healing plausibility guard at the data boundary (2026-06-13): a
    present-but-implausible value is auto-replaced with the OTHER vendor's
    in-band value, or dropped to None if neither vendor is sane (scorers skip
    None — never a fake 50, never garbage). Auto-fix degrades to a trusted
    source, never to a bad number. Works on a COPY (never mutates the caller's
    dict). What changed is recorded in _data_quality so the committee sees the
    scrub, and journaled for the audit trail."""
    out = dict(merged)                                   # never mutate the caller's dict
    dropped, replaced = [], {}
    for field in config.FUND_PLAUSIBLE_BOUNDS:
        v = out.get(field)
        if v is None or _fund_plausible(field, v):
            continue                                     # missing is fine; good is fine
        alt = None
        if field not in _DROP_ONLY_FIELDS:               # methodology-divergent fields: drop, don't substitute
            for src in (yf_data, fmp_data):              # substitute any sane alternate
                av = (src or {}).get(field)
                if av is not None and _fund_plausible(field, av):
                    alt = av
                    break
        if alt is not None:
            out[field] = alt
            replaced[field] = {"from": v, "to": alt}
        else:
            out[field] = None
            dropped.append(field)

    # Cross-vendor disagreement (2026-06-16): when BOTH vendors carry a plausible
    # value for an audited field but they diverge wildly, neither is trustworthy —
    # drop instead of silently keeping the merge winner. Independent third-source
    # scraping in the cycle hot-path was rejected (fragile/slow, and for an n/m
    # ratio a third source doesn't rescue the value) — distrust-both is the robust
    # call; an offline independent audit is the place for third-source grading.
    for field in config.VENDOR_AUDIT_FIELDS:
        if out.get(field) is None:
            continue
        a, b = (yf_data or {}).get(field), (fmp_data or {}).get(field)
        if (_fund_plausible(field, a) and _fund_plausible(field, b)
                and _vendors_disagree(a, b)):
            out[field] = None
            replaced.pop(field, None)
            if field not in dropped:
                dropped.append(field)

    # ROE (any equity-denominated ratio) is NOT MEANINGFUL on negative/zero book
    # equity — a normal profit / a sliver of equity explodes (STX 1788% on equity
    # that was negative for 3 straight years). book_value is per share so its sign
    # IS the equity sign. Drop so the committee never weighs the artifact; mirrors
    # the quality_score D/E negative-equity skip (2026-06-16 operator: STX ROE).
    # FAIL OPEN on a single-vendor book_value glitch (adversarial review 2026-06-17):
    # book_value isn't cross-vendor audited, so a spurious negative book_value from
    # one vendor can win the merge and wrongly suppress a valid ROE. Only drop ROE
    # when NEITHER raw vendor reports positive equity — i.e. the negative reading is
    # corroborated, not a lone sign-flip. A real negative-equity name (both vendors
    # agree) still drops; a +5/-4 vendor split keeps the ROE.
    bv = out.get("book_value")
    if isinstance(bv, (int, float)) and bv <= 0 and out.get("roe") is not None:
        _pos_bvs = [x for x in ((yf_data or {}).get("book_value"),
                                (fmp_data or {}).get("book_value"))
                    if isinstance(x, (int, float)) and x > 0]
        if _pos_bvs:
            # a vendor reports POSITIVE equity -> the negative merge value is a lone
            # glitch; correct book_value to the positive reading and KEEP roe so the
            # downstream quality_score book>0 guard also stays consistent (fail open).
            out["book_value"] = _pos_bvs[0]
            replaced["book_value"] = {"from": bv, "to": _pos_bvs[0]}
        else:
            # negative equity corroborated (or only vendor) -> ROE is n/m, drop it
            out["roe"] = None
            replaced.pop("roe", None)
            if "roe" not in dropped:
                dropped.append("roe")

    if dropped or replaced:
        out["_data_quality"] = {"dropped": dropped, "replaced": replaced}
        try:
            from firm import state
            state.journal("fundamentals_sanitized",
                          ticker=ticker or out.get("ticker"),
                          dropped=dropped, replaced=replaced)
        except Exception:
            pass
    return out


def fetch_fundamentals(ticker: str) -> dict:
    """FMP-first (clean TTM ratios + Piotroski/Altman) when a key is set, with
    yfinance as automatic fallback AND as the merge source for fields FMP
    leaves blank (e.g. revenue growth). No key -> pure yfinance, unchanged.
    Every path runs through the plausibility guard so vendor garbage can't
    reach the scorers or the committee."""
    yf_data = _fetch_fundamentals_yf(ticker)
    try:
        from firm import fmp
        f = fmp.fundamentals(ticker) if fmp.available() else None
    except Exception:
        f = None
    if not f:
        return _sanitize_fundamentals(yf_data, yf_data, {}, ticker)
    # FMP wins where it has a value; yfinance fills the gaps (growth fields)
    merged = dict(yf_data)
    for k, v in f.items():
        if v is not None:
            merged[k] = v
    merged["source"] = "fmp+yf"
    return _sanitize_fundamentals(merged, yf_data, f, ticker)


NEWS_MAX_AGE_DAYS = 10        # older headlines are already priced in — noise
# Higher-trust wire/desk sources: the committee should weight a dated Reuters
# item above an undated promotional blog. Not a whitelist (anything still
# shows) — a reliability HINT the judge can use.
_TRUSTED_SOURCES = {"reuters", "bloomberg", "associated press", "ap",
                    "the wall street journal", "wsj", "cnbc", "barrons",
                    "financial times", "ft", "marketwatch", "yahoo finance",
                    "investors business daily", "seeking alpha", "morningstar",
                    "zacks", "the motley fool", "benzinga", "mt newswires",
                    "dow jones newswires", "business wire", "pr newswire",
                    "globenewswire"}


def _norm_source(src) -> str:
    """Normalize a feed source string for trust matching (2026-07-01 purpose
    review #6ii): feeds emit variants — 'Barrons.com', "Barron's", 'MT
    Newswires (English)' — and EXACT lowercase equality silently dropped them
    to untrusted, which also gates bench news-reopens. Lowercase, strip
    apostrophes/punctuation, drop .com suffixes and parenthetical tails."""
    s = str(src or "").lower().strip()
    s = s.split("(")[0].strip()
    for junk in (".com", ".co.uk"):
        if s.endswith(junk):
            s = s[: -len(junk)]
    s = "".join(ch for ch in s if ch.isalnum() or ch == " ").strip()
    return " ".join(s.split())


def _news_age_days(it: dict) -> float | None:
    """Pull the publish age in days from either yfinance schema (ISO pubDate
    in content, or epoch providerPublishTime). None if undated."""
    import datetime as _dt
    c = it.get("content") or {}
    iso = c.get("pubDate") or c.get("displayTime")
    if iso:
        try:
            d = _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - d).total_seconds() / 86400
        except (ValueError, TypeError):
            pass
    epoch = it.get("providerPublishTime")
    if isinstance(epoch, (int, float)) and epoch > 0:
        return (datetime.now(timezone.utc).timestamp() - epoch) / 86400
    return None


def fetch_news(ticker: str, limit: int = 6) -> list[str] | None:
    """Recent headlines, each tagged with SOURCE and AGE so the committee can
    weight reliability + recency (2026-06-13: the old version threw both away,
    so a 3-day-old promo video read identically to a fresh Reuters report).
    Stale items (>10d, already priced in) and dupes are dropped; trusted wire
    sources are flagged.

    Returns None when the SOURCE ITSELF failed (throttle/outage) — distinct
    from [] (source up, genuinely no news). Before this, a Yahoo throttle day
    fed the committee 'treat the absence of news as the signal' across every
    name — a fabricated quiet-tape confirmation (purpose review #6i)."""
    try:
        items = yf.Ticker(ticker).news or []
    except Exception:
        return None
    out, seen = [], set()
    for it in items:
        c = it.get("content") or {}
        title = c.get("title") or it.get("title")
        if not title:
            continue
        key = str(title).strip().lower()[:80]
        if key in seen:
            continue
        seen.add(key)
        age = _news_age_days(it)
        if age is not None and age > NEWS_MAX_AGE_DAYS:
            continue
        src = ((c.get("provider") or {}).get("displayName")
               or it.get("publisher") or "unknown")
        trust = "✓" if _norm_source(src) in _TRUSTED_SOURCES else "·"
        age_txt = f"{age:.0f}d ago" if age is not None else "undated"
        out.append(f"[{trust} {src}, {age_txt}] {title}")
        if len(out) >= limit:
            break
    return out


def has_news_since(ticker: str, since_iso: str, max_age_days: float = 5.0) -> bool:
    """Did a fresh, trusted-source headline break AFTER `since_iso` (and within
    max_age_days)? For a bench thesis 'waiting for a news / sector-sentiment /
    geopolitical / research catalyst' — these are UNSCHEDULED (no FRED date), so
    the reopen signal is 'something actually happened'. Trusted-source-gated so a
    promo blog doesn't churn the bench. Fail-soft -> False."""
    try:
        since = datetime.fromisoformat(since_iso)
    except (ValueError, TypeError):
        return False
    days_since = (datetime.now(timezone.utc) - since).total_seconds() / 86400
    if days_since <= 0:
        return False
    try:
        items = yf.Ticker(ticker).news or []
    except Exception:
        return False
    for it in items:
        c = it.get("content") or {}
        if not (c.get("title") or it.get("title")):
            continue
        age = _news_age_days(it)
        if age is None or age > max_age_days or age >= days_since:
            continue                       # undated, stale, or older than the verdict
        src = ((c.get("provider") or {}).get("displayName")
               or it.get("publisher") or "")
        if _norm_source(src) in _TRUSTED_SOURCES:   # material wire item, newer than the verdict
            return True
    return False


# ── Geopolitical / macro breaking-news (2026-06-15, operator request) ───────────
# Bellwether tickers whose news streams surface MARKET-WIDE / geopolitical events
# (not single-name noise): the index, oil, gold, bonds, energy, defense, China.
_MACRO_NEWS_TICKERS = ("SPY", "QQQ", "USO", "GLD", "TLT", "XLE", "ITA", "FXI")
# Topic taxonomy — a headline must hit one of these to count (TOPIC-relevant), and
# be fresh (DATE-relevant). Keeps it to events that actually move the tape.
_MACRO_NEWS_TOPICS = {
    "geopolitical": ("war", "sanction", "iran", "russia", "ukraine", "china", "israel",
                     "gaza", "opec", "missile", "strike", "conflict", "nato", "taiwan",
                     "ceasefire", "tariff", "trade war", "trade deal", "embargo", "houthi"),
    "monetary": ("fed", "rate cut", "rate hike", "powell", "fomc", "inflation", "cpi",
                 "pce", "jobs report", "payroll", "yield", "treasury yield"),
    "fiscal_political": ("election", "shutdown", "debt ceiling", "stimulus", "tax bill",
                         "congress", "tariff"),
    "energy": ("oil price", "crude", "opec", "gas price", "energy crisis"),
    # Chip-cycle catalysts (IG 2026-07-06 med, ratified): the bench is semiconductor-
    # heavy (WDC/STX/MU/AMD/AMAT/KLIC/ICHR/UCTT/COHU/ACMR/STM) yet had no sector-
    # catalyst class, so a chip-cycle move (SEMI billings, TSMC monthly sales,
    # export-control / chip-tariff rulings) surfaced as single-name noise, not the
    # market-wide semis catalyst it is. These stay TRUSTED-source-gated like all
    # macro news, so a chip RUMOR is still filtered by the reliability layer.
    "semiconductors": ("semiconductor", "chip ", "chips", "tsmc", "asml", "nvidia",
                       "export control", "chip ban", "chip tariff", "foundry",
                       "wafer", "semi billings", "euv", "chipmaker", "fab "),
}
MACRO_NEWS_MAX_AGE_DAYS = 4.0       # only RECENT macro news — older is priced in


def _classify_macro_topic(title: str) -> str | None:
    tl = str(title).lower()
    for topic, kws in _MACRO_NEWS_TOPICS.items():
        if any(k in tl for k in kws):
            return topic
    return None


def macro_news_brief(max_age_days: float = MACRO_NEWS_MAX_AGE_DAYS, limit: int = 8) -> list[dict]:
    """Recent MARKET-WIDE geopolitical / macro headlines for the committee, filtered
    by TOPIC (must be a market-moving category) and DATE (must be fresh), trusted
    sources first. Each item carries its date + topic so the committee weights it by
    relevance, not vibe. Fail-soft -> []. This is CONTEXT the committee judges with,
    not a standalone trade signal (operator: Iran/US-deal-style events should be
    seen). Hourly-cached via macro_brief()."""
    out, seen = [], set()
    for tk in _MACRO_NEWS_TICKERS:
        try:
            items = yf.Ticker(tk).news or []
        except Exception:
            continue
        for it in items:
            c = it.get("content") or {}
            title = c.get("title") or it.get("title")
            if not title:
                continue
            key = str(title).strip().lower()[:80]
            if key in seen:
                continue
            age = _news_age_days(it)
            if age is None or age > max_age_days:       # DATE-relevant only
                continue
            topic = _classify_macro_topic(title)
            if not topic:                               # TOPIC-relevant only
                continue
            seen.add(key)
            src = ((c.get("provider") or {}).get("displayName")
                   or it.get("publisher") or "unknown")
            trusted = str(src).lower() in _TRUSTED_SOURCES
            day = (datetime.now(timezone.utc) - timedelta(days=age)).date().isoformat()
            out.append({"date": day, "age_days": round(age, 1), "topic": topic,
                        "trust": "✓" if trusted else "·", "source": str(src),
                        "headline": str(title)[:200]})
    # trusted + freshest first
    out.sort(key=lambda x: (x["trust"] != "✓", x["age_days"]))
    return out[:limit]


# ── Quant pre-screen ──────────────────────────────────────────────────────────

def _compress(x: float, cap: float, full_scale: float) -> float:
    """Signed log-compressive contribution — the anti-saturation transform.

    2026-07-28, operator-found. The three momentum legs used a LINEAR divisor
    with a hard clamp (`ret_6m/3` capped at 15, etc.). Those caps were calibrated
    for a general market population but are applied to an ALREADY momentum-ranked
    top-300, where they sit BELOW the median of the very population they rank.
    Measured on the live scan:

        input              pins at    universe median    % pinned
        ret_6m              45%          55.7%             70%
        ret_3m              30%          38.5%             70%
        rel_strength_3m     16           33.9              95%

    140 of 300 names pinned every leg simultaneously, so `quant_score` returned
    exactly 93.0 (= 50 + 15 + 10 + 8 + 5 + 5, the technical maximum) for 142 of
    300 names and the ENTIRE top 60 was one identical value. The screen decides
    which names ever reach the committee, so its top half was ordered by dict
    iteration rather than by strength — and every points-based consumer
    downstream (bench decay, the +5 improve bar) was reading a constant.

    Log compression is the right shape for this data because the tails are
    violent (max 6m +2358%, max 3m +3276%): it is monotonic so ordering is
    exactly preserved, it is near-linear in the region where most names live, and
    it has diminishing returns so one moonshot cannot dominate a weighted sum the
    way a widened linear cap would. `full_scale` is the input value that finally
    earns the full cap; above it the leg still saturates, deliberately, so an
    illiquid post-reverse-split artifact cannot outrank a real trend forever.

    full_scale is TUNABLE inside QUANT_COMPRESS_BOUNDS. How hard the screen
    should lean into extreme tails is an empirical question this fund cannot yet
    answer (21 closed trades), so it is a bounded lever the review calibrates
    from forward returns rather than a constant I guessed.
    """
    if not isinstance(x, (int, float)) or full_scale <= 0:
        return 0.0
    sign = 1.0 if x >= 0 else -1.0
    return sign * cap * min(1.0, math.log1p(abs(float(x))) / math.log1p(full_scale))


def quant_score(tech: dict, fund: dict | None = None) -> float:
    """Composite 0-100: momentum + trend + quality. The screen, not the verdict."""
    from firm import risk as _rk_q
    score = 50.0
    # Same legs, same weights, same 0-100 scale — only the raw-input -> points
    # mapping changed (hard clamp -> log compression). See _compress.
    score += _compress(tech["ret_6m"], 15,
                       _rk_q.tuned_cap("QUANT_FULL_SCALE_RET_6M"))
    score += _compress(tech["ret_3m"], 10,
                       _rk_q.tuned_cap("QUANT_FULL_SCALE_RET_3M"))
    score += _compress(tech["rel_strength_3m"], 8,
                       _rk_q.tuned_cap("QUANT_FULL_SCALE_RS_3M"))
    score += 5 if tech["above_sma50"] else -5
    score += 5 if tech["above_sma200"] else -5
    if tech["rsi14"] > 78:
        score -= 6                                          # overbought haircut
    if fund:
        rg = fund.get("rev_growth")
        if isinstance(rg, (int, float)):
            score += max(-5, min(8, rg * 40))
        roe = fund.get("roe")
        # only a positive-equity, in-band ROE (<=100%) is a real quality signal —
        # a negative/near-zero-equity ROE is a denominator artifact (see quality_score)
        _bk = fund.get("book_value")
        if (isinstance(roe, (int, float)) and 0.20 < roe <= 1.0
                and (_bk is None or _bk > 0)):
            score += 4
        pe = fund.get("fwd_pe")
        if isinstance(pe, (int, float)) and 0 < pe < 18:
            score += 3
    return round(max(0, min(100, score)), 1)


# ── Factor lenses (2026-06-13: momentum + value + quality, MEASURED) ──────────
# Three independent 0-100 reads so the committee — and the report card — can
# see each factor separately. The momentum screen stays the GATE; these are
# visibility + measurement, no capital/strategy change. None when the inputs
# don't exist (never a fake 50 that reads as a real score).

def momentum_score(tech: dict) -> float | None:
    """Pure price momentum + trend, independent of the valuation/quality
    bonuses baked into quant_score."""
    if not tech:
        return None
    s = 50.0
    s += max(-20, min(20, (tech.get("ret_6m") or 0) / 2))
    s += max(-12, min(12, (tech.get("ret_3m") or 0) / 3))
    s += max(-10, min(10, (tech.get("rel_strength_3m") or 0) / 2))
    s += 5 if tech.get("above_sma50") else -5
    s += 5 if tech.get("above_sma200") else -5
    if (tech.get("rsi14") or 0) > 78:
        s -= 6
    return round(max(0, min(100, s)), 1)


def value_score(fund: dict | None) -> float | None:
    """Cheapness: lower forward P/E, PEG, P/S = higher score. Neutral 50
    anchor; None if no valuation data at all."""
    if not fund:
        return None
    seen, s = False, 50.0
    pe = fund.get("fwd_pe")
    if isinstance(pe, (int, float)) and pe > 0:
        seen = True
        s += 20 if pe < 12 else 12 if pe < 18 else 4 if pe < 25 else -6 if pe < 40 else -14
    peg = fund.get("peg")
    if isinstance(peg, (int, float)) and peg > 0:
        seen = True
        s += 12 if peg < 1 else 6 if peg < 1.5 else 0 if peg < 2.5 else -8
    ps = fund.get("ps")
    if isinstance(ps, (int, float)) and ps > 0:
        seen = True
        s += 10 if ps < 2 else 4 if ps < 5 else -2 if ps < 12 else -10
    return round(max(0, min(100, s)), 1) if seen else None


def quality_score(fund: dict | None) -> float | None:
    """Business quality: ROE, margins, low leverage, positive FCF, growth.
    Neutral 50 anchor; None if no quality data at all."""
    if not fund:
        return None
    seen, s = False, 50.0
    roe = fund.get("roe")
    book = fund.get("book_value")
    # ROE is only a meaningful quality signal when book equity is POSITIVE and the
    # ratio sits in a sane band (|roe| <= 1.0 = 100%). On negative/near-zero equity
    # a normal profit / a sliver of equity explodes into a denominator artifact, not
    # elite profitability (STX 1788%, AAPL 147% are both buyback-driven near-zero
    # equity). Skip it then — exactly mirroring the D/E negative-equity skip below —
    # so a fragile balance sheet can't masquerade as quality. The sanitize layer
    # already drops ROE on book<=0; this is the in-band guard + defense-in-depth
    # (2026-06-16 operator: "the roe reported is false", Seagate).
    if (isinstance(roe, (int, float)) and (book is None or book > 0)
            and abs(roe) <= 1.0):
        seen = True
        s += 15 if roe > 0.25 else 10 if roe > 0.15 else 3 if roe > 0.05 else -10
    pm = fund.get("profit_margin")
    if isinstance(pm, (int, float)):
        seen = True
        s += 10 if pm > 0.20 else 5 if pm > 0.10 else 0 if pm > 0 else -10
    om = fund.get("op_margin")
    if isinstance(om, (int, float)):
        s += 6 if om > 0.20 else 3 if om > 0.10 else 0
    de = fund.get("debt_to_equity")
    # D/E is NOT a meaningful quality signal for banks/insurers (deposits and
    # float dwarf "debt"; vendors quote it as n/a or a non-standard figure —
    # verified: FMP gives JPM 3.39, yfinance None), NOR for negative-equity names
    # (MCD/SBUX/AZO buy back so much stock equity goes negative — the ratio's
    # sign flips and it's uninterpretable). Skip the leverage branch in both
    # cases so an uninterpretable D/E neither taxes nor flatters the business;
    # other quality factors (margins, FCF) still speak.
    _sector = str(fund.get("sector") or "").lower()
    if isinstance(de, (int, float)) and de > 0 and "financ" not in _sector:
        seen = True
        # D/E is a RATIO (0.3 = 0.3x, 0.8 = 0.8x, 2.0 = 2.0x) — canonicalized in
        # both fetch paths. Low leverage rewarded, heavy leverage penalized.
        s += 8 if de < 0.3 else 3 if de < 0.8 else -4 if de < 2.0 else -10
    fcf = fund.get("fcf")
    if isinstance(fcf, (int, float)):
        s += 6 if fcf > 0 else -8
    rg = fund.get("rev_growth")
    if isinstance(rg, (int, float)):
        s += 6 if rg > 0.20 else 2 if rg > 0.05 else -6 if rg < 0 else 0
    return round(max(0, min(100, s)), 1) if seen else None


def _size_bucket(fund: dict | None) -> dict:
    """The SIZE lens the quant desk's mandate names but its slice never carried
    (2026-07-02 review: 'a desk whose MANDATE names data it cannot see is a
    confabulation invitation'). Market cap in $B + a mega/large/mid/small label;
    None when the vendor didn't supply a cap."""
    cap = (fund or {}).get("mkt_cap")
    if not isinstance(cap, (int, float)) or cap <= 0:
        return {"mkt_cap_b": None, "bucket": None}
    cap_b = cap / 1e9
    bucket = ("mega" if cap_b >= 200 else "large" if cap_b >= 10
              else "mid" if cap_b >= 2 else "small")
    return {"mkt_cap_b": round(cap_b, 2), "bucket": bucket}


def revisions_score(fund: dict | None, tech: dict | None = None) -> float | None:
    """Street-expectations lens (fix brief 2026-07-16 #2): composite of the
    consensus fields the packet already carries — expected EPS growth (forward
    vs trailing consensus), mean analyst rating, and consensus target upside.
    No new API calls: every input rides the same .info download as the rest of
    the fundamentals (the rate-limit lesson stands). Snapshot-based: true
    revision DELTAS need consecutive packets, which only began persisting
    2026-07-16. The report card grades this lens exactly like momentum/value/
    quality, so the fund MEASURES whether Street expectations predict winners
    before any capital ever tilts toward them (evidence-over-theory). Returns
    None — not a neutral 50 — when the name carries no consensus coverage, so
    uncovered small-caps never dilute the edge grade."""
    f = fund or {}
    score, hits = 50.0, 0
    eps, fwd = f.get("eps"), f.get("fwd_eps")
    if isinstance(eps, (int, float)) and isinstance(fwd, (int, float)) \
            and abs(eps) > 0.01:
        g = (fwd - eps) / abs(eps)
        score += max(-25.0, min(25.0, g * 50.0))    # ±50% expected growth saturates
        hits += 1
    rec = f.get("analyst_rec_mean")
    if isinstance(rec, (int, float)) and 1 <= rec <= 5:
        score += (3.0 - rec) * 7.5                  # 1=strong buy → +15, 5=sell → -15
        hits += 1
    tgt = f.get("analyst_target_mean")
    px = (tech or {}).get("price")
    if isinstance(tgt, (int, float)) and isinstance(px, (int, float)) \
            and px > 0 and tgt > 0:
        score += max(-10.0, min(10.0, (tgt / px - 1) * 33.3))  # ±30% upside saturates
        hits += 1
    if not hits:
        return None
    return round(max(0.0, min(100.0, score)), 1)


def factor_scores(tech: dict, fund: dict | None) -> dict:
    """All five factor lenses for one name — momentum / value / quality / size
    / revisions (Street expectations; None when no consensus coverage)."""
    return {"momentum": momentum_score(tech),
            "value": value_score(fund),
            "quality": quality_score(fund),
            "size": _size_bucket(fund),
            "revisions": revisions_score(fund, tech)}


def screen_universe() -> dict:
    """Rank the investable universe; return technicals + ranked candidates.

    With FULL_MARKET_SCAN on, stocks come from the daily whole-market sweep
    (every listed US common stock, tiered large/mid/small). ETFs always come
    from the curated liquid list.
    """
    # ETF candidates come from the FULL liquidity-screened pool when it exists
    # (operator 2026-07-16: the fund must see everything Public trades, not a
    # hand-list of 20) — the curated list is only the cold-start fallback.
    _pool_etfs: list[str] = []
    try:
        from firm import allocation as _alloc_de
        _pool_etfs = _alloc_de._pool_symbols()[:40]
    except Exception:
        pass
    _etf_src = _pool_etfs if len(_pool_etfs) >= 10 else list(config.ETF_UNIVERSE)
    etf_tickers = sorted(set(_etf_src + config.ETF_UNIVERSE
                             + config.ROTATION_UNIVERSE
                             + [config.BENCHMARK, config.CASH_ETF]))
    closes = fetch_history(etf_tickers)
    bench = closes[config.BENCHMARK].dropna()
    tech = compute_technicals(closes, bench)
    etf_ranked = sorted(
        (t for t in _etf_src if t in tech and t != config.BENCHMARK),
        key=lambda t: quant_score(tech[t]), reverse=True)
    quant_scores = {t: quant_score(tech[t]) for t in tech}

    if config.FULL_MARKET_SCAN:
        from firm import universe
        scan = universe.scan_market()
        tech.update(scan.get("tech", {}))
        quant_scores.update(scan.get("quant_scores", {}))
        stock_candidates = [t for t in scan.get("candidates_flat", []) if t in tech]
        scan_info = {"symbols_listed": scan.get("symbols_listed"),
                     "symbols_tradable": scan.get("symbols_tradable"),
                     "tiers": scan.get("candidates", {}),
                     "tier_pool": scan.get("tier_pool", {}),
                     "market_caps": scan.get("market_caps", {})}
    else:
        stock_closes = fetch_history(sorted(set(config.STOCK_UNIVERSE + [config.BENCHMARK])))
        tech.update(compute_technicals(stock_closes, bench))
        quant_scores = {t: quant_score(tech[t]) for t in tech}
        stock_candidates = sorted(
            (t for t in config.STOCK_UNIVERSE if t in tech),
            key=lambda t: quant_score(tech[t]), reverse=True)[:config.MAX_STOCK_CANDIDATES]
        scan_info = {}

    return {
        "asof": datetime.now(timezone.utc).isoformat(),
        "technicals": tech,
        "stock_candidates": stock_candidates,
        "etf_candidates": etf_ranked[:config.MAX_ETF_CANDIDATES],
        "quant_scores": quant_scores,
        "scan_info": scan_info,
    }


def _earnings_session(ts) -> str:
    """Reports drop at a TIME, not just a date: before the open (BMO, ~7-9am
    ET) or after the close (AMC, ~4-8pm ET). The 9:45am cycle runs AFTER a
    BMO print (numbers exist) but BEFORE an AMC print (numbers tonight) —
    the fund must know which, or it reopens a name hours before its report."""
    try:
        import pandas as pd
        t = pd.Timestamp(ts)
        if t.tzinfo is not None:
            t = t.tz_convert("America/New_York")
        h = t.hour
        if h == 0 and getattr(t, "minute", 0) == 0:
            return "unknown"        # midnight = yfinance placeholder, no real time
        return "BMO" if h < 12 else "AMC" if h >= 16 else "intraday"
    except Exception:
        return "unknown"


def next_earnings(ticker: str) -> dict:
    """Next earnings DATE + TIME-of-day (session). Buying a momentum name into
    a report is a coin flip — the committee sees the date+session and the
    sleeves hard-skip entries within a day of it. FMP confirmed dates first
    (when keyed), then yfinance get_earnings_dates, then calendar. {} unknown."""
    try:
        from firm import fmp
        if fmp.available():
            fe = fmp.next_earnings(ticker)
            if fe:
                return fe
    except Exception:
        pass
    try:
        import datetime as _dt

        import pandas as pd
        today = datetime.now(timezone.utc).date()
        try:
            df = yf.Ticker(ticker).get_earnings_dates(limit=12)
            if df is not None and len(df):
                now = pd.Timestamp.now(tz=getattr(df.index, "tz", None) or "UTC")
                fut = df[df.index >= now].sort_index()
                if len(fut):
                    ts = fut.index[0]
                    d0 = ts.date()
                    return {"date": d0.isoformat(),
                            "days_away": (d0 - today).days,
                            "session": _earnings_session(ts)}
        except Exception:
            pass
        cal = yf.Ticker(ticker).calendar
        dates = (cal or {}).get("Earnings Date") or []
        if not dates:
            return {}
        d0 = dates[0]
        if isinstance(d0, _dt.datetime):
            d0 = d0.date()
        if not isinstance(d0, _dt.date):
            return {}
        return {"date": d0.isoformat(), "days_away": (d0 - today).days,
                "session": "unknown"}
    except Exception:
        return {}


def recent_earnings(ticker: str, within_days: int = 8) -> dict:
    """The ACTUAL reported result of a just-released print: estimate, reported,
    and the surprise %. When a name was held HOLD 'waiting for earnings' and
    the print lands, the re-research must judge the REAL numbers (beat/miss),
    not re-run blind (operator request 2026-06-12: SanDisk waiting on its
    earnings -> pull actuals and reprocess). FMP confirmed actuals first when
    keyed, yfinance fallback. {} if no recent report."""
    try:
        from firm import fmp
        if fmp.available():
            fr = fmp.recent_earnings(ticker, within_days)
            if fr:
                return fr
    except Exception:
        pass
    try:
        import pandas as pd
        df = yf.Ticker(ticker).get_earnings_dates(limit=8)
        if df is None or not len(df):
            return {}
        now = pd.Timestamp.now(tz=getattr(df.index, "tz", None) or "UTC")
        past = df[df.index <= now]
        if not len(past):
            return {}
        row = past.iloc[0]   # earnings_dates is newest-first
        when = past.index[0]
        age = (now - when).days
        if age > within_days:
            return {}
        est = row.get("EPS Estimate")
        rep = row.get("Reported EPS")
        sup = row.get("Surprise(%)")

        def _f(v):
            return round(float(v), 4) if isinstance(v, (int, float)) and v == v else None
        return {"reported_on": when.date().isoformat(), "days_ago": age,
                "session": _earnings_session(when),
                "eps_estimate": _f(est), "eps_reported": _f(rep),
                "surprise_pct": _f(sup),
                "verdict": ("beat" if isinstance(sup, (int, float)) and sup > 1
                            else "miss" if isinstance(sup, (int, float)) and sup < -1
                            else "in-line" if isinstance(sup, (int, float)) else None)}
    except Exception:
        return {}


_macro_cache: dict[str, dict] = {}


def macro_brief(regime: dict | None = None) -> dict:
    """Firm-wide macro read computed ONCE per cycle and stamped into every
    packet, so the whole committee judges against one coherent picture of
    'what's happening today and forward' — not a same-day compilation that
    forgets the regime (operator request 2026-06-12). Carries: market trend,
    volatility regime, sector leadership, the forward Fed/CPI/jobs calendar,
    and a 'just happened' flag when a high-impact event landed in the last
    2 days (so the committee re-evaluates AFTER the Fed acts). Hourly-cached."""
    from firm import macro_calendar
    key = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    if key in _macro_cache:
        out = dict(_macro_cache[key])
        if regime:
            out["sector_leadership"] = {"leading": regime.get("leading", []),
                                        "lagging": regime.get("lagging", [])}
        return out
    out: dict = {"asof": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    try:
        closes = fetch_history(["SPY", "^VIX"], period="1y")
        spy = closes["SPY"].dropna() if "SPY" in closes.columns else None
        if spy is not None and len(spy) > 200:
            last = float(spy.iloc[-1])
            sma50, sma200 = float(spy.tail(50).mean()), float(spy.tail(200).mean())
            r1m = (last / float(spy.iloc[-22]) - 1) * 100 if len(spy) > 22 else 0
            out["market"] = {
                "spy_vs_50dma_pct": round((last / sma50 - 1) * 100, 1),
                "spy_1m_pct": round(r1m, 1),
                "trend": ("risk-on (above rising 50 & 200DMA)" if last > sma50 > sma200
                          else "risk-off (below 50DMA)" if last < sma50
                          else "neutral/transitional")}
        vix = closes["^VIX"].dropna() if "^VIX" in closes.columns else None
        if vix is not None and len(vix):
            v = float(vix.iloc[-1])
            out["volatility"] = {
                "vix": round(v, 1),
                "regime": ("calm (<15)" if v < 15 else "normal (15-20)" if v < 20
                           else "elevated (20-28)" if v < 28 else "stressed (>28)")}
    except Exception:
        pass
    out["forward_calendar_10d"] = macro_calendar.upcoming(10)        # high-impact reopen set
    out["full_macro_calendar_10d"] = macro_calendar.upcoming_all(10)  # BROAD: all fed/macro prints
    # Breaking geopolitical / macro news the committee should SEE (Iran/US-deal-
    # style events): topic- and date-filtered so it's relevant, not noise. CONTEXT
    # for judgment, not a standalone signal (operator request 2026-06-15).
    try:
        gn = macro_news_brief()
        if gn:
            out["geopolitical_news"] = gn
            out["geopolitical_news_topics"] = sorted({g["topic"] for g in gn})
    except Exception:
        pass
    # A high-impact macro number just landed — re-evaluate after, not blind
    # through. "Fed numbers aren't only FOMC" (2026-06-14): CPI/PCE/NFP/GDP/etc.
    # all count, resolved through the live calendar tiers.
    import datetime as _dt
    today = datetime.now(timezone.utc).date()
    landed = macro_calendar.landed_since(today - _dt.timedelta(days=2), today)
    if landed:
        out["just_happened"] = [{"date": landed.isoformat(),
                                 "event": "macro data just landed — re-evaluate "
                                 "rate/inflation-sensitive positioning"}]
    _macro_cache[key] = dict(out)
    if len(_macro_cache) > 6:
        for k in sorted(_macro_cache)[:-3]:
            _macro_cache.pop(k, None)
    if regime:
        out["sector_leadership"] = {"leading": regime.get("leading", []),
                                    "lagging": regime.get("lagging", [])}
    return out


_transcript_warming: set = set()           # tickers with an in-flight prewarm (this process)


def _cached_transcript(ticker: str, win: int) -> dict:
    """Read the daily-cached transcript; on a miss, fetch it in the BACKGROUND and
    return {} now. The headless render is ~75s — FAR too slow for the cycle's
    critical path (it was hanging research workers and timing out to {} anyway,
    2026-06-29). So the cycle never blocks: the transcript lands in the cache
    off-cycle and is read on the NEXT cycle; meanwhile the 8-K + news carry the
    management commentary. One warm per ticker per process at a time."""
    from firm import state
    cache = state.load("transcript_cache", {}) or {}
    today = datetime.now(timezone.utc).date().isoformat()
    ent = cache.get(ticker)
    if isinstance(ent, dict) and ent.get("asof") == today:
        return ent.get("transcript") or {}
    if ticker not in _transcript_warming:
        _transcript_warming.add(ticker)

        def _warm() -> None:
            tr = {}
            try:
                from firm import transcripts
                tr = transcripts.latest(ticker, within_days=win) or {}
            except Exception:
                tr = {}
            try:
                c = state.load("transcript_cache", {}) or {}
                c[ticker] = {"asof": today, "transcript": tr}
                if len(c) > 120:
                    c = dict(list(c.items())[-120:])
                state.save("transcript_cache", c)
            except Exception:
                pass
            finally:
                _transcript_warming.discard(ticker)

        threading.Thread(target=_warm, daemon=True,
                         name=f"transcript-warm-{ticker}").start()
    return {}


def earnings_docs(ticker: str, recent: dict | None = None) -> dict:
    """Primary-source earnings DOCUMENTS for a name that JUST reported: the SEC
    8-K earnings release + latest 10-Q/10-K (free, official EDGAR) and the literal
    call-transcript excerpt (headless scrape, cached daily). Returns {} when there
    is no fresh print or the feature is off. Fully fail-soft + size-bounded — it
    only ADDS a packet field and can never break a cycle (operator 2026-06-25:
    read the actual filings + transcript on earnings, not just the surprise %)."""
    if not getattr(config, "EARNINGS_DOCS", True):
        return {}
    try:
        re_ = recent if recent is not None else recent_earnings(ticker)
        days_ago = (re_ or {}).get("days_ago")
        win = int(getattr(config, "EARNINGS_DOCS_WINDOW_DAYS", 8))
        if not isinstance(days_ago, (int, float)) or days_ago > win:
            return {}                       # only attach around an actual print
        from firm import edgar
        out: dict = {}
        # Filings and transcript are INDEPENDENTLY fail-soft — a transcript
        # (headless-browser) failure must never drop the SEC filings.
        try:
            fil = edgar.earnings_filings(ticker, within_days=win)
            if fil:
                out["filings"] = fil
        except Exception:
            pass
        try:
            tr = _cached_transcript(ticker, win)
            if tr:
                out["transcript"] = tr
        except Exception:
            pass
        return out
    except Exception:
        return {}


def _neutralized_news_tiers() -> set[str]:
    """News tiers the fund's OWN report card measured NET-HARMFUL over a real
    sample (learning.py writes them to edge_health.neutralized when a tier trails
    'blind' by >= NEWS_EDGE_NEUTRALIZE_PTS with n >= NEWS_EDGE_NEUTRALIZE_MIN_N).
    Their headlines get demoted to context-only in the packet so they cannot move
    conviction — a HARD, data-layer neutralize instead of the old soft prompt
    down-weight that left the pipeline still additive-negative (IG 2026-07-13
    Item 9). Fully self-reversing: the moment a tier's measured edge recovers
    above the floor it drops out of `neutralized` and its news flows back."""
    try:
        from firm import state
        eh = state.load("edge_health", {}) or {}
        tiers: set[str] = set()
        for e in (eh.get("neutralized") or []):
            edge = e.get("edge", "") if isinstance(e, dict) else str(e)
            if edge.startswith("news:"):
                tier = edge.split(":", 1)[1].split("-vs-")[0].strip()
                if tier:
                    tiers.add(tier)
        return tiers
    except Exception:
        return set()


def build_packet(ticker: str, tech: dict, quant: float) -> dict:
    """Full data packet handed to the analyst desks for one candidate."""
    fund = fetch_fundamentals(ticker)
    news = fetch_news(ticker)
    # Reliability gate (operator 2026-07-07): the fund's OWN report card measured
    # the news tape as net-harmful (blind beat trusted/aggregator), so rumor,
    # speculation and aggregator noise must be visible-but-non-actionable. Split
    # confirmed trusted-wire items (MATERIAL, kept first) from the rest
    # (SPECULATIVE) — the speculative items stay in the tape so the judge still
    # SEES them, but are inline-flagged so they cannot move conviction (the judge
    # guidance enforces it). Fail-soft: on any error the raw tape passes through.
    _news_quality = None
    if isinstance(news, list) and news:
        try:
            from firm import news_quality as _nq_mod
            _part = _nq_mod.partition(news)
            # Hard neutralize (IG 2026-07-13 Item 9): a news TIER measured
            # net-harmful over a real sample has its MATERIAL items demoted to
            # context-only too — a wire item is still net-negative if the wire
            # tier is inverted, and a soft note to the judge was not enough.
            _neu = _neutralized_news_tiers()
            _neu_demoted = 0
            if _neu:
                _keep, _demote = [], list(_part["speculative"])
                for h in _part["material"]:
                    if _nq_mod.trust_tag(h) in _neu:
                        _demote.append(h)
                        _neu_demoted += 1
                    else:
                        _keep.append(h)
                _part = {"material": _keep, "speculative": _demote}
            _news_quality = {"n_material": len(_part["material"]),
                             "n_speculative": len(_part["speculative"]),
                             "n_neutralized": _neu_demoted,
                             "neutralized_tiers": sorted(_neu)}
            news = (_part["material"]
                    + [f"[SPECULATIVE — context only, do NOT move conviction] {h}"
                       for h in _part["speculative"]])
        except Exception:
            pass
    recent = recent_earnings(ticker)
    # POSITIONING slice for the sentiment desk (AUDIT 2026-07-16 infodiet: its
    # mandate names positioning/crowding — short interest, insider and
    # institutional holdings, analyst consensus now ride the packet instead of
    # inviting confabulation). Kept as its OWN key so desk isolation holds.
    _pos_keys = ("short_pct_float", "shares_short", "shares_short_prior",
                 "insider_held_pct", "institution_held_pct",
                 "analyst_rec_mean", "analyst_count", "analyst_target_mean")
    positioning = {k: fund.get(k) for k in _pos_keys
                   if fund and fund.get(k) is not None}
    packet = {
        "ticker": ticker,
        "asof": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "quant_screen_score": quant_score(tech, fund) if fund else quant,
        "technicals": tech,
        "fundamentals": fund,
        # An empty list invites the sentiment desk to invent headlines —
        # say explicitly that silence is the data (2026-06-10 audit).
        # None = the SOURCE failed (purpose review #6i): the committee must
        # know the feed is DOWN, not read the outage as a quiet tape.
        "news_headlines": (news if news else
                           ["(NEWS SOURCE UNAVAILABLE this cycle — the feed "
                            "errored; you are judging WITHOUT news coverage. "
                            "Do NOT interpret the absence of headlines as calm "
                            "or as any signal.)"] if news is None else
                           ["(no recent headlines found — treat the absence of "
                            "news as the signal; do NOT assume or invent "
                            "coverage)"]),
        "next_earnings": next_earnings(ticker),
        "positioning": (positioning or
                        {"note": "(no positioning data available for this "
                                 "name — do NOT infer crowding or flows)"}),
        # Actuals from a just-released print — the committee judges beat/miss,
        # not just "earnings happened"
        "recent_earnings": recent,
        # Factor lenses side by side — momentum (what got it screened), value
        # (is it cheap?), quality (is the business good?), size, revisions
        # (Street expectations). The committee sees them all; the report card
        # measures which predicts winners.
        "factor_scores": factor_scores(tech, fund),
    }
    if _news_quality is not None:
        packet["news_quality"] = _news_quality      # material vs speculative counts
    # On a FRESH print, attach the primary-source filings (8-K/10-Q) + transcript
    # so the desks judge the print on the actual documents (gated, cached, soft).
    docs = earnings_docs(ticker, recent)
    if docs:
        packet["earnings_docs"] = docs
    return packet


def spy_return_since(start_iso: str, base_px: float | None = None,
                     now_px: float | None = None) -> float | None:
    """Benchmark return since a moment. With base_px (the ACTUAL SPY print
    captured at that moment) the comparison is exactly 1:1 with the fund's
    own start; without it, falls back to the close-of-start-date convention
    (2026-06-11 operator audit: the close baseline was 0.26pts off the true
    2:59 PM inception print — conventions are not reality).

    now_px pins the CURRENT side: during market hours every line on screen
    must price SPY at the same instant (2026-06-12 operator catch: TODAY
    used the live quote while SINCE used a cached daily series still ending
    at yesterday's close — the two numbers contradicted each other)."""
    try:
        last = None
        if isinstance(now_px, (int, float)) and now_px > 0:
            last = float(now_px)
        ser = fetch_history([config.BENCHMARK], period="6mo")[config.BENCHMARK].dropna()
        if last is None:
            if not len(ser):
                return None
            last = float(ser.iloc[-1])
        if isinstance(base_px, (int, float)) and base_px > 0:
            return (last / float(base_px) - 1) * 100
        if not start_iso:
            return None
        seg = ser[ser.index >= pd.Timestamp(start_iso[:10])]
        if not len(seg) or float(seg.iloc[0]) <= 0:
            return None
        return (last / float(seg.iloc[0]) - 1) * 100
    except Exception:
        return None


# ── ETF rotation signals ──────────────────────────────────────────────────────

def rotation_signals(universe: list[str] | None = None) -> dict:
    """Dual-momentum ranking (blended 3m/6m return on COMPLETED bars) over the
    rotation universe by default, or ANY ETF universe when passed (operator
    2026-07-16: the same validated engine now also earns the alpha/calls
    PARKING seat and the core_blend CORE seats — firm/allocation.py)."""
    uni = list(universe) if universe else list(config.ROTATION_UNIVERSE)
    tickers = sorted(set(uni + [config.CASH_ETF, config.BENCHMARK]))
    closes = fetch_history(tickers)
    # Decide on COMPLETED daily bars only (2026-06-10 audit): mid-morning the
    # last row is 90 minutes of partial trading — a 28-day allocation lock
    # must not key off intraday noise
    try:
        from zoneinfo import ZoneInfo
        today_et = datetime.now(ZoneInfo("America/New_York")).date()
        if len(closes) and pd.Timestamp(closes.index[-1]).date() >= today_et:
            closes = closes.iloc[:-1]
    except Exception:
        pass
    w3, w6 = config.SLEEVES["etf_rotation"]["mom_blend"]
    scores = {}
    for t in uni:
        if t not in closes.columns:
            continue
        s = closes[t].dropna()
        if len(s) < 130:
            continue
        scores[t] = round(w3 * _ret(s, 63) + w6 * _ret(s, 126), 2)
    cash_ret = _ret(closes[config.CASH_ETF].dropna(), 63) if config.CASH_ETF in closes else 0.0
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return {"ranked": ranked, "cash_hurdle_3m": round(cash_ret, 2)}


def _max_drawdown_pct(s: pd.Series, days: int = 126) -> float:
    """Worst peak-to-trough %% over the last `days` completed bars (positive
    number, e.g. 12.4 = a -12.4%% drawdown)."""
    w = s.dropna().iloc[-days:]
    if len(w) < 2:
        return 0.0
    dd = (w / w.cummax() - 1.0) * 100
    return float(round(-dd.min(), 2))


def _reservoir_fitness_from_closes(closes: pd.DataFrame) -> dict:
    """Pure math core of reservoir_fitness (separated so the offline suite can
    feed synthetic closes). Gates + score per the operator's 2026-07-20 spec:
    'like SPY but better — not too volatile, not too stagnant, safe parking
    best to keep going forward', explicitly NOT the momentum race.
      GATES (SPY-like behavior, no name list):
        - >= PARKING_MIN_HISTORY_D completed bars
        - ann vol inside PARKING_VOL_BAND x SPY's OWN ann vol (high side =
          'not too volatile' — SOXX-class sector funds fail here; low side =
          'not stagnant' — cash/short-bond funds fail here)
        - daily-return correlation to SPY >= PARKING_MIN_CORR_SPY (broad-
          market behavior — gold/commodity/inverse funds fail here)
      SCORE (risk-adjusted forward drift, Sharpe-like units):
        0.6 * (6m ret / ann vol) + 0.4 * (3m ret / ann vol)
        - 0.03 * max(0, own 6m max-drawdown - SPY's 6m max-drawdown)
    SPY always competes (the baseline the operator named); a challenger must
    beat the incumbent by PARKING_FITNESS_SWITCH_MARGIN across the streak."""
    bench = config.BENCHMARK
    out = {"ranked": [], "gates": {}}
    if bench not in closes.columns:
        return out
    spy = closes[bench].dropna()
    spy_dr = spy.pct_change().dropna().iloc[-126:]
    spy_vol = float(spy_dr.std() * (252 ** 0.5) * 100)
    spy_dd = _max_drawdown_pct(spy)
    if spy_vol <= 0 or len(spy) < int(getattr(config, "PARKING_MIN_HISTORY_D", 130)):
        return out
    lo_m, hi_m = getattr(config, "PARKING_VOL_BAND", (0.55, 1.35))
    min_corr = float(getattr(config, "PARKING_MIN_CORR_SPY", 0.60))
    min_hist = int(getattr(config, "PARKING_MIN_HISTORY_D", 130))
    scores = {}
    for t in closes.columns:
        s = closes[t].dropna()
        if len(s) < min_hist:
            out["gates"][t] = "history"
            continue
        dr = s.pct_change().dropna().iloc[-126:]
        vol = float(dr.std() * (252 ** 0.5) * 100)
        if t != bench and not (lo_m * spy_vol <= vol <= hi_m * spy_vol):
            out["gates"][t] = (f"vol {vol:.0f}% outside "
                               f"[{lo_m * spy_vol:.0f}-{hi_m * spy_vol:.0f}]")
            continue
        if t != bench:
            j = dr.index.intersection(spy_dr.index)
            if len(j) < 60:
                out["gates"][t] = "overlap"
                continue
            corr = float(dr.loc[j].corr(spy_dr.loc[j]))
            if not (corr >= min_corr):        # NaN corr fails too
                out["gates"][t] = f"corr {corr:.2f} < {min_corr}"
                continue
        if vol <= 0:
            out["gates"][t] = "zero vol"
            continue
        dd_pen = 0.03 * max(0.0, _max_drawdown_pct(s) - spy_dd)
        scores[t] = round(0.6 * _ret(s, 126) / vol
                          + 0.4 * _ret(s, 63) / vol - dd_pen, 3)
    out["ranked"] = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return out


def reservoir_fitness(universe: list[str] | None = None) -> dict:
    """Parking-seat selector (operator 2026-07-20) — see
    _reservoir_fitness_from_closes for the gates/score contract. Fetches 1y of
    completed daily bars for the candidate universe + SPY; same completed-bar
    discipline as rotation_signals (a seat review must not key off 90 minutes
    of partial trading)."""
    uni = sorted(set((universe or []) + [config.BENCHMARK]))
    closes = fetch_history(uni, period="1y")
    try:
        from zoneinfo import ZoneInfo
        today_et = datetime.now(ZoneInfo("America/New_York")).date()
        if len(closes) and pd.Timestamp(closes.index[-1]).date() >= today_et:
            closes = closes.iloc[:-1]
    except Exception:
        pass
    return _reservoir_fitness_from_closes(closes)
