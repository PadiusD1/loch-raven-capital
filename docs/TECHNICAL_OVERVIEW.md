# Loch Raven Capital — Technical Architecture Overview

An autonomous multi-strategy AI investment firm running on localhost. Python 3.11, FastAPI dashboard (port 8420), Public.com brokerage API, Claude Fable 5.1 at max effort as the research brain via CLI (Opus 5 refutes distilled lessons). Modeled on institutional asset managers (T. Rowe Price structure): research desks, an adversarial investment committee, a risk desk, an execution desk, and a self-improving learning loop. ~20 modules, 64-check offline regression suite, hardened by five independent audits.

## 1. The Investment Process Pipeline (one daily cycle)

1. **Full-market quant screen (8:30 AM)** — pulls the complete NYSE/NASDAQ/AMEX common-stock directory (~5,600 symbols, refreshed weekly), screens every name past a tradability floor (price >= $1, $500k avg dollar volume), computes momentum (1/3/6/12-month), RSI, trend (50/200 SMA), volatility, drawdown, and relative strength vs SPY for ~4,100 survivors. Chunked yfinance downloads with a retry pass for rate-limit casualties (99.4% coverage). Cached 18h.
2. **Market-cap tier slots** — top names are tiered: 3 large caps (>=$10B), 3 mid ($2-10B), 3 small (<$2B) get guaranteed research seats, plus 3 ETFs. Ties break on raw momentum, never alphabetically.
3. **Research floor (Claude Opus)** — five analyst desks score each candidate independently: Fundamental (valuation, growth quality), Technical/Momentum, News/Sentiment, Macro/Sector, Quant (factor profile). Each returns a -100..100 score, conviction, and risks as strict JSON.
4. **Debate room** — Bull, Bear, and Base-case agents argue each candidate adversarially; each must attack the others' weakest points.
5. **PM judge** — issues the binding verdict: rating (STRONG_BUY..STRONG_SELL), conviction 0-100, target weight, horizon, thesis, the single bear risk, calls-suitability, and a watch_trigger ("what would change my mind") for HOLDs. Research depth is configurable: fast (1 Opus call/name), standard (3), full (9).
6. **Four sleeves trade the verdicts** through the risk desk; an exit monitor protects positions continuously between cycles.

## 2. The Four Strategy Sleeves ($25k paper each, benchmarked vs SPY)

- **Alpha Stocks** — concentrated 8-name single-stock book. Buys BUY/STRONG_BUY conviction >= 60, sized to PM target weight (max 18%/position, 35% sector cap).
- **Core Blend** — 55% strategic ETF core (VOO/QQQ/GLD, exempt from per-name stops to prevent sell-low/rebuy-high churn against the rebalancer) + 45% conviction satellites (stocks or ETFs).
- **Calls Desk** — long call options only. STRONG_BUY + calls-suitable + conviction >= 70; picks ~2% OTM contracts 7-28 DTE; an Opus "exit strategist" designs a custom laddered exit per trade (SL, TP1 partial, TP2 all-but-runner, TP3, trailing ramp), clamped to hard guardrail ranges, floored above the bid/ask spread. Force-closed at <=1 DTE.
- **ETF Rotation** — mechanical dual momentum: monthly, hold top-3 of 17 sector/asset ETFs by blended 3m/6m momentum; flees to T-bills (BIL) when nothing beats cash. 5y backtest: 27.4% CAGR vs SPY 20.2%, Sharpe 1.32.

## 3. Risk Architecture (tiered, diagnostic, proportional)

