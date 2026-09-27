"""Loch Raven Capital — firm-wide configuration.

Four autonomous sleeves run under one roof, each benchmarked against SPY:
  alpha_stocks  — concentrated single-name equity book (stocks only)
  core_blend    — stocks + ETFs (core/satellite)
  calls_only    — long call options book (layered scaled exits)
  etf_rotation  — dual-momentum ETF rotation (ETFs only)
"""
import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).parent
STATE_DIR = BASE_DIR / "state"
LOGS_DIR = BASE_DIR / "logs"
DASHBOARD_DIR = BASE_DIR / "dashboard"
for _d in (STATE_DIR, LOGS_DIR):
    _d.mkdir(exist_ok=True)

# ── Public.com brokerage credentials ─────────────────────────────────────────
# Single source of truth: env vars first, then the market-analysis scalper's
# own credential module (so the secret lives in exactly one place on this
# machine).
#
# 2026-08-28 BREAKAGE — READ BEFORE EDITING. This used to scrape the literal
# out of trade_executor.py with the regex kept below. That day the scalper moved
# its secret into credentials.py (env -> gitignored .secrets.json) and the
# literal vanished, so BOTH values silently became "" — the regex matched
# nothing and this function has no way to say so. Nothing raised. The damage is
# not "live orders fail", it is far quieter: broker.equity_quotes_public()
# swallows the auth error and returns {}, so every equity mark falls back to a
# yfinance DAILY CLOSE, broker.fetch_marks stamps _mark_stale on all of them,
# and monitor._stale_skip then refuses to fire a protective exit on a stale mark
# (correctly — that guard exists so a stop never fires on yesterday's price).
# Net effect of an empty key during regular hours: NO stop, hard or trailing, on
# ANY equity position in ANY sleeve, while the dashboard still shows prices. It
# also costs 8.2 s per sweep instead of 0.26 s, because every mark takes the
# yfinance path (measured 2026-08-28: Public batch 50 ms for 21 symbols vs
# data_engine.batch_last_prices 8,239 ms).
#
# So: prefer the scalper's credentials module (the thing that ROTATES), and keep
# the regex only as a last resort for an older trade_executor.py. Never make
# this raise — config.py is imported by the offline regression suites, which
# must run on a box with no credentials at all. A missing credential is reported
# loudly at app startup instead (app.py _warm_public_client).
# 2026-09-22: the VPS keeps the scalper at /srv/repos/market-analysis. The
# Windows path alone meant the VPS fund never loaded live Public.com quotes and
# its exit monitor fell back to daily closes. LR_SCALPER_DIR overrides both.
_SCALPER_DIR = Path(os.environ.get("LR_SCALPER_DIR") or (BASE_DIR.parent / "market-analysis"))
_SCALPER_EXECUTOR = _SCALPER_DIR / "trade_executor.py"
_SCALPER_CREDENTIALS = _SCALPER_DIR / "credentials.py"


def _load_public_credentials() -> tuple[str, str]:
    key = os.environ.get("PUBLIC_API_KEY", "")
    acct = os.environ.get("PUBLIC_ACCOUNT_NUMBER", "")
    if key and acct:
        return key, acct
    # The scalper's credential module: env -> its gitignored .secrets.json.
    # Loaded BY PATH rather than by a sys.path insertion, so importing config.py
    # never changes what `import credentials` means anywhere else.
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("_lr_scalper_credentials",
                                             _SCALPER_CREDENTIALS)
        if _spec and _spec.loader:
            _mod = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            key = key or _mod.get("PUBLIC_API_KEY")
            acct = acct or _mod.get("PUBLIC_ACCOUNT_NUMBER")
    except Exception:
        pass                      # fall through to the legacy scrape
    if key and acct:
        return key, acct
    try:
        src = _SCALPER_EXECUTOR.read_text(encoding="utf-8", errors="replace")
        m_key = re.search(r'PUBLIC_API_KEY",\s*"([^"]+)"', src)
        m_acct = re.search(r'PUBLIC_ACCOUNT_NUMBER",\s*"([^"]+)"', src)
        return (key or (m_key.group(1) if m_key else ""),
                acct or (m_acct.group(1) if m_acct else ""))
    except OSError:
        return key, acct


PUBLIC_API_KEY, PUBLIC_ACCOUNT_NUMBER = _load_public_credentials()

# Demo isolation (hackUMBC 2026). LOCH_RAVEN_DEMO=1 runs a second copy of the
# firm next to a live one without touching it. No broker credentials, so quotes
# fall back to yfinance and nothing can route to a real account. No host wide
# RAM janitor or cleaner, no Windows toasts or Slack posts, no scripts that call
# the live app, and paper mode only. LOCH_RAVEN_PORT moves the dashboard off 8420.
DEMO_MODE = os.environ.get("LOCH_RAVEN_DEMO", "") == "1"
PORT = int(os.environ.get("LOCH_RAVEN_PORT", "8420"))
if DEMO_MODE:
    PUBLIC_API_KEY, PUBLIC_ACCOUNT_NUMBER = "", ""

# ── Financial Modeling Prep (clean fundamentals + confirmed earnings) ─────────
# Audit-grade value/quality inputs and confirmed earnings, vs yfinance's
# screening-grade .info. Key from env FMP_API_KEY first, then a local file
# ai-fund/fmp_key.txt (one line, the key — gitignored). NO key -> the fund
# transparently falls back to yfinance; nothing breaks. Get a free key at
# financialmodelingprep.com (no card; ~250 calls/day covers ~12 names/cycle).
_FMP_KEY_FILE = BASE_DIR / "fmp_key.txt"


def _load_fmp_key() -> str:
    k = os.environ.get("FMP_API_KEY", "").strip()
    if k:
        return k
    try:
        return _FMP_KEY_FILE.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


FMP_API_KEY = _load_fmp_key()
FMP_TIMEOUT_S = 8.0
FMP_BASE = "https://financialmodelingprep.com/stable"

# ── FRED (macro release calendar) — OPTIONAL, makes macro dates official-exact ─
# Free, instant key (no card) at fredaccount.stlouisfed.org/apikeys. Env
# FRED_API_KEY first, then ai-fund/fred_key.txt (gitignored). With a key the
# macro calendar pulls EXACT upcoming release dates for every fed/macro number
# (CPI/PCE/NFP/GDP/...). WITHOUT a key it still runs keyless via the Forex
# Factory feed + recurrence rules — never blind, just less exact; health reports
# which tier is live. This is the "data-driven, not a hardcoded list" upgrade.
_FRED_KEY_FILE = BASE_DIR / "fred_key.txt"


def _load_fred_key() -> str:
    k = os.environ.get("FRED_API_KEY", "").strip()
    if k:
        return k
    try:
        return _FRED_KEY_FILE.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


FRED_API_KEY = _load_fred_key()
FRED_TIMEOUT_S = 8.0
FRED_BASE = "https://api.stlouisfed.org/fred"

# ── Vendor data integrity (self-healing) ──────────────────────────────────────
# (1) Plausibility guard: a vendor value OUTSIDE its band is treated as garbage
# at the data boundary — the OTHER vendor's in-band value is substituted, else
# the field drops to None (scorers skip None; never a fake 50). Bands are WIDE
# on purpose so real extremes pass (AAPL ROE 1.47, Ford D/E 4.26x); only the
# impossible is caught (FMP's PEG -3.46, a future 100x scale regression).
FUND_PLAUSIBLE_BOUNDS = {
    "pe":             (-10000.0, 10000.0),  # negative ok (loss-maker); see sub-1 rule in code
    "fwd_pe":         (-10000.0, 10000.0),
    "peg":            (0.1, 10.0),          # <=0 / tiny / huge = undefined growth = garbage
    "ps":             (0.0, 200.0),
    "profit_margin":  (-2.0, 1.5),          # decimals; <-200% or >150% impossible
    "op_margin":      (-2.0, 1.5),
    "roe":            (-10.0, 10.0),         # decimals; allows AAPL 1.47x, neg-equity within reason
    "debt_to_equity": (-50.0, 50.0),         # RATIO scale; negative = neg-equity (real: MCD/SBUX) -> quality_score skips it; |>50| = garbage
    "piotroski":      (0.0, 9.0),
    "altman_z":       (-50.0, 200.0),
    # Positioning + consensus (AUDIT 2026-07-16 infodiet): decimals/ratios —
    # garbage magnitudes are dropped to None, never scored.
    "short_pct_float":      (0.0, 1.0),
    "analyst_rec_mean":     (1.0, 5.0),
    "insider_held_pct":     (0.0, 1.5),
    "institution_held_pct": (0.0, 2.0),
}
# (2) Weekly vendor audit: FMP vs yfinance + plausibility, auto-block on drift.
VENDOR_AUDIT_MAX_NAMES = 15        # held + top candidates, capped for time/budget
VENDOR_AUDIT_DIVERGENCE = 3.0      # FMP vs yfinance >3x on a field = scale/units rot (not a definitional diff)
VENDOR_AUDIT_STRIKES_TO_BLOCK = 2  # consecutive weekly strikes before auto-disabling an FMP field
VENDOR_AUDIT_STRIKES_TO_CLEAR = 2  # consecutive clean weeks before auto-re-enabling a blocked field
VENDOR_AUDIT_FIELDS = ("pe", "ps", "profit_margin", "op_margin", "roe", "debt_to_equity")
# Always-audited canary basket — a diverse, sector-spanning set so the weekly
# audit can detect SYSTEMATIC vendor rot (a scale flip, a field rename) even in
# a week the fund holds no individual stocks. Spans mega-cap tech, a bank, a
# levered automaker, staples, pharma — the regimes that expose scale/definition
# bugs (this is the same stress basket that surfaced the 2026-06-13 D/E bug).
VENDOR_AUDIT_CANARIES = ("AAPL", "MSFT", "NVDA", "JPM", "F", "KO", "PFE")

# ── Claude research brain ─────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-opus-5-5"  # operator 2026-09-22: Claude's top model is Opus 5.5 at max
                               # effort, the best Artificial Analysis Intelligence Index score
                               # of any model the office runs (58, Fable 5.1 53, read
                               # 2026-09-22). Verified on the VPS CLI 2.1.280 that the id is
                               # served as itself (2.1.278 silently served Opus 5 instead).
                               # History of this pin, kept for context:
                               # operator 2026-09-01: "max fable 5.1 default ... for all".
                               # Replaces the 2026-07-25 Opus 5 pin. Verified accepted by the
                               # installed CLI (2.1.258) before the switch with the exact brain
                               # flag set (`-p --model claude-fable-5-1 --effort max --safe-mode`).
                               # CLI 2.1.200 REJECTED this id (400, needs 2.1.251+) and its bare
                               # "fable" alias resolved to 5.0, which is why the id is pinned.
                               # NOTE this is a VERSIONED id, deliberately overriding the
                               # usual family-alias preference: the bare "opus" alias tracks
                               # whatever the CLI maps it to, which is NOT guaranteed to be
                               # the wanted version. The cost of pinning is staleness — when a newer Fable
                               # ships, this line must be bumped by hand. If the id is ever
                               # retired the CLI errors and EVERY workup degrades to the
                               # quant-only fallback (verdicts stop being real), so the
                               # 'degraded verdicts' health check is the tripwire to watch.
CLAUDE_EFFORT = "max"          # operator 2026-09-01: max reasoning effort on every brain call
                               # (brain.ask passes --effort; drops the flag once if the CLI
                               # ever rejects it, so an old CLI degrades to default effort,
                               # never to a dead brain). Empty string = no flag.
# Isolate every brain call from the operator's Claude Code customizations
# (measured 2026-07-02 from this box: a bare "say hi" -p call carried ~127.6k
# context tokens — 2,818 installed skills, user CLAUDE.md rule files, MCP
# servers, coding-agent persona — vs ~25k with --safe-mode, and API time
# DROPPED 5.0s->3.1s). That 100k of off-domain text competed with the packet
# for the judge's attention every call and burned the same subscription quota
# that ran dry on 06-25. Auto-falls back if a future CLI drops the flag.
BRAIN_SAFE_MODE = True
BRAIN_SYSTEM_PROMPT = (
    "You are the research brain of Loch Raven Capital, an institutional "
    "investment firm. You serve as its analyst desks, debate room, portfolio-"
    "manager judge, exit strategist, and performance reviewer. Reason "
    "rigorously from the evidence provided; weigh bear cases as seriously as "
    "bull cases; state uncertainty honestly instead of hedging into "
    "vagueness; never invent numbers not present in the materials. Follow "
    "the user instructions exactly and end with the exact output format "
    "requested.")
