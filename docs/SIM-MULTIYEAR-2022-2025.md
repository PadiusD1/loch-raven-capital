# Loch Raven — Multi-Year Strategy Simulation (2022–2025)

Run: `py -3.11 scripts/sim_2025.py` (multi-year). Real daily bars, vs SPY.

## Fidelity (what this IS and ISN'T)
- Replays each sleeve's **mechanical core** (momentum screen / dual-momentum rotation / core blend) — the **strategy FLOOR**.
- **NO Opus committee** (a 250-day live committee replay = thousands of subscription calls), **no daily stops / kill-switches / firm breaker** (so backtest drawdowns are WORSE than the live fund would allow), **58-name curated stock pool** (live screens ~3,800), **calls = rough 3x-delta + theta proxy** (not real option P&L).
- 10 bps/month transaction-cost drag applied.

## Results (alpha = vs SPY that year)

| Year | Regime | SPY | FIRM no-calls | alpha | maxDD | FIRM all-4 | rotation (cash filter) |
|---|---|---|---|---|---|---|---|
| 2022 | bear / rate-shock | **-18.6%** | **-11.5%** | **+7.1** | -20.8% | -27.4% | **-0.15% (alpha +18.5!)** |
| 2023 | recovery | +26.7% | +33.0% | +6.3 | -10.2% | +34.2% | +18.7% |
| 2024 | bull | +25.6% | +61.5% | +35.9 | -17.4% | +279.9%* | +32.1% |
| 2025 | bull (momentum) | +18.0% | +31.3% | +13.3 | -22.5% | +45.0% | +24.4% |

\* the all-4 / calls numbers are inflated by the unrealistic 3x proxy (calls: +935% in 2024, **-75% in 2025, -85% in 2022**) — ignore them; the **no-calls line is the trustworthy one.**

## Compounded 2022→2025 ($100k start)
- **FIRM no-calls: $100k → $249k (+149%)** vs **SPY $153k (+53%)**.
- FIRM all-4: $537k (+437%) — a fantasy driven by the calls proxy; not credible.

## Verdict — it "MAKES it", with caveats
1. **The trustworthy (no-calls) config beat SPY in EVERY year, including the 2022 bear (+7 pts).** That's not a single-year momentum fluke — it survived the stress year. Compounded +149% vs +53%.
2. **The hero is the dual-momentum ROTATION cash filter** — it went to cash in 2022 and was ~flat (-0.15%) while SPY fell -18.6%. Real, validated downside protection.
3. **The calls sleeve (as modeled) is a wrecking ball** (+935% / -85%). The proxy is unrealistic, BUT it flags that the leveraged book is the highest-risk, least-predictable sleeve — the live kill-switch + 10% position cap + the new overnight-premium cap exist precisely to bound this.
4. **alpha_stocks has no downside protection in the backtest** (-19% in 2022, just tracked SPY down) because monthly mechanical momentum has no stops. The LIVE alpha sleeve adds vol-aware stops + kill switch + the firm breaker — so live 2022 would likely cushion better than this floor shows.

## What would make it BREAK
- A choppy, trendless, mean-reverting market (whipsaws momentum) — 2022 hints at it; rotation's cash filter is the saver there.
- The calls sleeve un-bounded — guarded live, fatal in the raw proxy.
- Over-concentration — addressed by the firm name/sector caps shipped 2026-06-14.

**Bottom line:** the systematic backbone has genuine, multi-regime edge (driven by momentum + the rotation cash filter), and the live risk layer + committee are there to cushion the backtest's worst drawdowns and (hopefully) add to the floor. The single biggest real-world risk is the leveraged calls sleeve; the single biggest strength is the rotation cash filter.
