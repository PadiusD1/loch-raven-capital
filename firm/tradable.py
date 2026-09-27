"""Public.com tradability layer — paper trades ONLY what live can trade.

Operator mandate (2026-07-16): "the fund needs the same access as the app it
will use when it switches live (Public) — 20+ ETFs and 9k+ stocks, mainly
NYSE/NASDAQ — so that at the live switch, research was never limited to less
than what is actually accessible."

Ground truth: the Public API's own instrument list (verified live 2026-07-16:
`get_all_instruments(type_filter=[EQUITY])` returned 22,890 instruments, each
carrying its trading permission — BUY_AND_SELL / SELL-only / DISABLED — plus
fractional and options flags). The SDK's unfiltered response model chokes on
BOND rows, so the fetch is ALWAYS type-filtered to EQUITY (which at Public
includes ETFs).

  state/tradable_universe.json:
    {"asof": ts, "source": "public_api"|"exchange_dirs", "n_buyable": int,
     "buyable": [...], "options_enabled": [...]}

Fallback: when the API/auth is unreachable, the NYSE+NASDAQ exchange
directories approximate the same universe (Public lists mainly those venues);
the last good API snapshot is always preferred over a fresh fallback.

Consumers:
  is_buyable(sym)        — entry gates (paper parity: never build a paper
                           position live could not hold)
  options_enabled(sym)   — the calls desk must not pick contracts Public
                           won't trade
  tradable_etfs()        — the dynamic ETF pool source (allocation.py)
All fail OPEN with a journal line when the universe is missing/stale — a
telemetry gap must never halt trading that was legal yesterday.
"""
from datetime import datetime, timezone

from firm import state

_KEY = "tradable_universe"
_TTL_H = 24.0


def _age_h(iso: str) -> float:
    try:
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(iso)).total_seconds() / 3600
    except (ValueError, TypeError):
        return 1e9


def refresh(force: bool = False) -> dict:
    """Daily pull of Public's instrument list (self-gating TTL, fail-soft).
    Returns the current snapshot either way."""
    snap = state.load(_KEY, {}) or {}
    if not force and snap.get("buyable") and _age_h(snap.get("asof", "")) < _TTL_H:
        return snap
    fresh = _fetch_public()
    if not fresh:
        fresh = _fetch_exchange_dirs()
        if fresh and snap.get("source") == "public_api" \
                and _age_h(snap.get("asof", "")) < 7 * 24:
            # a week-old REAL Public snapshot beats a fresh approximation
            state.journal("tradable_universe_kept_stale_api",
                          age_h=round(_age_h(snap.get("asof", "")), 1))
            return snap
    if fresh:
        state.save(_KEY, fresh)
        state.journal("tradable_universe_refreshed", source=fresh["source"],
                      n_buyable=fresh["n_buyable"],
                      n_options=len(fresh.get("options_enabled") or []))
        return fresh
    state.journal("tradable_universe_refresh_failed",
                  kept=snap.get("source"), age_h=round(_age_h(snap.get("asof", "")), 1))
    return snap


def _fetch_public() -> dict | None:
    """Public API instrument list -> snapshot dict. None on any failure."""
    try:
        import public_api_sdk as sdk
        from firm import broker
        client = broker.get_client()
        req = sdk.InstrumentsRequest(type_filter=[sdk.InstrumentType.EQUITY])
        resp = client.get_all_instruments(req)
        buyable, opts = [], []
        for it in (resp.instruments or []):
            sym = getattr(getattr(it, "instrument", None), "symbol", None)
            if not sym:
                continue
            if str(getattr(it, "trading", "")).endswith("BUY_AND_SELL"):
                buyable.append(sym)
                if str(getattr(it, "option_trading", "")).endswith("BUY_AND_SELL"):
                    opts.append(sym)
        if len(buyable) < 5000:      # a thin list is a throttle/outage, not a market
            state.journal("tradable_universe_thin_api", n=len(buyable))
            return None
        return {"asof": datetime.now(timezone.utc).isoformat(),
                "source": "public_api", "n_buyable": len(buyable),
                "buyable": sorted(buyable), "options_enabled": sorted(opts)}
    except Exception as e:
        state.journal("tradable_universe_api_error", error=str(e)[:150])
        return None


def _fetch_exchange_dirs() -> dict | None:
    """NYSE+NASDAQ directory approximation (stocks + ETFs)."""
    try:
        from firm import universe
        stocks = universe.fetch_all_us_symbols()
        etfs = universe.fetch_all_us_etfs()
        both = sorted(set(stocks) | set(etfs))
        if len(both) < 5000:
            return None
        return {"asof": datetime.now(timezone.utc).isoformat(),
                "source": "exchange_dirs", "n_buyable": len(both),
                "buyable": both, "options_enabled": []}
    except Exception as e:
        state.journal("tradable_universe_dirs_error", error=str(e)[:150])
        return None


def _snap() -> dict:
    return state.load(_KEY, {}) or {}


def is_buyable(sym: str) -> bool:
    """Paper parity gate: True when Public can BUY this symbol. Fails OPEN
    (True) when no universe snapshot exists — a telemetry gap must never
    block trading; the health line surfaces the gap instead."""
    s = _snap()
    b = s.get("buyable")
    if not b:
        return True
    return sym in _buyable_set(s)


def options_enabled(sym: str) -> bool:
    """True when Public trades this name's OPTIONS. Fails OPEN only when the
    snapshot has no options data at all (dirs fallback)."""
    s = _snap()
    opts = s.get("options_enabled")
    if not opts:
        return True
    return sym in _options_set(s)


_cache: dict = {"asof": None, "buyable": frozenset(), "options": frozenset()}


def _buyable_set(s: dict) -> frozenset:
    if _cache["asof"] != s.get("asof"):
        _cache["asof"] = s.get("asof")
        _cache["buyable"] = frozenset(s.get("buyable") or [])
        _cache["options"] = frozenset(s.get("options_enabled") or [])
    return _cache["buyable"]


def _options_set(s: dict) -> frozenset:
    _buyable_set(s)
    return _cache["options"]


def universe_health() -> tuple[bool, str]:
    """Health line: fresh + plausibly sized, else the story."""
    s = _snap()
    if not s.get("buyable"):
        return False, ("no tradable-universe snapshot yet — paper is trading "
                       "without live-parity guardrails; premarket refresh builds it")
    age = _age_h(s.get("asof", ""))
    n = len(s.get("buyable") or [])       # trust the DATA, not the stored count
    detail = (f"{n:,} buyable ({s.get('source')}), "
              f"{len(s.get('options_enabled') or []):,} options-enabled, "
              f"{age:.0f}h old")
    if age > 72:
        # (2026-08-13 IG audit) was " — STALE; refresh failing", asserting a
        # mechanism this line never checked. refresh() always journals one of
        # tradable_universe_refreshed / kept_stale_api / refresh_failed when
        # invoked past its TTL — so journal SILENCE over a stale snapshot
        # means refresh was never invoked, not that it is failing.
        return False, detail + " — STALE; refresh not completing (see journal for last attempt)"
    if n < 8000 and s.get("source") == "public_api":
        return False, detail + " — implausibly thin for Public"
    return True, detail