# Institutional research doctrine — the distilled, VERIFIED best of the fund's
# finance-skill library (finance-metrics-quickref, charlie-cfo-skill, the WSP
# Equity-Research-Masterclass + Morgan/P&G DCF notes) plus standard PM knowledge,
# minus the retail-grade/persona slop (2026-07-02 review). THESIS & STATEMENT
# ANALYSIS block added 2026-07-04 from the completed WSP Sell-Side Equity
# Research Certification (ERC 1-8, all exams passed on the operator's account;
# full distillation in the operator's memory: primary_wsp_erc_curriculum.md) —
# variant-view thesis structure, catalyst discipline, DuPont/driver decomposition,
# re-rating rules. Rides EVERY Opus call
# via --system-prompt-file, so it is cached after the first call — the marginal
# per-call cost is a cache read, not 750 words re-sent. Tight and decision-
# relevant: it changes a BUY/HOLD/SELL + conviction + options-suitability, not a
# textbook. Each desk applies the block(s) relevant to its lens.
BRAIN_FINANCE_DOCTRINE = (
    "INSTITUTIONAL RESEARCH DOCTRINE — apply the parts relevant to your role. "
    "Every verdict is BUY/HOLD/SELL + conviction 0-100 + options-suitability, "
    "and the real edge is being DIFFERENTIATED from visible consensus, not "
    "being right about the obvious.\n\n"
    "THESIS & STATEMENT ANALYSIS (every desk, judge):\n"
    "- A thesis is a VARIANT VIEW: 2-4 load-bearing pillars, each naming where "
    "and why the market's expectation is wrong (above/below-consensus revenue, "
    "margin, or multiple). No laundry lists; if every pillar is consensus, the "
    "verdict is HOLD however good the company. A BUY does not require multiple "
    "expansion — above-consensus fundamentals alone can carry it.\n"
    "- A catalyst is a DATED FUTURE event (print, guidance, product, ruling). A "
    "streak of past beats or an event already priced in is NOT a catalyst. Every "
    "BUY states its catalyst path AND its kill criteria — the specific evidence "
    "that would change the call.\n"
    "- Re-rate only on MATERIAL fundamental change (guidance cut, margin break, "
    "balance-sheet event), never on price noise. Price moving against a thesis "
    "with fundamentals intact argues for patience, not a flip.\n"
    "- Decompose before concluding: revenue = price x volume (name WHICH moved); "
    "ROE via DuPont (margin x asset turnover x leverage) — leverage-driven ROE "
    "is low-quality, margin/turnover-driven is the real signal. ROIC vs WACC is "
    "the value-creation test; the cash-conversion cycle (DSO+DIO-DPO) vs peers "
    "and its own trend is the operating-health tell. Ratios only mean anything "
    "in comparison — vs peers AND vs history — never in isolation.\n"
    "- Multiple expansion must be JUSTIFIED (accelerating growth, steadier "
    "earnings, structural share gains); rising real yields compress multiples. A "
    "cyclical on peak earnings screens cheapest at the top (low-P/E trap) — use "
    "forward, mid-cycle economics. If your DCF sits far above market, suspect "
    "your own assumptions before declaring the market wrong.\n\n"
    "VALUATION (fundamental desk, judge):\n"
    "- DCF: discount unlevered FCF (EBIT*(1-tax)+D&A-capex-dNWC) at WACC -> EV; "
    "bridge to equity by subtracting net debt + minority/preferred. Distrust any "
    "model where terminal value > ~75% of EV, terminal growth g > long-run "
    "GDP/inflation (~2-3%), or WACC-g is razor-thin (a 1pt WACC/g change swinging "
    "fair value >25% = the output is an assumption, not a valuation). Quote a "
    "fair-value RANGE across a WACC x g grid, not a point; locate today's price in it.\n"
    "- Margin of safety: buy only meaningfully below base-case intrinsic value "
    "(wider discount for lower quality / higher uncertainty). A price above the "
    "optimistic-case IV is a SELL regardless of momentum.\n"
    "- Multiples are conditional, never absolute: judge P/E, EV/EBITDA, EV/Sales, "
    "P/FCF against the company's own history, sector peers, AND its "
    "growth+quality+capital-intensity. A high multiple is earned only by durable "
    "high ROIC/ROE compounding with reinvestment runway; a low multiple on eroding "
    "returns or a melting moat is a value trap. Prefer EV multiples over P/E when "
    "capital structures differ.\n"
    "- Quality-of-earnings gate BEFORE valuing: FCF should track net income "
    "(persistent accrual gap, rising DSO/inventory, capitalized costs = red flags); "
    "strip one-offs and SBC addbacks; distrust 'adjusted' EBITDA; penalize "
    "share-count creep; flag debt-maturity walls / covenant / refi-at-higher-rates risk.\n"
    "- Growth names: anchor on unit economics — gross margin, LTV:CAC >=3 with "
    "payback <18mo, net revenue retention >100% (>120% excellent), Rule of 40, burn "
    "multiple <1.5x. NRR<100% under heavy S&M = leaky bucket; discount the story.\n"
    "- Durability: require a moat (cost, brand/intangible, switching cost, network, "
    "efficient scale) a funded rival couldn't erode in 5 years; no moat caps "
    "conviction and shortens the terminal. Any customer >10% of revenue = "
    "concentration risk.\n\n"
    "FACTORS & TECHNICALS (quant, technical desks):\n"
    "- Factor regime first: momentum (12-1) leads mid-cycle trends and crashes at "
    "sharp reversals; value leads early-cycle / off lows / when spreads are wide; "
    "quality & low-vol lead late-cycle and risk-off. State the regime before "
    "weighting a factor.\n"
    "- Value + weak momentum = classic value trap (cheap AND falling); a low "
    "multiple pays only with a catalyst or momentum inflection. Treat extreme factor "
    "extension as mean-reverting, not a trend — fade euphoria, don't chase.\n"
    "- Technicals confirm/time a thesis, never create one. Weight the primary trend "
    "(200-day slope, price vs 50/200-day) far above oscillators. RSI is "
    "regime-dependent: >70 in a strong uptrend is continuation, not an auto-short; "
    "reversion only holds range-bound. Relative strength vs sector and vs SPY beats "
    "absolute price.\n"
    "- Base rates over the single-name story. Anti-overfitting: small samples lie; "
    "collinear indicators (RSI/MACD/stochastics all restate momentum) aren't "
    "independent evidence. Every signal states its invalidation level up front; an "
    "unfalsifiable signal shouldn't move conviction.\n\n"
    "RISK, SIZING & MACRO (risk, macro desks, judge):\n"
    "- Size on conviction AND volatility, never conviction alone: a fixed per-name "
    "risk budget (entry-to-stop loss = a bounded fraction of the book). Higher vol "
    "or wider stop -> fewer dollars. Bet a FRACTION of Kelly (quarter-to-half) "
    "because edge is estimated; when the debate is close, cut size, don't add. Even "
    "max conviction caps at the single-name limit.\n"
    "- Drawdown is asymmetric: recovery = 1/(1-loss)-1 (-20%->+25%, -33%->+50%, "
    "-50%->+100%). Capping downside beats maximizing upside; reject any thesis whose "
    "realistic downside can't be stopped before book-threatening size.\n"
    "- Correlation: names sharing a driver (sector, factor, rate/AI/oil sensitivity) "
    "act as one position in a drawdown — add effective correlated exposure, not "
    "nominal count.\n"
    "- Macro/rate-regime VETO: a clean bottom-up thesis can still be HOLD/SELL if "
    "the regime opposes it. Long-duration / high-multiple / unprofitable-growth "
    "names compress on rising real yields; rate-sensitives and cyclicals are hostage "
    "to the growth+policy regime. Map the firm-wide macro brief to THIS name's "
    "sector + factor loadings before voting; a tailwind raises conviction and "
    "permitted size, a headwind lowers both. State the veto explicitly rather than "
    "overriding it.\n\n"
    "OPTIONS (suitable_for_calls, exit strategist):\n"
    "- A long call is suitable only with (a) a dated catalyst landing inside the "
    "option's life with room to spare, (b) a move big enough to clear "
    "premium+spread+decay, (c) tight, liquid options (narrow bid/ask, real OI at "
    "the strike). A slow re-rate with no clock = shares, not calls. Conviction "
    "without a near-term catalyst is a shares thesis.\n"
    "- IV context by rank/percentile, not absolute: buying calls into already-rich "
    "IV needs a bigger move and eats vol-crush; cheap IV + a real catalyst is the "
    "ideal long-call setup, blown-out IV argues for shares or a debit spread. Align "
    "DTE so the catalyst is well inside the contract (theta accelerates in the last "
    "~30 days).\n"
    "- NEVER hold a long call through earnings / a binary event unless the trade IS "
    "that event bet (IV inflates in, crushes out — you can be right on direction and "
    "lose). Exit: scale out into strength at a preset level, trail the runner on "
    "price structure, place the hard stop BEYOND the bid/ask so spread noise can't "
    "fire it. P/C-ratio 'sentiment' is distorted by cheap deep-OTM lottery flow — "
    "corroborate with price + catalyst, never standalone.")
# STAGED (2026-07-02 review): CLI --fallback-model keeps REAL committee
# verdicts flowing on this model when Opus is unavailable, instead of the
# quant-composite degraded fallback. OFF by default — a fallback verdict is
# NON-degraded and WOULD trade, so enabling it is an operator decision (and it
# is unconfirmed whether the CLI falls back on subscription usage-limit vs only
# on 5xx overload). Set to 'sonnet' to enable.
# (operator 2026-09-02) Fall back to Opus 5 when Fable is quota-limited. The
# 2026-07-02 review left this empty on purpose -- a fallback verdict is
# NON-degraded and WILL trade, so it had to be an operator decision rather than
# a silent default. Patrick made that call when a Fable limit stopped the
# research floor mid-session. Measured the same day: claude-opus-5 answers
# while claude-fable-5-1 returns "You've reached your Fable limit". Every
# fallback call is journaled (brain_model_fallback) and the verdict records the
# model that produced it, so the report card never credits Fable for an Opus
# verdict. Set to "" to disable and return to quant-only degrade on quota.
# (operator 2026-09-22) The proposer is Opus 5.5 now, so the quota rung below it
# is Fable 5.1, the next best score, at the same max effort.
CLAUDE_FALLBACK_MODEL = "claude-fable-5-1"
CLAUDE_TIMEOUT_S = 600         # per-Opus-call cap. Raised 420->600 (operator 2026-06-30):
                               # on a RAM-loaded box concurrent calls run slower; a call
                               # that times out degrades the WHOLE workup to quant-only, so
                               # give it room to finish a REAL verdict rather than fall back.
RESEARCH_WORKERS = 2           # ceiling for concurrent candidates in research/debate.
                               # Cut 3->2 (operator 2026-06-30): fewer simultaneous
                               # `claude -p` subprocesses = each gets enough RAM/CPU to
                               # complete instead of timing out into degraded mode. The
                               # EFFECTIVE count is scaled DOWN further when RAM is tight —
                               # see WORKER_RAM_* below. Slower wall-clock, REAL verdicts.
# Research-floor concurrency vs AVAILABLE RAM (it chronically runs 50+ claude
# procs; too many concurrent Opus calls all time out into degraded mode).
# Operator 2026-07-07: worker count is now a STEP at SCAN_DOWNGRADE_RAM_GB — full
# RESEARCH_WORKERS above the floor, 1 worker at/below it — NOT the old continuous
# (avail_GB - HEADROOM) / PER_WORKER taper. These two constants are retained for
# reference/back-compat but no longer gate the live worker count.
WORKER_RAM_HEADROOM_GB = 1.0
WORKER_RAM_PER_WORKER_GB = 1.5
# SINGLE free-RAM floor for ALL scan/research downgrades (operator 2026-07-07:
# "only downgrade when there is 1 GB of RAM available or less when scanning").
# At/below this floor — and ONLY at/below it — the research cycle steps down:
# worker count -> 1 AND 'full' depth -> 'standard', and the 'ram headroom' health
# check reds. Above it the fund runs full-width research even on this chronically
# ~2 GB-free 16 GB box (the old 3.0/2.5/1.5 GB thresholds fired essentially every
# cycle and kept the fund permanently downgraded into standard/1-worker mode).
# TRADE-OFF the operator accepted: attempting full research down to 1 GB free can
# push some Opus calls into timeout -> degraded quant-only fallback; that harm is
# surfaced by the degraded-verdict watchdog (HEALTH_MAX_DEGRADED_SHARE_PCT +
# HEALTH_DEGRADED_ACC_GAP_PTS), so if degraded share/accuracy worsens, raise this.
SCAN_DOWNGRADE_RAM_GB = 1.0
# 'full'-depth -> 'standard' downgrade shares the single scan floor above (was a
# separate 3.0 GB knob; unified 2026-07-07). Full fans each workup into ~9 Opus
# calls; 'standard' folds them into ~2, so a real standard verdict still returns
# instead of timing out into a fake full-depth degraded one — but only once RAM is
# actually at/below the floor, not pre-emptively at 3 GB.
DEPTH_DOWNGRADE_RAM_GB = SCAN_DOWNGRADE_RAM_GB
# Zero-verdict-cycle RETRY RAM floor. GATE REMOVED -> 0.0 (operator 2026-07-03):
# this box chronically runs well under 4 GB free, so a 4 GB floor stranded EVERY
# retry forever ("without 4 GB free it won't start"). The original quota-burn
# concern is already covered by the graceful degradation above — the worker count
# scales to 1 and depth downgrades to 'standard' under RAM pressure, so a low-RAM
# retry returns REAL verdicts, not the degraded ones that burned quota. And the
# real cause of the 07-02/03 outage was an EXPIRED CLI LOGIN (401), not RAM — now
# detected distinctly via brain.auth_recently_failed() so an auth outage (which
# does NOT self-heal) is named rather than mis-blamed on resources. 0.0 = no gate.
RETRY_MIN_RAM_GB = 0.0
# Worker count in force when the PRE-2026-07-01 cycle_stats rows were learned
# (2-element legacy rows carry no worker field; the 3->2 cut was 2026-06-30) —
# used to normalize their pace. Ages out as the 8-row window refreshes.
LEGACY_STATS_WORKERS = 3

# Self-tuning research deadline (2026-06-10): the cycle proceeds with the
# verdicts in hand once the research phase passes its deadline — one hung
# Opus call must not push execution into the afternoon. The deadline sizes
# ITSELF from history instead of a fixed 75 min: state/cycle_stats.json keeps
# the last RESEARCH_STATS_KEEP normally-completed research phases per depth
# as [duration_min, n_candidates] pairs, and
#
#   deadline = clamp(MULT x worst-observed-minutes-per-candidate x today's
#              candidate count,  FLOOR[depth],  CAP)
#
# so it only ever fires on a true anomaly (hung CLI), never on a normal slow
# day. Observed 2026-06-10 (full depth, 12 candidates): 26 and 55 min.
RESEARCH_DEADLINE_MULT = 1.75              # headroom over the worst normal run
RESEARCH_DEADLINE_FLOOR_MIN = {            # minimum deadline per depth — also
    "fast": 20, "standard": 50, "full": 100}  # the deadline before any history
RESEARCH_DEADLINE_CAP_MIN = 150            # absolute ceiling — history can
                                           # never stretch the deadline past
                                           # this (was the fixed 75-min const)
