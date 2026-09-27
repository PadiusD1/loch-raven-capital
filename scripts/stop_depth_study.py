"""What stop depth would the firm's OWN closed trades have wanted?

WHY (operator 2026-08-19). Graded by exit family over 36-37 closed equity trades:

  trailing stop    n=11  excess  +1.50  win 45%   post_exit  -9.69
  hard stop        n=10  excess -16.88  win  0%   post_exit  -6.97
  PM verdict SELL  n=10  excess  -8.69  win  0%   post_exit  -0.72

Everything the firm sells keeps underperforming, so its sell DIRECTION is right.
The trailing stop is the only exit with positive excess AND strongly negative
post-exit: it fires at the right moment. The hard stop is directionally right
but fires at -16.88 and the name falls a further -6.97, i.e. losers are allowed
to run about 4x further than trailing-stop winners gain.

`stop_pct` is retained on only 7 of 37 ledger rows, so the firm cannot grade its
own stops from the durable record. This reconstructs MAXIMUM ADVERSE EXCURSION
from real daily bars between each trade's open and close, then prices the
counterfactual: at candidate depths, when would the stop have fired and what
would the trade have returned instead?

HONEST LIMITS, stated up front:
  * MAE is measured on daily CLOSES, so an intraday spike through a level is
    invisible. Real fills would be slightly worse than modelled.
  * n=10 hard stops. A depth fitted to 10 observations is not a fit, it is an
    anecdote. This script reports the whole CURVE across depths so the shape
    can be judged, not one "optimal" number chosen by peeking.
  * The counterfactual assumes the position is closed at the stop level and the
    capital sits idle afterwards. It does NOT credit redeployment, so it is
    conservative toward tightening.

Run:  py -3.11 scripts/stop_depth_study.py
"""
import statistics as st
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import config                              # noqa: E402
from firm import data_engine, state        # noqa: E402


def main() -> None:
    led = state.load("ledger", {}) or {}
    rows = led.get("trades") if isinstance(led, dict) else led
    eq = [r for r in (rows or [])
          if r.get("sleeve") != "calls_only"
          and isinstance(r.get("excess_ret_pct"), (int, float))
          and r.get("opened_at") and r.get("closed_at")
          and isinstance(r.get("entry"), (int, float)) and r["entry"] > 0]
    tickers = sorted({str(r.get("ticker") or r.get("symbol")) for r in eq})
    print(f"{len(eq)} closed equity trades, {len(tickers)} tickers", flush=True)

    closes = data_engine.fetch_history(tickers + [config.BENCHMARK], period="1y")
    closes = closes.loc[:, ~closes.columns.duplicated()]
    spy = closes[config.BENCHMARK].dropna()

    recon = []
    for r in eq:
        t = str(r.get("ticker") or r.get("symbol"))
        if t not in closes.columns:
            continue
        d0, d1 = str(r["opened_at"])[:10], str(r["closed_at"])[:10]
        s = closes[t].dropna()
        seg = s[(s.index >= d0) & (s.index <= d1)]
        sp = spy[(spy.index >= d0) & (spy.index <= d1)]
        if len(seg) < 2 or len(sp) < 2:
            continue
        entry = float(seg.iloc[0])
        # SPY-relative path: how far below entry did this name go vs SPY?
        rel = [(float(seg.iloc[i]) / entry - 1) * 100
               - (float(sp.iloc[min(i, len(sp) - 1)]) / float(sp.iloc[0]) - 1) * 100
               for i in range(len(seg))]
        mae = min(rel)                       # worst SPY-relative excursion
        recon.append({"ticker": t, "rel": rel, "mae": mae,
                      "realized": float(r["excess_ret_pct"]),
                      "family": _fam(r), "days": len(seg)})

    print(f"reconstructed MAE for {len(recon)} of {len(eq)}\n")

    mles = sorted(x["mae"] for x in recon)
    print("MAXIMUM ADVERSE EXCURSION (SPY-relative, daily closes)")
    print(f"  min {mles[0]:+.1f}  p25 {mles[len(mles)//4]:+.1f}  "
          f"median {st.median(mles):+.1f}  p75 {mles[3*len(mles)//4]:+.1f}  "
          f"max {mles[-1]:+.1f}")
    print()
    winners = [x for x in recon if x["realized"] > 0]
    losers = [x for x in recon if x["realized"] <= 0]
    if winners:
        print(f"  WINNERS (n={len(winners)}) worst excursion before winning: "
              f"median {st.median([x['mae'] for x in winners]):+.1f}  "
              f"deepest {min(x['mae'] for x in winners):+.1f}")
    if losers:
        print(f"  LOSERS  (n={len(losers)}) median MAE "
              f"{st.median([x['mae'] for x in losers]):+.1f}")
    print()
    print("=" * 82)
    print("COUNTERFACTUAL: cut at depth D (SPY-relative). 'saved' = the trade")
    print("would have exited better; 'cost' = a recoverable drawdown was realised.")
    print("=" * 82)
    base = st.mean([x["realized"] for x in recon])
    print(f"  {'depth':>7s} {'fired':>6s} {'new mean':>9s} {'delta':>7s} "
          f"{'saved':>6s} {'cost':>5s}  {'winners cut':>11s}")
    for depth in (-5, -8, -10, -12, -15, -18, -20, -25, -30):
        new, fired, saved, cost, wcut = [], 0, 0, 0, 0
        for x in recon:
            hit = next((i for i, v in enumerate(x["rel"]) if v <= depth), None)
            if hit is None:
                new.append(x["realized"])
                continue
            fired += 1
            out = depth                      # modelled fill at the level
            new.append(out)
            if out > x["realized"]:
                saved += 1
            elif out < x["realized"]:
                cost += 1
                if x["realized"] > 0:
                    wcut += 1
        m = st.mean(new)
        print(f"  {depth:>6.0f}% {fired:>6d} {m:>+9.2f} {m - base:>+7.2f} "
              f"{saved:>6d} {cost:>5d}  {wcut:>11d}")
    print(f"\n  baseline (no added stop): mean {base:+.2f} over n={len(recon)}")


def _fam(r: dict) -> str:
    s = str(r.get("exit_reason") or "")
    for k in ("PM verdict", "trailing stop", "hard stop", "profit ratchet",
              "rotation exit"):
        if k in s:
            return k
    return "other"


if __name__ == "__main__":
    main()
