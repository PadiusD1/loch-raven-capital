"""Full-year 2025 simulation — does Loch Raven's STRATEGY actually make or break?

HONEST FIDELITY (read this before trusting the numbers):
  - This replays each sleeve's MECHANICAL core (the quant screen / dual-momentum
    rotation / core blend) on real 2025 daily bars vs SPY. It is the "strategy
    FLOOR": it does NOT include the Opus committee's discretionary overlay (a
    250-day live committee replay would be thousands of subscription calls), the
    daily re-underwriting, or the exact option exit ladder.
  - The calls sleeve is a ROUGH 3x-delta + theta-drag proxy — directional only.
  - Costs: a per-rebalance transaction cost is applied so it isn't free-trading.
  So: treat this as "what did the systematic backbone do in 2025", and the live
  committee's job is to BEAT this floor. It is the most realistic FULL-year test
  that doesn't cost a fortune in Opus calls.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import config                                  # noqa: E402
from firm import backtest, data_engine         # noqa: E402

YEAR = 2025
START, END = f"{YEAR}-01-01", f"{YEAR}-12-31"
SLEEVE_CAP = 25_000.0
TXN_COST = 0.0010        # 10 bps per name per monthly rebalance (round-trip-ish)


def _slice_2025(curve: pd.Series) -> pd.Series:
    c = curve.dropna()
    c = c[(c.index >= START) & (c.index <= END)]
    return c / c.iloc[0] if len(c) else c       # renormalize to 1.0 at first 2025 bar


def _year_stats(curve: pd.Series, bench: pd.Series) -> dict:
    idx = curve.index.intersection(bench.index)
    curve, bench = curve[idx], bench[idx]
    if len(curve) < 2:
        return {}
    rets = curve.pct_change().dropna()
    tot = float(curve.iloc[-1] / curve.iloc[0] - 1)
    btot = float(bench.iloc[-1] / bench.iloc[0] - 1)
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() else 0.0
    mdd = float(((curve / curve.cummax()) - 1).min())
    bmdd = float(((bench / bench.cummax()) - 1).min())
    return {"return_pct": round(tot * 100, 2), "spy_return_pct": round(btot * 100, 2),
            "alpha_pct": round((tot - btot) * 100, 2), "sharpe": round(sharpe, 2),
            "max_dd_pct": round(mdd * 100, 2), "spy_max_dd_pct": round(bmdd * 100, 2),
            "days": len(curve)}


def _apply_costs(curve: pd.Series, rebalances_per_year: int = 12, n_names: int = 5) -> pd.Series:
    """Deduct a simple transaction-cost drag spread across the year so the floor
    isn't a free-trading fantasy. ~rebalances * cost * (turnover proxy)."""
    annual_drag = TXN_COST * rebalances_per_year      # ~ one full-name turnover/month
    daily = (1 - annual_drag) ** (1 / 252)
    factor = pd.Series(daily ** np.arange(len(curve)), index=curve.index)
    return curve * factor


def _build_curves(closes: pd.DataFrame) -> dict[str, pd.Series]:
    """Full-window mechanical curve per sleeve (sliced per-year later)."""
    out: dict[str, pd.Series] = {}
    out["alpha_stocks"] = backtest._monthly_rebalance_curve(
        closes, backtest._momentum_pick(config.STOCK_UNIVERSE, 5))
    out["etf_rotation"] = backtest._monthly_rebalance_curve(
        closes, backtest._momentum_pick(config.ROTATION_UNIVERSE,
                                        config.SLEEVES["etf_rotation"]["top_n"],
                                        abs_filter=config.CASH_ETF))
    core = config.SLEEVES["core_blend"]["core_weights"]
    sat = backtest._monthly_rebalance_curve(closes, backtest._momentum_pick(config.STOCK_UNIVERSE, 5))
    core_rets = sum((w / 100) * closes[t].pct_change().fillna(0)
                    for t, w in core.items() if t in closes.columns)
    core_curve = (1 + core_rets).cumprod()
    idx = sat.index.intersection(core_curve.index)
    cw = sum(core.values()) / 100
    blend = cw * core_curve[idx].pct_change().fillna(0) + (1 - cw) * sat[idx].pct_change().fillna(0)
    out["core_blend"] = (1 + blend).cumprod()
    under = backtest._monthly_rebalance_curve(closes, backtest._momentum_pick(config.STOCK_UNIVERSE, 3))
    lev = (under.pct_change().fillna(0) * 3.0) - 0.0008
    out["calls_only"] = (1 + lev.clip(lower=-0.95)).cumprod()
    return {k: _apply_costs(v) for k, v in out.items()}