RESEARCH_STATS_KEEP = 8                    # normal runs remembered per depth

# Research-floor STALL watcher (operator 2026-06-25: "the research deadline keeps
# getting hit, there has to be a better way to see hey it's still thinking or hey
# the stall is just there and it's a problem"). A daemon thread classifies the
# live floor every STALL_TICK_S as PROGRESSING / STALLED / QUOTA-OUT and surfaces
# it to the dashboard, a heartbeat file, the journal, and (on a sustained stall)
# an alert — adding ZERO Opus calls. It keeps reporting even while the main loop
# is blocked inside as_completed on a hung worker. A stall = no workup has
# completed in STALL_PACE_MULT x the worst-ever per-candidate pace (floored at
# STALL_GAP_FLOOR_MIN, capped under the deadline so it always fires first); a
# genuinely slow-but-progressing floor keeps clearing that bar.
STALL_WATCH = True
STALL_PACE_MULT = 3.0                      # since-last-completion > this x worst pace
STALL_GAP_FLOOR_MIN = 15.0                 # never flag a stall before this many minutes
STALL_TICK_S = 12                          # watcher heartbeat / re-classify cadence

# Sector earnings read-through (operator 2026-06-25: "when a big earning in the
# sector like MU ... sandisk jumped too and maybe they are connected ... if you
# wait till sandisk earnings to buy thats too late"). When a cohort BELLWETHER
# reports earnings, the fund force-reopens its PEERS the SAME cycle (cooldown
# overridden) with a read-through note, so the committee judges them on the
# sector signal + sentiment immediately instead of waiting for each peer's own
# print. Idempotent (a peer judged after the print is not re-forced) and bounded
# (READTHROUGH_MAX_PEERS caps how many extra workups one print can add).
EARNINGS_READTHROUGH = True
READTHROUGH_WINDOW_DAYS = 4                 # a bellwether print is "fresh" this many
                                           # CALENDAR days (covers a Fri print judged Mon)
READTHROUGH_MAX_PEERS = 6                  # cap peers force-judged per cycle (bound floor)

# IG triage escalation (IG 2026-07-13 item 2): findings un-reviewed past the
# SLA escalate ONE latched critical alert (health._escalate_ig_findings).
# BLOCK_BUYS is the harder stance the escalator's docstring promised but never
# wired (2026-07-16: a promised lever that exists nowhere is a
# measure-but-cannot-act hole) — OFF by default per validate-before-hardening:
# the adversarial IG always emits findings, so auto-gating trades on their
# mere existence would fire in a normal state. Flip deliberately if wanted.
IG_TRIAGE_ESCALATE_H = 24.0
IG_TRIAGE_BLOCK_BUYS = False

# Held-book re-underwrite schedule (operator 2026-07-16: "held re-underwriting
# gets done at 8:30 premarket; the sole focus is new names at the other times;
# if the computer was off don't cram — section by section till the backlog is
# done"). The 8:30 premarket pass (cycle.underwrite_held) researches held
# names and STORES verdicts (never trades); the trading cycles REUSE any held
# verdict fresher than REUSE_H and spend their workups on NEW names, acting on
# the stored held verdicts under normal market-hours gates.
# UNCAPPED by count (operator 2026-07-17: "no per-run cap for my 8:30 — the
# book can grow larger and larger"): 0 = no count cap; the 50-min premarket
# deadline in underwrite_held is the ONLY sectioner (measured pace 2026-07-17:
# 8 names in 13 min at 2 workers ≈ 30-name capacity per morning). Names the
# deadline cuts still backlog stalest-first under mechanical-stop protection.
HELD_UNDERWRITE_MAX_PER_RUN = 0
# The IN-CYCLE catch-up (premarket missed — computer off) is ALSO uncapped
# (operator 2026-07-17, superseding the original "don't cram" section cap):
# every stale held name joins the cycle's pool; the cycle's self-tuning,
# count-scaled deadline is the only sectioner.
HELD_VERDICT_REUSE_H = 6.0

# Street Research desk (operator 2026-07-16: "Goldman came out with a 30+ page
# report on AI — these reports help the fund make the best decisions and stay
# informed"). Drop institutional reports (PDF/.txt/.md) into
# research_docs/inbox/; each NEW file is distilled ONCE by Opus into a compact,
# number-preserving brief and the combined digest rides every candidate packet
# (fundamental/sentiment/macro desks + the judge) until it expires.
STREET_RESEARCH_DIR = BASE_DIR / "research_docs" / "inbox"
STREET_RESEARCH_TTL_DAYS = 30       # legacy/fallback doc shelf life
STREET_RESEARCH_MAX_DOCS = 8        # newest N kept in the digest
STREET_RESEARCH_MAX_NEW_PER_SCAN = 2   # Opus distills per sweep (cost bound)
STREET_RESEARCH_MAX_CHARS = 45_000  # extraction cap fed to the distiller
# Per-CLAIM shelf life by horizon (operator 2026-07-16: "some information is
# about the next decades, some just a couple weeks") — a tactical call dies in
# two weeks, a secular thesis earns months; every claim ALSO dies early the
# moment the review loop debunks it.
STREET_RESEARCH_TTL_BY_HORIZON = {"tactical": 14, "cyclical": 45,
                                  "structural": 180}
STREET_RESEARCH_REVIEW_EVERY_D = 7  # re-review cadence per doc (Opus, bounded)

# Deployment-aware entry scaler (re-grade audit 2026-07-16, A+ push): when
# the alpha book's REAL deployment sits below the floor, entry weights scale
# up by at most SCALE_MAX (never past max_position_pct; vol-scaling still
# applies after). Self-neutralizing: once the judge's own sizing lifts
# deployment above the floor the multiplier is 1.0. Journaled with the
# counterfactual weight so the learning loop can grade the lever itself.
ALPHA_DEPLOYMENT_FLOOR_PCT = 60.0
ALPHA_DEPLOYMENT_SCALE_MAX = 1.5
# (2026-08-13 operator decision) ALPHA DE-RISK until the screen re-validates:
# every alpha_stocks per-name target weight is multiplied by this factor at
# the entry-sizing site (sleeves._run_alpha_stocks) — HALVING single-name
# deployment while the screen's edge is unproven. Freed capital flows to the
# sleeve's existing SPY parking automatically via CASH_EQUITIZATION (invested
# share below min_invested_pct tops back up to target — no new journaling).
# RESTORE CONDITION (manual operator review, never automatic): set back to
# 1.0 only when the screen's graded-universe MEDIAN forward excess vs SPY
# (report_card_digest base rate) is >= 0 over a trailing 30-cycle window.
# The ALPHA_DEPLOYMENT_FLOOR_PCT scale-up machinery above is deliberately
# untouched: while the halved book sits under the floor its bounded 1.5x can
# claw back at most half the cut (net >= 0.5x, <= 0.75x per name) and it
# self-neutralizes exactly as designed.
ALPHA_SIZING_DERISK = 0.5
# Parking concentration visibility (re-grade audit: 39% of firm equity sat
# outside every firm-wide cap) — the health desk names it past this share.
# EOD mark-to-close feed-of-record SLA (operator 2026-08-19). The check used
# to grade two booleans — "zero failed marks" and "zero fallback events" — over
# journal slices that never truncate (32 mark-family events exist in 71 days),
# so a single fallback latched it RED with no clearing path. It failed 105
# consecutive sweeps on three 2026-08-11 rescues while poisoning four healthy
# checks. These three knobs make it an SLA that can both go red for the real
# reason and return to green when the feed recovers.
# COVERAGE is the leg that matters: it asks whether each recent session got a
# successful mark at all — the question the old check never asked, and the one
# that exposes days whose equity of record was carried from an intraday print.
# Exit-protection SESSION COVERAGE bars (fix brief 59, 2026-08-19). The firm
# emitted "EXIT PROTECTION WAS DOWN 260 min of trading" alerts that NO health
# check owned: 'exit monitor' grades time-since-last-sweep, so a hole that has
# closed reads green forever. Measured 2026-07-27..08-19: 1,235 blacked-out
# regular-session minutes across 8 holes plus 368 lock-stood-down sweeps =
# ~25.7% of trading minutes with zero stop evaluation, against ~$60k of
# unparked notional. 98% is the coverage floor; a SINGLE gap over 30 regular
# minutes fails on its own, because one long hole is what actually lets a stop
# fire late (CRS and TXN both stopped after a 16.07h blackout).
EXIT_COVERAGE_SLA_DAYS = 10.0
EXIT_COVERAGE_MAX_GAP_MIN = 30.0
EXIT_COVERAGE_MIN_PCT = 98.0
# ── Exit-protection CONTINUITY (2026-09-01; fix briefs 87/66/72/78/53/28) ────
# Root-cause chain measured 2026-08-31/09-01: run_cycle.py decided delegated-
# vs-headless from ONE 3 s probe of the heavy /api/overview; a busy or paged-
# out app (see data_engine.prune_yf_threads) failed it, the cycle ran HEADLESS
# holding state/cycle.lock, app.py's monitors stood down for the whole hold
# (37 equity_sweep_skipped rows, 09:50-10:25 ET) and the headless cycle itself
# swept ZERO times (a no_verdicts return exits before the end-of-cycle sweep).
APP_PROBE_TIMEOUT_S = 5.0        # per attempt against the state-free /api/ping
APP_PROBE_ATTEMPTS = 4           # ~30 s of patience for a busy-but-alive app
APP_PROBE_GAP_S = 5.0
CYCLE_DELEGATION_ATTEMPTS = 3    # POST /api/cycle retries before the headless path
HEADLESS_EXIT_SWEEP = True       # any headless lock-holder runs its OWN exit sweeper
HEADLESS_SWEEP_FRESH_MULT = 3.0  # app counts a foreign sweep < 3x the cadence old as coverage
EXIT_PROTECTION_ENTRY_GATE = True      # no NEW risk while stops cannot route in-session
EXIT_PROTECTION_MAX_SWEEP_AGE_S = 600.0  # 10 missed 60 s sweeps = protection is down
# 'state integrity' grades BOOK corruption as an immediate alarm and DERIVED
# (recomputable health/telemetry) recoveries against a rate budget — a few a
# day is a hot-file read/replace race on Windows, a rising rate is the host
# genuinely losing writes. Set from the observed baseline: 1 derived restore
# in the last 24h (reopen_path_inert, 2026-08-19T14:49).
STATE_DERIVED_RESTORE_BUDGET_24H = 3

# A factor edge is a median split, so it needs a real population STRICTLY on
# each side of the median score. Under heavy ties (the screen saturates
# momentum at 100) the boundary falls inside the tie block and the two halves
# are the same score — indistinguishable however ties are broken. Below this
# floor on either side the edge is published as undetermined and nothing is
# sized on it. 3 is deliberately permissive: the point is to kill the
# degenerate all-one-score case, not to demand significance.
FACTOR_EDGE_MIN_SIDE = 3
EOD_MARK_SLA_DAYS = 10.0
EOD_MARK_SESSION_COVERAGE_PCT = 80.0
EOD_MARK_FALLBACK_MAX = 3
PARKING_FIRM_ALERT_PCT = 45.0
# Per-sleeve parking bar, and how long a sleeve may sit past it before "all
# parking" is a DEFECT rather than a desk between trades (operator 2026-08-19).
# The per-sleeve leg used to fire on an instantaneous reading, so calls_only —
# which had just realised +$1,672 on GEO/IOVA/VAC (2026-08-14), closed out
# 08-17 and re-parked — was escalated as "that mandate is not being expressed"
# while being the fund's best sleeve (+6.8% lifetime, $26,688 on $25,000). A
# desk holding beta between qualifying signals is the mandate WORKING. 10 days
# is longer than the desk's observed gap between qualification batches (07-22,
# 07-23, 07-27, 07-28, 08-04, 08-14) so a genuinely dead gate still reds, and
# short enough that a real stall surfaces inside two trading weeks. The
# FIRM-WIDE leg is deliberately NOT recency-gated: a whole fund in parking is a
# red no matter how recently one desk traded.
PARKING_SLEEVE_ALERT_PCT = 85.0
PARKING_SLEEVE_IDLE_DAYS = 10.0
# Fleet re-grade action 2 (2026-07-16): the same momentum engine ranks the
# parking seat, the core seats AND the rotation sleeve — convergence into ONE
# ticker is guaranteed at momentum tops, and parking was invisible to every
# firm cap. Hard ceiling on ANY single ticker's look-through share of firm
# equity before new parking buys stop, plus a pre-flip check that refuses to
# hand the ~40%-of-firm parking reservoir to a challenger the firm already
# holds in size elsewhere.
PARKING_SYMBOL_MAX_FIRM_PCT = 50.0
ETF_SEAT_CONVERGENCE_MAX_PCT = 12.0
# Fleet re-grade action 1: the judge's suitable_for_calls flag ran 0-true in
# 575 verdicts — as a hard gate it kept 25% of firm capital inert for the
# fund's whole life. The flag is DEMOTED to a veto: an explicit False WITH a
# stated reason binds; a bare reflexive False hands the decision to the
# desk's own mechanical screen (options tradable on Public + no earnings
# print inside the option's possible life; chain liquidity is enforced
# downstream by pick_call/pick_put's OI + spread floors).
OPTIONS_JUDGE_VETO_MIN_REASON = 15   # chars of stated reason for a False to bind
# Queued-for-open intents that can't fill are retried each sweep; past this
# age an intent is stale (the thesis price is a week old) — drop + journal.
OPEN_QUEUE_TTL_DAYS = 5.0

