"""Weekly vendor-data integrity audit — self-healing (2026-06-13).

WHY: the FMP integration shipped two silent scale/units bugs (D/E 100x,
fwd_pe mislabel). A vendor can rot at any time — a field rename returns null,
a units flip multiplies by 100, a definition change skews a metric. This task
catches that class of rot within a week and AUTO-FIXES it.

HOW (robust + programmatic, no LLM in the hot path):
  - For held positions + top research candidates, fetch FMP raw (bypassing the
    blocklist so blocked fields are re-tested for recovery) and yfinance.
  - Per scored field, judge FMP: out-of-plausible-band, a sign flip, or a >3x
    magnitude divergence from yfinance = a "bad" reading. (3x, not 25%, so legit
    definitional gaps like bank margins don't false-trigger; a 100x scale flip
    trips instantly.)
  - A field STRIKES this week only if it reads bad across a MAJORITY of names
    (systematic rot hits every name; a one-off bank quirk hits one).
  - AUTO-FIX: STRIKES_TO_BLOCK consecutive weekly strikes -> the field is added
    to the FMP blocklist; fmp.fundamentals() stops emitting it and the merge
    keeps yfinance's value. STRIKES_TO_CLEAR clean weeks -> auto-re-enabled.
    Auto-fix always degrades to the trusted second source, never to garbage.
  - ALWAYS journals a summary and alerts the operator on any block/unblock (and
    a soft "watch" alert on the first strike). For a deep adjudication vs
    stockanalysis/SEC, the alert tells the operator to run the LLM audit.

Registered as the "LochRaven Vendor Audit" scheduled task (Sundays 17:00, S4U).
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import config                                  # noqa: E402
from firm import state                         # noqa: E402

# Per-field magnitude floor: differences below this are noise, not drift, so a
# margin of 0.001 vs 0.02 doesn't read as a huge ratio. Tuned so a real 100x
# scale flip still trips while definitional wobble does not.
_FIELD_FLOOR = {
    "pe": 1.0, "ps": 0.2, "profit_margin": 0.02, "op_margin": 0.02,
    "roe": 0.02, "debt_to_equity": 0.05,
}


def _field_verdict(field: str, fmp_v, yf_v) -> str:
    """'skip' | 'ok' | 'bad'. 'bad' = FMP value is impossible, sign-flipped, or
    diverges >Nx from a plausible yfinance value (i.e. a units/scale/rot bug)."""
    from firm import data_engine
    if not isinstance(fmp_v, (int, float)) or fmp_v != fmp_v:
        return "skip"                                   # FMP has nothing to audit here
    if not data_engine._fund_plausible(field, fmp_v):
        return "bad"                                    # impossible value = definite strike
    if (not isinstance(yf_v, (int, float)) or yf_v != yf_v
            or not data_engine._fund_plausible(field, yf_v)):
        return "ok"                                     # no trustworthy 2nd source; FMP in-band
    if (fmp_v < 0) != (yf_v < 0) and max(abs(fmp_v), abs(yf_v)) > 0.05:
        return "bad"                                    # sign flip on a non-trivial value
    floor = _FIELD_FLOOR.get(field, 0.01)
    a, b = max(abs(fmp_v), floor), max(abs(yf_v), floor)
    return "bad" if max(a, b) / min(a, b) > config.VENDOR_AUDIT_DIVERGENCE else "ok"


def decide_strikes(per_name: list[dict]) -> tuple[set, dict]:
    """per_name = [{field: verdict, ...}, ...]. A field strikes this week only
    if it reads 'bad' for a majority of names that had comparable data (>=2),
    so systematic rot trips but a single idiosyncratic name does not."""
    striking, summary = set(), {}
    for field in config.VENDOR_AUDIT_FIELDS:
        verdicts = [n.get(field, "skip") for n in per_name]
        bad = sum(1 for v in verdicts if v == "bad")
        comparable = sum(1 for v in verdicts if v != "skip")
        summary[field] = {"bad": bad, "comparable": comparable}
        if comparable >= 2 and bad >= max(2, (comparable + 1) // 2):
            striking.add(field)
    return striking, summary


def update_blocklist(prior: dict, striking: set, audited: set) -> tuple[dict, list]:
    """Pure state transition: strike counters -> block/unblock decisions.
    Returns (new_state, actions). Only fields with enough data this week
    ('audited') move their counters; a no-data week leaves them untouched."""
    fields = list(prior.get("fields", []))
    strikes = dict(prior.get("strikes", {}))
    clear = dict(prior.get("clear", {}))
    actions = []
    for field in audited:
        if field in striking:
            strikes[field] = strikes.get(field, 0) + 1
            clear[field] = 0
            if strikes[field] >= config.VENDOR_AUDIT_STRIKES_TO_BLOCK and field not in fields:
                fields.append(field)
                actions.append(("block", field, strikes[field]))
        else:
            clear[field] = clear.get(field, 0) + 1
            # DECAY, not hard-reset: a field that's bad every week blocks fast,
            # one that's bad most weeks (with occasional clean ones) still
            # accumulates and blocks, but a pure alternating blip nets ~0 and is
            # left alone. Hard-reset would let a mostly-broken field never block.
            strikes[field] = max(0, strikes.get(field, 0) - 1)
            if field in fields and clear[field] >= config.VENDOR_AUDIT_STRIKES_TO_CLEAR:
                fields.remove(field)
                actions.append(("unblock", field, clear[field]))
    return {"fields": fields, "strikes": strikes, "clear": clear,
            "last_audit": state._now()}, actions


def _audit_universe() -> list[str]:
    """Held equity tickers across all sleeves + latest research candidates,
    deduped and capped. Plain tickers only (option contracts/cash skipped)."""
    import re
    plain = re.compile(r"^[A-Z]{1,5}$")
    # Canaries first — a guaranteed diverse baseline so systematic vendor rot is
    # caught even in a week the fund holds no individual stocks.
    names: list[str] = list(config.VENDOR_AUDIT_CANARIES)
    for sleeve in config.SLEEVES:
        # Raw load, NOT get_sleeve: get_sleeve HALTS + alerts on a corrupt sleeve
        # file (state.py) — the audit must never trip a production halt just to
        # read a ticker list.
        sl = state.load(f"sleeve_{sleeve}", {})
        if not isinstance(sl, dict):
            continue
        for sym, pos in (sl.get("positions") or {}).items():
            for cand in (sym, (pos or {}).get("underlying"), (pos or {}).get("symbol")):
                if isinstance(cand, str) and plain.match(cand):
                    names.append(cand)
                    break
    hist = state.load("research", {}).get("history", [])
    if hist:
        for t in (hist[-1].get("verdicts") or {}):
            if plain.match(str(t)):
                names.append(t)
    seen, uniq = set(), []
    for n in names:
        if n not in seen and n not in (config.CASH_ETF, config.BENCHMARK):
            seen.add(n)
            uniq.append(n)
    return uniq[:config.VENDOR_AUDIT_MAX_NAMES]


def audit_once(universe: list[str] | None = None) -> dict:
    from firm import data_engine, fmp, alerts
    if not fmp.available():
        state.journal("vendor_audit", skipped="no FMP key")
        return {"skipped": "no FMP key"}
    names = universe if universe is not None else _audit_universe()
    per_name = []
    for t in names:
        try:
            fmp_raw = fmp.fundamentals(t, respect_blocklist=False)  # test blocked too
            yf_raw = data_engine._fetch_fundamentals_yf(t)
        except Exception:
            continue
        if not fmp_raw:
            continue
        per_name.append({f: _field_verdict(f, fmp_raw.get(f), yf_raw.get(f))
                         for f in config.VENDOR_AUDIT_FIELDS})
    striking, summary = decide_strikes(per_name)
    audited = {f for f, s in summary.items() if s["comparable"] >= 2}
    prior = state.load("fmp_blocklist", {"fields": [], "strikes": {}, "clear": {}})
    new_state, actions = update_blocklist(prior, striking, audited)
    state.save("fmp_blocklist", new_state)
    state.journal("vendor_audit", names=len(per_name), summary=summary,
                  striking=sorted(striking), blocked=new_state["fields"],
                  actions=[f"{a}:{f}" for a, f, _ in actions])
    for kind, field, n in actions:
        if kind == "block":
            alerts.alert(f"VENDOR DRIFT: FMP {field} auto-disabled",
                         f"FMP '{field}' diverged from yfinance for {n} straight weeks "
                         f"— auto-degraded to yfinance. Run the LLM audit to adjudicate "
                         f"vs stockanalysis/SEC, then clear state/fmp_blocklist.json if FMP is fixed.",
                         "critical")
        else:
            alerts.alert(f"FMP {field} re-enabled", f"'{field}' re-agreed with yfinance "
                         f"for {n} weeks — FMP sourcing restored.", "info")
    # soft watch alert on a fresh first strike (not yet a block)
    for field in striking:
        if new_state["strikes"].get(field, 0) == 1 and field not in new_state["fields"]:
            alerts.alert(f"VENDOR WATCH: FMP {field} drift",
                         f"FMP '{field}' diverged from yfinance this week (strike 1/"
                         f"{config.VENDOR_AUDIT_STRIKES_TO_BLOCK}). Auto-disables if it persists.",
                         "info")
    return {"names": len(per_name), "striking": sorted(striking),
            "blocked": new_state["fields"], "actions": [f"{a}:{f}" for a, f, _ in actions]}


def main() -> None:
    res = audit_once()
    print(f"vendor_audit: {res}")
    sys.exit(0)


if __name__ == "__main__":
    main()
