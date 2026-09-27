# Loch Raven Capital — Autonomous Multi-Strategy Desk

An AI investment firm modeled on institutional asset managers (T. Rowe Price
structure): a research floor of five analyst desks, an adversarial
bull/bear/base debate room, a PM judge, a risk desk, an execution desk wired
to Public.com, and four autonomous strategy sleeves — all behind a localhost
dashboard.

## Start

```powershell
cd loch-raven
py -3.11 app.py        # -> http://localhost:8420
```

Headless cycle (for Task Scheduler / cron):

```powershell
py -3.11 run_cycle.py                 # full research + trade cycle
py -3.11 run_cycle.py --monitor-only  # exit sweep only
py -3.11 run_cycle.py --depth full    # max research depth this run
```

## The firm structure

| Layer | What it does |
|---|---|
| Quant screen | **Full-market sweep: every listed US common stock (~10k symbols, NYSE/NASDAQ/AMEX)** ranked on momentum/trend/quality with a RISK-ADJUSTED tie-break (momentum/vol). Tradability floor: price >= $1, 60d MEDIAN $ volume >= $500k. Quality gates on research slots: all tiers reject vol > 150% ann. and 12m return <= -80% (squeeze/death-spiral junk); the small tier also needs mcap >= $300M and price >= $5. Tiered slots: 3 large (>=10B), 3 mid (2-10B), 3 small. Cached daily (`SCAN_CACHE_HOURS`) |
| Research floor | 5 desks (fundamental, technical, sentiment, macro, quant) — each sees ONLY its lens's evidence (`analysts._DESK_VIEWS`) so disagreement means something. Claude Opus on your CLI subscription. **Held names (and the underlyings of open calls) are re-underwritten every cycle** — a decayed thesis gets a fresh verdict, not just a mechanical stop |
| Debate room | Bull vs Bear vs Base argue every candidate adversarially (full depth: 3 separate calls; standard depth: debate+judge folded into ONE call — A/B-validated equal quality at 2/3 the cost) |
| PM judge | Issues the binding verdict: rating, conviction, target weight, calls-suitability. FIRM-MEMORY lessons go to the judge ONLY (echo-chamber guard) |
| Risk desk | Per-sleeve daily breakers (% of day-start equity, vol-widened) + drawdown kill switches with diagnosis; position/sector/correlation caps; **firm-wide caps across sleeves** (10% per single name, 25% per diversified ETF); entry price-drift guard (skip past max(3%, 0.75 sigma) from thesis price); earnings guard (no entries <=1 day to a report); vol-aware sizing. **Caps are LEARNABLE inside hard bounds** (`CAP_BOUNDS`, `risk.tuned_cap`) — the review re-tunes them with evidence |
| Execution desk | Public.com SDK (same key/account as market-analysis scalper); honest paper fills (equities cross 5bps/side, options cross half the half-spread + $0.65/contract), paper buys respect market hours, **dividends credited on ex-date**, split-adjusted positions (no phantom -50% stops) |
| Exit monitor | TWO loops while the app runs (session-gated — no overnight API burn): calls desk ticks ~1s via Public.com batch quotes; stock/ETF sleeves sweep every 60s. Public-quotes-first marks, yfinance fallback |
| Cash equitization | Alpha parks idle cash above a 70% invested floor in SPY (to 90% target) — a SPY-benchmarked book must never lose to its index by sitting in cash. Sold automatically to fund conviction buys; stands down whenever the sleeve is de-risking |
| The Bench (watchlist) | Near-miss names + PM HOLDs (with the judge's own "what would change my mind" trigger) tracked across cycles; promoted into full research the moment their setup completes (quant +5pts or HOLD re-check after 3 days, max 3/cycle, 21-day expiry) |
| Firm memory (learning) | Every closed trade carries its ENTRY verdict, research depth, desk scores, and **SPY-relative excess return over its own holding window** (state/ledger.json); after every 5+ closes, Opus reviews with a benchmark scoreboard, a CAPS AUDIT (forward outcome of every blocked entry), and a REGRET AUDIT (committee BUYs never owned) under an ambition mandate — beating SPY is the floor. Output: <=12 falsifiable lessons (soft 120-day expiry), bench tuning, and cap tuning, all hard-bounded |

## The four sleeves ($25k paper each)

1. **Alpha Stocks** — concentrated 8-name stock book from PM verdicts. Exits: vol-aware hard stop (base -12%), trail that stays armed once a position has EVER been green, SELL verdicts. Idle cash equitized into SPY.
2. **Core Blend** — 55% fixed core (VOO/QQQ/GLD) + 45% conviction-stock satellites.
3. **Calls Desk** — long calls only, STRONG_BUY + suitable_for_calls + conviction ≥ 70. **Exit ladder designed by the Opus exit strategist per trade** (`firm/exits.py`): it weighs DTE/theta, underlying volatility, conviction, and premium, then sets SL, TP tiers, scale-out sizes, and the trailing ramp — clamped to hard guardrails (SL 1-15%, TP1 3-30%, etc.). Falls back to the proven scalper default (SL -2 | TP1 +5 sell 50% | TP2 +60 | TP3 +100, trails 5/15/20) if the CLI is unavailable.
4. **ETF Rotation** — monthly dual momentum: top-3 of 17 ETFs by blended 3m/6m momentum, cash filter to BIL when nothing beats T-bills.

## When it runs (automation)

Six Windows scheduled tasks are registered — **S4U logon: they run whether or
not you are logged in** — all with StartWhenAvailable so a missed run fires as
soon as the PC can. The three market tasks are also network-gated
(RunOnlyIfNetworkAvailable): offline at trigger time, Windows defers them
until connectivity returns.

- **"LochRaven Premarket Scan"** — weekdays 8:30 AM: refreshes the full-market scan with overnight data (8:30 is when CPI/jobs data drops) so the 9:45 cycle trades fresh. Never trades.
- **"LochRaven Firm Cycle"** — weekdays 9:45 AM: full research + trade cycle at the depth saved in settings. Research deadline is SELF-TUNING (1.75x the worst observed normal run per depth, per-candidate normalized, clamped to floors/150 min). Delegates into the running app when it's up (single-writer safety).
- **"LochRaven Postmarket Wrap"** — weekdays 4:30 PM: final exit sweep after the close + dividend accrual + official EOD P&L per sleeve to the journal. Never buys.
- **"LochRaven Exit Monitor"** — every 15 minutes: sweeps TP/SL/trailing exits. Skips itself when the app (or a headless cycle) is already covering exits.
- **"LochRaven Keepalive"** — every 5 minutes + at boot: if the dashboard app died, restarts it and VERIFIES the restart took (journals `keepalive_restart`). Also serves the `/api/restart` graceful code reload.
- **"LochRaven Self-Test"** — Sundays 6 PM: runs BOTH test suites unattended (sandboxed, safe beside the live app), journals `self_test`, raises a dashboard alert on failure — environment drift is caught within a week.

Boot resilience: `LochRavenAutostart.cmd` in the user Startup folder relaunches the app at every login. Outage resilience: a cycle that produces ZERO verdicts (internet died mid-run) is NOT stamped as done — the health desk retries it within 30 min of connectivity returning (`health.internet_up` probe), and a missed EOD wrap self-books on reconnect (`health.eod_catchup`). Missed 9:45 cycles are also caught up in-app when research is >26h stale on a weekday.

Manage tasks: `schtasks /Query /TN "LochRaven Firm Cycle"`, `/Change /DISABLE`, `/Delete`. The PC must be on (logged in NOT required).

## Account care as the portfolio grows (health desk)

While the app runs, a health sweep every 30 min (`firm/health.py`, surfaced in the dashboard "Desk Health" panel and `/api/health`):

- **Self-checks** — cycle freshness, market-scan freshness, monitor heartbeat, risk halts, Claude CLI, broker connectivity. Failures are journaled as `health_warning`.
- **Capital rebalance** — weekly: if any sleeve's share of firm equity leaves the 12-40% band, idle CASH migrates from oversized sleeves to undersized ones (positions are never force-sold). Winners fund the next opportunity instead of concentrating risk.
- **Live reconciliation** — live mode only: internal books vs the real Public.com account; drift (manual trades, fill mismatches) is flagged on the dashboard and journaled, never silently "fixed".
- **Per-sleeve drawdown kill switches** — alpha 22%, core 20%, calls 15%, rotation 20% — tuned per mandate so a normal correction doesn't permanently halt a long-horizon book.
- **Journal rotation** — journal.jsonl trims itself past 3 MB so years of running never bloat state.

## Modes

- **PAPER (default)** — real quotes, simulated fills, full P&L tracking. Prove it here first.
- **LIVE** — real orders to the Public.com brokerage account configured in market-analysis/credentials.py. Toggle in the dashboard (asks for confirmation).

## Research depth (Opus calls per candidate)

- `fast` = 1 · `standard` = 3 · `full` = 9. Set in dashboard or `--depth`.
- ~9 candidates per cycle clear the quant screen (6 stocks + 3 ETFs).

## Backtests (5y vs SPY, as of build date)

- ETF Rotation (exact mechanical replay): **27.4% CAGR vs SPY 20.2%, Sharpe 1.32, MaxDD -17%**
- Alpha momentum floor (without the Opus layer): **69.3% CAGR, Sharpe 1.73, MaxDD -32%** — momentum had an exceptional semis run in this window; do not extrapolate
- Calls backtest is a labeled 3x-leverage proxy, not real options P&L

## Live-mode contract (hardened 2026-06-09, three independent audits)

- **Fills are confirmed, never assumed**: live orders become PENDING (cash reserved); `settle_pending()` polls Public and books only actual fills at actual average prices. Partial fills book the filled part, refund the rest. Phantom positions are impossible.
- **Market-hours gate**: live orders route only 9:30-16:00 ET on trading days (NYSE holiday calendar in `firm/market_hours.py`). An after-hours "trigger" waits and re-evaluates at the open.
- **Mode isolation**: switching paper<->live is refused while any positions/pending orders exist; positions are tagged with their opening mode and can never be closed by the other mode's orders (protects the scalper's shares in the shared account). Entering live re-seeds sleeve capital from REAL buying power x `live_pct_of_bp`.
- **Reconciliation halts, not just flags**: books-vs-broker drift in the dangerous direction (tracked but not held) auto-halts the owning sleeve. `RECONCILE_IGNORE_SYMBOLS` in config excludes the scalper's instruments.
- **Exit-failure escalation**: 3 consecutive failed live exits (expired key, broker down) halt the sleeve loudly instead of silently retrying into a falling price.
- **Mark-anomaly guard**: a >50% one-sweep price dislocation (split, bad print) must persist a second sweep before any stop/TP may act on it.
- **Corruption can never erase the book**: every state save keeps an atomic .bak; corrupt files restore from backup; double-corruption halts the sleeve instead of quietly trading a fresh $25k default.
- **Watchdog coverage**: hung equity monitor, hung calls monitor (with open options), or a cycle stuck "running" >3h all trigger self-restart via the 5-min keepalive.