# NAME-full vs CAPITAL-full (operator 2026-07-16: "$16K in SPY parked while
# BUYs were turned away — parking is supposed to be there UNTIL other stocks
# get bought"). Live state that day: 8/8 slots "full" with only 27.5% of the
# book deployed ($7,001 across 8 PM-small-sized positions) and 64.9% ($16,513)
# parked — the slot cap counted NAMES while the capital sat in SPY. When the
# alpha book is name-full but real deployment is under this floor, the slot cap
# YIELDS to a new conviction BUY (funded from parking via the existing
# _free_cash_from_equitization pull), up to a hard ceiling of
# max_positions + MAX_EXTRA names so the focus mandate still binds.
ALPHA_PARKED_OVERRIDE_INVESTED_PCT = 65.0
ALPHA_PARKED_OVERRIDE_MAX_EXTRA = 4

# Operator research requests (operator 2026-07-16: "PYPL went parabolic up and
# HTZ parabolic down but the firm didn't detect them — let me insert names to
# be researched with the next cycle"). Names queued via POST /api/research-request
# get a committee seat next cycle, cooldown OVERRIDDEN (the operator outranks
# the cadence gate, same override the earnings read-through uses). Bounded so a
# big paste can't blow the research deadline; the remainder carries over.
OPERATOR_REQUESTS_PER_CYCLE = 6
EARNINGS_COHORTS = {
    # bellwethers = names whose prints set the tape for the group;
    # peers = the rest, force-reopened on a bellwether print.
    "memory_storage": {
        "bellwethers": ["MU"],
        "peers": ["SNDK", "WDC", "STX"]},
    "ai_accelerators": {
        "bellwethers": ["NVDA", "AMD"],
        "peers": ["AVGO", "MRVL", "ALAB", "SMCI", "TSM"]},
    "semi_cap_equipment": {
        "bellwethers": ["AMAT", "LRCX", "KLAC", "ASML"],
        "peers": ["KLIC", "UCTT", "ICHR", "ONTO", "ACLS"]},
    "analog_mcu": {
        "bellwethers": ["TXN", "ADI"],
        "peers": ["MCHP", "ON", "NXPI", "STM"]},
    "foundry_logic": {
        "bellwethers": ["TSM", "INTC"],
        "peers": ["GFS", "UMC"]},
}

# Primary-source earnings DOCUMENTS (operator 2026-06-25: "make sure on earnings
# the files are read too like 8k"). When a name just reported, attach its SEC
# 8-K earnings release + latest 10-Q/10-K (free, official EDGAR — has the real
# numbers + management commentary + outlook) and a best-effort call-transcript
# excerpt to its packet, so the committee judges the print on the ACTUAL filings,
# not just the surprise %. Tightly gated (fresh-print names only), cached daily,
# fully fail-soft. SEC requires a descriptive contact User-Agent.
EARNINGS_DOCS = True
EARNINGS_DOCS_WINDOW_DAYS = 8              # attach docs only within N days of a print

# Add-on-strength for CONFIRMED leaders (operator 2026-06-25: "if it's proven the
# ride keeps going up because momentum keeps going up like the last months, it
# needs to discern ... loosen 'only add on a 15% pullback'"). The fund could not
# add to a winner at all — the sleeve skipped any already-held name. Now a held
# WINNER whose committee affirmatively wants more (a BUY/STRONG_BUY, never a HOLD)
# AND that is a confirmed momentum leader (intact multi-month uptrend) with a
# strengthening catalyst (earnings beat per the 8-K/transcript) OR sector strength
# gets a MEASURED add toward the higher target weight — pyramiding into a proven
# winner, never averaging down. Tightly gated so it discerns, never chases:
# winners-only, bounded add count, an add cooldown, a capped step, vol-scaled, and
# every existing risk cap still applies to the resulting TOTAL position.
ADD_ON_STRENGTH = True
ADD_MAX_COUNT = 2                          # max measured adds over a position's life
ADD_COOLDOWN_DAYS = 3                      # min days between adds (no daily topping-up)
ADD_STEP_MAX_PCT = 4.0                     # max sleeve-capital % added per step
ADD_MIN_SURPRISE_PCT = 5.0                 # "strengthening catalyst" = beat of >= this
ADD_MOM_MIN = 60.0                         # momentum-factor floor (alt confirmation path)

# ── Conviction-upgrade / replacement (2026-07-01) ─────────────────────────────
# When a sleeve's book is FULL and a materially-stronger new BUY appears, evict
# the weakest EVICTABLE holding to admit it — but rarely and only when clearly
# accretive, never churny. Every guard below must pass; the swap is journaled.
REPLACE_ON_CONVICTION = True               # master switch for the swap path
REPLACE_MIN_MARGIN = 12.0                  # new conviction must beat the weakest holding's by >= this (a tie/noise-edge never swaps)
REPLACE_HYSTERESIS_CYCLES = 2              # the SAME (buy<-evict) intent must recur this many cycles before firing (anti-whipsaw)
REPLACE_MIN_HOLD_DAYS = 3                  # never evict a holding entered within this window (let a fresh entry work)
REPLACE_ROUNDTRIP_DAYS = 3                 # an evicted name may not be re-bought for this long (anti-whipsaw)
REPLACE_MAX_PER_CYCLE = 1                  # at most this many swaps per sleeve per cycle (bound turnover)
SEC_UA = os.environ.get("SEC_USER_AGENT", "LochRaven Capital research contact@example.com")
# Literal earnings-call transcript. The operator authorized the RAM cost of a
# headless browser (2026-06-25) to read the ACTUAL transcript from stockanalysis.com
# (its body is JS-rendered). It is tightly bounded: only for a name that JUST
# reported, rendered ONCE per ticker per day (cached), short-lived, fail-soft. Set
# TRANSCRIPT_HEADLESS=False to disable if host RAM is tight (the SEC 8-K + news
# still carry management commentary). TRANSCRIPT_API is an optional keyed fallback
# (e.g. earningscalls.dev via RapidAPI) tried only if the headless scrape returns
# nothing.
TRANSCRIPT_HEADLESS = True
TRANSCRIPT_EXCERPT_CHARS = 6000
TRANSCRIPT_MIN_RAM_GB = 2.5                 # skip the headless render below this free RAM
TRANSCRIPT_API = ""

# Pre-flight RAM recovery (operator 2026-07-10: "fix the RAM starvation").
# Twice this week the research floor launched into a starved box (0.5-1.5 GB
# free) where every Opus call died to quant-only degraded (07-09 wedge, 07-10
# 13/13 + 17/17 failed workups) — the hog each time was a pile of FINISHED
# Claude sessions idling at ~0% CPU. The (now RAM-pressure-aware) session
# janitor reaps them; before sizing the research floor, the cycle fires that
# janitor when free RAM is below RAM_RECOVERY_GB and waits RAM_RECOVERY_WAIT_S
# for the reap. Fail-soft: task missing / psutil missing -> the existing
# worker/depth downgrades still bound the damage.
RAM_RECOVERY_GB = 2.0
RAM_RECOVERY_WAIT_S = 25
RAM_RECOVERY_TASK = "Claude Session Janitor"

# Momentum cooldown lane (operator 2026-06-23: "momentum gets its own screening,
# free space for the day-to-day judging"). An EXTENDED-momentum HOLD — the
# parabolic leaders the committee re-judges to the SAME "too extended, wait for
# a pullback/print" verdict every cycle — is made macro-IMMUNE in cooldown: a
# generic CPI/PCE/jobs print can't flip that verdict, yet today it reopens EVERY
# HOLD daily and defeats the 3-day cooldown, burning the research budget on a
# foregone conclusion. Laned names are NOT removed or forgotten — they stay on
# the Bench with their own pullback/RSI/SMA/earnings reopen trigger (watched
# every cycle), and re-enter the FULL committee the instant that fires. The lane
# only stops paying Opus to re-state a verdict that cannot change. Flag OFF =
# byte-for-byte legacy behavior. Threshold is evidence-tunable within bounds
# (forward-return scorecard via learning.momentum_lane_skip).
MOMENTUM_LANE = True
MOMENTUM_LANE_MOM_MIN = 80.0      # context.factors.momentum (0-100) = "extended"
MOMENTUM_LANE_MAX_DWELL_CYCLES = 3   # force a full re-research at least this often
                                     # (days); == RESEARCH_COOLDOWN_DAYS by default,
                                     # set LOWER for a tighter momentum re-look cadence
MOMENTUM_LANE_BOUNDS = {"MOMENTUM_LANE_MOM_MIN": (70.0, 92.0)}

# Thesis-gated bench cooldown (operator 2026-07-09: "a name already on the
# watchlist must not get re-rated when the thesis itself hasn't triggered the
# re-run"). A benched HOLD with its own non-macro re-look path (pending
# earnings or a technical reopen trigger) is exempt from the age timer AND the
# generic macro-print reopen — the Bench summons it the moment ITS setup
# completes. The 3-day timer re-rates were pure cost ("still HOLD") and fed
# the max-recheck counter that judged out PENG before its +31.7% earnings pop
# (IG 2026-07-09 #7). MAX_DWELL is the bounded staleness escape (no unbounded
# state, house doctrine): past it the name gets ONE fresh committee look —
# 21d ≈ a full report-card grading window, ~7x less churn than the old timer.
# Flag OFF = byte-for-byte legacy (timer + macro reopen).
BENCH_THESIS_GATE = True
BENCH_GATE_MAX_DWELL_DAYS = 21

# depth: "full"     = 5 analyst calls + 3 debate calls + 1 judge per candidate
#        "standard" = 1 analyst-panel call + 1 debate call + 1 judge per candidate
#        "fast"     = 1 combined call per candidate
DEFAULT_DEPTH = "standard"

# ── Universe ──────────────────────────────────────────────────────────────────
STOCK_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "LLY",
    "JPM", "V", "UNH", "XOM", "MA", "COST", "HD", "PG", "JNJ", "ABBV",
    "NFLX", "CRM", "BAC", "AMD", "KO", "PEP", "TMO", "WMT", "MRK", "CVX",
    "ADBE", "ORCL", "ACN", "MCD", "CSCO", "QCOM", "IBM", "CAT", "GE", "TXN",
    "INTU", "AMAT", "ISRG", "NOW", "UBER", "PLTR", "BKNG", "SPGI", "GS",
    "BLK", "VRTX", "REGN", "PANW", "SNOW", "SHOP", "MU", "ANET", "DE", "LMT",
]
ETF_UNIVERSE = [
    "SPY", "QQQ", "VOO", "IWM", "DIA", "XLK", "XLE", "XLF", "XLV", "XLI",
    "XLP", "XLY", "XLU", "XLB", "XLC", "SMH", "GLD", "TLT", "EEM", "EFA",
]
ROTATION_UNIVERSE = [
    "XLK", "XLE", "XLF", "XLV", "XLI", "XLP", "XLY", "XLU", "XLB", "XLC",
    "SMH", "QQQ", "IWM", "GLD", "TLT", "EEM", "EFA",
]
CASH_ETF = "BIL"               # absolute-momentum fallback
BENCHMARK = "SPY"

# Dynamic ETF seats (operator 2026-07-16: "auto pick the best ETFs for alpha
# and core... if a better ETF comes, switch and remove it, and when an ETF
# becomes a SELL, sell it — don't just stick with SPY"). The SAME validated
# dual-momentum engine that runs etf_rotation (data_engine.rotation_signals)
# now EARNS the parking seat and the core seats — firm/allocation.py.
# PARKING must stay broad + deep-liquid (it is the fund's instant-cash
# reservoir, sold intraday to fund conviction buys — a sector or thin ETF
# there would add tracking chaos to every entry).
# SELECTION IS NOT MOMENTUM (operator 2026-07-20: "like SPY but better, not
# too volatile, not too stagnant — safe parking best to keep going forward").
# The whole liquid ETF pool competes, but through BEHAVIORAL gates that make
# a fund SPY-like — an ann-vol corridor around SPY's OWN current vol (regime-
# aware: "not too volatile" on the high side, "not cash-stagnant" on the low
# side), broad-market correlation (sector/commodity/bond funds fail here — no
# name list needed), and real history — then ranked by RESERVOIR FITNESS:
# risk-adjusted drift (6m/3m return per unit of vol) minus a penalty for a
# max-drawdown worse than SPY's. SPY always competes as the baseline; the
# hand-list below is only the fail-safe when pool/price data is unavailable.
PARKING_VOL_BAND = (0.55, 1.35)     # ann-vol corridor, × SPY's own ann vol
PARKING_MIN_CORR_SPY = 0.60         # daily-return corr floor = broad-equity behavior
PARKING_MIN_HISTORY_D = 130         # completed bars needed to judge (matches rotation)
PARKING_FITNESS_SWITCH_MARGIN = 0.35  # fitness points; streak discipline still applies
PARKING_ELIGIBLE = ["SPY", "VOO", "VTI", "QQQ", "DIA", "RSP", "IWM"]
# CORE seats draw from the diversified rotation universe PLUS the broad set —
# growth, value, sectors, intl, bonds, gold all compete for the three slots.
CORE_ELIGIBLE = sorted(set(ROTATION_UNIVERSE + PARKING_ELIGIBLE))
# FULL ETF POOL (operator 2026-07-16: "there are more than 20 ETFs... the
# same access live will have"): weekly, every Public-buyable ETF is screened
# for price + median dollar-volume floors; the survivors, ranked by
# liquidity, form the pool that feeds the seat eligibles and the scan's ETF
# candidates. Floors are LIQUIDITY hygiene, not judgment — a thin ETF the
# fund can't exit cleanly has no seat claim regardless of momentum.
ETF_POOL_MAX = 150                # pool cap (liquidity-ranked survivors)
ETF_POOL_MIN_PRICE = 5.0
ETF_POOL_MIN_DOLLAR_VOL = 5_000_000.0    # 60d MEDIAN daily $ volume
ETF_POOL_TTL_DAYS = 7.0
CORE_ELIGIBLE_POOL_N = 60         # top-N of the pool compete for core seats
PARKING_MIN_DOLLAR_VOL = 1_000_000_000.0  # parking = reservoir; $1B+/day only