Escalation ladder, each step cheaper than the next:
1. **Per-position stops** — volatility-aware: stop = max(sleeve base, ~2.5 daily sigmas of that stock), capped 25%. A quiet staple keeps -12%; a 100%-vol small cap gets the room its nature demands.
2. **Daily circuit breaker** — percentage of day-start equity (scales with portfolio size), per-sleeve (stocks 3%, blend/rotation 2.5%, options 10% — a leveraged book breathes in multiples), widened up to 1.75x by a market volatility-regime multiplier (SPY 10d/60d realized vol). Blocks NEW entries only — exits always keep working — and un-trips automatically on intraday recovery.
3. **Soft tier** — at 70% of the kill threshold, all new positions half-size (hysteresis release at 50%).
4. **Risk Brain diagnosis (kill switch)** — at the drawdown threshold (alpha 22% / core 20% / calls 15% / rotation 20% from peak), the firm diagnoses BEFORE acting using a market X-ray (SPY 1-month move + all 11 sector ETFs + the sleeve's sector exposure):
   - **sector**: dominant sector ETF down >=10% while SPY holds — NO full halt; new buys blocked only in damaged sectors, others half-size; lifts when the sector ETF reclaims its level.
   - **market_wide**: SPY <= -8% or 6+ sectors red — full halt.
   - **idiosyncratic**: market calm but the sleeve bled — the picks are failing; full halt + flagged for strategy review.
   - Opus then confirms as Chief Risk Officer within 30 min (may escalate severity, never silently de-escalates). No data = safest assumption (market_wide).
5. **Recovery** — 3-day cooling-off, then resume when sleeve equity +3% OR SPY reclaims its halt-day level (works even from cash); resume at half size for 5 days; the kill switch re-arms from the resume point.

## 4. The Learning Loop (the firm gets smarter from its own history)

- **Trade ledger** — every closed trade matched to the verdict that drove it (rating, conviction, thesis, exit reason, P&L), tagged with strategy, sector, sector-tailwind-at-entry, stop-out-re-entry status, and mode (paper/live).
- **Opus performance review** — after every 5+ closed trades: distills <= 12 specific, falsifiable lessons ("BUY verdicts with conviction >= 70 win 78%..."), which are injected into EVERY future analyst, debate, and judge prompt as "FIRM MEMORY".
- **Self-tuning within bounds** — the review may retune the watchlist bench thresholds (promotion trigger, decay cut, age ceiling, stop-reclaim level) clamped to hard safety ranges. Verified live: the audit caught a +46% post-drop rally and the model raised the age ceiling itself.
- **Everything is learnable**: bench drop/promotion outcomes (forward returns after drops — did we cut a winner?), rotation signal history, sector regime history, ALL halts with diagnoses and outcomes, breaker trips, stop-out re-entry performance.
- **Fix briefs** — anything the review CANNOT fix within its powers (mis-designed rules, structural losers) is filed as an engineering ticket: problem, evidence, suspected module, and the exact instruction to paste into Claude Code (state/FIX_BRIEFS.md) + a Windows toast alert.

## 5. The Watchlist Bench (catch the wave early)

Near-miss scan names and PM HOLDs (with the judge's own written upgrade trigger) are tracked across cycles; promoted to full committee research when their setup completes (quant +5 pts, HOLD re-check after 3 days, max 3/cycle). Behavior-based exits, not calendar: decay cut (-6 pts from bench entry), vanished from ranks (3 cycles), judged out (still HOLD after 2 re-arguments), 30-day hard ceiling. **Stop-out recovery**: a stop-loss is a risk exit, not a thesis verdict — stopped positions auto-bench with their thesis attached, cool off 3 days (no falling knives), and are re-argued by the committee when price reclaims the stop level +3%. Re-entries are tagged and their win rate measured.

## 6. Live-Trading Execution Contract (paper and live are 1:1 on decisions)

Identical decision code in both modes; only the execution layer differs:
- **Paper**: instant simulated fills at live quotes, full P&L tracking.
- **Live**: orders become PENDING (cash reserved at placement); a settlement loop polls Public.com and books ONLY actual fills at actual average prices — partial fills book the filled part and refund the rest. Phantom positions impossible.
- **Market-hours gate**: entries route only 9:30-16:00 ET (NYSE holiday calendar); risk-REDUCING equity sells also route 8:30-9:30 pre-market and 16:00-20:00 after-hours as EXTENDED-session limit orders.
- **Exit chase**: an unfilled protective sell is cancelled after 150s and re-fired at the current price — a fast market cannot leave a stop dangling. Entries are never chased.
- **Fair pricing**: equity buys limit-capped at quote; option entries limit at MID (never crosses the spread); live equity marks come from Public's real-time pre/post-market-aware quotes (yfinance daily bars only as fallback); option marks at mid to match mid-booked entries.
- **Mode isolation**: every position tagged with its opening mode; cross-mode orders refused; mode flip refused unless all books are flat AND no cycle is running; entering live re-seeds sleeve capital from REAL buying power.
- **Reconciliation**: books vs broker every 30 min; missing positions auto-halt the owning sleeve; the user's own manual positions in the shared account are acknowledged once, remembered, and never touched.

## 7. Self-Healing Operations (runs unattended for months)

- **Schedules** (Windows Task Scheduler, all with missed-run catch-up): 8:30 pre-market scan, 9:45 firm cycle, 4:30 post-market wrap + EOD P&L booking, 15-min exit-sweep backstop, 5-min keepalive.
- **In-app loops**: ~1s options exit monitor (Public batch quotes, bid-for-exit semantics), 60s equity monitor, 30-min health desk (self-checks, live reconciliation, weekly cash-only rebalance across sleeves when one leaves the 12-40% share band, halt auto-recovery), 60s watchdog.
- **Watchdog**: a network drop can hang a thread in a dead socket without crashing it — the watchdog reads only local heartbeat files and hard-exits for the keepalive to restart when the equity monitor stalls, the calls monitor stalls with open options, or a cycle claims "running" >3h.
- **State integrity**: every save is atomic (tmp+replace) with throttled .bak backups; corrupt files restore from backup; double corruption HALTS the sleeve rather than silently trading a fresh default book. Journal rotates at 3MB; all collections capped; logs rotated by the keepalive.
- **Boot resilience**: a Startup-folder script relaunches everything at login; missed cycles are caught up in-app (research >26h stale on a weekday).
- **Alerts**: every halt = Windows toast + persistent red dashboard banner (until acknowledged) + journal. Auto-recoveries send info notices.
- **Concurrency**: in-process RLock serializes all sleeve state mutations; cross-process safety via cycle lockfiles and HTTP delegation of headless cycles into the running app (single-writer principle).

## 8. Dashboard (localhost:8420)

Dark cockpit terminal (Geist + JetBrains Mono, single emerald accent): four sleeve sections with positions, equity sparklines, day P&L (anchored to EASTERN midnight); investment-committee verdict feed with expandable bull/bear/base transcripts; The Bench watchlist with quant trend arrows and triggers; Firm Memory lessons with ledger stats; Desk Health panel; desk journal; alert banner; backtest-vs-SPY section; PAPER/LIVE toggle with confirmation; research depth selector. Panels are fixed-height internal scrollers that only re-render on data change.

## 9. Verification Posture

64-check offline regression suite in a sandboxed state directory (broker validation order, exit ladder ordering incl. gap-days, expiry guards, bench lifecycle, stop-out recovery, learning plumbing with mocked Opus, risk tiers, kill-switch diagnosis classifications, mode isolation, corruption recovery). Five independent audits (code review, chaos engineering, live-trading readiness, holistic final, delta) — ~60 findings, all fixed. Honest stance: backtests replay only the mechanical cores; nothing guarantees beating the S&P 500 — the system maximizes the probability of durable outperformance through process, risk control, and measured learning, and the paper phase exists to prove it before a real dollar follows.
