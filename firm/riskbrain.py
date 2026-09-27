"""Risk Brain — diagnoses WHY a drawdown happened and responds proportionally.

A kill-switch breach is a symptom with three different diseases:

  sector        the sleeve's concentrated sector is in a rout while the broad
                market holds -> DON'T stop the whole firm: restrict NEW buys
                in the damaged sector(s) only, size everything else at half,
                and recover when the sector ETF itself reclaims its level
  market_wide   SPY itself is down hard / most sectors red -> full halt
                (cooling-off + market-recovery resume, graduated re-entry)
  idiosyncratic market and sectors are fine but the sleeve bled anyway ->
                the PICKS are failing, not the market: full halt, and the
                learning review is explicitly told to study why

Diagnosis is deterministic and instant at trigger time (one batched price
fetch); Opus then confirms or escalates within the next health sweep. Every
event and its outcome lands in state/risk_events.json and is fed to the
performance review — the firm learns from every stop.
"""
import json
from datetime import datetime, timezone

import config
from firm import brain, state

SECTOR_ETF = {
    "Technology": "XLK", "Financial Services": "XLF", "Healthcare": "XLV",
    "Energy": "XLE", "Industrials": "XLI", "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP", "Utilities": "XLU", "Basic Materials": "XLB",
    "Communication Services": "XLC", "Real Estate": "XLRE",
}
MARKET_WIDE_SPY_RET = -8.0    # SPY 1-month return at/below this = market event
MARKET_WIDE_BREADTH = 6      # ... or this many sector ETFs down >5% in a month
SECTOR_HIT_RET = -10.0       # a dominant sector ETF down this much = sector event
MAX_EVENTS = 40


def market_snapshot() -> dict:
    """1-month returns for SPY + all 11 sector ETFs, plus current prices."""
    from firm import data_engine
    tickers = sorted(set(SECTOR_ETF.values()) | {config.BENCHMARK})
    closes = data_engine.fetch_history(tickers, period="3mo")
    rets, prices = {}, {}
    for t in tickers:
        if t not in closes.columns:
            continue
        s = closes[t].dropna()
        if len(s) > 21:
            rets[t] = round(float(s.iloc[-1] / s.iloc[-22] - 1) * 100, 2)
            prices[t] = round(float(s.iloc[-1]), 2)
    return {"ret_1m": rets, "prices": prices}


def _dominant_sectors(s: dict) -> list[str]:
    """Sleeve's sector exposure by position value, largest first."""
    by_sector: dict[str, float] = {}
    for pos in s.get("positions", {}).values():
        sec = pos.get("sector") or ""
        if sec:
            by_sector[sec] = by_sector.get(sec, 0) + pos.get(
                "market_value", pos.get("cost", 0))
    return sorted(by_sector, key=by_sector.get, reverse=True)


def diagnose(sleeve: str, s: dict, dd_pct: float,
             snapshot: dict | None = None) -> dict:
    """Classify the drawdown. Deterministic, fast, safe-by-default (anything
    unclassifiable is treated as market_wide -> full halt)."""
    try:
        snap = snapshot or market_snapshot()
    except Exception:
        snap = {"ret_1m": {}, "prices": {}}
    rets = snap.get("ret_1m", {})
    spy_ret = rets.get(config.BENCHMARK, 0.0)
    breadth_red = sum(1 for etf in SECTOR_ETF.values()
                      if rets.get(etf, 0) <= -5.0)
    dominant = _dominant_sectors(s)
    dom_etfs = [SECTOR_ETF[sec] for sec in dominant if sec in SECTOR_ETF]
    hit_sectors = [sec for sec in dominant
                   if SECTOR_ETF.get(sec) and rets.get(SECTOR_ETF[sec], 0) <= SECTOR_HIT_RET]

    if not rets:
        cls = "market_wide"          # no data -> safest assumption
    elif spy_ret <= MARKET_WIDE_SPY_RET or breadth_red >= MARKET_WIDE_BREADTH:
        cls = "market_wide"
    elif hit_sectors:
        cls = "sector"
    else:
        cls = "idiosyncratic"

    restricted = hit_sectors if cls == "sector" else []
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "sleeve": sleeve, "drawdown_pct": round(dd_pct, 2),
        "classification": cls, "spy_ret_1m": spy_ret,
        "sector_breadth_red": breadth_red,
        "dominant_sectors": dominant[:4],
        "hit_sectors": hit_sectors,
        "restricted_sectors": restricted,
        "restricted_levels": {SECTOR_ETF[sec]: snap.get("prices", {}).get(SECTOR_ETF[sec])
                              for sec in restricted if sec in SECTOR_ETF},
        "sector_returns_1m": {e: rets.get(e) for e in dom_etfs},
        "opus_confirmed": False, "source": "deterministic",
    }


def record_event(event: dict, action: str) -> None:
    ev = {**event, "action": action}
    with state.SLEEVE_LOCK:
        re_ = state.load("risk_events", {"events": []})
        re_["events"] = (re_.get("events", []) + [ev])[-MAX_EVENTS:]
        state.save("risk_events", re_)
    state.journal("risk_event", sleeve=event["sleeve"],
                  classification=event["classification"], action=action,
                  drawdown=event["drawdown_pct"])