# Switch discipline (hysteresis — upgrades without churn): a challenger must
# beat the incumbent's momentum score by MARGIN on N CONSECUTIVE looks. A
# committee SELL verdict on a held seat bypasses hysteresis (sell now,
# replace with next-best). Checks run at the 8:30 premarket pass.
ETF_SEAT_SWITCH_MARGIN = 4.0       # score points (3m/6m blend, ~% return)
ETF_SEAT_SWITCH_CONSECUTIVE = 2    # consecutive premarket looks
CORE_SLOT_WEIGHTS = (35.0, 15.0, 5.0)   # slot weights preserved from VOO/QQQ/GLD

# ── Full-market scan (no stock missed) ───────────────────────────────────────
FULL_MARKET_SCAN = True          # screen EVERY listed US common stock daily
MIN_PRICE = 1.0                  # tradability floor, not a quality judgment
MIN_AVG_DOLLAR_VOL = 500_000.0   # 60d avg $ volume — must actually be tradable
SCAN_CHUNK = 300                 # symbols per batched download
SCAN_CACHE_HOURS = 18            # one full sweep per day serves all cycles
# Degraded-scan guard (operator 2026-06-24, after a yfinance throttle returned only
# 918 tradable of 5609 listed vs the normal ~3836 and that thin, skewed universe got
# cached). A scan returning fewer tradable names than this floor is a PROVIDER
# THROTTLE, not a real market — don't overwrite a fuller prior scan with it.
SCAN_MIN_TRADABLE = 2000         # absolute floor of tradable names for a healthy scan
SCAN_MIN_TRADABLE_FRAC = 0.40    # ...or at least this fraction of listed symbols
TIER_POOL = 150                  # top quant names that get market-cap tiered
# 4/4/4 (operator 2026-07-16): the held book moved to its own 8:30 cycle, so
# the 9:45 cycle's freed workups widen the NEW-name intake — "space for an
# even broader outlook on the market". Was 3/3/3 when held names shared the
# same research budget.
TIER_SLOTS = {"large": 4, "mid": 4, "small": 4}  # research slots per cap tier

# Slate backfill (operator 2026-06-23: "day to day exploring MORE names, not the
# same names — don't shorten how many get brought up"). When the momentum lane /
# cooldown vacates an exploratory stock slot, REFILL it with the next-ranked
# FRESH (never-recently-judged) name from the same cap tier — so the committee
# always explores a full slate of NEW names instead of shrinking to the held
# book. The scan exposes a ranked reserve of SCAN_TIER_POOL_DEPTH names per tier
# for this. Flag OFF = legacy (a cooled slot just vanishes, slate shrinks).
SCAN_BACKFILL = True
SCAN_TIER_POOL_DEPTH = 12         # ranked reserve per tier (3 slots + 9 backfill)

# Compound reopen-triggers (operator 2026-06-23). When ON, the committee may emit a
# multi-leg reopen_trigger {op: all|any|after, legs:[...]} that mirrors a compound
# thesis ("a pullback AND RSI<55 AND holds the 50-DMA, AFTER the macro clears, OR
# the earnings"), and the bench evaluates the FULL condition — not just the primary
# leg. OFF reproduces the single-leg behavior byte-for-byte. Ship dark, prove both
# flag states, then flip. (Design + adversarial review: wf 2026-06-23.)
COMPOUND_TRIGGERS = True

# Quality gates on research candidates (2026-06-10 audit): parabolic squeezes
# and death spirals are lottery tickets, not momentum — a name down 96% on the
# year that 35x'd in 3 months must never consume an Opus research slot.
SMALL_TIER_MIN_MCAP = 300e6      # institutional floor for the small tier
SMALL_TIER_MIN_PRICE = 5.0       # sub-$5 small caps: spreads + manipulation
MAX_VOL_ANN_PCT = 150.0          # any tier: annualized vol above this = squeeze junk
MIN_RET_12M_PCT = -80.0          # any tier: death-spiral marker

# Entry price-drift guard (2026-06-10 audit): verdicts are formed on the prior
# close; execution happens 30-90+ min into the next session. Past this band
# (scaled up by the name's own daily sigma) the thesis price is stale — skip
# the entry instead of chasing the gap; the next cycle re-judges at the new px.
MAX_ENTRY_DRIFT_PCT = 3.0

# Honest paper fills (2026-06-10 audit): live fills pay the spread; a paper
# track record that fills at MID flatters the calls desk by the entire TP1
# tier. Paper buys/sells cross part of the spread + pay per-contract fees.
PAPER_SLIPPAGE = {
    "equity_bps": 5.0,              # per side, of fill price
    "option_spread_frac": 0.5,      # cross this fraction of the half-spread
    "option_fee_per_contract": 0.65,
}

# Paper BUYS respect market hours like live ones (catch-up cycles at 3am were
# booking entries at the last close — a fill no live order could get). Exits
# always work: protecting the book never waits for the bell.
PAPER_ENFORCE_MARKET_HOURS = True

# ── Cash equitization (anti-cash-drag) ────────────────────────────────────────
# An equity mandate benchmarked against SPY loses to SPY by default whenever
# it sits in cash (observed 2026-06-10: alpha 75% cash — the judge hands out
# conservative target weights and nothing re-deployed the residue). Idle cash
# above the floor parks in the benchmark; the position is sold automatically
# to fund real conviction buys, is exempt from per-name stops and committee
# SELLs (like the strategic core), never consumes a max_positions slot, and
# is skipped while the sleeve is de-risking (soft tier / breaker / halt) —
# in a drawdown, cash IS the position. Core has its 55% core floor; rotation
# has BIL. Calls desk was deliberately excluded ("premium dry powder") until
# 2026-07-01: 22 days at 0.0% invested earned exactly $0 on $25k (goal audit
# fix 3) — the dry powder now waits in BIL (T-bill ETF: instantly liquid,
# zero equity beta, ~$21-25/wk yield), and premium buys reclaim cash from the
# parking first, so the desk's readiness is unchanged while the wait is paid.
CASH_EQUITIZATION = {
    "alpha_stocks": {
        "symbol": BENCHMARK,          # park idle cash in the benchmark
        "min_invested_pct": 70.0,     # equitize when invested share is below
        "target_invested_pct": 90.0,  # ...topping up to this
    },
    "calls_only": {
        # SPY, not T-bills (operator 2026-07-02): on a LIVE account, cash out of
        # a T-bill isn't readily available to buy the next call — parking the
        # dry powder in SPY keeps it in one liquid, instantly-sellable
        # instrument (same as alpha's parking), at the cost of carrying beta
        # while idle. Reclaimed before every premium sizing.
        "symbol": BENCHMARK,
        "min_invested_pct": 0.0,
        "target_invested_pct": 95.0,
    },
    # Companion to the satellite de-dup (2026-07-01 purpose review #5b):
    # satellites filtered out for duplicating alpha's book must not strand
    # cash — for a SPY-benchmarked sleeve, idle cash is worse than the
    # duplication it cures (adversarial verify). SPY (not VOO) so the parking
    # stays a DISTINCT flagged position, never merged into the mechanical core.
    "core_blend": {
        "symbol": BENCHMARK,
        "min_invested_pct": 0.0,
        "target_invested_pct": 90.0,
    },
}

# ── Purpose-fitness review knobs (2026-07-01) ─────────────────────────────────
# Verdict-only advisory review: fires on GRADED-VERDICT evidence (the fund's
# fastest-accumulating signal) long before the 20-trade money-review gate can
# open. Writes provisional lessons ONLY — never cap/bench tuning.
VERDICT_REVIEW = True
VERDICT_REVIEW_MIN_ROWS = 60        # non-degraded graded rows required
VERDICT_REVIEW_MIN_BUYSIDE = 10     # of which buy-side
VERDICT_REVIEW_EVERY_DAYS = 7
# STAGED ~1 week (clean attribution of the STRONG_BUY semantics fix first —
# adversarial verify): the quality-vs-timing starter-BUY guidance block.
STARTER_TIMING_GUIDANCE = False
# (Removed 2026-07-02: the CORE_SAT_DEDUP "core must avoid alpha's names"
# constraint. A real multi-strategy fund holds the same name across independent
# books — the operator rejected forcing core to differ from alpha. Sleeves are
# isolated and may overlap; each manages its own book/stops.)
# Trailing-stop peak poisoning guard (2026-07-02): a one-sweep mark jump beyond
# this ABOVE the prior peak is treated as a bad tick and does NOT advance the
# high-water-mark, at BOTH peak-advance sites (broker.sleeve_equity marking +
# monitor sweep). A liquid stock rarely gaps >25% intraday; monitor.reconcile_
# peaks corrects a genuine gap from real daily-bar OHLC by EOD.
# A single-sleeve drawdown past this is re-priced from a fresh quote fetch
# before any halt (risk.py) — an equity book does not lose half its value while
# the index is flat, so such a reading is a claim about the DATA first.
# A DECLARED reopen path with no arms and no fires for this many days is a
# design promise nothing can keep (health: reopen-path liveness).
# Conviction calibration (escalation step 6, 2026-07-28). The judge scores each
# name in isolation and cannot produce cross-sample variance; debate.py
# rescales the component sum about the trailing MEDIAN so the ordering it DOES
# provide reaches the consumers that read conviction on an absolute scale.
# Target chosen from 900 real verdicts: 8.0 moves the sizing mix from
# 643/900 stuck in the 2-4% starter band to 521 starter / 244 full / 40
# concentrate, without pushing a materially larger share below the action
# floor. Set CONVICTION_TARGET_STDEV to 0 to disable and ship raw sums.
# Active fix-brief queue size. Was a hard [-10:] that DELETED the oldest items
# on every filing — on 2026-07-28 five resolutions and their written evidence
# vanished that way. Open briefs are now never evicted and resolved ones are
# archived rather than dropped (learning._file_fix_requests).
# A health check failing this many consecutive sweeps stops being an alert and
# becomes a DECLARED standing defect (health.record_standing_defects) — the
# state the rest of the firm can read. Gating the dependent sleeves off that
# state is opt-in: for conviction the defect costs benchmark-hugging, and
# halting the fund to avoid that is the larger harm. Flip deliberately.
STANDING_DEFECT_SWEEPS = 20
STANDING_DEFECT_BLOCKS_SLEEVES = False
FIX_BRIEF_MAX_ITEMS = 60
CONVICTION_TARGET_STDEV = 8.0
CONVICTION_CALIBRATION_MIN_N = 40   # reference verdicts before rescaling at all
CONVICTION_MAX_GAIN = 2.5           # ceiling so a freak-narrow window cannot explode the scale
REOPEN_PATH_INERT_DAYS = 5
IMPLAUSIBLE_DD_PCT = 40.0
PEAK_MAX_JUMP_PCT = 25.0
# Quote trust (operator 2026-07-28, PRSU). A two-sided book wider than this is
# not a price anyone could transact at — the mid of a 37 x 65 quote is fiction.
# broker.equity_quotes_public falls back to the last real print, and failing
# that returns NOTHING for the symbol so the caller uses a daily bar instead.
QUOTE_MAX_SPREAD_PCT = 8.0
# Before a protective exit may ROUTE on a mark, that mark must be corroborated
# by an independent source within this tolerance (monitor._mark_corroborated).
# A stop is the one place a single bad tick converts directly into realized
# loss, so it is the one place a second opinion is worth a sweep of delay.
STOP_MARK_CONFIRM_TOLERANCE_PCT = 3.0
# ...unless the SAME stop condition survives this many consecutive sweeps on a
# self-consistent mark. A phantom tick does not persist; a real breakdown does,
# so an unreachable second source can never leave a genuine crash unprotected.
STOP_UNCONFIRMED_MAX_SWEEPS = 3
# (2026-09-01) When the SECOND source is UNAVAILABLE (0/None — a process-wide
# yfinance outage, 08-31: 22 positions unpriceable all morning) rather than in
# DISAGREEMENT, a triggered stop on a sanitized two-sided Public quote is held
# this many sweeps, not the full 3: a real breakdown must not wait 3 minutes
# for a witness that cannot show up. Never 1 — one held sweep still kills the
# one-tick phantom class.
STOP_UNCONFIRMED_MAX_SWEEPS_NO_REF = 2
# (2026-09-02 full audit) A network call made while holding state.SLEEVE_LOCK
# freezes EVERY stop in the firm for its whole duration. The Public SDK's
# ApiClient defaults are timeout=30 / max_retries=3 / backoff 0.3, and its POST
# path retries in-process (urllib3's adapter excludes POST), so one degraded
# endpoint could hold the lock ~2 min per call -- at the 1 s options cadence.
PUBLIC_API_TIMEOUT_S = 6          # per HTTP attempt
PUBLIC_API_MAX_RETRIES = 1        # in-process retries for non-safe methods
YF_RESET_MIN_INTERVAL_S = 300.0   # floor between yfinance session rebuilds
# NASDAQ symbol-directory fetch: after a failure, do not retry (or journal)
# again for this long. Every call used to re-fetch and re-journal on failure —
# 2,562 universe_etf_fetch_error rows in 7 days (71% of the journal) and a
# 21 s stall per call INSIDE the trading cycle (allocation._is_leveraged).
ETF_DIRECTORY_RETRY_MIN = 60.0
# Calls sleeve gate reachability (operator 2026-07-02): STRONG_BUY-only left the
# leveraged sleeve inert (0/357 verdicts). It now ALSO takes a BUY the committee
# marked options-suitable at >= this conviction — a HIGHER bar than the
# STRONG_BUY path's min_conviction (70), so only the firm's very best BUYs
# qualify. Tune up to tighten the leveraged book, never below the buy gate (60).
CALLS_BUY_CONVICTION_MIN = 72   # static CEILING/fallback for the options gate
# AUDIT 2026-07-16 F1/#3: the live bar is the rolling p90 of the committee's
# own emitted conviction (sleeves._options_conviction_min), floored here so an
# apathetic regime can never drag the leveraged desk's bar into noise.
# (2026-08-13 operator decision) 65 -> 60: the 65 floor was UNREACHABLE in a
# compressed regime — committee max ran 57-61 for two weeks, zero STRONG_BUYs
# in 357 verdicts, calls_no_candidates x62 — so the floor, not the adaptive
# bar, was what kept the desk inert. 60 remains the absolute noise guard
# (never below the buy gate); the rolling-p90 adaptive bar in
# sleeves._options_conviction_min stays the selectivity mechanism.
CALLS_BUY_CONVICTION_FLOOR = 60
# Fix brief 2026-07-17 #1: option opens are reconciled against decision-time
# options_gate_qualified journal events (same window, per side). Orders BEFORE
# this date predate the qualification journaling and cannot be reconciled —
# never move this backward.
OPTIONS_GATE_RECONCILE_SINCE = "2026-07-20"
# How far back the gate-CONVERSION leg looks for a qualified-but-never-converted
# silent drop (operator 2026-08-19). The BYPASS leg is bounded by the epoch
# above; the conversion leg had no bound at all, so a single silent drop latched
# 'options gate reachability' CRITICAL permanently — PEB (2026-08-04) was fixed
# on 08-13 and the next batch converted 3/3, yet the line failed 53 more sweeps
# and manufactured two fix briefs claiming an "arithmetically unreachable" gate.
# 14 days = long enough that a real regression cannot hide inside it (the desk
# qualifies on the order of once a week), short enough that a remediated defect
# clears. Aged rows survive in gate_conversion.aged_unexplained, never deleted.
OPTIONS_GATE_CONVERSION_WINDOW_DAYS = 14
# Fix brief 2026-07-17 #3: consecutive research cycles the "conviction
# distribution" health check may fail before the failure ESCALATES to a filed
# fix brief (warn-only redlines proved ignorable for 15 days). ~1 week of
# cycles — enough fresh verdicts for the 07-17 component-sum prompt fix to
# prove itself before the next escalation step is demanded.
CONVICTION_SPREAD_ESCALATE_CYCLES = 10
# Fix brief 2026-07-21 #7: buy_skipped_book_full events are reconciled against
# bench waiters (book_full_to_bench) + explicit discard records
# (book_full_discarded). Skips BEFORE this date predate the discard records
# and cannot be reconciled — never move this backward.
BOOK_FULL_COVERAGE_SINCE = "2026-07-22"
# Fix brief 2026-07-21 #8: graded HOLDs below -15% forward excess needed to
# flag sell-path under-utilization when sell-family issuance is ~zero.
SELL_PATH_DEEP_NEG_MIN = 5
# Operator 2026-07-21 ("it was at 8/9, now it restarted at 0/9"): completed
# research workups checkpoint mid-pass and a restarted pass resumes them —
# but only same-ET-day entries younger than this. Older research is re-run;
# a checkpoint must save an interrupted morning, not stand in for research.
RESEARCH_CHECKPOINT_MAX_AGE_H = 6.0
# Operator 2026-07-21 ("swaps need to learn from themselves — they must not
# keep losing money"): while the trailing rank-won seat-switch record is
# LOSING (per learning.seat_switch_scorecard's counterfactual grade), the
# seat governor tightens — challengers need MARGIN_MULT x the lead held
# EXTRA_STREAK additional looks. Self-reversing when the record recovers.
SEAT_BRAKE_MIN_GRADED = 4          # graded flips before the record can brake
SEAT_BRAKE_WIN_RATE = 0.40         # below this win rate (or mean edge < 0) -> brake
SEAT_BRAKE_MARGIN_MULT = 2.0
SEAT_BRAKE_EXTRA_STREAK = 1