## Regression suite

Two fully-offline suites in sandboxed state dirs — run BOTH after ANY code change:

- `py -3.11 tests\test_firm.py` — 132 regression checks covering every safety property above
- `py -3.11 tests\test_week_sim.py` — 50-check week-in-the-life simulator: 5 scripted trading days (buys, gaps, stops, SELL exits, breaker trips, the learning review, rotation) plus the full LIVE-mode plumbing against a mock broker (pending orders, partial fills, exit chasing, mode-flip guards). **Run this before any live flip.**

The "LochRaven Self-Test" task re-runs both every Sunday unattended and alerts on failure. Full change history: `docs/AUDIT-2026-06-10.md` (10 hardening rounds, 90+ findings fixed).

## Honest expectations

No system can guarantee beating the S&P 500. This firm gives you an
institutional *process* — multi-perspective research, adversarial debate,
strict risk control, and momentum tilts that have historically carried
positive alpha — plus full measurement against SPY so you always know the
truth. Backtests replay the mechanical core only; past performance does not
promise future results. Run paper until the equity curves earn your trust.

## Files

- `config.py` — universe, sleeves, ladders, risk limits
- `firm/` — data_engine, brain (Claude CLI), analysts, debate, risk, broker, sleeves, monitor, cycle, backtest, state
- `state/` — sleeve states, settings, research log, journal.jsonl
- `app.py` — FastAPI server + background exit monitor (port 8420)
