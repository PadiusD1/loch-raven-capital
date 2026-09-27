# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Loch Raven Capital** — an autonomous AI investment firm (paper-trading by default, live-capable to a Public.com account). It models an institutional desk: a quant screen over the whole US market, a five-desk research floor, an adversarial bull/bear/base debate room, a PM judge, a risk desk, an execution desk, and four independent strategy sleeves — all behind a localhost FastAPI dashboard on **port 8420**. The "brain" is Claude Fable 5.1 (`claude-fable-5-1`, max effort, operator 2026-09-01; Opus 5 refutes distilled lessons) run through the local `claude` CLI in print mode (subscription-billed, no API key).

## Commands

```powershell
# Run the dashboard + background exit monitors (this is the long-lived process)
py -3.11 app.py                       # -> http://localhost:8420

# Headless cycle entrypoint (what the Windows scheduled tasks call)
py -3.11 run_cycle.py                 # full research + trade cycle
py -3.11 run_cycle.py --monitor-only  # exit sweep only
py -3.11 run_cycle.py --premarket     # 8:30am full-market scan refresh, never trades
py -3.11 run_cycle.py --postmarket    # 4:30pm final sweep + dividends + EOD P&L
py -3.11 run_cycle.py --depth full    # override research depth (fast|standard|full)

# Regression suites — run BOTH after ANY code change (fully offline, sandboxed state)
py -3.11 tests\test_firm.py           # 658 safety-property regression checks
py -3.11 tests\test_week_sim.py       # 51-check week-in-the-life + LIVE-mode plumbing

# Lint (ruff is used; no config file — defaults)
ruff check .
```

Always use `py -3.11` (the project targets 3.11). The test files are standalone assertion scripts, not pytest test functions — there is no single-test selector; run the whole file. `test_week_sim.py` must pass before any paper→live flip.

## Single-writer rule (the most important invariant)

Sleeve state is money; a lost write is a lost trade — the one unrecoverable corruption. Only ONE process may write sleeve state at a time, enforced by a **PID-aware cross-process lock** at `state/cycle.lock` (`firm/state.py`, 15-min stale window, heartbeated every 300s).

- If `app.py` is already running, `run_cycle.py` **delegates** the cycle/sweep/dividends to it over HTTP (`POST /api/cycle`, `/api/monitor-now`, `/api/dividends`) rather than writing state itself. Preserve this delegation whenever you touch `run_cycle.py` or those endpoints.
- Within one process, monitor threads and the cycle thread serialize on `state.SLEEVE_LOCK`.
- Every `state.save()` writes an atomic `.bak`; corrupt files restore from backup, double-corruption halts the sleeve rather than silently trading a fresh default.

## Architecture: how one cycle flows

`firm/cycle.py::run_cycle()` is the orchestrator and the best file to read first. The pipeline:

1. **Quant screen** (`data_engine.screen_universe` + `universe.scan_market`) — ranks every listed US common stock on momentum/trend/quality with a risk-adjusted tie-break; tiered into 3 large / 3 mid / 3 small + 3 ETFs. Cached daily.
2. **Candidate assembly** — adds held names (re-underwritten every cycle), watchlist promotions ("the Bench"), and the Monday core-advisory pass; applies a HOLD/SELL **cooldown** so non-held names aren't re-judged daily without new information.
3. **Research floor** (`analysts.py`) — five desks (fundamental, technical, sentiment, macro, quant), each seeing ONLY its lens's slice of the packet (`analysts._DESK_VIEWS`) so disagreement is real. Runs in a `ThreadPoolExecutor` (`config.RESEARCH_WORKERS`) under a **self-tuning deadline** — a hung Opus call must never hold the whole cycle hostage.
4. **Debate + verdict** (`debate.py`) — bull/bear/base + PM judge. At `full` depth these are separate Opus calls; at `standard` they fold into one.
5. **Sleeves trade** (`sleeves.py`) — each sleeve runs its mandate through the risk desk; sleeves are isolated so one failure never blocks the others.
6. **Exit monitor** (`monitor.py`) sweeps; then **watchlist** + **learning** update.

Data packets are built per-candidate in `data_engine.build_packet` and enriched with a single firm-wide `macro_brief`, sector regime, and the macro calendar — every candidate is judged against one coherent macro picture.

## The four sleeves (`config.SLEEVES`, $25k paper each)

- **alpha_stocks** — concentrated single-name equity book from PM verdicts; idle cash equitized into SPY above a 70% invested floor.
- **core_blend** — 55% mechanical core (VOO/QQQ/GLD) + 45% conviction satellites.
- **calls_only** ("Options Desk") — combined long-options desk: long CALLS on STRONG_BUY names AND long PUTS on SELL/STRONG_SELL names (2026-07-07), sharing one capital pool, caps, and per-trade exit ladder designed by the Opus exit strategist (`firm/exits.py`, direction-aware), clamped to hard guardrails, with a proven scalper-default fallback. Long-options only (debit, defined risk — no naked/margin).
- **etf_rotation** — purely mechanical dual-momentum top-3 of 17 ETFs; **never overridden by the committee** (committee meddling would unvalidate a validated strategy). Verdicts on mechanical/core holdings are journaled as advisory-only.

## Module map (`firm/`)

`cycle` (orchestrator) · `data_engine` (packets, screen, technicals) · `universe` (full-market scan) · `brain` (Claude CLI wrapper + defensive JSON extraction) · `analysts` (research desks) · `debate` (bull/bear/base + judge) · `risk` / `riskbrain` (breakers, caps, sector/correlation, regime) · `broker` (Public.com SDK, paper + live fills) · `sleeves` (the four mandates) · `monitor` (exit sweeps) · `exits` (Opus call-exit ladder) · `learning` (closed-trade review, lessons, cap/bench tuning) · `watchlist` (the Bench) · `health` (self-checks, rebalance, reconciliation) · `macro_calendar` / `market_hours` / `dividends` / `fmp` / `alerts` / `state` / `backtest`.

`config.py` is the single source of truth for the universe, sleeves, ladders, risk limits, deadlines, and credential loading. `state/` holds all runtime books/journals/settings (gitignored, never commit). `dashboard/index.html` is the single-page UI; `app.py` serves it and exposes the `/api/*` endpoints.

## Conventions that matter here

- **Paper vs Live**: PAPER is default (real quotes, simulated fills). LIVE routes real orders only 9:30–16:00 ET on trading days; mode switching is refused while positions/pending orders exist; positions are tagged with their opening mode and can't be closed by the other mode's orders (protects shares held by the external scalper in the shared Public.com account). See the "Live-mode contract" in `README.md` before touching `broker.py`.
- **Secrets**: `fmp_key.txt`, `fred_key.txt`, `.env` are gitignored. Public.com credentials are loaded from env vars first, then fall back to the external scalper's `trade_executor.py` (one secret on the machine, one place). No FMP key → transparent yfinance fallback; nothing breaks.
- **Fail-soft, journal loudly**: cycle steps wrap in try/except and write to `state/journal.jsonl`; operator-facing problems also raise a dashboard `alert`. A ZERO-verdict cycle (outage) is deliberately NOT stamped "fresh" so the health loop retries it.
- The codebase carries dense inline comments citing dated audits and operator decisions (e.g. "2026-06-14 audit A13", "operator caught 2026-06-15"). These encode hard-won invariants — read them before changing the surrounding logic, and preserve the reasoning when you edit.
- `docs/AUDIT-*.md` and `docs/TECHNICAL_OVERVIEW.md` hold the full hardening history and design rationale.