# How many names clear the quant screen into full multi-agent research per cycle
MAX_STOCK_CANDIDATES = 6         # used only when FULL_MARKET_SCAN = False
MAX_ETF_CANDIDATES = 3

# ── Sleeves ───────────────────────────────────────────────────────────────────
SLEEVES = {
    "alpha_stocks": {
        "label": "Alpha Stocks",
        "desc": "Committee-sized single-name equity book. Stocks only.",
        "instruments": "stocks",
        "paper_capital": 25_000.0,
        "live_pct_of_bp": 0.25,
        # OPERATOR MANDATE (2026-07-16): "the matter of positions bought doesn't
        # matter — the size of positions is judged by the committee... use all
        # if not most of the cash for what's best, SPY only to park." The name
        # count is NOT the mandate; capital allocation is the committee's job
        # (target_weight_pct + the risk gates: 18% per-name cap, vol scaling,
        # sector/correlation/liquidity/firm caps). This ceiling exists ONLY as
        # a research-bandwidth backstop — every held name is re-underwritten
        # daily and each workup is a multi-minute Opus committee run, so an
        # unbounded book would blow the research deadline. Was 8, which at the
        # committee's 3-4% sizing could never deploy the book (27.5% invested,
        # 64.9% parked on 07-16). routing.parked_capacity still lets even this
        # yield (+4) when the book is name-full but capital-empty.
        "max_positions": 16,
        "max_position_pct": 18.0,
        "min_conviction": 60,
        # 12.0 -> 10.0 (operator 2026-08-19, "use your best bet based on data").
        # scripts/stop_depth_study.py reconstructs the ABSOLUTE adverse-excursion
        # path of every closed equity trade from real daily closes. For THIS
        # sleeve (n=22, the worst book at -8.79 mean SPY-excess and the source of
        # 22 of the fund's 34 reconstructable paths):
        #    depth  fired  mean   delta  winners cut
        #     -8%      7   -7.19  +1.60      0
        #    -10%      5   -7.75  +1.04      0     <- chosen
        #    -12%      4   -8.14  +0.66      0     <- previous value
        #    -15%      2   -8.45  +0.34      0
        # The sleeve's 4 winners had a DEEPEST drawdown of -4.9%, so 10% keeps
        # roughly 2x margin over the deepest recovery on record and cut ZERO
        # winners at every depth tested. -8% scores better still, but the curve
        # is monotone with no interior optimum, which at n=22 means "tighter
        # looks better" is as likely to be sample shape as signal; 10% is the
        # move the data supports without chasing the edge of the curve.
        # NOT applied to core_blend (n=8, +0.32, too small) or etf_rotation
        # (n=4, -1.60 — tightening HURTS the one sleeve beating SPY).
        "hard_stop_pct": 10.0,      # exit at -10% from entry
        "trail_pct": 15.0,          # trail 15% behind peak once green
    },
    "core_blend": {
        "label": "Core Blend",
        "desc": "Core ETFs + satellite conviction stocks.",
        "instruments": "stocks+etfs",
        "paper_capital": 25_000.0,
        "live_pct_of_bp": 0.25,
        "core_weights": {"VOO": 35.0, "QQQ": 15.0, "GLD": 5.0},
        # Satellite capacity is BUDGET-bound, not count-bound (operator
        # 2026-07-17: "let's not cap the core blend too" — the book grows).
        # Satellites may fill everything the mechanical core doesn't claim
        # (100 - 55 = 45% of equity, derived LIVE from the earned core seats).
        # An entry needs at least satellite_min_viable_pct of budget room;
        # past that the swap engine (conviction + hysteresis) rotates the
        # weakest satellite out for a stronger name — the book always has a
        # path to the better idea: cash -> budget room -> else swap.
        "satellite_position_pct": 9.0,
        "satellite_min_viable_pct": 3.0,
        "min_conviction": 55,
        "hard_stop_pct": 15.0,
        "trail_pct": 18.0,
    },
    "calls_only": {
        "label": "Options Desk",
        "desc": "Long calls on top STRONG_BUY names + long puts on top SELL/STRONG_SELL "
                "names — the highest-conviction directional options book.",
        "instruments": "calls",
        "paper_capital": 25_000.0,
        "live_pct_of_bp": 0.25,
        "max_positions": 4,
        # Premium at risk per trade must fit INSIDE the sleeve's 15% kill
        # switch — at the old 25%, one worthless expiry blew straight through
        # the drawdown halt in a single trade (2026-06-10 audit).
        # RAISED 10 -> 15 BY THE OPERATOR, 2026-07-28, to unblock a whole price
        # class: at ~$24.6k sleeve equity the 10% cap was $2,465, i.e. a
        # premium ceiling of $24.65/share, so ANY underlying whose ~2-6% OTM
        # contract cost more was structurally untradeable no matter how good
        # the signal — MU (one put $2,905) was refused `puts_skipped_size_zero`
        # every cycle while being the only name to clear the conviction bar.
        # THE TRADE-OFF, STATED PLAINLY: 15% is exactly AT the 15% kill_dd_pct,
        # not inside it. One position taken at the full cap and expiring
        # worthless is a -15% sleeve drawdown = the halt trips on that single
        # trade. That is a deliberate operator choice, not an oversight; the
        # 2026-06-10 invariant above ("fit INSIDE") now holds only for
        # positions sized below the cap. If this proves too hot, the two knobs
        # are this number and SLEEVES["calls_only"]["kill_dd_pct"] below.
        "max_position_pct": 15.0,   # of sleeve capital per trade
        "min_conviction": 70,
        "otm_pct": 2.0,             # strike ~2% OTM
        "min_dte": 7,
        "max_dte": 28,
        # Layered scaled-exit ladder — proven rules from the market-analysis scalper
        "hard_sl_pct": 2.0,
        "tp1_pct": 5.0, "tp1_sell_pct": 50,
        "tp2_pct": 60.0,
        "tp3_pct": 100.0,
        "trail_after_tp1": 5.0, "trail_after_tp2": 15.0, "trail_after_tp3": 20.0,
        "always_keep_runner": True,
    },
    "etf_rotation": {
        "label": "ETF Rotation",
        "desc": "Dual-momentum sector/asset rotation. ETFs only.",
        "instruments": "etfs",
        "paper_capital": 25_000.0,
        "live_pct_of_bp": 0.25,
        "top_n": 3,
        "rotation_days": 28,        # rotate roughly monthly
        "mom_blend": (0.5, 0.5),    # weights on 3m / 6m momentum
        "hard_stop_pct": 20.0,
    },
}

# ── Monitor cadence ───────────────────────────────────────────────────────────
CALLS_MONITOR_INTERVAL_S = 1.0   # fast tick while option positions are open
CALLS_MONITOR_IDLE_S = 20.0      # re-check cadence when the calls sleeve is flat
EQUITY_MONITOR_INTERVAL_S = 60.0 # stock/ETF sleeves move slower

# Options dead-man's switch — the leveraged book sweeps every 1s, so a hung
# options loop is the most dangerous failure: it gets a MUCH tighter staleness
# threshold than the 20-min equity/cycle watchdog. The calls heartbeat refreshes
# every ~30s, so 180s won't false-trigger on a healthy loop but catches a real
# hang within ~3-4 min (watchdog restart + keepalive relaunch). Equities, which
# sweep every 60s and re-underwrite daily, do not need this urgency.
OPTIONS_MONITOR_STALE_S = 180.0  # >3 min stale w/ open options = restart / health-FAIL
OPTIONS_MONITOR_GRACE_S = 120.0  # don't tight-check until the app has settled 2 min
# Calls-sleeve gap protection (2026-06-14 audit A5): a long call is the most gap-
# sensitive instrument and its SL cannot route after hours, so a print/overnight
# gap opens the next day already through the stop, worthless. Cap the TOTAL open
# call premium held overnight (worst case ~zero) as a % of calls-sleeve equity.
MAX_OVERNIGHT_CALL_PREMIUM_PCT = 25.0

# Coverage-over-time health thresholds (IG 2026-06-14 — rolling 24h journal scan).
# The PRIMARY instability signal is FAILED restarts (keepalive verified_up=False
# = crash loop) and held-position pricing failures. A benign verified-up restart
# happens on every laptop sleep/wake AND every operator deploy, so the total-
# restart gate is only an EXTREME-flap ceiling (an app crash-recovering dozens of
# times), set high so normal deploy/sleep churn never cries wolf — the exact
# market-aware-not-trigger-happy discipline the rest of the health desk follows.
HEALTH_MAX_FAILED_RESTARTS_24H = 3   # crash-loop signal (failed relaunch) — tight
HEALTH_MAX_RESTARTS_24H = 40         # extreme-flap ceiling only; deploys/sleep are benign
HEALTH_MAX_UNPRICEABLE_24H = 15      # held-position pricing failures in a day
HEALTH_MAX_BENCHMARK_MISS_24H = 10   # SPY (the SPY-relative BENCHMARK) mark_missing this
                                     # many times/24h fails 'benchmark data' SEPARATELY —
                                     # a missing index silently skips report-card excess
                                     # grades (IG 2026-07-07 critical); louder than a
                                     # single-name miss because it degrades EVERY grade
HEALTH_COVERAGE_RECOVERY_H = 3.0     # quiet-this-long after a blown budget = recovered (stop the red WARN once the incident is over and just aging out of the 24h window)
HEALTH_STOP_SLIPPAGE_MAX_PCT = 2.0   # avg points an INTRADAY stop may fill past its
                                     # designed distance before 'stop slippage' reds.
                                     # Intraday only, deliberately: the first measured
                                     # cohort ran intraday +1.02 (n=8, worst -0.34) while
                                     # gap_at_open ran -4.24 and extended -9.30 — the deep
                                     # fills are overnight gaps and thin pre/post-market
                                     # prints, which no monitor cadence can catch. Redding
                                     # on those would pin a permanent alarm on the tape
                                     # instead of on anything the desk can fix.
HEALTH_MIN_FREE_DISK_GB = 2.0        # below this, state writes risk silently failing
HEALTH_CRITICAL_RAM_GB = 1.5         # below this free RAM the box is actively starving
                                     # (audit 2026-07-04 F1a: Opus calls time out into
                                     # degraded quant-only mode; the WARN threshold is
                                     # WORKER_RAM_PER_WORKER_GB + WORKER_RAM_HEADROOM_GB)
HEALTH_MAX_RAM_DEFERRALS_24H = 2     # cycle_retry_deferred_ram events in 24h at/above
                                     # this = the retry loop is starved, FAIL loudly