def record_halt(sleeve: str, kind: str, reason: str) -> None:
    """EVERY halt becomes a learnable risk event — exit failures, reconcile
    drift, state corruption — not just kill switches. The review judges
    whether each halt was warranted and whether recovery was handled well."""
    record_event({
        "ts": datetime.now(timezone.utc).isoformat(),
        "sleeve": sleeve, "classification": kind,
        "drawdown_pct": None, "reason": reason[:200],
        "opus_confirmed": True,   # operational halts need no market diagnosis
        "source": "operational",
    }, action="full_halt")


def record_outcome(sleeve: str, outcome: str, **fields) -> None:
    """Attach what happened next to the most recent event for this sleeve —
    the learning review judges whether the response was right."""
    with state.SLEEVE_LOCK:
        re_ = state.load("risk_events", {"events": []})
        for ev in reversed(re_.get("events", [])):
            if ev.get("sleeve") == sleeve and "outcome" not in ev:
                ev["outcome"] = outcome
                ev["outcome_ts"] = datetime.now(timezone.utc).isoformat()
                ev.update(fields)
                break
        state.save("risk_events", re_)


def opus_confirm(event: dict) -> dict | None:
    """Second opinion from Opus on a pending diagnosis. Conservative rule:
    Opus may ESCALATE severity (sector -> market_wide upgrades a restriction
    to a full halt) but never de-escalate a halt — annotations only."""
    if not brain.available():
        return None
    prompt = (
        "You are the CHIEF RISK OFFICER at an institutional asset manager. A "
        "drawdown circuit breaker fired and was classified deterministically. "
        "Review the evidence and give your independent classification:\n"
        "  market_wide   — broad market event\n"
        "  sector        — concentrated sector rout, rest of market fine\n"
        "  idiosyncratic — market and sectors fine; the sleeve's picks failed\n\n"
        f"EVENT:\n{json.dumps({k: v for k, v in event.items() if k != 'opus_confirmed'}, indent=1, default=str)}\n\n"
        'Respond with ONLY this JSON: {"classification": "market_wide|sector|'
        'idiosyncratic", "confidence": <0-100>, '
        '"note": "<2 sentences: what is really going on and what to watch>"}'
    )
    return brain.ask_json(prompt, retries=1)


def events_for_review(n: int = 8) -> list[dict]:
    return state.load("risk_events", {}).get("events", [])[-n:]


# ── Sector regime: the same X-ray, pointed UP as well as down ────────────────

LEADING_RET = 5.0    # sector ETF 1m return >= this AND beating SPY = leading
LAGGING_RET = -5.0   # sector ETF 1m return <= this = lagging

_ETF_SECTOR = {v: k for k, v in SECTOR_ETF.items()}


def sector_regime(snapshot: dict | None = None) -> dict:
    """Which sectors have the wind at their back, which are bleeding.
    Saved every cycle (history) and stamped onto every research packet and
    every trade, so the firm learns whether riding sector tailwinds wins."""
    try:
        snap = snapshot or market_snapshot()
    except Exception:
        snap = {"ret_1m": {}}
    rets = snap.get("ret_1m", {})
    spy = rets.get(config.BENCHMARK, 0.0)
    leading, lagging = [], []
    for etf, sector in _ETF_SECTOR.items():
        r = rets.get(etf)
        if r is None:
            continue
        if r >= LEADING_RET and r > spy:
            leading.append(sector)
        elif r <= LAGGING_RET:
            lagging.append(sector)
    regime = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "spy_ret_1m": spy,
        "leading": sorted(leading, key=lambda sec: -rets[SECTOR_ETF[sec]]),
        "lagging": sorted(lagging, key=lambda sec: rets[SECTOR_ETF[sec]]),
        "sector_returns_1m": {SECTOR_ETF[s]: rets.get(SECTOR_ETF[s])
                              for s in _ETF_SECTOR.values()
                              if SECTOR_ETF.get(s) in rets},
    }
    with state.SLEEVE_LOCK:
        st = state.load("sector_regime", {"history": []})
        st["latest"] = regime
        st["history"] = (st.get("history", []) + [
            {k: regime[k] for k in ("ts", "spy_ret_1m", "leading", "lagging")}])[-60:]
        state.save("sector_regime", st)
    return regime


def latest_regime() -> dict:
    return state.load("sector_regime", {}).get("latest", {})


def vol_multiplier() -> float:
    """Volatility-regime scaling for the daily breaker: when the MARKET is
    moving fast (10-day realized vol vs 60-day baseline), a bigger daily
    swing is normal breathing, not a crisis. Clamped 1.0-1.75x, cached 2h."""
    cached = state.load("vol_regime", {})
    try:
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(cached.get("ts", ""))).total_seconds() / 3600
        if 0 <= age_h < 2:
            return float(cached.get("multiplier", 1.0))
    except (ValueError, TypeError):
        pass
    mult = 1.0
    try:
        from firm import data_engine
        closes = data_engine.fetch_history([config.BENCHMARK],
                                           period="6mo")[config.BENCHMARK].dropna()
        rets = closes.pct_change().dropna()
        vol10 = float(rets.tail(10).std())
        vol60 = float(rets.tail(60).std())
        if vol60 > 0:
            mult = max(1.0, min(1.75, vol10 / vol60))
    except Exception:
        mult = 1.0
    state.save("vol_regime", {"ts": datetime.now(timezone.utc).isoformat(),
                              "multiplier": round(mult, 2)})
    return round(mult, 2)
