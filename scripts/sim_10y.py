"""TEN-YEAR strategy test — does the mechanical backbone beat SPY, and WHERE in
the momentum distribution does the edge actually live?

WHY THIS EXISTS (operator 2026-08-19). The live fund is 10 weeks old, n=36 closed
equity trades. Every parameter conclusion at that sample is noise. Separately,
`learning._factor_edge` was found to have sorted (score, excess) tuples
lexicographically, so names TIED on a factor score were ordered by their own
forward outcome — fabricating a "+14.3 momentum edge" whose tie-safe value is
-4.5. That fake edge was the only measured edge with a money consumer
(`sleeves._edge_weight_tilt`), and it was upsizing every high-momentum entry.
So the question "is high momentum actually good for this strategy?" has to be
answered on a sample the fund itself cannot supply. Ten years of daily bars can.

HONEST FIDELITY — read before trusting any number here:
  * This replays the MECHANICAL core only (quant screen / dual-momentum rotation
    / core blend). It cannot replay the Opus committee, the daily re-underwriting
    or the real option ladder. Treat it as the STRATEGY FLOOR the live committee
    has to beat, not as the fund's expected return.
  * SURVIVORSHIP BIAS IS REAL AND IT FLATTERS EVERY STOCK NUMBER. config.
    STOCK_UNIVERSE is 58 names chosen TODAY; a 10-year replay over them is a
    replay over companies that survived and stayed large. The ETF-rotation and
    core-blend results are far less exposed (sector/asset ETFs rarely vanish);
    the alpha_stocks and calls_only numbers are the most inflated. The
    MOMENTUM-BAND COMPARISON below is the part that survives this bias best,
    because every band is drawn from the SAME biased universe — the bias
    largely cancels in the band-vs-band contrast even though it inflates all of
    their absolute levels.
  * Costs: a per-name per-rebalance transaction cost is charged so nothing
    trades for free.

Run:  py -3.11 scripts/sim_10y.py
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import config                                       # noqa: E402
from firm import backtest, data_engine              # noqa: E402

PERIOD = "10y"
TXN_COST = 0.0010          # 10 bps per name per monthly rebalance
LOOKBACK = 260             # one year of bars before the first pick


def _fmt(stats: dict) -> str:
    return (f"CAGR {stats['cagr_pct']:>6.2f}%  vs SPY {stats['spy_cagr_pct']:>5.2f}%  "
            f"alpha {stats['alpha_pct']:>+6.2f}  Sharpe {stats['sharpe']:>5.2f}  "
            f"maxDD {stats['max_dd_pct']:>7.2f}%")


def _apply_costs(curve: pd.Series, n_names: int) -> pd.Series:
    """Charge TXN_COST per held name at each monthly rebalance."""
    rets = curve.pct_change().fillna(0)
    month_ends = curve.groupby([curve.index.year, curve.index.month]).tail(1).index
    drag = pd.Series(0.0, index=curve.index)
    drag.loc[month_ends] = TXN_COST * max(1, n_names) / max(1, n_names)
    return (1 + rets - drag).cumprod()


def _band_pick(universe: list[str], lo_pct: float, hi_pct: float):
    """Hold the momentum band between two PERCENTILES of the ranked universe.

    lo_pct=0.0, hi_pct=0.1 is the top decile — what the live fund actually buys
    (its screen saturates momentum at 100). Running every band on the same bars
    answers the question the fund cannot: is the TOP of the momentum
    distribution where the money is, or is it the part that mean-reverts?
    """
    def pick(window: pd.DataFrame) -> list[str]:
        scores = {}
        for t in universe:
            if t not in window.columns:
                continue
            s = window[t].dropna()
            if len(s) < 260:
                continue
            scores[t] = float(s.iloc[-22] / s.iloc[-252] - 1)   # 12-1 momentum
        if not scores:
            return []
        ranked = sorted(scores, key=scores.get, reverse=True)
        a, b = int(len(ranked) * lo_pct), int(len(ranked) * hi_pct)
        b = max(b, a + 1)
        return ranked[a:b]
    return pick


def main() -> None:
    tickers = sorted(set(config.STOCK_UNIVERSE + config.ROTATION_UNIVERSE
                         + list(config.SLEEVES["core_blend"]["core_weights"])
                         + [config.BENCHMARK, config.CASH_ETF]))
    print(f"fetching {len(tickers)} tickers over {PERIOD} ...", flush=True)
    closes = data_engine.fetch_history(tickers, period=PERIOD)
    closes = closes.dropna(how="all")
    bench = closes[config.BENCHMARK].dropna()
    span_y = len(bench) / 252
    print(f"got {len(closes)} bars, {closes.index[0].date()} -> {closes.index[-1].date()} "
          f"({span_y:.1f}y)\n", flush=True)

    out: dict = {"asof": datetime.now(timezone.utc).isoformat(),
                 "period": PERIOD, "bars": len(closes),
                 "start": str(closes.index[0].date()), "end": str(closes.index[-1].date()),
                 "txn_cost_bps": TXN_COST * 1e4, "sleeves": {}, "momentum_bands": {},
                 "caveat": ("mechanical floor only; config.STOCK_UNIVERSE is a "
                            "TODAY-chosen 58-name list so all stock numbers carry "
                            "survivorship bias — the band-vs-band contrast is the "
                            "robust part, the absolute levels are not")}

    print("=" * 96)
    print("SLEEVE MECHANICAL CORES over 10y (survivorship-biased on the stock sleeves)")
    print("=" * 96)
    for sleeve in ("etf_rotation", "core_blend", "alpha_stocks", "calls_only"):
        try:
            r = backtest.run_backtest(sleeve, period=PERIOD)
            if "error" in r:
                print(f"  {sleeve:14s} ERROR {r['error']}")
                continue
            out["sleeves"][sleeve] = r["stats"]
            print(f"  {sleeve:14s} {_fmt(r['stats'])}")
        except Exception as e:
            print(f"  {sleeve:14s} FAILED {type(e).__name__}: {e}")

    print()
    print("=" * 96)
    print("MOMENTUM BANDS — the live fund buys the TOP decile (its screen pins momentum")
    print("at 100). Same bars, same costs, same rebalance: where does the money actually")
    print("live? A NEGATIVE gradient from top to middle would confirm the -4.5 tie-safe")
    print("factor edge measured on the fund's own verdicts.")
    print("=" * 96)
    bands = [(0.00, 0.10, "top 10%  <- what the fund buys"),
             (0.00, 0.20, "top 20%"),
             (0.10, 0.30, "10-30%"),
             (0.20, 0.40, "20-40%"),
             (0.30, 0.50, "30-50%"),
             (0.50, 0.70, "50-70%"),
             (0.70, 1.00, "bottom 30%")]
    for lo, hi, label in bands:
        try:
            pick = _band_pick(config.STOCK_UNIVERSE, lo, hi)
            curve = backtest._monthly_rebalance_curve(closes, pick, lookback=LOOKBACK)
            n_names = max(1, int(len(config.STOCK_UNIVERSE) * (hi - lo)))
            curve = _apply_costs(curve, n_names)
            st = backtest._stats(curve, bench)
            out["momentum_bands"][label] = st
            print(f"  {label:28s} {_fmt(st)}")
        except Exception as e:
            print(f"  {label:28s} FAILED {type(e).__name__}: {e}")

    p = BASE / "state" / "backtest_10y.json"
    p.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {p}")


if __name__ == "__main__":
    main()