HEALTH_MAX_DEGRADED_SHARE_PCT = 30.0 # a completed cycle whose degraded-fallback share of
                                     # verdicts is at/above this reds the health desk
                                     # (IG 2026-07-03 high: degraded output entered the
                                     # pipeline unalarmed while its share rose with RAM
                                     # starvation; 100% degraded is already caught by
                                     # cycle_retry — this catches the partial flood)
HEALTH_DEGRADED_ACC_GAP_PTS = 15.0   # graded degraded-cohort correct% this many points
                                     # below real-verdict correct% (both n>=15) = the
                                     # fallback is measurably value-destroying, FAIL
HEALTH_RECON_ESCALATE_H = 4.0        # display reconciliation failing this long stops
                                     # being a benign health_warning and raises ONE
                                     # critical alert (IG 2026-07-03 high: 6h of drift
                                     # re-emitted 30-min warnings nobody acted on)
REOPEN_SLA_DAYS = 5                   # a benched HOLD whose catalyst LANDED must be
                                     # re-judged within this many days; a single overdue
                                     # item older than this reds 'reopen SLA' independent
                                     # of the aggregate count (IG 2026-07-06 critical: the
                                     # quant-sorted drain starved the tail 9-11 days). The
                                     # oldest-first drain (watchlist) keeps this satisfied;
                                     # the check catches a regression / a stalled cycle.
HEALTH_MONOCULTURE_CYCLES = 6        # consecutive completed cycles (with verdicts) that
                                     # emit ZERO BUY-side (STRONG_BUY/BUY) verdicts = the
                                     # alpha engine has gone inert as a signal generator
                                     # and cannot clear its own action bar (IG 2026-07-06
                                     # high: 27/27 + 41/41 HOLD while HOLD-rated names ripped)

# ── News reliability & edge auto-management (operator 2026-07-07) ──────────────
# The fund's own report card measured the news tape as net-harmful (blind beat
# trusted/aggregator fwd excess). These gate how much UNRELIABLE news can move a
# decision. firm/news_quality.py scores each already-tagged headline [0..1]; below
# the threshold it is SPECULATIVE (kept as context, flagged so it cannot move
# conviction). And an edge (news tier / factor) whose measured fwd-value is
# inverted past the neutralize floor with enough samples is DISABLED in edge_health
# (the judge is told to IGNORE it), not merely down-weighted — the IG's ratchet.
NEWS_RELIABLE_THRESHOLD = 0.5     # reliability >= this = MATERIAL; below = SPECULATIVE
NEWS_SPECULATION_FACTOR = 0.35    # rumor/hedge grammar multiplies a headline's reliability
NEWS_EDGE_NEUTRALIZE_PTS = -5.0   # a news-tier fwd-excess-vs-blind at/below this ...
NEWS_EDGE_NEUTRALIZE_MIN_N = 20   # ... with at least this many samples = NEUTRALIZE (disable)
EDGE_COLLINEAR_TOL = 1.0          # two edges whose value matches within this AND whose n
EDGE_COLLINEAR_N_TOL = 2          # matches within this = the SAME variable -> de-dup, count once

# ── Risk desk ─────────────────────────────────────────────────────────────────
# Daily circuit breaker scales WITH the portfolio (percentage of day-start
# equity, never a fixed dollar amount) AND with the book's character: an
# options book breathes in multiples of an equity book's daily range. The
# live threshold is also widened up to 1.75x in high-volatility regimes
# (riskbrain.vol_multiplier) — a fast day at a healthy level is not a crisis.
# NOTE: the breaker only blocks NEW entries. Stops/exits always keep working.
DAILY_LOSS_BREAKER_PCT = 2.0     # fallback when a sleeve sets none
SLEEVES["alpha_stocks"]["day_breaker_pct"] = 3.0
SLEEVES["core_blend"]["day_breaker_pct"] = 2.5
SLEEVES["calls_only"]["day_breaker_pct"] = 10.0   # leveraged book: ±10% is a normal day
SLEEVES["etf_rotation"]["day_breaker_pct"] = 2.5
# ETF rotation vs the daily LOSS breaker (operator 2026-07-02, live-journal bug).
# A DUE monthly rotation is a validated mechanical REBALANCE, not a discretionary
# entry: it never raises gross exposure (fully-invested -> fully-invested, or to
# BIL cash) and carries its OWN risk management (the abs-momentum -> BIL cash
# filter + the -20% hard stop). The daily loss breaker exists to stop
# DISCRETIONARY overtrading on a red day; gating the rebalance on it strands the
# sleeve in the exact decayed ETFs it is due to rotate OUT of — and now that
# rotation is CALENDAR-anchored, `_due` stays True all month, so a breaker that
# trips on each single daily cycle freezes the sleeve for the WHOLE month (the
# June->July freeze: SMH/XLK/EEM held into July, last_rotation stuck at Jun 9,
# "rotation deferred" logged every cycle). The 2026-06-10 rationale for the gate
# ("sell winners then be unable to buy targets -> park in cash 28 days") is moot:
# the rotation sells non-targets AND buys targets in ONE pass with no per-order
# breaker re-check, so it never strands cash. When True (default) a DUE rotation
# runs regardless of the loss breaker (a hard sleeve HALT — corruption / kill
# switch — still blocks the buy leg, and the protective abs-momentum exit routes
# even then). Set False to restore the legacy "defer the whole rotation when the
# breaker is tripped" behavior.
ROTATION_IGNORES_LOSS_BREAKER = True
# When a DUE rotation's BUY leg is starved (LIVE sells still settling, or a
# transient no-price blip), the rotation HOLDS last_rotation and retries next
# cycle instead of stranding in cash. Bound that hold: after this many
# consecutive starved cycles the starvation is treated as STRUCTURAL (a
# persistent data outage / genuinely-unaffordable target), and the clock is
# advanced with a ONE-TIME alert rather than freezing + logging every cycle.
ROTATION_MAX_BUY_DEFERS = 3
DRAWDOWN_KILL_PCT = 15.0         # fallback kill switch when a sleeve sets none
MAX_SECTOR_PCT = 35.0            # max single-sector weight in equity sleeves
MIN_CASH_BUFFER = 100.0          # never spend the last $100
MAX_BOOK_CORRELATION = 0.7       # block entries that average above this return
                                 # correlation with the existing book (factor
                                 # concentration disguised as diversification)
MAX_POSITION_OF_ADV_PCT = 0.5    # entry notional <= this % of avg daily $ volume
MAX_FIRM_NAME_PCT = 10.0         # one underlying across ALL sleeves (alpha
                                 # shares + core satellite + calls premium) —
                                 # sleeve caps see one book at a time and let
                                 # a name quietly stack to 3x any single limit
CORE_SEAT_MAX_CORR = 0.85        # a core-seat challenger whose 6-mo daily-return
                                 # correlation with ANY other seated core slot exceeds
                                 # this is excluded from contention (2026-07-26 audit:
                                 # ticker-only dedup seated SOXX 35% + SMH 5% — the same
                                 # semiconductor exposure twice — as the firm's ballast,
                                 # with broad-market weight 0%). Fail-open on data gaps.
# Quant-screen anti-saturation (2026-07-28, operator-found). Each value is the
# raw input that finally earns its leg's FULL point cap under the log-compressive
# transform in data_engine._compress. Before this, the legs used a linear divisor
# with a hard clamp whose pin-points (45% / 30% / 16) sat BELOW the median of the
# already momentum-ranked top-300 they were ranking (55.7% / 38.5% / 33.9), so
# 142 of 300 names scored an identical 93.0 and the entire top 60 was one value.
# The screen picks who reaches the committee, so its top half was ordered by dict
# iteration. Higher full_scale = the screen leans HARDER into extreme tails
# before saturating; lower = it treats "strong" and "extreme" as more alike.
# Which is correct is an open empirical question (21 closed trades), so these are
# bounded levers the Opus review calibrates from forward returns, not constants.
QUANT_FULL_SCALE_RET_6M = 500.0
QUANT_FULL_SCALE_RET_3M = 300.0
QUANT_FULL_SCALE_RS_3M = 300.0
QUANT_COMPRESS_BOUNDS = {
    "QUANT_FULL_SCALE_RET_6M": (100.0, 1500.0),
    "QUANT_FULL_SCALE_RET_3M": (60.0, 1000.0),
    "QUANT_FULL_SCALE_RS_3M": (60.0, 1000.0),
}
QUANT_SATURATION_SHARE = 0.10   # share of the scored universe tied at the ceiling
                                # that counts as a SATURATED scale. Consumed by
                                # watchlist._scale_saturated (the near-miss improve
                                # fallback) and by the health desk's spread check.
                                # A ratio, never a hard-coded 93.0 — the ceiling is
                                # not ours to assume and a rescale must not silently
                                # re-arm an unreachable bar.
PROFIT_RATCHET_MIN_GAIN_PCT = 12.0   # a position must have peaked at least this far in
                                 # profit before the give-back cap engages. Below it the
                                 # wide vol-aware trail governs, so ordinary noise cannot
                                 # shake out a young position.
PROFIT_RATCHET_GIVEBACK_PCT = 35.0   # once engaged, never hand back more than this share
                                 # of the PEAK GAIN. A trail set as a % of PRICE is the
                                 # wrong instrument on a big winner: an 18% trail on a
                                 # +49% peak surrenders 27 points. Measured across every
                                 # winner the fund has closed it kept only 35% of peak
                                 # gain, which is why avg win (+13.1%) and avg loss
                                 # (-12.4%) are nearly symmetric — and a 22% hit rate on a
                                 # 1:1 payoff cannot profit (breakeven needs 3.5:1).
                                 # 35% give-back = ~65% capture, lifting the modelled avg
                                 # win to +22.8%. RAISE it to let winners breathe longer,
                                 # LOWER it to bank more — this is the single most
                                 # return-sensitive knob in the fund.
EXTENDED_HOURS_STOPS = True      # evaluate equity protective stops during the extended
                                 # session (8:30-9:30, 16:00-20:00 ET) against a LIVE
                                 # Public quote, instead of skipping them because the
                                 # book mark is a daily close. Off = the pre-2026-07-26
                                 # behaviour, where 5.5h of every trading day had NO
                                 # stop protection: PRSU exited -28.4% and -29.2% against
                                 # 15% and 12% stops, and those two entries are 57% of
                                 # every dollar the fund has lost. Turn OFF only if
                                 # extended-session fills prove materially worse than the
                                 # gap risk they avoid — that is the real trade-off.
CONVICTION_MIN_SPREAD = 20.0     # BUY-family conviction p90-p10 below this means the
                                 # judge is not ranking its own ideas, and the debate
                                 # prompt injects a corrective calibration note. Was
                                 # hard-coded at 8 — LOOSER than the compression it
                                 # existed to catch (live spread 10 sailed past it while
                                 # the health desk failed the same check 5 cycles
                                 # running). Conviction feeds position sizing AND the
                                 # adaptive p90 calls bar, so a flat signal makes both
                                 # discriminate on noise: last cycle 21 of 31 names died
                                 # on a bar sitting 2 points above the median.
ENFORCE_STOP_COOLOFF_AT_ENTRY = True   # honour the post-stop cool-off + reclaim bar at
                                 # EVERY entry path, not just bench promotion. The
                                 # contract (3 days OR reclaim stop +3%) was written
                                 # into watchlist.py's promotion gate only, and the
                                 # bench entry is CONSUMED the moment the committee
                                 # returns BUY — so a stopped name re-entering via the
                                 # quant scan, or re-underwritten as a held duplicate
                                 # in another sleeve, walked straight back in. Live:
                                 # CON stopped -16.7% on 07-25 and was queued to
                                 # re-buy ~16h later at 31.63 against a 32.56 bar.
STALE_CYCLE_MAX_MIN = 240.0      # a cycle whose wall clock exceeds this has priced its
                                 # decisions against a market that has moved on: the
                                 # sleeves skip trading and the exit sweep, and a retry
                                 # is queued (cycle.py stale-cycle guard). The live
                                 # incident ran 796 min against a 150-min research
                                 # deadline after the host slept mid-cycle, then traded
                                 # at 07:09 ET on a SATURDAY off Friday's closing marks.
                                 # Paired with a session-boundary test; a raw ET-date
                                 # comparison was rejected as too blunt (a Saturday
                                 # evening cycle rolling into Sunday crosses no session).
MIN_VERDICT_COVERAGE = 0.6       # a cycle must produce a REAL committee verdict for
                                 # at least this share of its candidates to count as
                                 # a healthy research pass. Below it, cycle_retry is
                                 # armed and a critical alert fires (cycle.py).
                                 # 2026-07-25 audit: every outage guard was a ZERO
                                 # threshold, so 1 real verdict of 32 candidates read
                                 # as a completed cycle — 13 held positions went
                                 # un-re-underwritten while the health desk stayed
                                 # green. Coverage, not liveness, is the question.
CALLS_MAX_OTM_STRETCH_PCT = 6.0  # options desk: when ONE contract at the desk's
                                 # normal otm_pct costs more than the hard
                                 # per-position cap, retry progressively further
                                 # OTM up to this moneyness for a cheaper strike
                                 # (sleeves.py affordability stretch). Bounded on
                                 # purpose: an unbounded stretch would fit ANY
                                 # premium by turning the firm's top signal into a
                                 # lottery ticket. If the capped stretch still does
                                 # not fit, the name is refused and the journal
                                 # states the per-share premium ceiling — the
                                 # constraint MU hit twice while the desk reported
                                 # only a bare "size zero" (operator 2026-07-25).
                                 # A $25k sleeve at a 10% cap tops out near $25 of
                                 # premium per share; genuinely reaching names like
                                 # MU (~$990) needs a LARGER calls sleeve, not a
                                 # looser cap — 25% was already tried and blew
                                 # through the drawdown halt in one expiry.