def _slice_year(curve: pd.Series, year: int) -> pd.Series:
    c = curve.dropna()
    c = c[(c.index >= f"{year}-01-01") & (c.index <= f"{year}-12-31")]
    return c / c.iloc[0] if len(c) else c


def main() -> None:
    tickers = sorted(set(config.STOCK_UNIVERSE + config.ROTATION_UNIVERSE
                         + list(config.SLEEVES["core_blend"]["core_weights"])
                         + [config.BENCHMARK, config.CASH_ETF]))
    print(f"Loch Raven — multi-year regime simulation ({len(tickers)} tickers)\n")
    closes = data_engine.fetch_history(tickers, period="6y")   # 2021 lookback + 2022..2025
    if config.BENCHMARK not in closes:
        print("ERROR: no benchmark data"); return
    spy_full = closes[config.BENCHMARK].dropna()
    curves = _build_curves(closes)
    years = [2022, 2023, 2024, 2025]
    regime = {2022: "bear / rate-shock", 2023: "recovery", 2024: "bull", 2025: "bull (momentum)"}

    for year in years:
        spy_y = _slice_year(spy_full, year)
        if len(spy_y) < 2:
            print(f"== {year}: no data ==\n"); continue
        sc = {s: _slice_year(c, year) for s, c in curves.items()}
        common = spy_y.index
        for c in sc.values():
            common = common.intersection(c.index)
        if len(common) < 2:
            print(f"== {year}: insufficient overlap ==\n"); continue

        def _firm(keys):
            return sum(SLEEVE_CAP * (sc[k][common] / sc[k][common].iloc[0]) for k in keys)
        firm_all = _firm(list(sc.keys()))
        firm_noc = _firm([k for k in sc if k != "calls_only"])
        spy_c = spy_y[common]
        fa = _year_stats(firm_all / firm_all.iloc[0], spy_c / spy_c.iloc[0])
        fn = _year_stats(firm_noc / firm_noc.iloc[0], spy_c / spy_c.iloc[0])
        print(f"== {year}  ({regime[year]})   SPY {fa['spy_return_pct']:+.1f}%  "
              f"[{len(common)} days] ==")
        print(f"   {'sleeve':<14}{'ret%':>8}{'alpha':>8}{'sharpe':>8}{'maxDD%':>9}")
        for s, c in sc.items():
            st = _year_stats(c, spy_c)
            if st:
                print(f"   {s:<14}{st['return_pct']:>8}{st['alpha_pct']:>8}"
                      f"{st['sharpe']:>8}{st['max_dd_pct']:>9}")
        print(f"   {'FIRM all4':<14}{fa['return_pct']:>8}{fa['alpha_pct']:>8}"
              f"{fa['sharpe']:>8}{fa['max_dd_pct']:>9}")
        print(f"   {'FIRM no-calls':<14}{fn['return_pct']:>8}{fn['alpha_pct']:>8}"
              f"{fn['sharpe']:>8}{fn['max_dd_pct']:>9}\n")

    # Multi-year compounding (no-calls firm = the trustworthy line) vs SPY.
    print("== Compounded across the years above (FIRM no-calls vs SPY) ==")
    for label, keys in (("FIRM no-calls", [k for k in curves if k != "calls_only"]),
                        ("FIRM all 4", list(curves.keys()))):
        fv, sv = 1.0, 1.0
        for year in years:
            spy_y = _slice_year(spy_full, year)
            sc = {s: _slice_year(c, year) for s, c in curves.items()}
            common = spy_y.index
            for k in keys:
                common = common.intersection(sc[k].index)
            if len(common) < 2:
                continue
            firm = sum(SLEEVE_CAP * (sc[k][common] / sc[k][common].iloc[0]) for k in keys)
            fv *= float(firm.iloc[-1] / firm.iloc[0])
            sv *= float(spy_y[common].iloc[-1] / spy_y[common].iloc[0])
        print(f"   {label:<14} ${100_000*fv:>12,.0f}   vs SPY ${100_000*sv:,.0f}   "
              f"({(fv-1)*100:+.0f}% vs {(sv-1)*100:+.0f}%)")
    print("\n  NOTE: mechanical strategy FLOOR — no Opus committee, no daily stops/kill-"
          "switches (so drawdowns are WORSE than the live fund would allow), 58-name\n"
          "  curated stock pool (live screens ~3,800), calls = rough 3x proxy. Momentum\n"
          "  shines in trends and suffers in reversals — the 2022 row is the stress test.")


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
