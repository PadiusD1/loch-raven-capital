"""Nightly display reconciliation — the dashboard's numbers rebuilt from
FIRST PRINCIPLES (position quantities x official closes + cash) and compared
against what /api/overview actually serves. Any daylight beyond tolerance is
journaled and alerted.

Born 2026-06-11 after three same-day operator catches that all shared one
root: numbers about the same thing computed under different conventions
(live prints vs official closes, first-mark-of-day vs prior-close anchors,
date conventions vs recorded prints). The convention is now uniform; this
script is the standing proof that it stays uniform.

Runs from the postmarket wrap (closes are final by then). Read-only on the
books; pure comparison — except the one self-heal below.

Two permanent guards (operator 2026-07-17, "these keep popping up"):
  1. MARKET-OPEN SKIP — mid-session the comparison is convention-INVALID:
     /api/overview marks positions at LIVE Public quotes while yfinance
     serves a delayed feed, so the "drift" is just the tape's last ~15
     minutes (the 15:23Z calls_only flap alerted and self-cleared 17 min
     later). Something invoked this script every ~15 min intraday; guarding
     HERE makes every invoker — known or unknown — safe. The skip leaves the
     stored verdict untouched so the health line keeps the last VALID result.
  2. SELF-HEAL BEFORE ALERTING — nearly every after-close drift so far was
     frozen display marks (no live quote after the bell + the FIX 1 stale
     guard), not a book error. On drift, snap the books to official closes
     via the app's /api/mark-close (single-writer: the app books it) and
     re-compare once; alert only if the drift SURVIVES the snap. What
     survives is a genuine book/convention defect worth a human.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

TOLERANCE_PCT = 0.25      # display vs reconstruction, in return points


def _fetch_overview() -> dict:
    with urllib.request.urlopen("http://127.0.0.1:8420/api/overview",
                                timeout=15) as r:
        return json.load(r)


def _rebuild_drifts(yf, config, state, full: dict) -> list:
    """One full comparison pass: SPY-since line + every equity-only sleeve."""
    shown = full["sleeves"]
    drifts = []

    # Benchmark line: after the close, every displayed SPY figure must equal
    # the raw-closes computation against the stored inception print — drift
    # here means a freshness/convention regression (the 2026-06-12 class:
    # TODAY priced live while SINCE priced off a stale cached series)
    try:
        spy_last = float(yf.Ticker(config.BENCHMARK)
                         .history(period="5d")["Close"].iloc[-1])
        # The overview's firm SPY line uses the base_px of the OLDEST-inception
        # sleeve (app.py firm_start_ts/firm_base_px) — rebuild with the SAME
        # convention. Hardcoding alpha_stocks here caused the 2026-07-01 20:31
        # false alarm: alpha's baseline was mid-recovery while the firm line
        # (correctly) used another sleeve's print. Convention checks must share
        # the displayed number's own convention or they alert on themselves.
        oldest = min((state.get_sleeve(n) for n in config.SLEEVES),
                     key=lambda s: s.get("inception") or "9999")
        base = oldest.get("bench_inception_px")
        shown_spy = (full.get("firm") or {}).get("spy_return_pct")
        if (isinstance(base, (int, float)) and base > 0
                and isinstance(shown_spy, (int, float))):
            truth_spy = (spy_last / base - 1) * 100
            if abs(truth_spy - shown_spy) > 0.05:
                drifts.append({"sleeve": "SPY-since-line", "shown": shown_spy,
                               "rebuilt": round(truth_spy, 2)})
    except Exception:
        pass
    for name in config.SLEEVES:
        s = state.get_sleeve(name)
        total = s["cash"]
        for sym, p in s.get("positions", {}).items():
            if p.get("instrument") != "equity":
                total = None      # options books reconcile via the EOD record
                break
            try:
                px = float(yf.Ticker(sym).history(period="2d")["Close"].iloc[-1])
            except Exception:
                total = None
                break
            total += p["quantity"] * px
        if total is None:
            continue
        truth_pct = (total / s["starting_capital"] - 1) * 100
        shown_pct = shown.get(name, {}).get("total_return_pct")
        if shown_pct is None or abs(shown_pct - truth_pct) > TOLERANCE_PCT:
            drifts.append({"sleeve": name, "shown": shown_pct,
                           "rebuilt": round(truth_pct, 2)})
    return drifts


def main() -> int:
    import yfinance as yf

    import config
    from firm import state

    # Guard 1: only grade against SETTLED closes. Deliberately does NOT
    # overwrite state/reconcile_display.json — the health line keeps the last
    # valid verdict instead of flapping on an intraday artifact.
    try:
        from firm import market_hours
        # REGULAR hours only. A 2026-08-20 attempt widened this to
        # `or extended_session_open()` to stop a morning false-drift alarm, and
        # that was WRONG: the after-hours window runs to 20:00 ET, so it also
        # silenced the postmarket wrap's own reconciliation at ~16:30 ET — the
        # nightly rebuild simply stopped happening (operator: "the fund doesn't
        # close at market close like it used to"). The morning false drift is
        # real but it is not a session question, and the fix for it is the
        # REPAINT precondition below, which asks the outcome question directly:
        # is the display still carrying the closes it was snapped to?
        _phase = market_hours.session_phase()
        _plabel = market_hours.session_phase_label()
        if _phase == "market_hours":
            state.journal("reconcile_display_skipped", phase=_phase,
                          reason="MARKET HOURS — display marks are live; the "
                                 "raw-close rebuild is only valid after the "
                                 "close")
            print(f"display reconciliation: SKIPPED [{_plabel}] — marks are "
                  "live, the raw-close rebuild is only valid after the close")
            return 0
    except Exception:
        pass                      # gate unavailable: proceed as before

    # Guard 2: REPAINT PRECONDITION (operator 2026-08-21). This check compares a
    # LIVE display surface against a STATIC rebuild from official closes, so it
    # is only meaningful while the display still carries those closes. The
    # monitor repaints continuously (it must — stops read the same field), so
    # outside the narrow window after mark_books_to_close the two sides are
    # simply different quantities and any difference is an artifact, not a book
    # defect. Ask the books, not the clock: do the held marks still equal the
    # closes we stamped? Skipping without overwriting the stored verdict keeps
    # the health line on its last VALID reading rather than flapping.
    try:
        import config as _cfg2
        from firm import state as _st2
        _stamp = _st2.load("eod_mark_stamp", {}) or {}
        _sm = _stamp.get("marks") or {}
        if _sm:
            _moved, _checked = [], 0
            for _sl in _cfg2.SLEEVES:
                for _sym, _p in (_st2.get_sleeve(_sl).get("positions") or {}).items():
                    if _p.get("instrument") != "equity" or _sym not in _sm:
                        continue
                    _cur = _p.get("last_price")
                    if not isinstance(_cur, (int, float)):
                        continue
                    _checked += 1
                    if abs(float(_cur) - float(_sm[_sym])) > 0.005:
                        _moved.append(_sym)
            if _checked and _moved:
                # Name the phase: repainted during PRE-MARKET means the
                # stamped close is yesterday's and the day has moved on;
                # repainted during AFTER HOURS means today's close is fresh and
                # the post-close quotes have simply drifted off it. Same skip,
                # different meaning, and the operator should not have to guess.
                _st2.journal("reconcile_display_skipped", phase=_phase,
                             reason=f"{_plabel} — books repainted since the "
                                    "EOD mark",
                             moved=len(_moved), checked=_checked,
                             symbols=sorted(_moved)[:12])
                print(f"display reconciliation: SKIPPED [{_plabel}] — "
                      f"{len(_moved)} of {_checked} marks have moved off the "
                      "stamped close, so a raw-close rebuild is not comparable")
                return 0
    except Exception:
        pass                      # precondition unavailable: grade as before

    try:
        full = _fetch_overview()
    except Exception as e:
        state.journal("reconcile_display", ok=False,
                      error=f"overview unreachable: {e}")
        return 1

    drifts = _rebuild_drifts(yf, config, state, full)

    # Guard 2: self-heal frozen marks, then re-judge. Sleeve drifts are almost
    # always display marks frozen at the last live quote; the authoritative
    # close fixes them. A surviving drift is the real signal.
    if any(d["sleeve"] in config.SLEEVES for d in drifts):
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8420/api/mark-close", method="POST")
            with urllib.request.urlopen(req, timeout=120):
                pass
            time.sleep(2)
            full = _fetch_overview()
            healed = _rebuild_drifts(yf, config, state, full)
            state.journal("reconcile_display_selfheal",
                          drifts_before=len(drifts), drifts_after=len(healed))
            drifts = healed
        except Exception:
            pass                  # heal unavailable: judge the raw comparison

    ok = not drifts
    state.journal("reconcile_display", ok=ok,
                  **({"drifts": drifts} if drifts else {}))
    # Persist the latest result where health_report can SEE it (IG 2026-07-01:
    # a critical reconcile failure lived only in the journal while every
    # health line read green — the report card contradicted the books)
    from datetime import datetime, timezone
    state.save("reconcile_display", {
        "asof": datetime.now(timezone.utc).isoformat(),
        "ok": ok, "drifts": drifts})
    if drifts:
        try:
            from firm import alerts
            alerts.alert("DISPLAY RECONCILIATION FAILED",
                         f"{len(drifts)} sleeve(s) drift from raw-close "
                         f"reconstruction AFTER a mark-to-close self-heal: "
                         f"{drifts[0]} — a real book/convention defect; "
                         "open Claude in ai-fund")
        except Exception:
            pass
    print(f"display reconciliation: {'OK' if ok else 'DRIFT ' + str(drifts)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