PROPAGATE_STOPS_ACROSS_SLEEVES = False
                                 # When one discretionary equity sleeve stops a
                                 # satellite out, ALSO exit the other sleeve's copy
                                 # (monitor._propagate_stop). Built 2026-07-25 after
                                 # the operator saw alpha stop OSCR/CON while core
                                 # kept holding both on its wider 15/18 stops
                                 # ($4,415, 17.9% of core; TXN duplicated 26 days)
                                 # and read it as the stop not being recorded.
                                 #
                                 # DEFAULT OFF, deliberately. The firm already draws
                                 # a coherent line the propagation would erase: a PM
                                 # SELL is a verdict on the NAME and DOES exit every
                                 # sleeve (test_week_sim D3), while a STOP is a
                                 # RISK-TOLERANCE exit and is sleeve-local — core
                                 # runs a longer horizon and wider stops on purpose
                                 # (test_week_sim D2/D3/D4 assert that isolation).
                                 # Turning this on makes the tightest sleeve's stop
                                 # govern every book, which is a real change to the
                                 # firm's risk semantics, not a bug fix.
                                 #
                                 # The genuine anomaly the operator surfaced is not
                                 # the isolation but the DUPLICATION: one satellite
                                 # held in two sleeves stacks single-name exposure
                                 # that per-sleeve caps cannot see (the same reason
                                 # MAX_FIRM_NAME_PCT exists). Fix that at ENTRY
                                 # rather than by overriding a deliberate stop
                                 # design. Flip to True for strict single-owner
                                 # exits; strategic-core and parking slots stay
                                 # exempt either way and etf_rotation is never
                                 # overridden.
MAX_FIRM_ETF_PCT = 25.0          # diversified ETFs are exposure, not single-
                                 # name risk: rotation's validated 33%-of-
                                 # sleeve slugs (~8% of firm) must never be
                                 # strangled by a stock-sized cap, but core +
                                 # rotation + satellites stacking ONE fund
                                 # past a quarter of the firm still gets caught
MAX_FIRM_SECTOR_PCT = 45.0       # (2026-06-14 audit A4) aggregate single-GICS-
                                 # sector exposure across ALL sleeves vs firm
                                 # equity — per-sleeve sector caps let alpha+core+
                                 # calls each be one sector under their own limit
                                 # while the whole firm is one sector bet
FIRM_DAILY_LOSS_BREAKER_PCT = 4.0  # (2026-06-14 audit A1) firm-wide combined day-
                                 # loss ceiling: once the WHOLE book is down this
                                 # % of firm day-start equity (vol-widened), NO
                                 # sleeve opens new entries — exits always work.
                                 # Sized above the largest single-sleeve breaker
                                 # so it catches CORRELATED bleed, not one sleeve.

# LEARNABLE CAPS (2026-06-10 operator goal): the values above are defaults,
# not dogma. The Opus performance review may re-tune them — but ONLY inside
# these hard bounds, and only with CAPS-AUDIT evidence (forward SPY-relative
# outcomes of blocked entries). The learning loop can adapt the firm's risk
# posture toward its best self; it can never break the firm. Effective values
# come from risk.tuned_cap() (state/tuning.json "caps", clamped here).
CAP_BOUNDS = {
    "MAX_FIRM_NAME_PCT": (6.0, 18.0),
    "MAX_FIRM_ETF_PCT": (15.0, 40.0),
    "MAX_SECTOR_PCT": (25.0, 50.0),
    "MAX_FIRM_SECTOR_PCT": (30.0, 60.0),
    "FIRM_DAILY_LOSS_BREAKER_PCT": (3.0, 7.0),
    "MAX_BOOK_CORRELATION": (0.5, 0.85),
    "MAX_ENTRY_DRIFT_PCT": (2.0, 6.0),
}

# Learnable equity stop/trail sigma multipliers (2026-07-13 learning audit):
# the vol-aware stop was a HARDCODED 2.5-sigma / 3.0-sigma with no mechanical
# tuning lever, while the fund already MEASURES premature stops via
# by_exit_reason.avg_post_exit_excess_pct. The review (Job 2g) tunes these from
# that evidence within hard bounds; sleeves._vol_stops live-reads tuning.json
# 'stops'. Bounds keep any single tune small; the 25%/30% hard caps still apply.
# The profit ratchet's two knobs are TUNABLE (operator 2026-07-26, "calibrate
# everything that should be calibrated"). The give-back cap is the single most
# return-sensitive constant in the fund, and the right value is an empirical
# question the ledger answers over time. Whipsaw evidence says raise it, that is
# ratchet exits whose names kept RISING after the exit. Give-back evidence says
# lower it, winners that faded far below their peaks before any exit. The review
# proposes, _apply_cap_tuning clamps into these bounds, risk.tuned_cap serves
# the live value to the monitor. Never tuned outside these rails.
RATCHET_BOUNDS = {
    "PROFIT_RATCHET_GIVEBACK_PCT": (20.0, 60.0),   # default 35.0
    "PROFIT_RATCHET_MIN_GAIN_PCT": (8.0, 20.0),    # default 12.0
}

# Cross-model adversarial refutation for distilled lessons (operator 2026-07-26,
# "a combination of fable 5 and opus 5"). The review's refute-your-own-lessons
# step used to run inside the SAME Opus call that proposed them, a same-model
# echo chamber. A different model family judging the same evidence is a genuine
# independent check. Empty string disables the pass (lessons keep flowing).
REVIEW_REFUTE_MODEL = "claude-fable-5-1"  # operator 2026-09-22: the proposer moved to Opus 5.5, so
                                       # the refuter is Fable 5.1, a different line, keeping the
                                       # independent check. Earlier, 2026-09-01: the proposer (CLAUDE_MODEL) moved to
                                       # Fable 5.1, so the refuter moves to Opus 5 to stay a
                                       # DIFFERENT family (was claude-fable-5 while Opus proposed).

STOP_BOUNDS = {
    "stop_sigma_mult": (2.0, 3.5),    # default 2.5
    "trail_sigma_mult": (2.5, 4.0),   # default 3.0
}

# ABSOLUTE per-position loss floor (fix brief 2026-07-27 #6). The vol-aware
# stop is a RELATIVE construct: stop = max(sleeve base, sigma_mult x daily
# sigma), capped at 25%. On a high-vol small cap that arithmetic legitimately
# lands near the 25% cap, and the realized exits went further still — PRSU
# stopped at -28.4% and, on re-entry, -29.2% against a 2.5-sigma design that
# should have been roughly -8% to -13% on these names (MU -17.6, INNV -13.3,
# DAVE -12.1, OSCR -12.1; -$2,576 across 8 stop exits). The known mechanical
# root causes (no extended-session stop coverage, monitor blackouts) are
# already fixed upstream; this is the LAST-RESORT ceiling that binds no matter
# what the sigma math produces or how wide a gap the mark opens on.
#
# It is a CEILING, never a floor-raiser: the monitor evaluates the per-position
# stop FIRST, so a tighter vol stop (e.g. -12%) still fires at -12% with its own
# label. This can only ever make an exit EARLIER, never later, and it cannot
# widen any existing stop.
# KEPT AT 15.0 (operator 2026-08-19). I briefly set this to 10.0 on a POOLED
# counterfactual over all 35 closed equity trades, and tests/test_week_sim.py
# caught it: "D2 stop isolation: core (-13% < its 15% stop) still holds NVDA"
# went red, because a firm-wide 10% ceiling binds before EVERY sleeve's own stop
# and collapses the deliberate per-sleeve risk differentiation. Re-cutting the
# same counterfactual PER SLEEVE showed the pooled number was hiding opposite
# signs:
#   alpha_stocks n=22  -10%: +1.04 pts, 0 winners cut   (tightening HELPS)
#   core_blend   n= 8  -10%: +0.32 pts                  (too small to move)
#   etf_rotation n= 4  -10%: -1.60 pts                  (tightening HURTS)
# etf_rotation is the purely mechanical sleeve that is BEATING SPY (+1.01), and
# a firm-wide floor would have degraded the one book that works. This constant
# stays what it was designed to be: a LAST-RESORT catastrophe ceiling for the
# vol-aware stop's tail (PRSU -28.4%/-29.2%), not the binding stop for the firm.
# The evidence-backed tightening went where the evidence actually was — see
# SLEEVES["alpha_stocks"]["hard_stop_pct"].
ABS_STOP_FLOOR_PCT = 15.0

# Same bounded-learnable contract as CAP_BOUNDS/RATCHET_BOUNDS: the review may
# move the floor inside these rails and monitor._abs_stop_floor() live-reads the
# clamped value from tuning.json "caps".
# (2026-08-19) The note that used to sit here said the WRITE side still gated
# this key out and called wiring it "a one-line follow-up not owned by this
# change". That follow-up HAS since landed — learning._apply_cap_tuning merges
# ABS_STOP_BOUNDS into its allowed keys — so the lever is connected end to end:
# reader live, writer whitelisted, value clamped into the rails below. Left
# uncorrected, a stale "not wired yet" comment is how a live actuator gets
# mistaken for a dead one and a real tuning result gets dismissed.
ABS_STOP_BOUNDS = {"ABS_STOP_FLOOR_PCT": (10.0, 25.0)}

# ── Options: underlying-referenced invalidation stop (fix brief 2026-07-27 #7) ─
# A percentage-of-PREMIUM stop measures the wrong variable. Premium moves on
# bid/ask width, IV and delta, so a -15% premium stop is reachable on quote
# noise minutes after entry with the thesis fully intact: both calls_only trades
# were HPE and both died on 'hard SL -15.0%'/'-15.4%' at 47 and 19 minutes after
# entry (-$336, -$528; -17.0 and -25.4 SPY-relative). What actually invalidates
# a long call is the UNDERLYING trading through the level the thesis needed.
#
# Stop level = underlying entry price - MULT x its daily sigma (vol_ann/15.9),
# stamped once at entry (broker._book_buy) and evaluated by the monitor against
# a LIVE underlying quote only. The premium SL survives as the CATASTROPHE
# backstop at OPT_CATASTROPHE_SL_PCT.
OPT_UNDERLYING_STOP_SIGMA = 1.5
OPT_UNDERLYING_STOP_SIGMA_BOUNDS = (1.0, 2.5)   # rails; never stamped outside

# Premium loss at which the option exits REGARDLESS of the underlying — a
# defined-risk long option that has lost this much premium is a broken trade
# (IV crush, delta collapse, or the underlying gapped past the stop while the
# quote was unavailable). Deliberately far outside spread/IV noise. Applied as
# max(ladder sl, this) and ONLY for positions that actually carry an armed
# underlying stop: a position with no stamp (legacy row, or a failed vol/quote
# lookup at entry) keeps its ORIGINAL tighter premium SL, because widening a
# stop with nothing to replace it would remove protection outright.
OPT_CATASTROPHE_SL_PCT = 40.0

# Re-entry cooldown for a stopped-out long option (fix brief 2026-07-27 #7):
# both HPE calls were bought back into the same name after the first stop. The
# desk-side gate lives in sleeves.py; broker._book_sell writes the stopped
# underlying + timestamp onto the closed-trade row so the ledger carries the
# evidence the gate reads.
CALLS_REENTRY_COOLDOWN_DAYS = 10

# Volatility REGIME buckets for per-regime stop tuning (2026-07-13, operator:
# "MU drops 5-10% in a day and bounces; COST takes days to reach -5% — the stop
# and its LEARNING should treat those differently"). The stop DISTANCE is already
# each name's own daily-sigma x multiplier, but the MULTIPLE itself can now be
# learned SEPARATELY per regime: a high-vol name that mean-reverts intraday needs
# a wider sigma-multiple to avoid whipsaw than a calm name. Boundaries are on
# ANNUALIZED vol %. The review buckets exit evidence the same way, so it tunes
# each regime from ITS OWN whipsaw data, never one global number for all.
VOL_BUCKETS = (("low", 0.0, 30.0), ("mid", 30.0, 60.0), ("high", 60.0, 1e9))


def vol_bucket(vol_ann) -> str:
    """Classify a name's annualized vol% into a VOL_BUCKETS regime (default 'mid'
    when vol is unknown, so a missing datum never lands in an extreme bucket)."""
    if not isinstance(vol_ann, (int, float)) or vol_ann <= 0:
        return "mid"
    for name, lo, hi in VOL_BUCKETS:
        if lo <= vol_ann < hi:
            return name
    return "high"

# Per-sleeve drawdown kill switches (set per mandate so a normal equity
# correction doesn't permanently halt a long-horizon book, while the
# volatile calls desk stays on a short leash)
SLEEVES["alpha_stocks"]["kill_dd_pct"] = 22.0
SLEEVES["core_blend"]["kill_dd_pct"] = 20.0
SLEEVES["calls_only"]["kill_dd_pct"] = 15.0
SLEEVES["etf_rotation"]["kill_dd_pct"] = 20.0

# Symbols in the shared Public account that this firm does NOT manage (e.g.
# the market-analysis scalper's instruments) — excluded from reconcile drift
RECONCILE_IGNORE_SYMBOLS: list[str] = []

# ── Long-horizon account care ─────────────────────────────────────────────────
REBALANCE_CHECK_DAYS = 7         # firm-level capital rebalance cadence
REBALANCE_BAND = (0.12, 0.40)    # sleeve share of firm equity outside this -> rebalance
HEALTH_INTERVAL_S = 1800         # health desk sweep cadence while app runs
JOURNAL_MAX_BYTES = 3_000_000    # rotate journal.jsonl past this size
JOURNAL_KEEP_LINES = 3000


# Institutional Memory (built at hackUMBC 2026) -------------------------------
# Retrieval over the firm's own graded committee decisions in data/memory.
# The judge prompt gets the most similar past decisions, strictly point in
# time, and cites them as [P#]. Memory informs the committee. It never
# overrules it, because the walk forward test (firm/memory_eval.py) shows a
# mechanical memory veto would not have helped in this sample.
MEMORY_DIR = BASE_DIR / "data" / "memory"
MEMORY_RAG_ENABLED = True
MEMORY_K = 6
MEMORY_MAX_TARGET_WEIGHT_PCT = 18.0   # same single name cap as alpha_stocks
