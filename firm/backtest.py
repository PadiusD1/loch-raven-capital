"""Backtest desk — every sleeve's strategy replayed against SPY.

These are honest, daily-bar approximations of each sleeve's mechanical core
(the quant screen / rotation logic). They cannot replay the Opus research
layer historically, so treat results as the strategy floor, not a promise.
The calls-only test is an explicitly labeled leverage proxy.
"""
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import config
from firm import data_engine


def _stats(curve: pd.Series, bench: pd.Series) -> dict:
    curve, bench = curve.dropna(), bench.dropna()
    idx = curve.index.intersection(bench.index)
    curve, bench = curve[idx], bench[idx]
    years = max(0.5, len(curve) / 252)
    rets = curve.pct_change().dropna()
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1
    bench_cagr = (bench.iloc[-1] / bench.iloc[0]) ** (1 / years) - 1
    sharpe = (rets.mean() / rets.std() * math.sqrt(252)) if rets.std() else 0
    mdd = float(((curve / curve.cummax()) - 1).min())
    return {
        "cagr_pct": round(float(cagr) * 100, 2),
        "spy_cagr_pct": round(float(bench_cagr) * 100, 2),
        "alpha_pct": round(float(cagr - bench_cagr) * 100, 2),
        "sharpe": round(float(sharpe), 2),
        "max_dd_pct": round(float(mdd) * 100, 2),
        "total_return_pct": round(float(curve.iloc[-1] / curve.iloc[0] - 1) * 100, 1),
        "spy_total_return_pct": round(float(bench.iloc[-1] / bench.iloc[0] - 1) * 100, 1),
        "years": round(years, 1),
    }


def _curve_points(curve: pd.Series, bench: pd.Series, n: int = 260) -> list[dict]:
    idx = curve.index.intersection(bench.index)
    curve, bench = curve[idx] / curve[idx].iloc[0], bench[idx] / bench[idx].iloc[0]
    step = max(1, len(curve) // n)
    return [{"date": d.strftime("%Y-%m-%d"),
             "strategy": round(float(curve.iloc[i]), 4),
             "spy": round(float(bench.iloc[i]), 4)}
            for i, d in enumerate(curve.index) if i % step == 0]


def _monthly_rebalance_curve(closes: pd.DataFrame, pick_fn, lookback: int = 260) -> pd.Series:
    """Generic monthly-rebalanced portfolio: pick_fn(window_df) -> list of tickers."""
    closes = closes.dropna(how="all")
    rets = closes.pct_change().fillna(0)
    month_ends = closes.groupby([closes.index.year, closes.index.month]).tail(1).index
    holdings: list[str] = []
    equity = [1.0]
    dates = [closes.index[lookback]]
    for i in range(lookback + 1, len(closes)):
        d = closes.index[i]
        if closes.index[i - 1] in month_ends:
            holdings = pick_fn(closes.iloc[:i])
        day_ret = float(np.mean([rets[t].iloc[i] for t in holdings])) if holdings else 0.0
        equity.append(equity[-1] * (1 + day_ret))
        dates.append(d)
    return pd.Series(equity, index=pd.DatetimeIndex(dates))


def _momentum_pick(universe: list[str], top_n: int, abs_filter: str | None = None):
    def pick(window: pd.DataFrame) -> list[str]:
        scores = {}
        for t in universe:
            if t not in window.columns:
                continue
            s = window[t].dropna()
            if len(s) < 260:
                continue
            # 12-1 momentum: 12m return excluding the most recent month
            scores[t] = float(s.iloc[-22] / s.iloc[-252] - 1)
        ranked = sorted(scores, key=scores.get, reverse=True)[:top_n]
        if abs_filter and abs_filter in window.columns:
            hurdle = float(window[abs_filter].dropna().iloc[-63:].pct_change().add(1).prod() - 1)
            ranked = [t for t in ranked if scores[t] > hurdle] or [abs_filter]
        return ranked
    return pick


def run_backtest(sleeve: str, period: str = "5y") -> dict:
    tickers = sorted(set(config.STOCK_UNIVERSE + config.ROTATION_UNIVERSE
                         + list(config.SLEEVES["core_blend"]["core_weights"])
                         + [config.BENCHMARK, config.CASH_ETF]))
    closes = data_engine.fetch_history(tickers, period=period)
    bench = closes[config.BENCHMARK].dropna()

    if sleeve == "etf_rotation":
        pick = _momentum_pick(config.ROTATION_UNIVERSE,
                              config.SLEEVES["etf_rotation"]["top_n"],
                              abs_filter=config.CASH_ETF)
        curve = _monthly_rebalance_curve(closes, pick)
        note = "Exact mechanical replay: monthly dual-momentum top-3 with cash filter."
    elif sleeve == "alpha_stocks":
        pick = _momentum_pick(config.STOCK_UNIVERSE, 5)
        curve = _monthly_rebalance_curve(closes, pick)
        note = ("Mechanical floor: monthly 12-1 momentum top-5. The live sleeve "
                "adds the Opus research/debate layer on top of this screen.")
    elif sleeve == "core_blend":
        core = config.SLEEVES["core_blend"]["core_weights"]
        pick_sat = _momentum_pick(config.STOCK_UNIVERSE, 5)
        sat_curve = _monthly_rebalance_curve(closes, pick_sat)
        core_rets = sum((w / 100) * closes[t].pct_change().fillna(0)
                        for t, w in core.items() if t in closes.columns)
        core_curve = (1 + core_rets).cumprod()
        idx = sat_curve.index.intersection(core_curve.index)
        core_w = sum(core.values()) / 100
        blended_rets = (core_w * core_curve[idx].pct_change().fillna(0)
                        + (1 - core_w) * sat_curve[idx].pct_change().fillna(0))
        curve = (1 + blended_rets).cumprod()
        note = "Mechanical floor: 55% fixed core + 45% momentum-top-5 satellite, monthly."
    elif sleeve == "calls_only":
        pick = _momentum_pick(config.STOCK_UNIVERSE, 3)
        under = _monthly_rebalance_curve(closes, pick)
        lev_rets = (under.pct_change().fillna(0) * 3.0) - 0.0008  # 3x delta proxy, theta drag
        curve = (1 + lev_rets.clip(lower=-0.95)).cumprod()
        note = ("ROUGH PROXY ONLY: 3x-levered momentum-top-3 with daily theta drag. "
                "Real options P&L depends on IV, strikes, and the exit ladder.")
    else:
        return {"error": f"unknown sleeve {sleeve}"}

    return {
        "sleeve": sleeve, "period": period, "note": note,
        "asof": datetime.now(timezone.utc).isoformat(),
        "stats": _stats(curve, bench),
        "curve": _curve_points(curve, bench),
    }
