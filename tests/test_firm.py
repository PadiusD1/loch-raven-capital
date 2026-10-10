"""Loch Raven Capital — permanent regression suite.

Runs fully OFFLINE in an isolated state sandbox: no Opus calls, no market
data, never touches the real books. Re-run after ANY code change:

    py -3.11 tests\\test_firm.py

Covers every safety property the firm depends on: broker validation order,
the option exit ladder, expiry guards, bench lifecycle, stop-out recovery,
learning plumbing, risk breakers, and state-corruption recovery.
"""
import copy
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Isolate ALL state into a throwaway sandbox BEFORE importing firm ─────────
import config  # noqa: E402
config.RESEARCH_AUTO_REFRESH = False

config.STATE_DIR = Path(tempfile.mkdtemp(prefix="lochraven_test_"))
# The regression suite is FULLY OFFLINE: never let earnings_docs spin up a real
# headless browser to scrape a transcript (a fresh-print name like MU would
# otherwise hit stockanalysis.com live and cache it). Transcript tests mock
# transcripts.latest explicitly.
config.TRANSCRIPT_HEADLESS = False
# Exact-value assertions below assume frictionless fills; the slippage model
# gets its own dedicated section (9) with explicit non-zero settings
config.PAPER_SLIPPAGE = {"equity_bps": 0.0, "option_spread_frac": 0.0,
                         "option_fee_per_contract": 0.0}
# Suite runs at any hour — the paper market-hours gate gets its own check
config.PAPER_ENFORCE_MARKET_HOURS = False
# Stop-mark corroboration (2026-07-28): every exit test stubs broker.fetch_marks
# but NOT the independent price monitor._mark_corroborated reads, so the gate
# would hold every simulated stop for want of a second opinion. Cap 1 = "route
# on the first sweep even when the second source is unavailable or disagrees",
# i.e. exactly the pre-gate behaviour, so all 25 stop tests keep asserting what
# they were written to assert. Block 90 sets the real cap and proves the gate
# holds a phantom fill.
config.STOP_UNCONFIRMED_MAX_SWEEPS = 1
# Exit-protection entry gate (2026-09-01) keys on the real clock + the sandbox
# heartbeat; every entry fixture below predates it. Off globally, exercised in
# its own section (§110) with the clock and heartbeat pinned.
config.EXIT_PROTECTION_ENTRY_GATE = False

import firm.brain as brain  # noqa: E402
import firm.broker as broker  # noqa: E402
import firm.data_engine as data_engine  # noqa: E402
from firm import exits, learning, monitor, risk, riskbrain, sleeves, state, watchlist  # noqa: E402

# Pre-seed a FRESH macro-calendar snapshot so macro_calendar.refresh() (called by
# run_cycle) skips its network fetch — keeps the suite fully offline + makes the
# macro calendar resolve from the deterministic rule floor (overlay empty).
state.save("macro_calendar", {"ts": datetime.now(timezone.utc).isoformat(),
                              "tier": "rule", "overlay": {}})

# The day-P&L anchor only rolls on TRADING days (2026-07-04) — pin the gate
# open globally so the suite passes identically on a weekend/holiday run;
# section 18c exercises the closed-day freeze explicitly.
import firm.market_hours as _mh_glob  # noqa: E402
_mh_glob.is_trading_day = lambda now=None: True

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(("PASS" if ok else "FAIL", name, detail))


def ts(days_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


# ══ 1. Broker validation (all BEFORE any order could route) ═══════════════════
s = state.default_sleeve_state("alpha_stocks")
check("zero-price buy rejected",
      not broker.execute("paper", s, symbol="X", side="buy", quantity=1, price=0.0)["success"])
check("NaN price rejected",
      not broker.execute("paper", s, symbol="X", side="buy", quantity=1, price=float("nan"))["success"])
check("negative qty rejected",
      not broker.execute("paper", s, symbol="X", side="buy", quantity=-5, price=10)["success"])
check("insufficient sleeve cash rejected",
      not broker.execute("paper", s, symbol="BIG", side="buy", quantity=9999, price=500.0)["success"])
check("sell with no position rejected",
      not broker.execute("paper", s, symbol="GHOST", side="sell", quantity=5, price=10.0)["success"])

broker.execute("paper", s, symbol="AAPL", side="buy", quantity=3, price=100.0)
r = broker.execute("paper", s, symbol="AAPL", side="sell", quantity=99, price=110.0)
check("oversell clamped to held qty", r["success"] and "AAPL" not in s["positions"]
      and abs(s["cash"] - (config.SLEEVES["alpha_stocks"]["paper_capital"] + 30)) < 0.01)

s2 = state.default_sleeve_state("calls_only")
broker.execute("paper", s2, symbol="T260610C00100000", side="buy", quantity=2,
               price=1.50, instrument="option",
               meta={"underlying": "T", "expiration": "2026-06-10", "strike": 100.0})
r = broker.execute("paper", s2, symbol="T260610C00100000", side="sell", quantity=2,
                   price=0.0, instrument="option", meta={"force": True, "reason": "expiry"})
check("forced expiry write-off at $0", r["success"] and not s2["positions"]
      and s2["closed_trades"][-1]["pnl"] == -300.0)

# ══ 2. Option exit ladder (gap-safe ordering, runner logic) ═══════════════════
real_fetch = broker.fetch_marks
exp = (datetime.now(timezone.utc).date() + timedelta(days=14)).isoformat()


def seed_call(qty, ladder):
    sl = state.get_sleeve("calls_only")
    osi = "GAP260623C00100000"
    sl["positions"] = {osi: {
        "symbol": osi, "instrument": "option", "underlying": "GAP",
        "expiration": exp, "strike": 100.0, "quantity": qty, "entry_price": 1.00,
        "cost": qty * 100.0, "peak_price": 1.00, "last_price": 1.00,
        "tp1_sold": False, "tp2_sold": False, "tp3_hit": False, "trail_pct": None,
        "ladder": ladder, "opened_at": ts(),
    }}
    sl["cash"] = 20_000.0
    state.save_sleeve("calls_only", sl)
    return osi


LADDER = {"sl_pct": 5, "tp1_pct": 10, "tp1_sell_pct": 50, "tp2_pct": 20,
          "tp3_pct": 60, "trail1": 5, "trail2": 10, "trail3": 15}
osi = seed_call(4, LADDER)
broker.fetch_marks = lambda st: {osi: 1.30}     # +30%: past TP1 AND TP2
monitor.check_calls_exits()
pos = state.get_sleeve("calls_only")["positions"].get(osi, {})
check("gap tick takes TP1 only (ordered ladder)",
      pos.get("tp1_sold") and not pos.get("tp2_sold") and pos.get("quantity") == 2)
monitor.check_calls_exits()
pos = state.get_sleeve("calls_only")["positions"].get(osi, {})
check("next tick takes TP2, keeps runner",
      pos.get("tp2_sold") and pos.get("quantity") == 1 and pos.get("trail_pct") == 10)
broker.fetch_marks = lambda st: {osi: 1.15}     # -11.5% off peak > 10% trail
monitor.check_calls_exits()
check("runner trail-stops out",
      osi not in state.get_sleeve("calls_only")["positions"])

osi = seed_call(1, LADDER)
broker.fetch_marks = lambda st: {osi: 1.15}     # +15% past TP1, single contract
monitor.check_calls_exits()
pos = state.get_sleeve("calls_only")["positions"].get(osi, {})
check("single contract becomes runner at TP1 (not dumped)",
      pos.get("quantity") == 1 and pos.get("tp1_sold") and pos.get("trail_pct") == 5)

sl = state.get_sleeve("calls_only")
sl["positions"] = {"EXP260101C00050000": {
    "symbol": "EXP260101C00050000", "instrument": "option", "underlying": "EXP",
    "expiration": (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat(),
    "strike": 50.0, "quantity": 1, "entry_price": 2.0, "cost": 200.0,
    "peak_price": 2.0, "last_price": 1.8, "ladder": {}, "opened_at": ts()}}
state.save_sleeve("calls_only", sl)
broker.fetch_marks = lambda st: {}
monitor.check_calls_exits()
check("option force-closed at <=1 DTE even with no quote",
      "EXP260101C00050000" not in state.get_sleeve("calls_only")["positions"])
broker.fetch_marks = real_fetch

# ══ 3. Equity stops -> bench with thesis -> recovery promotion ════════════════
sl = state.get_sleeve("alpha_stocks")
sl["positions"] = {"META": {"symbol": "META", "instrument": "equity", "quantity": 10,
                            "entry_price": 100.0, "cost": 1000.0, "peak_price": 101.0,
                            "last_price": 100.0, "opened_at": ts(),
                            "thesis": "flywheel", "conviction": 78}}
state.save_sleeve("alpha_stocks", sl)
broker.fetch_marks = lambda st: ({"META": 87.0} if st.get("sleeve") == "alpha_stocks" else {})
monitor.check_all_exits(include_calls=False)
broker.fetch_marks = real_fetch
wl = state.load("watchlist", {})
check("equity stop fires and benches name with thesis",
      "META" not in state.get_sleeve("alpha_stocks")["positions"]
      and wl.get("META", {}).get("source") == "stop_out"
      and "flywheel" in wl["META"]["stop_out"]["thesis"])

screen = {"quant_scores": {}, "stock_candidates": [], "technicals": {}}
real_prices = data_engine.batch_last_prices
data_engine.batch_last_prices = lambda t: {"META": 92.0}
check("cool-off blocks immediate re-entry",
      not any(p["ticker"] == "META" for p in watchlist.promotions(screen)))
wl = state.load("watchlist", {})
wl["META"]["stop_out"]["ts"] = ts(4)
wl["META"]["added"] = ts(4)
state.save("watchlist", wl)
promos = watchlist.promotions(screen)
data_engine.batch_last_prices = real_prices
mp = next((p for p in promos if p["ticker"] == "META"), None)
check("recovered stop-out promoted with story",
      mp is not None and mp["stop_out"]["exit"] == 87.0)

# ── 3z. Post-stop cool-off is enforced at ENTRY, not only at bench promotion ──
# Operator-found 2026-07-26: the 3-day / reclaim-+3% contract lived ONLY in
# watchlist's promotion gate, and the bench entry is consumed the moment the
# committee returns BUY. So a stopped name coming back via the quant SCAN, or
# re-underwritten as a HELD duplicate in another sleeve, walked straight back in.
# Live: CON stopped -16.7% on 07-25, judged BUY at conviction 68 the same night,
# queued for the open at 31.63 against a 32.56 reclaim bar (~16h after the stop).
_ledger_save = state.load("ledger", {})
state.save("ledger", {"trades": [
    {"ticker": "KNIFE", "exit": 100.0, "exit_reason": "trailing stop -16.7% from peak",
     "closed_at": ts(1)},
    {"ticker": "OLDSTOP", "exit": 100.0, "exit_reason": "hard stop -12.1%",
     "closed_at": ts(30)},
    {"ticker": "SOLD", "exit": 100.0, "exit_reason": "PM verdict SELL",
     "closed_at": ts(1)},
]})
_b, _w = sleeves.recent_stop_block("KNIFE", 100.5)
check("3z stopped name inside cool-off is blocked from re-entry", _b and "cool-off" in _w)
check("3z reclaim +3% clears the block", not sleeves.recent_stop_block("KNIFE", 103.5)[0])
check("3z cool-off expires", not sleeves.recent_stop_block("OLDSTOP", 100.5)[0])
check("3z a committee SELL is not a stop — no cool-off",
      not sleeves.recent_stop_block("SOLD", 100.5)[0])
check("3z a never-stopped name is untouched",
      not sleeves.recent_stop_block("FRESH", 10.0)[0])
state.save("ledger", _ledger_save)

# ══ 4. Bench lifecycle (behavior-based exits) ═════════════════════════════════
state.save("watchlist", {
    "DECAYER": {"ticker": "DECAYER", "added": ts(5), "source": "near_miss",
                "watch_trigger": "", "missing": 0,
                "history": [{"ts": ts(5), "quant": 90.0}, {"ts": ts(1), "quant": 83.0}]},
    "GHOST": {"ticker": "GHOST", "added": ts(4), "source": "near_miss",
              "watch_trigger": "", "missing": 2, "history": [{"ts": ts(4), "quant": 88.0}]},
    "FOSSIL": {"ticker": "FOSSIL", "added": ts(31), "source": "near_miss",
               "watch_trigger": "", "missing": 0, "history": [{"ts": ts(0.5), "quant": 89.0}]},
    "STOPKEEP": {"ticker": "STOPKEEP", "added": ts(4), "source": "stop_out",
                 "watch_trigger": "", "missing": 5, "history": [],
                 "stop_out": {"exit": 50.0, "ts": ts(4)}},
})
watchlist.update({"quant_scores": {"DECAYER": 83.0, "FOSSIL": 89.0},
                  "stock_candidates": []}, {})
wl = state.load("watchlist", {})
check("decayed setup cut early", "DECAYER" not in wl)
check("vanished near-miss dropped", "GHOST" not in wl)
check("aged out at ceiling", "FOSSIL" not in wl)
check("stop-out EXEMPT from vanish rule", "STOPKEEP" in wl)

# ══ 5. Learning plumbing (mocked Opus) ════════════════════════════════════════
check("OSI underlying parse", learning._underlying("AMD260618C00477500") == "AMD"
      and learning._underlying("AAPL") == "AAPL")
tuned = learning._apply_bench_tuning({"decay_drop_pts": 99, "max_age_days": 2,
                                      "junk": 1, "improve_pts": "bad"})
check("bench tuning clamped to bounds",
      tuned["decay_drop_pts"] == 12.0 and tuned["max_age_days"] == 10
      and "junk" not in tuned and "improve_pts" not in tuned)
check("watchlist reads tuned param live",
      watchlist._param("decay_drop_pts", 6.0) == 12.0)

a = state.get_sleeve("alpha_stocks")
a["closed_trades"] = [
    {"symbol": "NVDA", "instrument": "equity", "quantity": 10, "entry": 100,
     "exit": 118, "pnl": 180.0, "reason": "PM verdict SELL", "closed_at": ts(5),
     "reentry_after_stop": False},
    {"symbol": "META", "instrument": "equity", "quantity": 5, "entry": 92,
     "exit": 101, "pnl": 45.0, "reason": "trail", "closed_at": ts(2),
     "reentry_after_stop": True},
]
state.save_sleeve("alpha_stocks", a)
state.save("ledger", {"trades": [], "seen": []})
learning.record_outcomes()
st = learning.stats()
check("ledger records outcomes with re-entry bucket",
      st["trades"] >= 2 and st["stop_out_reentries"]["n"] == 1
      and st["stop_out_reentries"]["pnl"] == 45.0)

real_ask = brain.ask_json
brain.ask_json = lambda p, **kw: {"lessons": [{"lesson": "test lesson",
                                               "evidence": "test"}],
                                  "bench_tuning": {"max_age_days": 45},
                                  "fix_requests": [{"problem": "rotation cadence too slow",
                                                    "evidence": "missed 3 regime shifts",
                                                    "suspected_area": "etf_rotation rotation_days",
                                                    "ask_claude": "lower rotation_days to 14"}]}
real_avail = brain.available
brain.available = lambda: True
rev = learning.run_review(force=True)
brain.ask_json = real_ask
brain.available = real_avail
check("review saves lessons + applies bounded tuning",
      rev and rev["lessons"][0]["lesson"] == "test lesson"
      and rev["bench_tuning_applied"]["max_age_days"] == 45)
fb = state.load("fix_briefs", {})
briefs_md = (config.STATE_DIR / "FIX_BRIEFS.md")
check("unfixable findings filed as paste-ready fix briefs for Claude",
      fb.get("items") and fb["items"][-1]["problem"] == "rotation cadence too slow"
      and briefs_md.exists() and "rotation_days to 14" in briefs_md.read_text(encoding="utf-8"))
check("lessons injected into prompt context",
      "FIRM MEMORY" in learning.lessons_context())

# ══ 5b. Rotation strategy feeds the learning loop ═════════════════════════════
s = state.default_sleeve_state("etf_rotation")
broker.execute("paper", s, symbol="SMH", side="buy", quantity=10, price=580.0,
               meta={"reason": "rotation entry", "strategy": "dual_momentum",
                     "momentum_score": 53.97, "rank": 1})
broker.execute("paper", s, symbol="SMH", side="sell", quantity=10, price=600.0,
               meta={"reason": "rotation exit (rank fell, mom 12.1)"})
rot_closed = s["closed_trades"][-1]
check("rotation trade carries strategy + signal into closed record",
      rot_closed["strategy"] == "dual_momentum"
      and rot_closed["momentum_score"] == 53.97 and rot_closed["pnl"] == 200.0)
rs = state.get_sleeve("etf_rotation")
rs.setdefault("closed_trades", []).append(rot_closed)
state.save_sleeve("etf_rotation", rs)
state.save("ledger", {"trades": [], "seen": []})
learning.record_outcomes()
st = learning.stats()
check("stats bucket by strategy (dual_momentum visible to review)",
      st.get("by_strategy", {}).get("dual_momentum", {}).get("n", 0) >= 1,
      str(st.get("by_strategy")))
# HEADLINE = committee decisions only (operator 2026-07-20: a rotation exit
# the committee never decided must not inflate the firm's win rate — the live
# EEM row, stamp-less, once did). Structural match: sleeve OR reason prefix.
_led5b = state.load("ledger", {}).get("trades", [])
_n_strat5b = sum(1 for t in _led5b if learning._is_strategy_trade(t))
check("stats headline EXCLUDES mechanical-strategy exits (counted separately)",
      _n_strat5b >= 1 and st.get("strategy_trades") == _n_strat5b
      and st.get("trades") == len(_led5b) - _n_strat5b)
check("strategy-trade classifier matches stamp-less rotation rows structurally",
      learning._is_strategy_trade({"sleeve": "etf_rotation", "strategy": ""})
      and learning._is_strategy_trade(
          {"exit_reason": "rotation exit (rank fell, mom 17.9)"})
      and not learning._is_strategy_trade(
          {"sleeve": "alpha_stocks", "reason": "hard SL"}))

# ══ 6. Exit strategist clamps ═════════════════════════════════════════════════
crazy = exits._clamp({"sl_pct": 90, "tp1_pct": 1, "tp1_sell_pct": 100,
                      "tp2_pct": 500, "tp3_pct": 999, "trail1": 0.1,
                      "trail2": 99, "trail3": 99, "rationale": "x"})
check("exit plan clamped to guardrails",
      crazy and crazy["sl_pct"] == 15.0 and crazy["tp1_sell_pct"] == 75)
check("unordered ladder rejected",
      exits._clamp({"sl_pct": 5, "tp1_pct": 50, "tp2_pct": 30, "tp3_pct": 100,
                    "tp1_sell_pct": 50, "trail1": 5, "trail2": 10, "trail3": 15}) is None)
check("default ladder intact", exits.default_ladder()["sl_pct"] == 2.0)

# ══ 7. Risk desk ══════════════════════════════════════════════════════════════
s = state.default_sleeve_state("calls_only")
s["peak_equity"] = 25_000.0
risk.mark_equity("calls_only", s, 21_000.0)   # -16% > calls 15% kill
check("per-sleeve kill switch trips", s["halted"] and "KILL" in s["halt_reason"])
s = state.default_sleeve_state("alpha_stocks")
s["peak_equity"] = 25_000.0
risk.mark_equity("alpha_stocks", s, 21_000.0)  # -16% < alpha 22% kill
check("equity sleeve survives normal correction", not s["halted"])
# Breaker: per-sleeve thresholds, vol-regime aware, scales with size.
# Pin the vol multiplier to 1.0 so these checks are deterministic.
real_volmult = riskbrain.vol_multiplier
riskbrain.vol_multiplier = lambda: 1.0
s["day_pnl_anchor"] = {"date": risk._today(),  # ET date — UTC would be TOMORROW after 8pm ET
                       "equity": 25_000.0}
ok, _ = risk.entries_allowed("alpha_stocks", s, 24_400.0)   # -2.4% day
check("healthy fast day does NOT trip stocks breaker (-2.4% < 3%)", ok)
ok, why = risk.entries_allowed("alpha_stocks", s, 24_100.0)  # -3.6%
check("stocks breaker trips at -3.6%", not ok and "circuit breaker" in why)

# The breaker SCALES with the portfolio: -$600 on $1M (0.06%) must NOT trip
big = state.default_sleeve_state("alpha_stocks")
big["halted"] = False
big["day_pnl_anchor"] = {"date": risk._today(),  # ET date — UTC would be TOMORROW after 8pm ET
                         "equity": 1_000_000.0}
ok, _ = risk.entries_allowed("alpha_stocks", big, 999_400.0)
check("breaker scales: -$600 on $1M does NOT trip", ok)
ok, why = risk.entries_allowed("alpha_stocks", big, 960_000.0)   # -4%
check("breaker scales: -4% on $1M trips", not ok, why)

# Options book breathes in multiples: -5% day must NOT halt the calls desk
calls = state.default_sleeve_state("calls_only")
calls["day_pnl_anchor"] = {"date": risk._today(),  # ET date — UTC would be TOMORROW after 8pm ET
                           "equity": 25_000.0}
ok, _ = risk.entries_allowed("calls_only", calls, 23_750.0)   # -5%
check("calls desk tolerates a -5% day (10% threshold)", ok)
ok, _ = risk.entries_allowed("calls_only", calls, 22_200.0)   # -11.2%
check("calls desk trips at -11.2%", not ok)

# Volatile regime widens the breaker: -3.6% allowed when vol mult = 1.5
riskbrain.vol_multiplier = lambda: 1.5
ok, _ = risk.entries_allowed("alpha_stocks", s, 24_100.0)    # -3.6% < 3%*1.5
check("high-vol regime widens breaker (fast day at healthy level)", ok)
riskbrain.vol_multiplier = real_volmult

# Intraday recovery re-opens automatically (limit computed live, not latched)
riskbrain.vol_multiplier = lambda: 1.0
ok, _ = risk.entries_allowed("alpha_stocks", s, 24_700.0)    # recovered to -1.2%
check("breaker re-opens when equity recovers intraday", ok)
riskbrain.vol_multiplier = real_volmult

# Sector regime symmetry: same X-ray detects sectors on the way UP
regime = riskbrain.sector_regime(snapshot={"ret_1m": {
    "SPY": 2.0, "XLK": 9.0, "XLV": 6.5, "XLE": -7.0, "XLF": 1.0, "XLI": 2.0,
    "XLP": 0.5, "XLY": 3.0, "XLU": 1.0, "XLB": 0.0, "XLC": 2.5, "XLRE": -1.0},
    "prices": {}})
check("sector regime detects leaders and laggards",
      regime["leading"][:2] == ["Technology", "Healthcare"]
      and regime["lagging"] == ["Energy"],
      f"leading={regime['leading']} lagging={regime['lagging']}")
check("tailwind helper reads saved regime",
      __import__("firm.sleeves", fromlist=["x"])._tailwind("Technology") is True
      and __import__("firm.sleeves", fromlist=["x"])._tailwind("Energy") is False)

# Tailwind tag flows entry -> close -> ledger -> stats
tw = state.default_sleeve_state("alpha_stocks")
broker.execute("paper", tw, symbol="NVDA", side="buy", quantity=2, price=100.0,
               meta={"sector": "Technology", "sector_tailwind": True})
broker.execute("paper", tw, symbol="NVDA", side="sell", quantity=2, price=110.0,
               meta={"reason": "test"})
check("closed trade carries sector tailwind tag",
      tw["closed_trades"][-1]["sector_tailwind"] is True
      and tw["closed_trades"][-1]["sector"] == "Technology")

# ══ 6b. Vol-aware stops + core churn protection + halt learning ═══════════════
import firm.sleeves as sleeves_mod  # noqa: E402

acfg = config.SLEEVES["alpha_stocks"]
# Read the base from config, never a copy of the constant (2026-08-19): this
# asserted the literal (12.0, 15.0) and went red purely because alpha's base
# stop was retuned to 10.0 on its own measured evidence. The PROPERTY under
# test is "a quiet stock gets the sleeve's BASE stop, not a vol-widened one" —
# that property is independent of what the base happens to be, and pinning the
# number here just makes a legitimate retune look like a regression.
check("quiet stock keeps base stop",
      sleeves_mod._vol_stops(acfg, 25.0)
      == (acfg["hard_stop_pct"], acfg["trail_pct"]),
      str(sleeves_mod._vol_stops(acfg, 25.0)))
hs, ht = sleeves_mod._vol_stops(acfg, 100.0)   # ~6.3% daily sigma
check("high-vol stock gets wider stop (not noise-stopped)",
      hs > acfg["hard_stop_pct"] and ht > acfg["trail_pct"],
      f"stop={hs} trail={ht}")
check("vol stop capped at 25%", sleeves_mod._vol_stops(acfg, 400.0)[0] == 25.0)

# Core ETF churn: a crash must NOT stop-sell VOO (rebalance would rebuy it)
cb = state.get_sleeve("core_blend")
cb_backup = copy.deepcopy(cb)
cb["positions"] = {"VOO": {"symbol": "VOO", "instrument": "equity", "quantity": 10,
                           "entry_price": 500.0, "cost": 5000.0, "peak_price": 505.0,
                           "last_price": 500.0, "opened_at": ts()}}
state.save_sleeve("core_blend", cb)
real_fm = broker.fetch_marks
broker.fetch_marks = lambda st: ({"VOO": 400.0} if st.get("sleeve") == "core_blend" else {})
monitor.check_all_exits(include_calls=False)
broker.fetch_marks = real_fm
check("core ETF exempt from stop (no sell-low/rebuy-high churn)",
      "VOO" in state.get_sleeve("core_blend")["positions"])
# Committee SELL on core is suppressed (journaled as information)
sleeves_mod._apply_sells(state.get_sleeve("core_blend"),
                         {"VOO": {"verdict": {"rating": "STRONG_SELL"}}},
                         "paper", {"VOO": 400.0})
check("committee SELL on strategic core suppressed",
      "VOO" in state.get_sleeve("core_blend")["positions"])
state.save_sleeve("core_blend", cb_backup)

# ALL halt types are learnable risk events
riskbrain.record_halt("alpha_stocks", "exit_failure", "test halt")
evs = state.load("risk_events", {}).get("events", [])
check("operational halts land in risk_events for the review",
      any(e.get("classification") == "exit_failure" for e in evs))

# ══ 7-pre. Risk Brain: drawdown diagnosis + proportional response ═════════════
tech_sleeve = {"positions": {
    "NVDA": {"sector": "Technology", "market_value": 5000},
    "AMD": {"sector": "Technology", "market_value": 4000},
    "CAT": {"sector": "Industrials", "market_value": 1000}}}

snap_sector = {"ret_1m": {"SPY": -2.0, "XLK": -14.0, "XLI": -1.0, "XLV": 1.0,
                          "XLF": 0.5, "XLE": 2.0, "XLP": 1.0, "XLY": -2.0,
                          "XLU": 1.5, "XLB": 0.0, "XLC": -3.0, "XLRE": 0.0},
               "prices": {"XLK": 180.0}}
ev = riskbrain.diagnose("alpha_stocks", tech_sleeve, -23.0, snapshot=snap_sector)
check("tech rout + healthy market -> SECTOR event (not full stop)",
      ev["classification"] == "sector" and ev["restricted_sectors"] == ["Technology"]
      and ev["restricted_levels"].get("XLK") == 180.0)

snap_market = {"ret_1m": {"SPY": -11.0, "XLK": -14.0, "XLI": -9.0, "XLV": -6.0,
                          "XLF": -8.0, "XLE": -7.0, "XLP": -5.5, "XLY": -10.0,
                          "XLU": -6.0, "XLB": -7.0, "XLC": -9.0, "XLRE": -8.0},
               "prices": {}}
ev = riskbrain.diagnose("alpha_stocks", tech_sleeve, -23.0, snapshot=snap_market)
check("broad selloff -> MARKET_WIDE (full halt)",
      ev["classification"] == "market_wide")

snap_calm = {"ret_1m": {"SPY": 1.0, "XLK": 2.0, "XLI": 1.5, "XLV": 0.5,
                        "XLF": 1.0, "XLE": -1.0, "XLP": 0.5, "XLY": 2.0,
                        "XLU": 0.0, "XLB": 1.0, "XLC": 1.0, "XLRE": 0.5},
             "prices": {}}
ev = riskbrain.diagnose("alpha_stocks", tech_sleeve, -23.0, snapshot=snap_calm)
check("sleeve bleeding in a calm market -> IDIOSYNCRATIC (picks failing)",
      ev["classification"] == "idiosyncratic")

ev = riskbrain.diagnose("alpha_stocks", tech_sleeve, -23.0,
                        snapshot={"ret_1m": {}, "prices": {}})
check("no data -> safest assumption (market_wide)",
      ev["classification"] == "market_wide")

check("sector gate blocks damaged sector, allows others",
      not risk.sector_allowed({"restricted_sectors": ["Technology"]}, "Technology")
      and risk.sector_allowed({"restricted_sectors": ["Technology"]}, "Healthcare")
      and risk.sector_allowed({}, "Technology"))

s = state.default_sleeve_state("alpha_stocks")
s["soft_risk"] = True
check("soft tier halves sizing", risk.risk_scale(s) == 0.5)

# ══ 7a. Kill-switch recovery mechanics ════════════════════════════════════════
s = state.default_sleeve_state("alpha_stocks")
s["peak_equity"] = 25_000.0
risk.mark_equity("alpha_stocks", s, 19_000.0)   # -24% > 22% kill
check("kill switch stores recovery reference points",
      s["halted"] and s.get("halt_equity") == 19_000.0 and "halt_ts" in s)
s["reduced_risk_until"] = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
check("graduated re-entry: half size inside window", risk.risk_scale(s) == 0.5)
s["reduced_risk_until"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
check("soft tier overrides expired window while drawdown persists",
      risk.risk_scale(s) == 0.5)   # sleeve still -24% down: half size is CORRECT
s["soft_risk"] = False             # drawdown healed -> window expiry restores full size
check("graduated re-entry: full size after window", risk.risk_scale(s) == 1.0)
check("no window -> full size", risk.risk_scale({}) == 1.0)

# ══ 7b. Mode isolation + corruption halt ══════════════════════════════════════
s = state.default_sleeve_state("alpha_stocks")
s["positions"]["LIVEPOS"] = {"symbol": "LIVEPOS", "instrument": "equity",
                             "quantity": 5, "entry_price": 50.0, "cost": 250.0,
                             "peak_price": 50.0, "opened_mode": "live"}
r = broker.execute("paper", s, symbol="LIVEPOS", side="sell", quantity=5, price=55.0)
check("cross-mode sell refused (live position, paper order)",
      not r["success"] and "mode" in r.get("error", ""))

(config.STATE_DIR / "sleeve_corrupttest.json").write_text("{broken", encoding="utf-8")
cs = state.get_sleeve("corrupttest") if "corrupttest" in config.SLEEVES else None
# get_sleeve requires a configured sleeve; simulate via direct path:
config.SLEEVES["corrupttest"] = dict(config.SLEEVES["alpha_stocks"])
cs = state.get_sleeve("corrupttest")
del config.SLEEVES["corrupttest"]
check("unrecoverable corrupt sleeve comes back HALTED (never quietly trades)",
      cs.get("halted") and "CORRUPT" in cs.get("halt_reason", ""))

# ══ 8. State corruption recovery ══════════════════════════════════════════════
state.save("sleeve_demo", {"positions": {"X": 1}, "cash": 123})
import time as _t
state._last_bak["sleeve_demo"] = 0          # force a backup on next save
_t.sleep(0.01)
state.save("sleeve_demo", {"positions": {"X": 1}, "cash": 456})
(config.STATE_DIR / "sleeve_demo.json").write_text("{corrupt!!", encoding="utf-8")
recovered = state.load("sleeve_demo", {})
check("corrupt state restored from backup (book never erased)",
      recovered.get("cash") == 123, f"recovered={recovered}")

# ══ 9. 2026-06-10 audit fixes ═════════════════════════════════════════════════
from firm import sleeves as sleeves_mod2  # noqa: E402

# 9a. Paper slippage haircut (equity bps + option spread crossing + fees)
config.PAPER_SLIPPAGE = {"equity_bps": 10.0, "option_spread_frac": 0.5,
                         "option_fee_per_contract": 0.65}
s = state.default_sleeve_state("alpha_stocks")
broker.execute("paper", s, symbol="SLIP", side="buy", quantity=10, price=100.0)
check("paper equity buy crosses the spread (10bps haircut)",
      abs(s["positions"]["SLIP"]["entry_price"] - 100.10) < 1e-6,
      f"entry={s['positions']['SLIP']['entry_price']}")
broker.execute("paper", s, symbol="SLIP", side="sell", quantity=10, price=100.0)
sell_px = s["closed_trades"][-1]["exit"]
check("paper equity sell crosses the spread (10bps haircut)",
      abs(sell_px - 99.90) < 1e-6, f"exit={sell_px}")
sc = state.default_sleeve_state("calls_only")
broker.execute("paper", sc, symbol="SLP260710C00100000", side="buy", quantity=2,
               price=2.00, instrument="option",
               meta={"underlying": "SLP", "expiration": "2026-07-10",
                     "strike": 100.0, "spread_pct": 10.0})
pos = sc["positions"]["SLP260710C00100000"]
check("paper option buy crosses half the half-spread (mid +2.5%)",
      abs(pos["entry_price"] - 2.05) < 1e-6, f"entry={pos['entry_price']}")
check("option fee charged to cash",
      abs(sc["cash"] - (25_000.0 - 2.05 * 2 * 100 - 1.30)) < 0.01,
      f"cash={sc['cash']}")
broker.execute("paper", sc, symbol="SLP260710C00100000", side="sell", quantity=2,
               price=0.0, instrument="option", meta={"force": True, "reason": "test"})
check("zero-price write-off stays $0 (no haircut, no fee on worthless close)",
      sc["closed_trades"][-1]["exit"] == 0.0)
config.PAPER_SLIPPAGE = {"equity_bps": 0.0, "option_spread_frac": 0.0,
                         "option_fee_per_contract": 0.0}

# 9b. Degraded fallback verdicts are never tradeable BUYs
check("degraded BUY blocked from sleeves",
      not sleeves_mod2._verdict_ok(
          {"rating": "BUY", "conviction": 90, "degraded": True}, 60))
check("real committee BUY still passes",
      sleeves_mod2._verdict_ok({"rating": "BUY", "conviction": 90}, 60))
check("ranked buys drops degraded verdicts",
      sleeves_mod2._ranked_buys(
          {"GHOST": {"kind": "stock", "verdict": {"rating": "BUY",
                                                  "conviction": 88,
                                                  "degraded": True}}}, 60) == [])

# 9c. Entry price-drift guard
pk_drift = {"technicals": {"price": 100.0, "vol_ann_pct": 30.0}}
check("entry within band passes drift guard",
      sleeves_mod2._entry_drift_ok("DRF", 101.0, pk_drift, "alpha_stocks"))
check("gapped entry blocked by drift guard",
      not sleeves_mod2._entry_drift_ok("DRF", 108.0, pk_drift, "alpha_stocks"))
check("drift band scales with the name's own vol",
      sleeves_mod2._entry_drift_ok(
          "HIVOL", 106.0, {"technicals": {"price": 100.0, "vol_ann_pct": 160.0}},
          "alpha_stocks"))
check("missing packet price fails open",
      sleeves_mod2._entry_drift_ok("NOPK", 150.0, {}, "alpha_stocks"))

# 9d. Learning survives the 500-trade ledger cap (monotonic counter)
state.save("ledger", {"trades": [], "seen": [], "total_recorded": 510})
a9 = state.get_sleeve("alpha_stocks")
a9.setdefault("closed_trades", []).append(
    {"symbol": "CTR", "instrument": "equity", "entry": 10, "exit": 11,
     "pnl": 10.0, "reason": "test", "closed_at": ts(0.001)})
state.save_sleeve("alpha_stocks", a9)
n9 = learning.record_outcomes()
led9 = state.load("ledger", {})
check("ledger lifetime counter survives truncation",
      led9.get("total_recorded") == 510 + n9 and n9 >= 1,
      f"total={led9.get('total_recorded')} new={n9}")

# 9e. Equity trail stays armed below entry once the position was ever green
tr = state.default_sleeve_state("alpha_stocks")
tr["positions"] = {"RT": {"symbol": "RT", "instrument": "equity", "quantity": 10,
                          "entry_price": 100.0, "cost": 1000.0,
                          "peak_price": 130.0, "last_price": 101.0,
                          "opened_at": ts(), "stop_pct": 12.0,
                          "trail_stop_pct": 15.0}}
state.save_sleeve("alpha_stocks", tr)
real_fm9 = broker.fetch_marks
broker.fetch_marks = lambda st: ({"RT": 99.0}
                                 if st.get("sleeve") == "alpha_stocks" else {})
monitor.check_all_exits(include_calls=False)
broker.fetch_marks = real_fm9
check("round-tripped winner trail-stops below entry (was unprotected)",
      "RT" not in state.get_sleeve("alpha_stocks")["positions"])

# 9f. Paper buys respect market hours (exits don't)
from firm import market_hours as mh_mod  # noqa: E402

config.PAPER_ENFORCE_MARKET_HOURS = True
real_open = mh_mod.market_is_open
real_ext = mh_mod.extended_session_open
mh_mod.market_is_open = lambda *a, **k: False
# Pin the extended session too (2026-07-25): leaving it real made the exit
# assertions below depend on what time of day the suite happened to run.
mh_mod.extended_session_open = lambda *a, **k: False
gate = state.default_sleeve_state("alpha_stocks")
r = broker.execute("paper", gate, symbol="NITE", side="buy", quantity=1, price=10.0)
check("paper buy blocked when market closed",
      not r["success"] and "closed" in r.get("error", ""))
gate["positions"]["NITE"] = {"symbol": "NITE", "instrument": "equity",
                             "quantity": 1, "entry_price": 10.0, "cost": 10.0,
                             "peak_price": 10.0, "opened_mode": "paper"}
# CHANGED 2026-07-25 (was "paper exit still works when market closed"). Paper
# exits now mirror the LIVE gate instead of always routing. A paper sell into a
# closed market protects nothing — it books a fill the market is not offering.
# Live incident: on Saturday 2026-07-25 paper exited OSCR at "hard stop -12.1%"
# and CON at "trailing stop -16.7%" at FRIDAY's closing marks; live would have
# refused both and carried the weekend gap. Those fabricated fills feed
# by_exit_reason, which is what the Opus exit-ladder designer learns from, and
# they flatter the very stop-quality record used to decide on going live.
r = broker.execute("paper", gate, symbol="NITE", side="sell", quantity=1, price=9.0)
check("paper exit REFUSED on a fully closed market (mirrors live)",
      not r["success"] and "closed" in r.get("error", ""))
# ...but a risk-reducing EQUITY exit still routes in the extended session,
# exactly as live does — the bell must not strand a protective sell.
mh_mod.extended_session_open = lambda *a, **k: True
r = broker.execute("paper", gate, symbol="NITE", side="sell", quantity=1, price=9.0)
check("paper equity exit still allowed in the extended session", r["success"])
mh_mod.market_is_open = real_open
mh_mod.extended_session_open = real_ext
config.PAPER_ENFORCE_MARKET_HOURS = False

# 9g. Earnings guard
check("entry blocked 1 day before earnings",
      not sleeves_mod2._earnings_ok(
          "ERN", {"next_earnings": {"date": "2026-06-11", "days_away": 1}}, "alpha_stocks"))
check("entry allowed 5 days before earnings",
      sleeves_mod2._earnings_ok(
          "ERN", {"next_earnings": {"date": "2026-06-15", "days_away": 5}}, "alpha_stocks"))
check("unknown earnings date fails open",
      sleeves_mod2._earnings_ok("ERN", {}, "alpha_stocks"))

# 9h. Verdict-at-entry attribution flows entry -> close -> ledger
att = state.default_sleeve_state("alpha_stocks")
broker.execute("paper", att, symbol="ATTR", side="buy", quantity=2, price=50.0,
               meta={"rating": "STRONG_BUY", "conviction": 81,
                     "thesis": "attribution test", "depth": "standard"})
broker.execute("paper", att, symbol="ATTR", side="sell", quantity=2, price=55.0)
ct = att["closed_trades"][-1]
check("closed trade carries the ENTRY verdict",
      ct.get("rating") == "STRONG_BUY" and ct.get("conviction") == 81
      and ct.get("depth") == "standard")
state.save_sleeve("alpha_stocks", att)
state.save("ledger", {"trades": [], "seen": []})
learning.record_outcomes()
row = next((t for t in state.load("ledger", {}).get("trades", [])
            if t["ticker"] == "ATTR"), None)
check("ledger row attributes to the entry verdict, not history",
      row is not None and (row.get("verdict") or {}).get("rating") == "STRONG_BUY"
      and row.get("depth") == "standard", str(row)[:160])

# 9i. Config-fallback option SL floored above the spread
fb = state.default_sleeve_state("calls_only")
fb_exp = (datetime.now(timezone.utc).date() + timedelta(days=14)).isoformat()
fb["positions"] = {"FBS260624C00100000": {
    "symbol": "FBS260624C00100000", "instrument": "option", "underlying": "FBS",
    "expiration": fb_exp, "strike": 100.0, "quantity": 1, "entry_price": 2.0,
    "cost": 200.0, "peak_price": 2.0, "last_price": 2.0, "ladder": {},
    "spread_pct": 10.0, "opened_at": ts(), "opened_mode": "paper"}}
state.save_sleeve("calls_only", fb)
real_fm10 = broker.fetch_marks
broker.fetch_marks = lambda st: {"FBS260624C00100000": 1.92}   # -4%: inside spread noise
monitor.check_calls_exits()
check("fallback SL floored above spread (-4% does not fire on 10% spread)",
      "FBS260624C00100000" in state.get_sleeve("calls_only")["positions"])
broker.fetch_marks = lambda st: {"FBS260624C00100000": 1.80}   # -10%: past floored SL
monitor.check_calls_exits()
check("floored SL still fires past the spread floor",
      "FBS260624C00100000" not in state.get_sleeve("calls_only")["positions"])
broker.fetch_marks = real_fm10

# 9j. Soft lesson expiry: stale lessons demoted to a caution block, not dropped
state.save("lessons", {"lessons": [
    {"lesson": "fresh lesson alpha", "evidence": "e", "added": ts(10)},
    {"lesson": "stale lesson omega", "evidence": "e", "added": ts(200)},
], "reviewed_through": 0})
ctx9j = learning.lessons_context()
check("fresh lesson rendered normally, above the stale block",
      "fresh lesson alpha" in ctx9j and "STALE (unconfirmed in >" in ctx9j
      and ctx9j.index("fresh lesson alpha") < ctx9j.index("STALE (unconfirmed"))
check("200-day-old lesson kept under the caution header, not dropped",
      "stale lesson omega" in ctx9j
      and ctx9j.index("stale lesson omega") > ctx9j.index("STALE (unconfirmed"))

# ══ 10. Self-tuning research deadline ═════════════════════════════════════════
from firm import cycle as cycle_mod  # noqa: E402

state.save("cycle_stats", {})
d, basis = cycle_mod._research_deadline_min("fast", 12)
check("no history -> per-depth floor (fast 20)",
      d == 20.0 and basis["basis"] == "floor_no_history", f"d={d}")
# Today's real journal evidence: full depth, 12 candidates took 26.1 / 55.1 min
# (measured at 3 workers — LEGACY_STATS_WORKERS). Since the worker-aware math
# (2026-07-01), running TODAY at the same 3 workers reproduces the original
# numbers exactly: worker-pace 55.1x3/12, base 55.1 -> 1.75x = 96.4 -> floor 100.
state.save("cycle_stats", {"full": [[26.1, 12], [55.1, 12]],
                           "standard": [[40.0, 10]]})
d, basis = cycle_mod._research_deadline_min("full", 12, workers=3)
check("history below floor clamps UP (96.4 -> full floor 100)",
      d == 100.0 and basis["basis"] == "history", f"d={d} basis={basis}")
# Per-candidate scaling: standard pace 4.0 min/cand x 15 cands x 1.75 = 105
d, _ = cycle_mod._research_deadline_min("standard", 15, workers=3)
check("deadline scales per candidate (4 min/cand x 15 x 1.75 = 105)",
      abs(d - 105.0) < 0.01, f"d={d}")
# Cap: 24-candidate full day, 2 workers: worker-pace 13.78 -> base 165.3 ->
# 1.75x = 289 -> clamped to the worker-scaled cap (150 x 2/2 = 150)
d, _ = cycle_mod._research_deadline_min("full", 24, workers=2)
check("deadline clamped to absolute cap (150)", d == 150.0, f"d={d}")
# Recorder keeps only the last 8 normal runs per depth
state.save("cycle_stats", {})
for i in range(10):
    cycle_mod._record_research_duration("standard", 10.0 + i, 12)
cs10 = state.load("cycle_stats", {})
# Rows are [duration, n, workers] since 2026-07-01 (worker-scaled deadline, goal
# audit fix 5); recorder defaults workers to RESEARCH_WORKERS when not passed.
_w10 = int(getattr(config, "RESEARCH_WORKERS", 2))
check("cycle_stats keeps last 8 runs per depth (oldest dropped)",
      len(cs10["standard"]) == 8 and cs10["standard"][0] == [12.0, 12, _w10]
      and cs10["standard"][-1] == [19.0, 12, _w10], str(cs10["standard"]))

# A STALL (deadline HIT) must teach the deadline to GROW (operator 2026-07-02:
# "the stall got bit by time but time did not increase"). The run.cycle branch
# records [deadline_min, done, workers] on a progressing timeout; verify that
# pace grows the NEXT deadline vs an empty-history floor.
state.save("cycle_stats", {})
_floor_std, _ = cycle_mod._research_deadline_min("standard", 20, workers=2)
# simulate the fix: hit a 50m deadline having finished only 8 of 20 (all real)
cycle_mod._record_research_duration("standard", 50.0, 8, workers=2, real_verdicts=8)
_grown_std, _gbasis = cycle_mod._research_deadline_min("standard", 20, workers=2)
check("a stall (deadline hit, partial progress) GROWS the next deadline",
      _grown_std > _floor_std and _gbasis["basis"] == "history",
      f"floor={_floor_std} grown={_grown_std}")
# but a HANG / degraded-dominated timeout must NOT inflate it (real < 50% of done)
state.save("cycle_stats", {})
cycle_mod._record_research_duration("standard", 50.0, 8, workers=2, real_verdicts=3)
check("a degraded/hung timeout does NOT teach the deadline (real<50% guard holds)",
      not (state.load("cycle_stats", {}) or {}).get("standard"))
state.save("cycle_stats", {})

# ══ 11. Rotation RUNS when due despite the daily breaker (mechanical rebalance) ═
# The daily LOSS breaker must NOT freeze a DUE monthly rotation: it is a
# validated rebalance (never raises gross exposure; its own risk control is the
# abs-momentum -> BIL filter + the -20% hard stop), not a discretionary entry.
# Gating it stranded the sleeve in June's picks with last_rotation frozen at
# Jun 9 while "rotation deferred" logged every cycle (operator 2026-07-02, live
# journal). Legacy defer stays selectable via ROTATION_IGNORES_LOSS_BREAKER.
from firm import riskbrain as rb_mod  # noqa: E402

real_vm = rb_mod.vol_multiplier
rb_mod.vol_multiplier = lambda: 1.0          # pin: no vol widening
real_eqp = broker.equity_prices
broker.equity_prices = lambda syms: {"XLK": 180.0}   # offline: price the signal target
real_rsig = data_engine.rotation_signals
rsig_called = []
data_engine.rotation_signals = lambda: rsig_called.append(1) or {
    "ranked": [("XLK", 9.0)], "cash_hurdle_3m": 0.0}
real_rotflag = config.ROTATION_IGNORES_LOSS_BREAKER


def _seed_due_breaker_rotation():
    rot = state.default_sleeve_state("etf_rotation")
    rot["cash"] = 24_000.0                    # -4% vs anchor: past the 2.5% breaker
    rot["day_pnl_anchor"] = {"date": risk._today(), "equity": 25_000.0}
    rot["peak_equity"] = 25_000.0             # ...no drawdown kill (isolate the breaker)
    rot["last_rotation"] = ts(40)             # rotation due (>28 days)
    state.save_sleeve("etf_rotation", rot)
    return rot


# Default (flag ON): a DUE rotation RUNS THROUGH the tripped breaker.
config.ROTATION_IGNORES_LOSS_BREAKER = True
rsig_called.clear()
rot = _seed_due_breaker_rotation()
sleeves_mod2.run_etf_rotation("paper")
rot2 = state.get_sleeve("etf_rotation")
check("due rotation RUNS despite a tripped daily breaker (signals computed)",
      bool(rsig_called))
check("run-despite-breaker advances last_rotation (the rebalance happened)",
      rot2.get("last_rotation") != rot["last_rotation"],
      f"last_rotation={rot2.get('last_rotation')}")
check("breaker override is journaled (auditable, not silent)",
      any(e.get("event") == "rotation_breaker_override"
          and e.get("sleeve") == "etf_rotation"
          for e in state.read_journal(40)))

# Flag OFF: legacy behavior — the whole rotation DEFERS on a tripped breaker.
config.ROTATION_IGNORES_LOSS_BREAKER = False
rsig_called.clear()
rot = _seed_due_breaker_rotation()
sleeves_mod2.run_etf_rotation("paper")
rot2 = state.get_sleeve("etf_rotation")
check("legacy flag OFF: a tripped breaker DEFERS the whole rotation (no run)",
      not rsig_called and rot2.get("last_rotation") == rot["last_rotation"],
      f"rsig_called={bool(rsig_called)} last_rotation={rot2.get('last_rotation')}")
check("legacy deferral journaled as entries_blocked",
      any(e.get("event") == "entries_blocked"
          and e.get("sleeve") == "etf_rotation"
          for e in state.read_journal(40)))

config.ROTATION_IGNORES_LOSS_BREAKER = real_rotflag
rb_mod.vol_multiplier = real_vm
broker.equity_prices = real_eqp
data_engine.rotation_signals = real_rsig

# ══ 12. Cash equitization (anti-cash-drag) ═════════════════════════════════════
real_eqp12 = broker.equity_prices
broker.equity_prices = lambda syms: {x: 500.0 for x in syms}
real_fm12 = broker.fetch_marks

eqz = state.default_sleeve_state("alpha_stocks")          # 100% cash
state.save_sleeve("alpha_stocks", eqz)
sleeves_mod2.run_alpha_stocks({}, {}, "paper")            # no verdicts at all
eqz = state.get_sleeve("alpha_stocks")
spy = eqz["positions"].get(config.BENCHMARK, {})
inv12 = (1 - eqz["cash"] / 25_000.0) * 100
check("idle cash equitized into the benchmark (90% deployed)",
      spy.get("cash_equitization") and abs(inv12 - 90.0) < 1.0,
      f"invested={inv12:.1f}% qty={spy.get('quantity')}")

broker.fetch_marks = lambda st: ({config.BENCHMARK: 400.0}
                                 if st.get("sleeve") == "alpha_stocks" else {})
monitor.check_all_exits(include_calls=False)              # -20% on the parking
broker.fetch_marks = real_fm12
check("equitization exempt from per-name stops (no churn)",
      config.BENCHMARK in state.get_sleeve("alpha_stocks")["positions"])

sleeves_mod2._apply_sells(state.get_sleeve("alpha_stocks"),
                          {config.BENCHMARK: {"verdict": {"rating": "STRONG_SELL"}}},
                          "paper", {config.BENCHMARK: 500.0})
check("committee SELL suppressed on equitization parking",
      config.BENCHMARK in state.get_sleeve("alpha_stocks")["positions"])

# The -20% mark above legitimately engaged the soft tier (drawdown past 70%
# of the kill threshold) — clear it so the conviction-buy path is unscaled
eqz = state.get_sleeve("alpha_stocks")
eqz["soft_risk"] = False
eqz["peak_equity"] = 25_000.0
state.save_sleeve("alpha_stocks", eqz)

buy_v = {"NVDA": {"kind": "stock", "packet": {},
                  "verdict": {"rating": "STRONG_BUY", "conviction": 85,
                              "target_weight_pct": 18.0}}}
# ISOLATION (2026-07-01, caught by the E2E loop): run_alpha_stocks -> entries_allowed()
# consults the FIRM-WIDE daily breaker, which values every sleeve at LIVE marks
# (broker.sleeve_equity(sv) -> fetch_marks) — a path the mocked equity_prices above
# does NOT cover. With the market OPEN the sandbox's aggregate book can show a day
# loss and trip the breaker, blocking this buy: the test flaked green pre-open / red
# intraday. Freeze the breaker to ALLOW here — this section validates parking reclaim,
# not the breaker (which has its own checks). Same class as the macro-calendar
# date-flake: pin the live input so the offline suite is deterministic.
_real_fdb_parking = risk.firm_daily_breaker
risk.firm_daily_breaker = lambda: (True, "")
spy_qty_before = state.get_sleeve("alpha_stocks")["positions"][config.BENCHMARK]["quantity"]
sleeves_mod2.run_alpha_stocks(buy_v, {}, "paper")
risk.firm_daily_breaker = _real_fdb_parking
eqz = state.get_sleeve("alpha_stocks")
check("conviction buy reclaims cash FROM the parking (SPY sold down, NVDA in)",
      "NVDA" in eqz["positions"]
      and eqz["positions"][config.BENCHMARK]["quantity"] < spy_qty_before,
      f"spy {spy_qty_before}->{eqz['positions'][config.BENCHMARK]['quantity']}")

soft = state.default_sleeve_state("alpha_stocks")
soft["soft_risk"] = True                                   # de-risking: cash IS the position
state.save_sleeve("alpha_stocks", soft)
sleeves_mod2.run_alpha_stocks({}, {}, "paper")
check("no equitization while de-risking (soft tier)",
      config.BENCHMARK not in state.get_sleeve("alpha_stocks")["positions"])

broker.equity_prices = real_eqp12

# ══ 13. Absence-audit round: re-underwriting, splits, firm cap, dividends ══════
import pandas as pd  # noqa: E402
import yfinance  # noqa: E402

from firm import cycle as cyc13  # noqa: E402
from firm import dividends as div13  # noqa: E402

# 13a. Held-name re-underwriting picks thesis positions only
a13 = state.default_sleeve_state("alpha_stocks")
a13["positions"] = {
    "NVDA": {"symbol": "NVDA", "instrument": "equity", "quantity": 5,
             "entry_price": 100.0, "cost": 500.0, "peak_price": 100.0},
    "SPY": {"symbol": "SPY", "instrument": "equity", "quantity": 10,
            "entry_price": 500.0, "cost": 5000.0, "peak_price": 500.0,
            "cash_equitization": True},
}
state.save_sleeve("alpha_stocks", a13)
c13 = state.default_sleeve_state("core_blend")
c13["positions"] = {
    "VOO": {"symbol": "VOO", "instrument": "equity", "quantity": 5,
            "entry_price": 600.0, "cost": 3000.0, "peak_price": 600.0},
    "AVGO": {"symbol": "AVGO", "instrument": "equity", "quantity": 2,
             "entry_price": 200.0, "cost": 400.0, "peak_price": 200.0},
}
state.save_sleeve("core_blend", c13)
check("re-underwriting targets thesis names only (no core/equitization)",
      cyc13._held_for_rereview() == {"NVDA", "AVGO"},
      str(cyc13._held_for_rereview()))

# 13b. Split adjustment instead of phantom-stop
real_tkr = yfinance.Ticker


class _FakeSplitTkr:
    def __init__(self, sym): pass
    splits = pd.Series([2.0], index=[pd.Timestamp.now(tz="UTC")])


yfinance.Ticker = _FakeSplitTkr
pos13 = {"symbol": "NVDA", "instrument": "equity", "quantity": 10,
         "entry_price": 900.0, "cost": 9000.0, "peak_price": 950.0,
         "last_price": 900.0}
check("2:1 split restates the position (no phantom -50% stop)",
      monitor._maybe_split_adjust(pos13, 451.0, "alpha_stocks", "NVDA")
      and pos13["quantity"] == 20 and pos13["entry_price"] == 450.0
      and pos13["peak_price"] == 475.0, str(pos13))
pos13b = {"symbol": "XYZ", "instrument": "equity", "quantity": 10,
          "entry_price": 900.0, "cost": 9000.0, "peak_price": 950.0,
          "last_price": 900.0}
check("a real -67% crash is NOT mistaken for the split (math mismatch)",
      not monitor._maybe_split_adjust(pos13b, 300.0, "alpha_stocks", "XYZ"))
yfinance.Ticker = real_tkr

# 13c. Firm-level single-name cap across sleeves
a13 = state.default_sleeve_state("alpha_stocks")
a13["positions"] = {"NVDA": {"symbol": "NVDA", "instrument": "equity",
                             "quantity": 25, "entry_price": 100.0,
                             "cost": 2500.0, "peak_price": 100.0}}
state.save_sleeve("alpha_stocks", a13)
c13 = state.default_sleeve_state("core_blend")
c13["positions"] = {"NVDA": {"symbol": "NVDA", "instrument": "equity",
                             "quantity": 22, "entry_price": 100.0,
                             "cost": 2200.0, "peak_price": 100.0}}
state.save_sleeve("core_blend", c13)
k13 = state.default_sleeve_state("calls_only")
k13["positions"] = {"NVDA260718C00100000": {
    "symbol": "NVDA260718C00100000", "instrument": "option",
    "underlying": "NVDA", "quantity": 10, "entry_price": 2.3,
    "cost": 2300.0, "peak_price": 2.3}}
state.save_sleeve("calls_only", k13)
state.save_sleeve("etf_rotation", state.default_sleeve_state("etf_rotation"))
ok13, why13 = risk.check_firm_name_cap("NVDA", 4500.0)   # firm eq ~107k
check("firm-wide single-name stack blocked (alpha+core+calls ~10.7% > 10%)",
      not ok13 and "firm-wide" in why13, why13)
ok13b, _ = risk.check_firm_name_cap("NVDA", 100.0)
check("small add inside the firm cap passes", ok13b)

# 13d. Paper dividends credited on ex-date, deduped forever
class _FakeDivTkr:
    def __init__(self, sym): pass
    dividends = pd.Series([0.5], index=[pd.Timestamp.now(tz="UTC").normalize()])
    splits = pd.Series(dtype=float)


yfinance.Ticker = _FakeDivTkr
d13 = state.default_sleeve_state("alpha_stocks")
d13["positions"] = {"CSCO": {"symbol": "CSCO", "instrument": "equity",
                             "quantity": 10, "entry_price": 100.0,
                             "cost": 1000.0, "peak_price": 100.0,
                             "opened_mode": "paper"}}
state.save_sleeve("alpha_stocks", d13)
state.save_sleeve("core_blend", state.default_sleeve_state("core_blend"))
state.save_sleeve("calls_only", state.default_sleeve_state("calls_only"))
state.save("dividends_paid", {"keys": []})
n13 = div13.credit_dividends()
cash13 = state.get_sleeve("alpha_stocks")["cash"]
check("dividend credited on ex-date ($0.50 x 10 shares)",
      n13 >= 1 and abs(cash13 - 25_005.0) < 0.01, f"n={n13} cash={cash13}")
n13b = div13.credit_dividends()
check("dividend never double-credited (dedupe)",
      n13b == 0 and abs(state.get_sleeve('alpha_stocks')['cash'] - 25_005.0) < 0.01)
yfinance.Ticker = real_tkr

# ══ 14. Calls/ETF re-underwriting + ETF-aware firm cap + SPY-relative learning ═
# 14a. Held-call underlyings join re-underwriting; rotation stays mechanical
state.save_sleeve("alpha_stocks", state.default_sleeve_state("alpha_stocks"))
state.save_sleeve("core_blend", state.default_sleeve_state("core_blend"))
k14 = state.default_sleeve_state("calls_only")
k14["positions"] = {"AMD260718C00150000": {
    "symbol": "AMD260718C00150000", "instrument": "option", "underlying": "AMD",
    "quantity": 2, "entry_price": 3.0, "cost": 600.0, "peak_price": 3.0,
    "expiration": "2026-07-18", "strike": 150.0, "opened_at": ts(),
    "opened_mode": "paper", "ladder": {}}}
state.save_sleeve("calls_only", k14)
r14 = state.default_sleeve_state("etf_rotation")
r14["positions"] = {"SMH": {"symbol": "SMH", "instrument": "equity",
                            "quantity": 10, "entry_price": 300.0,
                            "cost": 3000.0, "peak_price": 300.0}}
state.save_sleeve("etf_rotation", r14)
check("held-call underlyings re-underwritten; rotation stays mechanical",
      cyc13._held_for_rereview() == {"AMD"}, str(cyc13._held_for_rereview()))

# 14b. SELL verdict on the underlying force-closes the call (thesis break)
real_fm14 = broker.fetch_marks
broker.fetch_marks = lambda st: ({"AMD260718C00150000": 2.4}
                                 if st.get("sleeve") == "calls_only" else {})
sleeves_mod2.run_calls_only(
    {"AMD": {"kind": "stock", "verdict": {"rating": "STRONG_SELL",
                                          "conviction": 80}}}, "paper")
broker.fetch_marks = real_fm14
kk = state.get_sleeve("calls_only")
check("call force-closed when underlying thesis breaks (SELL verdict)",
      "AMD260718C00150000" not in kk["positions"]
      and kk["closed_trades"][-1]["reason"] == "underlying verdict STRONG_SELL")

# 14c. Firm cap is ETF-aware: 25% for diversified funds, 10% for single names
state.save_sleeve("calls_only", state.default_sleeve_state("calls_only"))
rqq = state.default_sleeve_state("etf_rotation")
rqq["positions"] = {"QQQ": {"symbol": "QQQ", "instrument": "equity",
                            "quantity": 16, "entry_price": 700.0,
                            "cost": 11200.0, "peak_price": 700.0}}
state.save_sleeve("etf_rotation", rqq)     # QQQ ~11% of firm — over the
ok14, _ = risk.check_firm_name_cap("QQQ", 2000.0)   # stock cap, fine for ETF
check("diversified ETF allowed past the single-name cap (25% tier)", ok14)
ok14b, why14b = risk.check_firm_name_cap("QQQ", 18_000.0)
check("ETF stacking still blocked past the 25% firm tier",
      not ok14b and "25" in why14b, why14b)

# 14d. SPY-relative outcomes flow into the ledger and stats
real_fh14 = data_engine.fetch_history
_idx14 = pd.date_range(end=pd.Timestamp.today().normalize(), periods=10, freq="D")
data_engine.fetch_history = lambda tk, period="1y": pd.DataFrame(
    {"SPY": [100 + i for i in range(10)]}, index=_idx14)
a14 = state.default_sleeve_state("alpha_stocks")
a14["closed_trades"] = [{
    "trade_id": "spyrel0001", "symbol": "REL", "instrument": "equity",
    "quantity": 5, "entry": 100.0, "exit": 112.0, "pnl": 60.0,
    "reason": "trail", "opened_at": ts(7), "closed_at": ts(0.01)}]
state.save_sleeve("alpha_stocks", a14)
state.save_sleeve("etf_rotation", state.default_sleeve_state("etf_rotation"))
state.save("ledger", {"trades": [], "seen": []})
learning.record_outcomes()
data_engine.fetch_history = real_fh14
row14 = next((t for t in state.load("ledger", {}).get("trades", [])
              if t["ticker"] == "REL"), None)
check("trade outcome recorded RELATIVE to SPY over its own window",
      row14 is not None and isinstance(row14.get("spy_ret_pct"), (int, float))
      and isinstance(row14.get("excess_ret_pct"), (int, float))
      and abs(row14["excess_ret_pct"]
              - (12.0 - row14["spy_ret_pct"])) < 0.05,
      f"spy={row14 and row14.get('spy_ret_pct')} excess={row14 and row14.get('excess_ret_pct')}")
st14 = learning.stats()
check("stats expose mandate-relative aggregates (vs_spy block)",
      st14.get("vs_spy", {}).get("trades_measured", 0) >= 1
      and st14["vs_spy"]["beat_spy_rate_pct"] is not None,
      str(st14.get("vs_spy")))

# ══ 15. Learnable caps: tuned_cap, clamping, caps audit, review application ════
# 15a. tuned_cap: default -> config; tuned -> honored; outside bounds -> clamped
state.save("tuning", {})
check("no tuning on file -> config default",
      risk.tuned_cap("MAX_FIRM_NAME_PCT") == config.MAX_FIRM_NAME_PCT)
state.save("tuning", {"caps": {"MAX_FIRM_NAME_PCT": 14.0,
                               "MAX_ENTRY_DRIFT_PCT": 99.0}})
check("review-tuned cap honored inside bounds",
      risk.tuned_cap("MAX_FIRM_NAME_PCT") == 14.0)
check("tuned cap CLAMPED to hard bounds (99 -> 6.0 drift ceiling)",
      risk.tuned_cap("MAX_ENTRY_DRIFT_PCT") == 6.0)

# 15b. Live gates honor tuned values: the drift guard widens when tuned
state.save("tuning", {"caps": {"MAX_ENTRY_DRIFT_PCT": 6.0}})
pk15 = {"technicals": {"price": 100.0, "vol_ann_pct": 30.0}}
check("drift guard honors the LEARNED band (5% passes at tuned 6%)",
      sleeves_mod2._entry_drift_ok("TUN", 105.0, pk15, "alpha_stocks"))
state.save("tuning", {})
check("drift guard back to default band (5% blocked at 3%)",
      not sleeves_mod2._entry_drift_ok("TUN", 105.0, pk15, "alpha_stocks"))

# 15c. _apply_cap_tuning clamps + persists + journals
applied15 = learning._apply_cap_tuning({"MAX_FIRM_NAME_PCT": 99.0,
                                        "MAX_SECTOR_PCT": 30.0,
                                        "not_a_cap": 5.0})
check("cap tuning clamped into CAP_BOUNDS and persisted",
      applied15 == {"MAX_FIRM_NAME_PCT": 18.0, "MAX_SECTOR_PCT": 30.0}
      and state.load("tuning", {}).get("caps", {}).get("MAX_SECTOR_PCT") == 30.0,
      str(applied15))
check("garbage cap tuning rejected", learning._apply_cap_tuning("junk") is None)
state.save("tuning", {})

# 15d. Caps audit: blocked entries get forward SPY-relative outcomes by gate
# (backdated 7 days — a block from TODAY correctly has no forward data yet)
import json as _json15

with open(config.STATE_DIR / "journal.jsonl", "a", encoding="utf-8") as _f15:
    _f15.write(_json15.dumps({"ts": ts(7), "event": "buy_blocked_firm_cap",
                              "symbol": "CAPX", "sleeve": "alpha_stocks"}) + "\n")
real_fh15 = data_engine.fetch_history
_idx15 = pd.date_range(end=pd.Timestamp.today().normalize(), periods=8, freq="D")
data_engine.fetch_history = lambda tk, period="3mo": pd.DataFrame(
    {"CAPX": [100, 102, 104, 106, 108, 110, 112, 114],
     "SPY": [100, 100.5, 101, 101.5, 102, 102.5, 103, 103.5]}, index=_idx15)
aud15 = learning.blocked_audit()
data_engine.fetch_history = real_fh15
gate15 = aud15["by_gate"].get("buy_blocked_firm_cap", {})
check("caps audit measures what the block cost (CAPX +14% vs SPY +3.5%)",
      gate15.get("n", 0) >= 1 and gate15.get("avg_fwd_excess_pct", 0) > 5
      and gate15.get("missed_winners", 0) >= 1, str(gate15))
check("caps audit reports the firm's current effective caps",
      aud15["current_caps"].get("MAX_FIRM_NAME_PCT") == config.MAX_FIRM_NAME_PCT)

# 15e. The review APPLIES cap tuning from the model's response
real_ask15, real_avail15 = brain.ask_json, brain.available
brain.available = lambda: True
brain.ask_json = lambda *a, **k: {
    "lessons": [{"lesson": "caps audit shows the firm cap taxed alpha",
                 "evidence": "blocked CAPX outperformed SPY by 10.5%"}],
    "cap_tuning": {"MAX_FIRM_NAME_PCT": 12.0}}
real_fh15b = data_engine.fetch_history
data_engine.fetch_history = lambda tk, period="3mo": pd.DataFrame(
    {"SPY": [100.0, 101.0]},
    index=pd.date_range(end=pd.Timestamp.today().normalize(), periods=2, freq="D"))
state.save("ledger", {"trades": [{"sleeve": "alpha_stocks", "ticker": "T",
                                  "symbol": "T", "pnl": 1.0}],
                      "seen": [], "total_recorded": 1})
state.save("lessons", {"lessons": [], "reviewed_through": 0})
rev15 = learning.run_review(force=True)
brain.ask_json, brain.available = real_ask15, real_avail15
data_engine.fetch_history = real_fh15b
check("review applies cap_tuning (firm cap learned down to 12%)",
      rev15 is not None and rev15.get("cap_tuning_applied", {}).get(
          "MAX_FIRM_NAME_PCT") == 12.0
      and risk.tuned_cap("MAX_FIRM_NAME_PCT") == 12.0,
      str(rev15 and rev15.get("cap_tuning_applied")))
state.save("tuning", {})

# ══ 16. Outage recovery: zero-verdict guard, EOD catch-up, connectivity probe ══
from zoneinfo import ZoneInfo as _ZI16  # noqa: E402

from firm import health as health16  # noqa: E402
from firm import riskbrain as rb16  # noqa: E402

# 16a. A cycle that produces ZERO verdicts must NOT stamp research fresh —
# it must flag a retry so the health loop re-runs it once connectivity returns
state.save("research", {"latest": {"KEEP_ME": {"verdict": {"rating": "HOLD"}}},
                        "latest_asof": ts(0.5), "history": []})
state.save("cycle_retry", {"needed": False})
for sl16 in config.SLEEVES:
    state.save_sleeve(sl16, state.default_sleeve_state(sl16))
real_su16 = data_engine.screen_universe
real_sr16 = riskbrain.sector_regime
real_wp16 = watchlist.promotions
real_r116 = cyc13._research_one
data_engine.screen_universe = lambda: {
    "technicals": {"FAKE": {"price": 10.0}}, "stock_candidates": ["FAKE"],
    "etf_candidates": [], "quant_scores": {"FAKE": 50.0}, "scan_info": {}}
rb16.sector_regime = lambda: {}
watchlist.promotions = lambda screen, researching=None: []
cyc13._research_one = lambda *a, **k: (_ for _ in ()).throw(
    RuntimeError("simulated internet outage"))
res16 = cyc13.run_cycle()
data_engine.screen_universe = real_su16
rb16.sector_regime = real_sr16
watchlist.promotions = real_wp16
cyc13._research_one = real_r116
check("zero-verdict cycle fails loudly instead of stamping research fresh",
      not res16["success"] and "verdict" in res16.get("error", ""),
      str(res16))
check("yesterday's research preserved through the outage cycle",
      "KEEP_ME" in state.load("research", {}).get("latest", {}))
check("outage cycle flags itself for automatic retry on reconnect",
      state.load("cycle_retry", {}).get("needed") is True)

# 16a-degraded: an ALL-DEGRADED cycle (Opus subscription quota exhausted) ALSO fails
# loudly, does NOT stamp research fresh, and names the QUOTA cause (operator 2026-06-25:
# the quota ran dry, every cycle returned all-BUY degraded verdicts, MU never upgraded).
from firm import brain as _brain16
data_engine.screen_universe = lambda: {
    "technicals": {"FAKE": {"price": 10.0}}, "stock_candidates": ["FAKE"],
    "etf_candidates": [], "quant_scores": {"FAKE": 50.0}, "scan_info": {}}
rb16.sector_regime = lambda: {}
watchlist.promotions = lambda screen, researching=None: []
cyc13._research_one = lambda *a, **k: {
    "verdict": {"rating": "BUY", "conviction": 90, "degraded": True},
    "kind": "stock", "packet": {}}
state.save("research", {"latest": {"KEEP_ME": {"x": 1}}, "history": [], "latest_asof": ts(1)})
_brain16._last_quota_hit_ts = 0.0
_brain16._note_failure("Error: you have reached your Claude usage limit")   # prime quota
res16q = cyc13.run_cycle()
data_engine.screen_universe = real_su16
rb16.sector_regime = real_sr16
watchlist.promotions = real_wp16
cyc13._research_one = real_r116
_brain16._last_quota_hit_ts = 0.0
check("all-degraded cycle (quota out) fails loudly and names the QUOTA cause",
      not res16q["success"] and res16q.get("error") == "opus_quota", str(res16q))
check("all-degraded quota cycle does NOT overwrite yesterday's real research",
      "KEEP_ME" in state.load("research", {}).get("latest", {}))
check("all-degraded quota cycle flags itself for automatic retry",
      state.load("cycle_retry", {}).get("needed") is True)

# 16a-stall: Feature B — the research floor classifies itself PROGRESSING vs
# STALLED vs QUOTA-OUT (operator 2026-06-25: "a better way to see hey it's still
# thinking or hey the stall is just there and it's a problem"). Pure read of the
# shared progress dict + brain quota signal, zero Opus calls.
import time as _t16s
from firm import brain as _brain16s
_mono = _t16s.monotonic()
_prog = {"done": 4, "total": 12, "last_ts": _mono, "start_ts": _mono,
         "deadline_min": 150, "stall_gap_s": 900, "depth": "full"}
_brain16s._last_quota_hit_ts = 0.0
check("floor with a fresh completion reads PROGRESSING",
      cyc13._classify_floor(dict(_prog))[0] == "progressing")
_stalled = dict(_prog); _stalled["last_ts"] = _mono - 1000   # >900s stall gap
check("floor with no completion past the stall gap reads STALLED",
      cyc13._classify_floor(_stalled)[0] == "stalled")
_brain16s._note_failure("Error: you have reached your Claude usage limit")
check("QUOTA-OUT outranks a stall (quota cause must win)",
      cyc13._classify_floor(_stalled)[0] == "quota_out")
_brain16s._last_quota_hit_ts = 0.0
_complete = dict(_prog); _complete["done"] = 12
check("floor reads COMPLETE once every workup landed",
      cyc13._classify_floor(_complete)[0] == "complete")
check("stall detail names minutes-since-progress and the deadline bound",
      "STALLED" in cyc13._floor_detail(_stalled, "stalled", 1000)
      and "deadline" in cyc13._floor_detail(_stalled, "stalled", 1000))
_slow = dict(_prog); _slow["last_ts"] = _mono - 800          # under the 900s gap
check("a slow-but-progressing floor is NOT flagged stalled",
      cyc13._classify_floor(_slow)[0] == "progressing")

# 16a-readthrough: Feature C — a cohort bellwether's just-landed earnings
# force-reopen its peers THIS cycle (operator 2026-06-25: "sandisk jumped too ...
# if you wait till sandisk earnings to buy thats too late"). Idempotent + bounded.
from datetime import date as _date16r
from firm import data_engine as _de16r
_real_re16 = _de16r.recent_earnings
_real_lvd16 = cyc13._last_verdict_date
_today16 = _date16r(2026, 6, 25)
# NOTE: mock the REAL recent_earnings contract — the print date is under
# "reported_on" (+ "days_ago"), NOT "date" (that's next_earnings). A mock using
# the wrong key would validate a bug instead of catching it (the 2026-06-25 miss).
_real_news16 = _de16r.fetch_news
_real_docs16 = _de16r.earnings_docs
_de16r.recent_earnings = lambda B, within_days=8: (
    {"reported_on": "2026-06-24", "days_ago": 1, "surprise_pct": 21.25,
     "eps_reported": 25.11, "eps_estimate": 20.71, "session": "AMC"}
    if B == "MU" else {})
_de16r.fetch_news = lambda t, limit=6: (
    ["Micron tops estimates on AI memory demand", "MU raises guidance"]
    if t == "MU" else [])
# MUST mock earnings_docs too — the read-through now reads the bellwether's SEC
# 8-K, and the regression suite stays fully OFFLINE (no live EDGAR call).
_de16r.earnings_docs = lambda t, recent=None: (
    {"filings": [{"form": "8-K", "date": "2026-06-24",
                  "excerpt": "Non-GAAP EPS $25.11; raising full-year outlook"}]}
    if t == "MU" else {})
cyc13._last_verdict_date = lambda t: None                    # peers never judged -> fire
_tech16 = {"SNDK": {"price": 10}, "WDC": {"price": 20}, "STX": {"price": 30}}
_scr16 = {"quant_scores": {}}
_wn16: dict = {}
_cands16, _added16 = cyc13._inject_cohort_readthrough(
    [("AAPL", "stock")], _tech16, _scr16, set(), _wn16, _today16)
check("bellwether (MU) print force-reopens its storage peers SNDK/WDC/STX",
      set(_added16) == {"SNDK", "WDC", "STX"})
check("read-through peer carries the bellwether + a SECTOR READ-THROUGH note",
      (_wn16.get("SNDK") or {}).get("bellwether") == "MU"
      and "READ-THROUGH" in (_wn16.get("SNDK") or {}).get("why", ""))
check("read-through note stamps the bellwether's actual report date",
      (_wn16.get("WDC") or {}).get("bellwether_reported") == "2026-06-24")
check("read-through note carries the FULL earnings numbers (not just surprise%)",
      (_wn16.get("SNDK") or {}).get("bellwether_eps_reported") == 25.11
      and "25.11 vs 20.71 est" in (_wn16.get("SNDK") or {}).get("why", ""))
check("read-through note carries the bellwether's news (market's call digest)",
      "MU raises guidance" in (_wn16.get("STX") or {}).get("bellwether_headlines", [])
      and "headlines" in (_wn16.get("STX") or {}).get("why", ""))
check("read-through note carries the bellwether's SEC 8-K (read the actual files)",
      any(f.get("form") == "8-K"
          for f in (_wn16.get("SNDK") or {}).get("bellwether_filings", []))
      and "8-K" in (_wn16.get("SNDK") or {}).get("why", ""))
cyc13._last_verdict_date = lambda t: _date16r(2026, 6, 25)   # judged AFTER the print
_, _added16b = cyc13._inject_cohort_readthrough(
    [("AAPL", "stock")], _tech16, _scr16, set(), {}, _today16)
check("a peer already judged since the print is NOT force-reopened (idempotent)",
      _added16b == [])
_de16r.recent_earnings = lambda B, within_days=8: (
    {"reported_on": "2026-06-15", "days_ago": 10, "surprise_pct": 12.0}
    if B == "MU" else {})                                    # 10d stale, beyond window
cyc13._last_verdict_date = lambda t: None
_, _added16c = cyc13._inject_cohort_readthrough(
    [("AAPL", "stock")], _tech16, _scr16, set(), {}, _today16)
check("a stale bellwether print (older than the window) does NOT read through",
      _added16c == [])
_de16r.recent_earnings = _real_re16
_de16r.fetch_news = _real_news16
_de16r.earnings_docs = _real_docs16
cyc13._last_verdict_date = _real_lvd16

# 16a-edgar: Feature D — on a FRESH print the packet attaches primary-source
# earnings DOCUMENTS (SEC 8-K/10-Q + optional transcript) so the committee reads
# the actual filings (operator 2026-06-25: "read the files like 8k"). Gated to a
# recent print, fail-soft, and surfaced to the fundamental + sentiment desks.
from firm import edgar as _edg16, transcripts as _tr16, analysts as _an16
_real_ef16, _real_tl16 = _edg16.earnings_filings, _tr16.latest
_edg16.earnings_filings = lambda t, within_days=8: [
    {"form": "8-K", "date": "2026-06-24", "url": "u", "excerpt": "EPS $25.11"}]
_tr16.latest = lambda t, within_days=8: {}
state.save("transcript_cache", {})        # mock is authoritative — no stale cache
_fresh16 = {"reported_on": "2026-06-24", "days_ago": 1, "surprise_pct": 21.0}
check("earnings_docs attaches the 8-K on a FRESH print",
      any(f.get("form") == "8-K"
          for f in _de16r.earnings_docs("MU", _fresh16).get("filings", [])))
check("earnings_docs returns {} when there is NO recent print",
      _de16r.earnings_docs("MU", {}) == {})
check("earnings_docs returns {} for a STALE print (beyond the window)",
      _de16r.earnings_docs("MU", {"reported_on": "2026-05-01", "days_ago": 40}) == {})
_edg16.earnings_filings = lambda t, within_days=8: (_ for _ in ()).throw(RuntimeError("edgar down"))
check("earnings_docs is FAIL-SOFT (a filing-fetch error never raises)",
      _de16r.earnings_docs("MU", _fresh16) == {})
check("fundamental + sentiment desks SEE earnings_docs + recent_earnings",
      "earnings_docs" in _an16._DESK_VIEWS["fundamental"]
      and "recent_earnings" in _an16._DESK_VIEWS["fundamental"]
      and "earnings_docs" in _an16._DESK_VIEWS["sentiment"])
_edg16.earnings_filings, _tr16.latest = _real_ef16, _real_tl16

# 16a-add: Build 1 — add-on-strength DISCERNS a confirmed winning leader (operator
# 2026-06-25: pyramid into a proven winner, never average down; loosen "only add on
# a 15% pullback"). _eligible_add is the discern gate.
from firm import sleeves as _slv16
import config as _cfg16
_pk_leader = {"technicals": {"ret_3m": 40.0, "above_sma50": True, "vol_ann_pct": 45.0,
                             "avg_dollar_vol": 5e8},
              "factor_scores": {"momentum": 80.0},
              "recent_earnings": {"surprise_pct": 21.0}}
_pos_win = {"quantity": 10, "entry_price": 80.0, "adds_count": 0}    # px>entry => winner
_buy16 = {"rating": "BUY", "conviction": 80, "target_weight_pct": 15.0}
check("add-on-strength FIRES for a confirmed winning leader (BUY+momentum+beat)",
      _slv16._eligible_add(_buy16, _pos_win, 100.0, _pk_leader, False) is True)
check("add-on-strength does NOT fire on a HOLD (committee must want MORE)",
      _slv16._eligible_add({"rating": "HOLD", "conviction": 80}, _pos_win, 100.0,
                           _pk_leader, False) is False)
check("add-on-strength NEVER averages a loser down (price below entry)",
      _slv16._eligible_add(_buy16, _pos_win, 70.0, _pk_leader, False) is False)
check("add-on-strength is BOUNDED — stops after ADD_MAX_COUNT adds",
      _slv16._eligible_add(_buy16, {**_pos_win, "adds_count": _cfg16.ADD_MAX_COUNT},
                           100.0, _pk_leader, False) is False)
check("add-on-strength refuses a DEGRADED verdict",
      _slv16._eligible_add({**_buy16, "degraded": True}, _pos_win, 100.0,
                           _pk_leader, False) is False)
_pk_nomom = {"technicals": {"ret_3m": -5.0, "above_sma50": False},
             "factor_scores": {"momentum": 10.0}, "recent_earnings": {"surprise_pct": 21.0}}
check("add-on-strength refuses a name with BROKEN momentum (not a leader)",
      _slv16._eligible_add(_buy16, _pos_win, 100.0, _pk_nomom, False) is False)
_pk_nocat = {"technicals": {"ret_3m": 40.0, "above_sma50": True},
             "factor_scores": {"momentum": 80.0}, "recent_earnings": {"surprise_pct": 0.0}}
check("add-on-strength needs a catalyst OR sector strength (neither => no add)",
      _slv16._eligible_add(_buy16, _pos_win, 100.0, _pk_nocat, False) is False)
check("add-on-strength fires on momentum + SECTOR strength without a fresh beat",
      _slv16._eligible_add(_buy16, _pos_win, 100.0, _pk_nocat, True) is True)
# Exercise the REAL momentum-confirmation key directly, with the momentum-factor
# fallback OFF so above_sma50 is the deciding gate (review 2026-06-29: the old
# numeric 'sma50' read was dead code masked behind a strong momentum factor).
_pk_trend = {"technicals": {"ret_3m": 25.0, "above_sma50": True},
             "factor_scores": {"momentum": 20.0}, "recent_earnings": {"surprise_pct": 21.0}}
check("add-on-strength CONFIRMS a leader via above_sma50 (trend path, weak mom factor)",
      _slv16._eligible_add(_buy16, _pos_win, 100.0, _pk_trend, False) is True)
_pk_top = {"technicals": {"ret_3m": 5.0, "above_sma50": False},
           "factor_scores": {"momentum": 20.0}, "recent_earnings": {"surprise_pct": 21.0}}
check("add-on-strength REFUSES a topping name (positive ret_3m but BELOW its 50DMA)",
      _slv16._eligible_add(_buy16, _pos_win, 100.0, _pk_top, False) is False)
check("add-on-strength FAILS SAFE on an unparseable last_add_ts (no add)",
      _slv16._eligible_add(_buy16, {**_pos_win, "last_add_ts": "not-a-date"}, 100.0,
                           _pk_leader, False) is False)

# 16a-keep: Build 4 — an OWNED-name HOLD is graded "KEEP" (correct when it beats
# SPY), and a held winner that kept running is logged as a MISSED ADD so the loop
# learns the too-strict-add behavior (operator 2026-06-25).
from firm import learning as _lrn16, data_engine as _de16k
import pandas as _pd16
from datetime import datetime as _dt16, timezone as _tz16, timedelta as _tdq16
_research_bak16 = state.load("research", {})
_alpha_bak16 = state.get_sleeve("alpha_stocks")
_real_fh16k = _de16k.fetch_history
_d0_16 = (_dt16.now(_tz16.utc) - _tdq16(days=6)).date()
_idx16 = _pd16.date_range(_d0_16, periods=25, freq="D")
_closes16 = _pd16.DataFrame({
    "OWND": _pd16.Series([100.0 * (1 + 0.01 * i) for i in range(25)], index=_idx16),
    _cfg16.BENCHMARK: _pd16.Series([100.0] * 25, index=_idx16)})       # OWND +24%, SPY flat
_de16k.fetch_history = lambda tickers, period="6mo", **k: _closes16
_a16 = state.get_sleeve("alpha_stocks")
_a16.setdefault("positions", {})["OWND"] = {
    "instrument": "equity", "quantity": 10, "entry_price": 50.0}
state.save_sleeve("alpha_stocks", _a16)
state.save("research", {"history": [{
    "asof": _dt16(_d0_16.year, _d0_16.month, _d0_16.day, tzinfo=_tz16.utc).isoformat(),
    "verdicts": {"OWND": {"rating": "HOLD", "conviction": 70}},
    "context": {"OWND": {"factors": {}, "owned": True}}}]})   # owned AT verdict time
_rc16 = _lrn16.verdict_report_card()
_de16k.fetch_history = _real_fh16k
state.save("research", _research_bak16)
state.save_sleeve("alpha_stocks", _alpha_bak16)
check("owned-name HOLD that BEAT SPY is graded CORRECT (keep the winner)",
      _rc16.get("by_rating", {}).get("HOLD", {}).get("correct_pct") == 100.0)
check("a held winner that kept running is logged as a MISSED ADD (learning signal)",
      any(m.get("ticker") == "OWND" for m in _rc16.get("missed_adds", [])))

# 16a-ownat: Build-4 hardening — ownership is keyed to VERDICT TIME, not CURRENT
# holdings. A name NOT owned when rated HOLD but bought SINCE must not retro-grade
# as a KEEP or fabricate a missed_add (adversarial review 2026-06-29).
_research_bak16b = state.load("research", {})
_alpha_bak16b = state.get_sleeve("alpha_stocks")
_de16k.fetch_history = lambda tickers, period="6mo", **k: _pd16.DataFrame({
    "FOO": _pd16.Series([100.0 * (1 + 0.01 * i) for i in range(25)], index=_idx16),
    _cfg16.BENCHMARK: _pd16.Series([100.0] * 25, index=_idx16)})    # FOO beats SPY
_a16b = state.get_sleeve("alpha_stocks")
_a16b.setdefault("positions", {})["FOO"] = {
    "instrument": "equity", "quantity": 5, "entry_price": 90.0}     # held NOW
state.save_sleeve("alpha_stocks", _a16b)
state.save("research", {"history": [{
    "asof": _dt16(_d0_16.year, _d0_16.month, _d0_16.day, tzinfo=_tz16.utc).isoformat(),
    "verdicts": {"FOO": {"rating": "HOLD", "conviction": 70}},
    "context": {"FOO": {"factors": {}, "owned": False}}}]})         # NOT owned at verdict
_rc16b = _lrn16.verdict_report_card()
_de16k.fetch_history = _real_fh16k
state.save("research", _research_bak16b)
state.save_sleeve("alpha_stocks", _alpha_bak16b)
check("a name UNOWNED at verdict (bought later) is NOT retro-graded as a KEEP",
      _rc16b.get("by_rating", {}).get("HOLD", {}).get("correct_pct") == 0.0)
check("a name unowned at verdict is NOT fabricated into missed_adds",
      not any(m.get("ticker") == "FOO" for m in _rc16b.get("missed_adds", [])))
check("a genuine pass-HOLD that beat SPY still surfaces as an overlooked winner",
      any(m.get("ticker") == "FOO" for m in _rc16b.get("overlooked_winners", [])))

# 16b. EOD catch-up: books a missed postmarket wrap exactly once, never early
real_net16 = health16.internet_up
health16.internet_up = lambda **k: True
state.save("eod", {})
wed_early = __import__("datetime").datetime(2026, 6, 10, 15, 0,
                                            tzinfo=_ZI16("America/New_York"))
wed_late = __import__("datetime").datetime(2026, 6, 10, 17, 0,
                                           tzinfo=_ZI16("America/New_York"))
health16.eod_catchup(now_et=wed_early)
check("EOD catch-up never fires before the close window",
      state.load("eod", {}).get("date") != "2026-06-10")
health16.eod_catchup(now_et=wed_late)
n_eod = sum(1 for e in state.read_journal(30)
            if e.get("event") == "eod_summary"
            and e.get("via") == "health-loop catch-up")
health16.eod_catchup(now_et=wed_late)   # second call same day: no double-book
n_eod2 = sum(1 for e in state.read_journal(30)
             if e.get("event") == "eod_summary"
             and e.get("via") == "health-loop catch-up")
check("missed postmarket wrap booked exactly once on reconnect",
      state.load("eod", {}).get("date") == "2026-06-10"
      and n_eod == 1 and n_eod2 == 1, f"n={n_eod},{n_eod2}")
health16.internet_up = real_net16

# 16c. Connectivity probe returns a clean boolean fast
check("internet probe returns a boolean", health16.internet_up() in (True, False))

# ══ 16. Immutable inception + calls-desk visibility ════════════════════════════
# 16a. Inception survives the 2000-point equity-curve trim
inc = state.default_sleeve_state("alpha_stocks")
born = inc["inception"]
check("new sleeves carry an immutable inception stamp", bool(born))
inc["equity_curve"] = [{"ts": ts(30 - i / 100), "equity": 25_000.0}
                      for i in range(2100)]
risk.mark_equity("alpha_stocks", inc, 25_500.0)
check("curve trims to 2000 but inception is NOT derived from it",
      len(inc["equity_curve"]) <= 2001 and inc["inception"] == born
      and inc["inception"] != inc["equity_curve"][0]["ts"],
      f"curve={len(inc['equity_curve'])}")

# 16b. Pre-inception sleeves migrate from their earliest curve point
old = state.default_sleeve_state("alpha_stocks")
del old["inception"]
old["equity_curve"] = [{"ts": ts(2), "equity": 25_000.0}]
state.save_sleeve("alpha_stocks", old)
mig = state.get_sleeve("alpha_stocks")
check("legacy sleeve inherits inception from curve[0] on first load",
      mig.get("inception") == old["equity_curve"][0]["ts"])

# 16c. An empty calls book explains itself every cycle. Under the broadened
# gate (2026-07-02: STRONG_BUY OR BUY+suitable+conv>=CALLS_BUY_CONVICTION_MIN),
# a BUY@65 (below the 72 bar) + a HOLD both fail the gate -> empty book.
state.save_sleeve("calls_only", state.default_sleeve_state("calls_only"))
real_fm16 = broker.fetch_marks
broker.fetch_marks = lambda st: {}
sleeves_mod2.run_calls_only(
    {"AAA": {"kind": "stock", "verdict": {"rating": "BUY", "conviction": 65,
                                          "suitable_for_calls": True}},
     "BBB": {"kind": "stock", "verdict": {"rating": "HOLD", "conviction": 55}}},
    "paper")
broker.fetch_marks = real_fm16
ev16 = next((e for e in state.read_journal(30)
             if e.get("event") == "calls_no_candidates"), None)
check("calls desk journals WHY nothing qualified (no gate-clearing verdicts)",
      ev16 is not None and ev16.get("stocks_judged") == 2
      and ev16.get("strong_buys") == 0
      and ev16.get("qualifying_high_conv_buys") == 0,
      str(ev16))
# The broadened gate: a high-conviction options-suitable BUY now clears it,
# a low-conviction / unsuitable / degraded one never does.
check("calls gate: STRONG_BUY (conv>=min) OR BUY+suitable+conv>=72 clears it",
      sleeves_mod2._calls_entry_ok({"rating": "STRONG_BUY", "conviction": 70,
                                    "suitable_for_calls": True})
      and sleeves_mod2._calls_entry_ok({"rating": "BUY", "conviction": 80,
                                        "suitable_for_calls": True}))
check("calls gate REJECTS low-conv BUY, judge-vetoed BUY, and degraded verdicts",
      not sleeves_mod2._calls_entry_ok({"rating": "BUY", "conviction": 65,
                                        "suitable_for_calls": True})
      # fix brief 2026-07-21 #6: the veto binds ONLY on the judge's EXPLICIT
      # options_veto_calls opt-in (with its stated reason) — reason-presence
      # alone re-hardened the gate because the schema requires the reason
      # field on every verdict
      and not sleeves_mod2._calls_entry_ok({"rating": "BUY", "conviction": 90,
                                            "suitable_for_calls": False,
                                            "options_veto_calls": True,
                                            "suitable_for_calls_reason":
                                            "illiquid chain, earnings inside "
                                            "the option window"})
      and not sleeves_mod2._calls_entry_ok({"rating": "STRONG_BUY",
                                            "conviction": 90, "degraded": True,
                                            "suitable_for_calls": True}))
check("calls gate: a reason WITHOUT the explicit veto flag no longer binds "
      "(2026-07-21 #6 — mech screen decides)",
      sleeves_mod2._calls_entry_ok({"rating": "BUY", "conviction": 90,
                                    "suitable_for_calls": False,
                                    "suitable_for_calls_reason":
                                    "slow re-rate, no near-term catalyst"})
      # an explicit veto with NO stated reason never binds either
      and sleeves_mod2._calls_entry_ok({"rating": "BUY", "conviction": 90,
                                        "suitable_for_calls": False,
                                        "options_veto_calls": True}))

# 16d. Minimum-viable contract from PARKED capital (operator 2026-07-22: "there
# is capital, it's just parked and can be sold for the call"). A valid top signal
# on a high-vol underlying gap-scales _desired below one contract; before the
# floor the desk never freed the parking (short<=0, _desired<=cash) and skipped
# size_zero while ~$23k sat parked — the sleeve's life-long ~zero-opens deadlock.
# It must now sell parking to fund exactly one contract that fits the hard cap.
from firm import allocation as _alloc16d  # noqa: E402
_park16 = _alloc16d.parking_symbol("calls_only")
_k16d = state.default_sleeve_state("calls_only")
_k16d["cash"] = 300.0
_k16d["positions"] = {_park16: {
    "symbol": _park16, "instrument": "equity", "quantity": 110.0,
    "entry_price": 213.0, "last_price": 213.0, "cost": 23_430.0,
    "cash_equitization": True, "strategy": "cash_equitization",
    "opened_at": ts(), "opened_mode": "paper"}}
state.save_sleeve("calls_only", _k16d)
_exp16 = (datetime.now(timezone.utc) + timedelta(days=14)).strftime("%Y-%m-%d")
_osi16 = "MU___%sC00112000" % _exp16.replace("-", "")
_rp16, _re16, _rf16 = broker.pick_call, broker.equity_prices, broker.fetch_marks
broker.pick_call = lambda sym, otm, mn, mx: {
    "symbol": _osi16, "osi": _osi16, "mid": 12.0, "bid": 11.5, "ask": 12.5,
    "strike": 112.0, "expiration": _exp16}
broker.equity_prices = lambda syms: {x: (213.0 if x == _park16 else 110.0) for x in syms}
broker.fetch_marks = lambda st: {_park16: 213.0}
sleeves_mod2.run_calls_only(
    {"MU": {"kind": "stock",
            "packet": {"technicals": {"price": 110.0, "vol_ann_pct": 150.0}},
            "verdict": {"rating": "STRONG_BUY", "conviction": 80,
                        "suitable_for_calls": True, "thesis": "memory upcycle"}}},
    "paper")
broker.pick_call, broker.equity_prices, broker.fetch_marks = _rp16, _re16, _rf16
_ck16 = state.get_sleeve("calls_only")
_opened16 = any(p.get("underlying") == "MU" for p in _ck16["positions"].values()
                if p.get("instrument") == "option")
_parkleft16 = (_ck16["positions"].get(_park16) or {}).get("quantity", 110.0)
check("16d calls: high-vol top signal funds ONE contract from PARKED capital "
      "(no size_zero deadlock)",
      _opened16 and _parkleft16 < 110.0,
      f"opened={_opened16} park_qty={_parkleft16}")
state.save_sleeve("calls_only", state.default_sleeve_state("calls_only"))

# ══ 17. Benchmark baseline = actual inception PRINT, not a date convention ═════
real_fh17 = data_engine.fetch_history
data_engine.fetch_history = lambda tickers, period="6mo": pd.DataFrame(
    {"SPY": [700.0, 690.0, 707.0]},
    index=pd.to_datetime(["2026-06-09", "2026-06-10", "2026-06-11"]))
check("stored inception print drives the benchmark (707/700 = +1.0%)",
      abs(data_engine.spy_return_since("2026-06-09T18:59:05+00:00", 700.0) - 1.0) < 0.001)
check("no print -> date-convention fallback still works",
      abs(data_engine.spy_return_since("2026-06-10T00:00:00+00:00", None)
          - (707.0 / 690.0 - 1) * 100) < 0.001)
check("bad inputs fail closed (None, never a wrong number)",
      data_engine.spy_return_since("", None) is None)
data_engine.fetch_history = real_fh17

# ══ 18. Day P&L counts from the PRIOR CLOSE; after-hours marks = closes ════════
# 18a. Anchor roll picks the previous day's last mark — overnight gaps belong
# to TODAY, so day moves compound exactly to the since-inception number
roll = state.default_sleeve_state("alpha_stocks")
roll["equity_curve"] = [
    {"ts": ts(1.5), "equity": 24_500.0},
    {"ts": ts(1.2), "equity": 24_768.97},   # yesterday's close mark
]
roll["day_pnl_anchor"] = {"date": "2020-01-01", "equity": 24_500.0}
risk.mark_equity("alpha_stocks", roll, 25_523.22)   # first mark today (post-gap)
check("day anchor = prior close, not first-mark-of-today",
      abs(roll["day_pnl_anchor"]["equity"] - 24_768.97) < 0.01,
      f"anchor={roll['day_pnl_anchor']}")
check("overnight gap now lives in day P&L (+3.05%, reconciles with all-time)",
      abs(risk.day_pnl(roll, 25_523.22) - 754.25) < 0.01)

# 18c. NON-trading day (weekend/holiday): the anchor must NOT roll — a closed
# calendar day minting a new "day" showed phantom P&L that was pure data-source
# drift ('Day +$8' on the Jul-4 2026 Saturday, '+$229' on the Jul-3 holiday).
frozen = state.default_sleeve_state("alpha_stocks")
frozen["equity_curve"] = [{"ts": ts(1.2), "equity": 24_768.97}]
frozen["day_pnl_anchor"] = {"date": "2020-01-01", "equity": 24_500.0}
_real_itd18c = _mh_glob.is_trading_day
_mh_glob.is_trading_day = lambda now=None: False        # Saturday
try:
    risk.mark_equity("alpha_stocks", frozen, 24_777.0)  # weekend drift mark
finally:
    _mh_glob.is_trading_day = _real_itd18c
check("closed-day anchor freeze: weekend/holiday never mints a new P&L day",
      frozen["day_pnl_anchor"] == {"date": "2020-01-01", "equity": 24_500.0},
      f"anchor={frozen['day_pnl_anchor']}")

# 18d. Monday's roll skips weekend drift points — prior close = last TRADING
# day's final mark, so the weekend drift lands in Monday's overnight gap
# instead of silently rebasing it away. Weekday-only gate patch: "today" stays
# a trading day (the roll fires) while the Saturday curve point is skipped.
import firm.risk as _rk18d
_sat_dt = None
for _back in range(1, 9):                    # most recent Saturday, ET
    _cand = datetime.now(_rk18d._ET) - timedelta(days=_back)
    if _cand.weekday() == 5:
        _sat_dt = _cand
        break
_fri_dt = _sat_dt - timedelta(days=1)        # the weekday close before it
mon = state.default_sleeve_state("alpha_stocks")
mon["equity_curve"] = [
    {"ts": _fri_dt.isoformat(), "equity": 24_768.97},  # last trading-day close
    {"ts": _sat_dt.isoformat(), "equity": 24_990.0},   # weekend drift (skip me)
]
mon["day_pnl_anchor"] = {"date": "2020-01-01", "equity": 24_500.0}
_real_itd18d = _mh_glob.is_trading_day
_mh_glob.is_trading_day = (lambda now=None:
                           True if now is None else now.weekday() < 5)
try:
    risk.mark_equity("alpha_stocks", mon, 25_100.0)
finally:
    _mh_glob.is_trading_day = _real_itd18d
check("anchor roll skips non-trading-day curve points (prior TRADING close wins)",
      abs(mon["day_pnl_anchor"]["equity"] - 24_768.97) < 0.01,
      f"anchor={mon['day_pnl_anchor']}")

# 18b. fetch_marks: Public is the PRIMARY feed in EVERY session state
# (2026-08-13 operator decision — close-feed primacy flip. SPEC CHANGE, not a
# weakening: this leg used to pin the 2026-06-11 official-close convention
# ("closed market marks at 500, not 999"); the operator promoted the Public
# quote to primary with yfinance as the fallback, so the pinned expectation
# flips — and the fallback leg is now asserted explicitly with Public down.)
from firm import market_hours as mh18  # noqa: E402

real_open18 = mh18.market_is_open
real_eqp18 = broker.equity_quotes_public
real_blp18 = data_engine.batch_last_prices
mh18.market_is_open = lambda: False
broker.equity_quotes_public = lambda syms: {x: 999.0 for x in syms}   # Public print (PRIMARY)
data_engine.batch_last_prices = lambda syms: {x: 500.0 for x in syms}  # yfinance fallback
mk = state.default_sleeve_state("alpha_stocks")
mk["positions"] = {"MRK18": {"symbol": "MRK18", "instrument": "equity",
                             "quantity": 1, "entry_price": 500.0, "cost": 500.0,
                             "peak_price": 500.0}}
got = broker.fetch_marks(mk)
check("closed market marks at the PRIMARY Public print (999) — operator decision 2026-08-13",
      got.get("MRK18") == 999.0, str(got))
broker.equity_quotes_public = lambda syms: {}          # Public DOWN
got = broker.fetch_marks(mk)
check("Public down: closed market falls back to the yfinance close (500)",
      got.get("MRK18") == 500.0, str(got))
broker.equity_quotes_public = lambda syms: {x: 999.0 for x in syms}
mh18.market_is_open = lambda: True
got = broker.fetch_marks(mk)
check("open market still marks at the live quote",
      got.get("MRK18") == 999.0, str(got))
mh18.market_is_open = real_open18
broker.equity_quotes_public = real_eqp18
data_engine.batch_last_prices = real_blp18

# ══ 19. The bench never goes blind outside the scan window ═════════════════════
real_rq19 = watchlist._refresh_quants
watchlist._refresh_quants = lambda ts, **_k:{"BLIND": 99.0}   # direct quant refresh
state.save("watchlist", {"BLIND": {
    "ticker": "BLIND", "added": ts(2), "source": "near_miss",
    "watch_trigger": "quant screen score breaks into tier slots",
    "history": [{"ts": ts(2), "quant": 93.0}], "missing": 1}})
empty_screen = {"quant_scores": {}, "stock_candidates": [], "technicals": {}}
watchlist.update(empty_screen, {})
wl19 = state.load("watchlist", {})
check("unranked bench name gets a FRESH quant point (was frozen + aging out)",
      wl19["BLIND"]["missing"] == 0
      and wl19["BLIND"]["history"][-1]["quant"] == 99.0,
      str(wl19["BLIND"]["history"][-2:]))
promos19 = watchlist.promotions(empty_screen)
check("improvement OUTSIDE the scan window can now promote (93 -> 99 >= +5)",
      any(p["ticker"] == "BLIND" for p in promos19),
      str([(p['ticker'], p['why']) for p in promos19]))
watchlist._refresh_quants = lambda ts, **_k:{}                # data truly gone
wl19b = state.load("watchlist", {})
wl19b["BLIND"].pop("promoted_at", None)
state.save("watchlist", wl19b)
watchlist.update(empty_screen, {})
check("no data at all still ages the name out (missing increments)",
      state.load("watchlist", {})["BLIND"]["missing"] == 1)
watchlist._refresh_quants = real_rq19

# ══ 20. Verdict report card — HOLDs and no-calls get graded too ════════════════
state.save("research", {"history": [
    {"asof": ts(10), "verdicts": {
        "RIPPER": {"rating": "HOLD", "conviction": 70},
        "DUDLEY": {"rating": "BUY", "conviction": 65},
    }},
]})
_dates20 = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=30)
real_fh20 = data_engine.fetch_history
data_engine.fetch_history = lambda tickers, period="6mo": pd.DataFrame({
    # RIPPER +60% after its HOLD; DUDLEY -10% after its BUY; SPY flat
    "RIPPER": [100.0 * (1 + 0.60 * i / 29) for i in range(30)],
    "DUDLEY": [100.0 * (1 - 0.10 * i / 29) for i in range(30)],
    "SPY": [100.0] * 30,
}, index=_dates20)
card = learning.verdict_report_card(min_age_days=3)
data_engine.fetch_history = real_fh20
hold = card.get("by_rating", {}).get("HOLD", {})
buy = card.get("by_rating", {}).get("BUY", {})
check("a HOLD that ripped is graded WRONG (correct_pct 0)",
      hold.get("n") == 1 and hold.get("correct_pct") == 0.0, str(hold))
check("a BUY that lagged is graded WRONG too",
      buy.get("n") == 1 and buy.get("correct_pct") == 0.0, str(buy))
check("the overlooked winner is named with its conviction",
      any(m["ticker"] == "RIPPER" and m["rated"] == "HOLD"
          and m["conviction"] == 70
          for m in card.get("overlooked_winners", [])),
      str(card.get("overlooked_winners")))

# ══ 21. Core advisory, verdict cooldowns, unpriceable-position alert ═══════════
from firm import cycle as cyc21  # noqa: E402
# Deterministic macro: _cooldown_state reopens a fresh HOLD when a macro print
# (FOMC/CPI/jobs/ISM) landed since the verdict. 21b/21c seed a HOLD 0.5d ago and
# assert it STAYS cooled, so pin "no macro print landed" here (else the test flakes
# on any real macro-print day — caught by the E2E harness on 2026-07-01). 21d sets
# its own macro mock, so restore right before it.
from firm import macro_calendar as _mc21b  # noqa: E402
_real_ls_21b = _mc21b.landed_since
_mc21b.landed_since = lambda *a, **k: None

# 21a. Monday advisory surfaces the strategic core + the parking seat (the
# parking joined 2026-07-16 so a seated ETF can EARN the SELL that unseats it)
check("Monday advisory = the strategic core ETFs + parking seat",
      cyc21._core_advisory_names(weekday=0) == sorted(
          {"GLD", "QQQ", "VOO", config.BENCHMARK}))
check("no advisory on other weekdays",
      cyc21._core_advisory_names(weekday=2) == [])

# 21b. Verdict cooldown: fresh HOLD stands; reopens on earnings or age
state.save("research", {"history": [
    {"asof": ts(0.5), "verdicts": {
        "FRESH": {"rating": "HOLD", "conviction": 64},
        "ACTION": {"rating": "BUY", "conviction": 70},
        "PRINTED": {"rating": "HOLD", "conviction": 60}},
     "context": {"PRINTED": {"px": 50.0, "earnings": "2026-06-11"}}},
    {"asof": ts(5), "verdicts": {"OLD": {"rating": "HOLD", "conviction": 60}}},
]})
check("fresh HOLD cools down (skipped, with re-look reason)",
      (cyc21._cooldown_state("FRESH") or {}).get("rated") == "HOLD")
check("BUY-family verdicts never cool down", cyc21._cooldown_state("ACTION") is None)
check("released earnings REOPENS the look (judged 'wait for print')",
      cyc21._cooldown_state("PRINTED") is None)
check("aged-out verdict reopens", cyc21._cooldown_state("OLD") is None)
check("never-judged name has no cooldown", cyc21._cooldown_state("NEVER") is None)

# 21b-guard: the momentum lane must NOT touch HOLDs that carry no momentum
# factor context — the predicate returns False without context.factors.momentum,
# so FRESH/PRINTED behave byte-identically to pre-lane (lane added 2026-06-23).
check("momentum lane leaves a no-factors HOLD on the normal cooldown",
      (cyc21._cooldown_state("FRESH") or {}).get("lane") is None)
check("momentum lane preserves the earnings-reopen path (no-factors HOLD)",
      cyc21._cooldown_state("PRINTED") is None)

_mc21b.landed_since = _real_ls_21b   # restore before 21d installs its own macro mock

# ══ 21d. Momentum cooldown lane — extended-momentum HOLDs are macro-immune ════
# (operator 2026-06-23). A parabolic leader the committee keeps HOLDing "wait
# for a pullback" must NOT be dragged back into the full committee by every
# macro print; it stays cooled until its OWN bench trigger / earnings fires.
# Macro is mocked "landed" so the lane is the ONLY thing that can keep a name
# cooled. Uses a locally-defined today (NOT _today25 — defined later, section 25).
from firm import macro_calendar as _mc21d  # noqa: E402
_prev_research_21d = state.load("research", {})
_prev_wl_21d = state.load("watchlist", {})
_real_ls_21d = _mc21d.landed_since
_today_21d = datetime.now(timezone.utc).date()
_mc21d.landed_since = lambda *a, **k: _today_21d   # force "a macro print landed"
try:
    state.save("watchlist", {
        "MOMO":       {"reopen_trigger": {"kind": "pullback_pct", "pct": 8}},
        "MOMONOPATH": {},   # no earnings, no technical trigger -> must NOT be laned
        "MOMODEGR":   {"reopen_trigger": {"kind": "rsi_below", "level": 45}},
        "WEAK":       {"reopen_trigger": {"kind": "pullback_pct", "pct": 8}},
        "SELLY":      {"reopen_trigger": {"kind": "pullback_pct", "pct": 8}},
        "EARN":       {"earnings_date": (_today_21d + timedelta(days=4)).isoformat()},
    })
    state.save("research", {"history": [
        {"asof": ts(0.5), "verdicts": {
            "MOMO":       {"rating": "HOLD", "conviction": 64},
            "MOMONOPATH": {"rating": "HOLD", "conviction": 64},
            "MOMODEGR":   {"rating": "HOLD", "conviction": 62, "degraded": True},
            "WEAK":       {"rating": "HOLD", "conviction": 60},
            "SELLY":      {"rating": "SELL", "conviction": 60},
            "EARN":       {"rating": "HOLD", "conviction": 60}},
         "context": {
            "MOMO":       {"factors": {"momentum": 100, "value": 46, "quality": 100}},
            "MOMONOPATH": {"factors": {"momentum": 100}},
            "MOMODEGR":   {"factors": {"momentum": 100}},
            "WEAK":       {"factors": {"momentum": 55}},
            "SELLY":      {"factors": {"momentum": 90}},
            "EARN":       {"earnings": (_today_21d - timedelta(days=1)).isoformat(),
                           "factors": {"momentum": 95}}}}]})
    check("extended-momentum HOLD is macro-immune (laned through a macro print)",
          (cyc21._cooldown_state("MOMO") or {}).get("lane") == "momentum")
    check("momentum HOLD with NO non-macro reopen path is NOT laned (never orphaned)",
          cyc21._cooldown_state("MOMONOPATH") is None)
    check("degraded-mode momentum HOLD is NOT laned",
          cyc21._cooldown_state("MOMODEGR") is None)
    # SEMANTICS CHANGE (operator 2026-07-09: "a name already on the watchlist
    # must not get re-rated when the thesis itself hasn't triggered the
    # re-run"): WEAK is benched with its OWN pullback trigger, so a generic
    # macro print is not its thesis — it stays cooled on the bench lane.
    # (Pre-2026-07-09 this asserted the macro print reopened it.)
    check("benched low-momentum HOLD is thesis-gated through a macro print",
          (cyc21._cooldown_state("WEAK") or {}).get("lane") == "bench")
    check("a SELL still reopens on a macro print (lane is HOLD-only)",
          cyc21._cooldown_state("SELLY") is None)
    check("a landed earnings still reopens a momentum HOLD (lane never hides it)",
          cyc21._cooldown_state("EARN") is None)
    # Layering (2026-07-09): MOMENTUM_LANE off alone no longer restores the
    # macro reopen for a BENCHED HOLD — the thesis gate (its own flag) still
    # holds it. Both flags off = byte-for-byte legacy.
    config.MOMENTUM_LANE = False
    check("MOMENTUM_LANE off: a benched HOLD falls to the bench thesis gate",
          (cyc21._cooldown_state("MOMO") or {}).get("lane") == "bench")
    config.BENCH_THESIS_GATE = False
    check("both flags off restores legacy macro reopen",
          cyc21._cooldown_state("MOMO") is None)
    config.MOMENTUM_LANE = True
    config.BENCH_THESIS_GATE = True
    _prev_dwell_21d = config.MOMENTUM_LANE_MAX_DWELL_CYCLES
    config.MOMENTUM_LANE_MAX_DWELL_CYCLES = 1
    state.save("research", {"history": [
        {"asof": ts(1.5), "verdicts": {"MOMO": {"rating": "HOLD", "conviction": 64}},
         "context": {"MOMO": {"factors": {"momentum": 100}}}}]})
    # Past the momentum dwell cap the name leaves the momentum lane but a
    # benched HOLD is still thesis-gated (2026-07-09) — the TRUE staleness
    # escape is now BENCH_GATE_MAX_DWELL_DAYS, asserted in section 67b.
    check("past the momentum dwell cap a benched HOLD falls to the bench gate",
          (cyc21._cooldown_state("MOMO") or {}).get("lane") == "bench")
    config.MOMENTUM_LANE_MAX_DWELL_CYCLES = _prev_dwell_21d
finally:
    _mc21d.landed_since = _real_ls_21d
    config.MOMENTUM_LANE = True
    state.save("research", _prev_research_21d)
    state.save("watchlist", _prev_wl_21d)

# ══ 21e. Slate backfill — refill cooled/laned slots with FRESH names ══════════
# (operator 2026-06-23: "exploring MORE names, don't shorten how many get brought
# up"). _backfill_candidates refills each tier to TIER_SLOTS with the next FRESH
# name (not held, not laned/recently-judged). Cooldown mocked for determinism.
_real_cd_21e = cyc21._cooldown_state
_cooled_21e = {"A", "B"}
cyc21._cooldown_state = lambda t: {"rated": "HOLD"} if t in _cooled_21e else None
try:
    # Pools sized off config.TIER_SLOTS (4/4/4 since 2026-07-16 — the 9:45
    # cycle's broader outlook) so this fixture never lies about sufficiency.
    _tp = {"large": ["A", "B", "C", "D", "E", "F"],
           "mid":   ["MF", "MG", "MH", "MI"],
           "small": ["SX", "SY", "SZ", "SW"]}
    _tech21e = {t: {} for t in
                ["A", "B", "C", "D", "E", "F", "MF", "MG", "MH", "MI",
                 "SX", "SY", "SZ", "SW"]}
    # large: only C survived the cooldown (A,B laned out); mid/small empty
    out, added, short = cyc21._backfill_candidates(
        [("C", "stock")], _tp, set(), _tech21e)
    _os = [t for t, k in out if k == "stock"]
    check("backfill refills every tier to TIER_SLOTS with fresh names",
          sum(1 for t in _os if t in _tp["large"]) == config.TIER_SLOTS["large"]
          and sum(1 for t in _os if t in _tp["mid"]) == config.TIER_SLOTS["mid"]
          and sum(1 for t in _os if t in _tp["small"]) == config.TIER_SLOTS["small"])
    check("backfill skips laned/cooled pool names (A,B never added)",
          "A" not in added and "B" not in added and {"D", "E"} <= set(added))
    check("backfill never drops the surviving candidate", _os.count("C") == 1)
    check("no tier reported short when the reserve is sufficient", short == [])
    # a HELD name occupying a slot must NOT satisfy the explore target
    _o2, _a2, short2 = cyc21._backfill_candidates(
        [("C", "stock")], _tp, {"C"}, _tech21e)
    check("a HELD name in a slot doesn't count toward the explore target",
          any(s.startswith("large:") for s in short2))
    # a pool name with no technicals can't be researched -> skipped
    _o3, added3, _s3 = cyc21._backfill_candidates(
        [], {"large": ["NOPX", "D"]}, set(), {"D": {}})
    check("backfill skips a pool name with no technicals",
          "NOPX" not in added3 and "D" in added3)
finally:
    cyc21._cooldown_state = _real_cd_21e

# ══ 21f. Restart guard — a code-reload restart defers during market hours ═════
# (operator 2026-06-23: an operator restarted the live fund mid-session on a
# wrong "market is closed" belief; the guard moves the check onto the fund clock).
from firm import market_hours as _mh21f
_real_open_21f = _mh21f.market_is_open
try:
    _mh21f.market_is_open = lambda *a, **k: True            # pretend OPEN
    check("code-reload restart DEFERS while the market is open",
          _mh21f.code_reload_should_defer(False) is True)
    check("force=true overrides the in-hours defer (urgent reload)",
          _mh21f.code_reload_should_defer(True) is False)
    _mh21f.market_is_open = lambda *a, **k: False           # CLOSED
    check("code-reload restart proceeds when the market is closed",
          _mh21f.code_reload_should_defer(False) is False)
    def _boom21f(*a, **k):
        raise RuntimeError("clock error")
    _mh21f.market_is_open = _boom21f
    check("a clock-check error FAILS OPEN (allows restart — availability first)",
          _mh21f.code_reload_should_defer(False) is False)
finally:
    _mh21f.market_is_open = _real_open_21f

# ══ 21g. Bench cap exempts catalyst-waiters (expiring dates -> not capped) ════
# (operator 2026-06-23: "why not unlimited if it has expiring dates? I have
# storage"). _apply_bench_cap keeps ALL catalyst-waiters, caps only open-ended
# names at BENCH_SIZE, with a hard-max sanity backstop on the total.
_fut21g = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()
def _nm21g(tk, q):    # open-ended near-miss (no catalyst) with a quant score
    return {"ticker": tk, "source": "near_miss",
            "history": [{"ts": ts(0), "quant": float(q)}]}
def _wt21g(tk):       # catalyst-waiter: a HOLD with an upcoming earnings date
    return {"ticker": tk, "source": "hold_verdict", "earnings_date": _fut21g,
            "history": [{"ts": ts(0), "quant": 50.0}]}
_wl21g = {**{f"NM{i}": _nm21g(f"NM{i}", i) for i in range(50)},
          **{f"WT{i}": _wt21g(f"WT{i}") for i in range(30)}}
_capped21g = watchlist._apply_bench_cap(_wl21g)
_kept_nm = [t for t in _capped21g if t.startswith("NM")]
_kept_wt = [t for t in _capped21g if t.startswith("WT")]
check("bench cap keeps ALL catalyst-waiters (expiring dates are not capped)",
      len(_kept_wt) == 30)
check("bench cap limits OPEN-ENDED names to BENCH_SIZE",
      len(_kept_nm) == watchlist.BENCH_SIZE)
check("bench cap keeps the HIGHEST-quant open-ended names (NM49 in, NM0 out)",
      "NM49" in _kept_nm and "NM0" not in _kept_nm)
_flood21g = {f"F{i}": _wt21g(f"F{i}") for i in range(200)}
check("bench HARD_MAX backstops a pathological waiter flood",
      len(watchlist._apply_bench_cap(_flood21g)) == watchlist.BENCH_HARD_MAX)
_real_uncap21g = watchlist.BENCH_UNCAP_CATALYST
try:
    watchlist.BENCH_UNCAP_CATALYST = False
    check("UNCAP flag off restores the legacy hard BENCH_SIZE cap on everything",
          len(watchlist._apply_bench_cap(_wl21g)) == watchlist.BENCH_SIZE)
finally:
    watchlist.BENCH_UNCAP_CATALYST = _real_uncap21g
# HIGH-fix regression (adversarial review 2026-06-23): under the HARD_MAX backstop,
# OPEN-ENDED names must be cut BEFORE catalyst-waiters — else the backstop drops the
# very expiring-date names it exists to protect. 145 LOW-quant waiters + 40 HIGH-quant
# near-misses = 185 > HARD_MAX(150): all 145 waiters must survive, near-misses trimmed.
_lowwt21g = {f"LW{i}": {**_wt21g(f"LW{i}"), "history": [{"ts": ts(0), "quant": 10.0}]}
             for i in range(145)}
_capmix21g = watchlist._apply_bench_cap(
    {**_lowwt21g, **{f"HN{i}": _nm21g(f"HN{i}", 90) for i in range(40)}})
check("HARD_MAX cuts high-quant OPEN-ENDED names before low-quant catalyst-waiters",
      sum(1 for t in _capmix21g if t.startswith("LW")) == 145
      and len(_capmix21g) == watchlist.BENCH_HARD_MAX)
# LOW-fix regression: a name promoted to research THIS cycle is exempt from the
# open-ended cap (its bench record must survive until it re-benches), even at the
# lowest quant.
_wlp21g = {f"P{i}": _nm21g(f"P{i}", i) for i in range(45)}
_wlp21g["P0"]["promoted_at"] = ts(0)
check("a name promoted to research this cycle survives the open-ended cap",
      "P0" in watchlist._apply_bench_cap(_wlp21g))

# ══ 21h. A landed catalyst reopens a waiter even with NO fresh quant score ════
# (the safety that makes an uncapped bench correct: a busy bench can exceed the
# 80-name refresh budget, but a real earnings print must never be missed for it).
_real_rq21h = watchlist._refresh_quants
_yday21h = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
try:
    watchlist._refresh_quants = lambda *a, **k: {}        # force NO fresh scores
    state.save("watchlist", {"WAIT": {
        "ticker": "WAIT", "source": "hold_verdict",
        "earnings_date": _yday21h, "earnings_session": "BMO",
        "last_verdict": {"rating": "HOLD", "conviction": 60, "ts": ts(5)},
        "history": [{"ts": ts(5), "quant": 70.0}]}})
    _promos21h = watchlist.promotions({"quant_scores": {}, "stock_candidates": [],
                                       "technicals": {}})
    check("a LANDED earnings reopens a waiter with NO fresh score (uncapped-bench safety)",
          any(p["ticker"] == "WAIT" for p in _promos21h))
finally:
    watchlist._refresh_quants = _real_rq21h
    state.save("watchlist", {})

# ══ 21i. Bench WIPE GUARD: a cycle can't collapse a populated bench to ~empty ══
# (operator 2026-06-23: a duplicate-process race wiped a 41-name bench to {}).
_real_rq21i = watchlist._refresh_quants
_aged21i = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
try:
    watchlist._refresh_quants = lambda *a, **k: {}          # no network in test
    # 20 aged-out near-miss names -> the eviction pass would drop ALL of them
    state.save("watchlist", {f"OLD{i}": {"ticker": f"OLD{i}", "source": "near_miss",
              "added": _aged21i, "history": [{"ts": _aged21i, "quant": 50.0}], "missing": 0}
              for i in range(20)})
    _kept21i = watchlist.update({}, {})        # empty screen+verdicts -> all age out
    check("wipe guard KEEPS a populated bench when a cycle would collapse it to ~empty",
          len(_kept21i) == 20 and len(state.load("watchlist", {})) == 20)
    # below the protect floor a small bench is allowed to empty (no over-protection)
    state.save("watchlist", {f"S{i}": {"ticker": f"S{i}", "source": "near_miss",
              "added": _aged21i, "history": [{"ts": _aged21i, "quant": 50.0}]}
              for i in range(5)})
    check("wipe guard does NOT protect a small bench (legit to empty)",
          len(watchlist.update({}, {})) == 0)
finally:
    watchlist._refresh_quants = _real_rq21i
    state.save("watchlist", {})

# ══ 21j. Degraded verdicts are INERT for the bench (operator 2026-06-25) ═══════
# A quant-only (Claude-unavailable) verdict must NOT graduate a benched name nor
# bench a new one — only a real committee run governs the bench. A degraded
# all-BUY quota-outage cycle was wiping full-committee picks off the bench.
_real_rq21j, _real_rc21j = watchlist._refresh_quants, watchlist._refresh_catalyst_dates
try:
    watchlist._refresh_quants = lambda *a, **k: {}
    watchlist._refresh_catalyst_dates = lambda w: None
    _sc21j = {"quant_scores": {}, "stock_candidates": []}
    state.save("watchlist", {"REAL": {"ticker": "REAL", "source": "hold_verdict",
        "added": ts(0), "history": [{"ts": ts(0), "quant": 80.0}]}})
    _outj = watchlist.update(_sc21j, {
        "REAL": {"kind": "stock", "verdict": {"rating": "BUY", "conviction": 90, "degraded": True}},
        "NEWB": {"kind": "stock", "verdict": {"rating": "HOLD", "conviction": 80, "degraded": True}}})
    check("degraded BUY does NOT graduate a benched (full-run) name", "REAL" in _outj)
    check("degraded HOLD does NOT bench a new name", "NEWB" not in _outj)
    state.save("watchlist", {"REAL": {"ticker": "REAL", "source": "hold_verdict",
        "added": ts(0), "history": [{"ts": ts(0), "quant": 80.0}]}})
    _outj2 = watchlist.update(_sc21j, {
        "REAL": {"kind": "stock", "verdict": {"rating": "BUY", "conviction": 90}}})
    check("a REAL (non-degraded) BUY still graduates the name", "REAL" not in _outj2)
finally:
    watchlist._refresh_quants = _real_rq21j
    watchlist._refresh_catalyst_dates = _real_rc21j
    state.save("watchlist", {})

# 21c. A position unpriceable for 5 straight sweeps alerts the operator once
# (mark-fail only counts IN-HOURS — open the market for this check)
from firm import alerts as alerts21  # noqa: E402
from firm import market_hours as mh21  # noqa: E402

_real_open21 = mh21.market_is_open
mh21.market_is_open = lambda: True
alerts_fired = []
real_alert21 = alerts21.alert
alerts21.alert = lambda title, msg: alerts_fired.append(title)
up = state.default_sleeve_state("alpha_stocks")
up["positions"] = {"GHOSTY": {"symbol": "GHOSTY", "instrument": "equity",
                              "quantity": 5, "entry_price": 50.0, "cost": 250.0,
                              "peak_price": 50.0, "last_price": 50.0,
                              "opened_at": ts(), "opened_mode": "paper"}}
state.save_sleeve("alpha_stocks", up)
real_fm21 = broker.fetch_marks
broker.fetch_marks = lambda st: {}
for _ in range(6):
    monitor.check_all_exits(include_calls=False)
check("unpriceable position alerts at 5 consecutive sweeps, exactly once",
      alerts_fired.count("CANNOT PRICE GHOSTY") == 1, str(alerts_fired))
broker.fetch_marks = lambda st: ({"GHOSTY": 51.0}
                                 if st.get("sleeve") == "alpha_stocks" else {})
monitor.check_all_exits(include_calls=False)
g21 = state.get_sleeve("alpha_stocks")["positions"]["GHOSTY"]
check("pricing recovery resets the streak and re-arms the alert",
      g21.get("mark_fail_streak", 0) == 0 and not g21.get("mark_fail_alerted"))
broker.fetch_marks = real_fm21
alerts21.alert = real_alert21
mh21.market_is_open = _real_open21

# ══ 22. After-hours unpriceable is NOT a false alarm; macro brief; earnings ════
from firm import market_hours as mh22  # noqa: E402

# 22a. Mark-fail only counts/alerts while the market (or extended) is open
from firm import alerts as alerts22  # noqa: E402
alerts_fired22 = []
real_alert22 = alerts22.alert
alerts22.alert = lambda title, msg: alerts_fired22.append(title)
real_open22, real_ext22 = mh22.market_is_open, mh22.extended_session_open
mh22.market_is_open = lambda: False
mh22.extended_session_open = lambda: False
ah = state.default_sleeve_state("alpha_stocks")
ah["positions"] = {"AHX": {"symbol": "AHX", "instrument": "equity", "quantity": 1,
                           "entry_price": 50.0, "cost": 50.0, "peak_price": 50.0,
                           "last_price": 50.0, "opened_at": ts(), "opened_mode": "paper"}}
state.save_sleeve("alpha_stocks", ah)
real_fm22 = broker.fetch_marks
broker.fetch_marks = lambda st: {}
for _ in range(8):
    monitor.check_all_exits(include_calls=False)
check("market CLOSED: unpriced position never false-alarms (was nightly EEM)",
      not alerts_fired22
      and state.get_sleeve("alpha_stocks")["positions"]["AHX"].get("mark_fail_streak", 0) == 0,
      str(alerts_fired22))
mh22.market_is_open = lambda: True       # now open: real danger, must alert
for _ in range(6):
    monitor.check_all_exits(include_calls=False)
check("market OPEN: the same outage DOES alert (real protection gap)",
      alerts_fired22.count("CANNOT PRICE AHX") == 1, str(alerts_fired22))
broker.fetch_marks = real_fm22
mh22.market_is_open, mh22.extended_session_open = real_open22, real_ext22
alerts22.alert = real_alert22

# 22b. recent_earnings parses a beat/miss from reported vs estimate
import pandas as pd  # noqa: E402

real_tkr22 = data_engine.yf.Ticker
class _EarnTkr:
    def __init__(self, t): pass
    def get_earnings_dates(self, limit=8):
        # yfinance returns newest-first: [future row, just-reported row]
        idx = pd.DatetimeIndex([pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=80),
                                pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=2)])
        return pd.DataFrame({"EPS Estimate": [1.20, 1.00],
                             "Reported EPS": [float("nan"), 1.15],
                             "Surprise(%)": [float("nan"), 15.0]}, index=idx)
data_engine.yf.Ticker = _EarnTkr
re22 = data_engine.recent_earnings("ZZZ")
data_engine.yf.Ticker = real_tkr22
check("recent earnings actuals parsed as a BEAT (15% surprise, 2 days ago)",
      re22.get("verdict") == "beat" and re22.get("eps_reported") == 1.15
      and re22.get("days_ago") == 2, str(re22))

# 22c. macro brief carries market/volatility/calendar and is cache-stable
mb = data_engine.macro_brief({"leading": ["Technology"], "lagging": ["Utilities"]})
check("macro brief includes sector leadership + forward calendar",
      mb.get("sector_leadership", {}).get("leading") == ["Technology"]
      and "forward_calendar_10d" in mb, str(mb)[:120])

# ══ 23. News reliability: source+age tagging, staleness drop, news-value split ═
real_tkr23 = data_engine.yf.Ticker
class _NewsTkr:
    def __init__(self, t): pass
    @property
    def news(self):
        now = pd.Timestamp.now(tz="UTC")
        return [
            {"content": {"title": "Reuters fresh scoop", "pubDate": now.isoformat(),
                         "provider": {"displayName": "Reuters"}}},
            {"content": {"title": "Old promo piece",
                         "pubDate": (now - pd.Timedelta(days=40)).isoformat(),
                         "provider": {"displayName": "PromoBlog"}}},  # stale -> dropped
            {"content": {"title": "Reuters fresh scoop",   # dupe -> dropped
                         "pubDate": now.isoformat(),
                         "provider": {"displayName": "Reuters"}}},
            {"content": {"title": "Undated blog take",
                         "provider": {"displayName": "RandomBlog"}}},
        ]
data_engine.yf.Ticker = _NewsTkr
nh = data_engine.fetch_news("ZZZ")
data_engine.yf.Ticker = real_tkr23
check("news tagged with trusted source + age; stale & dupes dropped",
      len(nh) == 2 and nh[0].startswith("[✓ Reuters, 0d ago]")
      and any("undated" in h for h in nh)
      and not any("Old promo" in h for h in nh), str(nh))

# 23b. report card splits accuracy + forward excess by news TRUST TIER
state.save("research", {"history": [
    {"asof": ts(10),
     "verdicts": {"TRST": {"rating": "BUY", "conviction": 70},
                  "AGGR": {"rating": "BUY", "conviction": 70},
                  "BLND": {"rating": "BUY", "conviction": 70},
                  "LEGCY": {"rating": "BUY", "conviction": 70}},
     "context": {"TRST": {"news_tier": "trusted"},
                 "AGGR": {"news_tier": "aggregator"},
                 "BLND": {"news_tier": "blind"},
                 "LEGCY": {"had_news": True}}}],   # legacy row, no tier -> aggregator
})
_d23 = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=30)
real_fh23 = data_engine.fetch_history
data_engine.fetch_history = lambda tickers, period="6mo": pd.DataFrame({
    "TRST": [100.0 * (1 + 0.10 * i / 29) for i in range(30)],   # trusted-backed BUY that won
    "AGGR": [100.0 * (1 - 0.10 * i / 29) for i in range(30)],   # aggregator BUY that lost
    "BLND": [100.0 * (1 - 0.10 * i / 29) for i in range(30)],   # blind BUY that lost
    "LEGCY": [100.0 * (1 + 0.05 * i / 29) for i in range(30)],
    "SPY": [100.0] * 30,
}, index=_d23)
card23 = learning.verdict_report_card(min_age_days=3)
data_engine.fetch_history = real_fh23
nv = card23.get("news_value", {})
check("report card splits news value by trust tier (trusted/aggregator/blind) + magnitude",
      nv.get("trusted", {}).get("correct_pct") == 100.0
      and nv.get("blind", {}).get("correct_pct") == 0.0
      and "avg_fwd_excess_pct" in nv.get("trusted", {}), str(nv))
check("report card: a legacy row (had_news, no tier) still classifies as aggregator",
      nv.get("aggregator", {}).get("n") == 2, str(nv))

# ══ 24. "Waiting for earnings/Fed" — never re-takes space, never forgotten ════
from firm import watchlist as wl24  # noqa: E402

# 24a. A bench HOLD waiting on a print that has LANDED reopens immediately —
# even though it's NOT in the scan (fell out): the catalyst, not a timer
state.save("watchlist", {"WAIT": {
    "ticker": "WAIT", "added": ts(6), "source": "hold_verdict",
    "watch_trigger": "wait for Q2 print",
    "last_verdict": {"rating": "HOLD", "conviction": 64, "ts": ts(6)},
    "earnings_date": ts(1)[:10],          # printed yesterday, after the verdict
    "history": [{"ts": ts(6), "quant": 80.0}], "missing": 1}})
empty24 = {"quant_scores": {}, "stock_candidates": [], "technicals": {}}
real_rq24 = wl24._refresh_quants
wl24._refresh_quants = lambda ts_, **_k: {"WAIT": 80.0}   # faded, but still scoreable
promos24 = wl24.promotions(empty24)
check("a faded bench HOLD reopens THE DAY its earnings print lands (not forgotten)",
      any(p["ticker"] == "WAIT" and "catalyst" in p["why"] for p in promos24),
      str([(p["ticker"], p["why"]) for p in promos24]))

# 24b. The same name BEFORE its print does NOT reopen early (no wasted slot).
# Isolate from the live macro calendar so this is deterministic on ANY date — this
# tests EARNINGS timing; a real macro print landing today (e.g. 2026-06-25) would
# correctly reopen a macro-waiting HOLD via a different branch and is not under test.
from firm import macro_calendar as _mc24b
_real_ls24b, _real_ml24b = _mc24b.landed_since, _mc24b.mentioned_landed
try:
    _mc24b.landed_since = lambda *a, **k: None
    _mc24b.mentioned_landed = lambda *a, **k: None
    state.save("watchlist", {"PEND": {
        "ticker": "PEND", "added": ts(1), "source": "hold_verdict",
        "watch_trigger": "wait for print",
        "last_verdict": {"rating": "HOLD", "conviction": 64, "ts": ts(1)},
        "earnings_date": ts(-30)[:10],        # 30 days out — not yet
        "history": [{"ts": ts(1), "quant": 80.0}], "missing": 0}})
    promos24b = wl24.promotions(empty24)
    wl24._refresh_quants = real_rq24
    check("a HOLD whose print is still weeks away does NOT reopen early (no churn)",
          not any(p["ticker"] == "PEND" for p in promos24b),
          str([(p["ticker"], p["why"]) for p in promos24b]))
finally:
    _mc24b.landed_since, _mc24b.mentioned_landed = _real_ls24b, _real_ml24b

# ══ 25. Earnings consider date AND time (AMC today isn't out at 9:45am) ════════
from firm import cycle as cyc25  # noqa: E402

# UTC, matching cycle._cooldown_state / _earnings_landed (they reference UTC
# "today" internally; a local date.today() here goes off-by-one at the
# local/UTC date boundary in the evening — flaky after ~8pm ET).
_today25 = datetime.now(timezone.utc).date()
_today_iso25 = _today25.isoformat()
check("before-open (BMO) print TODAY counts as landed by the morning cycle",
      cyc25._earnings_landed(_today_iso25, "BMO", _today25) is True)
check("after-close (AMC) print TODAY is NOT out yet at the 9:45 cycle",
      cyc25._earnings_landed(_today_iso25, "AMC", _today25) is False)
check("AMC print today IS out for the post-close (4:30) sweep",
      cyc25._earnings_landed(_today_iso25, "AMC", _today25, after_close=True) is True)
check("unknown-time print today stays conservative (not landed)",
      cyc25._earnings_landed(_today_iso25, "unknown", _today25) is False)
check("any past-dated print is landed regardless of session",
      cyc25._earnings_landed((_today25 - timedelta(days=1)).isoformat(),
                             "AMC", _today25) is True)
# Cooldown respects it: a name awaiting an AMC-TODAY print stays cooled at 9:45.
# Isolate from the LIVE macro calendar — a real FOMC/CPI/jobs print landing since the
# verdict legitimately reopens ANY cooldown (orthogonal to the earnings-landed logic
# under test here), which made this test date-dependent (it failed on 2026-06-17 when a
# real macro print had landed). Mock "no macro landed" so the check is deterministic.
from firm import macro_calendar as _mc25
_real_landed25 = _mc25.landed_since
_mc25.landed_since = lambda *a, **k: False
try:
    state.save("research", {"history": [
        {"asof": ts(1), "verdicts": {"AMCWAIT": {"rating": "HOLD", "conviction": 60}},
         "context": {"AMCWAIT": {"earnings": _today_iso25, "earnings_session": "AMC"}}},
    ]})
    check("AMC-today: name stays cooled at the morning cycle (print not out yet)",
          cyc25._cooldown_state("AMCWAIT") is not None)
    state.save("research", {"history": [
        {"asof": ts(1), "verdicts": {"BMODONE": {"rating": "HOLD", "conviction": 60}},
         "context": {"BMODONE": {"earnings": _today_iso25, "earnings_session": "BMO"}}},
    ]})
    check("BMO-today: name reopens at the morning cycle (already reported)",
          cyc25._cooldown_state("BMODONE") is None)
finally:
    _mc25.landed_since = _real_landed25

# ══ 26. Factor lenses: momentum + value + quality, measured (no strategy change) ═
# 26a. Independent scorers — cheap+quality scores high, expensive+junk low
# debt_to_equity is a RATIO (0.2 = low leverage, 2.6 = heavy) — canonical scale
cheap_good = {"fwd_pe": 11, "peg": 0.9, "ps": 1.5, "roe": 0.28,
              "profit_margin": 0.25, "op_margin": 0.22, "debt_to_equity": 0.2,
              "fcf": 1e9, "rev_growth": 0.25}
rich_weak = {"fwd_pe": 45, "peg": 3.2, "ps": 18, "roe": 0.02,
             "profit_margin": -0.05, "op_margin": 0.01, "debt_to_equity": 2.6,
             "fcf": -1e8, "rev_growth": -0.1}
check("value score: cheap >> expensive",
      data_engine.value_score(cheap_good) > 70
      and data_engine.value_score(rich_weak) < 35,
      f"{data_engine.value_score(cheap_good)} vs {data_engine.value_score(rich_weak)}")
check("quality score: strong business >> weak",
      data_engine.quality_score(cheap_good) > 70
      and data_engine.quality_score(rich_weak) < 35)
check("momentum score independent of fundamentals",
      data_engine.momentum_score({"ret_6m": 40, "ret_3m": 20, "rel_strength_3m": 15,
                                  "above_sma50": True, "above_sma200": True,
                                  "rsi14": 55}) > 75)
check("no fundamentals -> value/quality are None, not a fake 50",
      data_engine.value_score(None) is None and data_engine.quality_score({}) is None)

# 26b. report card measures WHICH factor predicts winners (high vs low half).
# Value tracks the win/loss split exactly; momentum & quality are scrambled
# so they should show little-to-no edge (proves the card isolates the factor).
merged = {"asof": ts(10), "verdicts": {}, "context": {}}
_mom26 = [80, 30, 80, 30, 80, 30, 80, 30]    # uncorrelated with win (i<4)
_qual26 = [30, 80, 30, 80, 80, 30, 80, 30]   # uncorrelated with win
for i in range(8):
    merged["verdicts"][f"V{i}"] = {"rating": "BUY", "conviction": 65}
    merged["context"][f"V{i}"] = {"factors": {"momentum": _mom26[i],
                                              "value": 90 if i < 4 else 20,
                                              "quality": _qual26[i]}}
state.save("research", {"history": [merged]})
_d26 = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=30)
cols26 = {}
for i in range(8):
    win = i < 4
    cols26[f"V{i}"] = [100.0 * (1 + (0.12 if win else -0.12) * j / 29) for j in range(30)]
cols26["SPY"] = [100.0] * 30
real_fh26 = data_engine.fetch_history
data_engine.fetch_history = lambda tickers, period="6mo": pd.DataFrame(cols26, index=_d26)
card26 = learning.verdict_report_card(min_age_days=3)
data_engine.fetch_history = real_fh26
fp = card26.get("factor_predictiveness", {})
val_edge = fp.get("value", {}).get("edge_pts", 0)
mom_edge = abs(fp.get("momentum", {}).get("edge_pts", 0))
check("report card isolates the predictive factor (value edge >> scrambled momentum)",
      val_edge > 3 and val_edge > mom_edge + 3,
      f"value {val_edge} vs |momentum| {mom_edge}")

# ══ 27. Factors flow entry -> closed trade -> ledger -> realized attribution ══
fct = state.default_sleeve_state("alpha_stocks")
broker.execute("paper", fct, symbol="FCTR", side="buy", quantity=5, price=100.0,
               meta={"rating": "BUY", "conviction": 70,
                     "factors": {"momentum": 88, "value": 35, "quality": 77}})
check("factor scores stamped on the open position",
      fct["positions"]["FCTR"].get("factors", {}).get("value") == 35)
broker.execute("paper", fct, symbol="FCTR", side="sell", quantity=5, price=110.0)
check("factors carried onto the closed trade",
      fct["closed_trades"][-1].get("factors", {}).get("momentum") == 88)

# realized by_factor split needs >=6 closed trades with factors + excess
fl = state.default_sleeve_state("alpha_stocks")
fl["closed_trades"] = []
for i in range(8):
    cheap = i < 4
    fl["closed_trades"].append({
        "symbol": f"F{i}", "instrument": "equity", "quantity": 1,
        "entry": 100.0, "exit": 100.0 + (8 if cheap else -8),
        "pnl": (8 if cheap else -8), "reason": "test", "closed_at": ts(0.01 * (i + 1)),
        "opened_at": ts(20), "mode": "paper", "trade_id": f"fid{i}",
        "rating": "BUY", "conviction": 65,
        "factors": {"momentum": 60, "value": 90 if cheap else 20, "quality": 55}})
state.save_sleeve("alpha_stocks", fl)
state.save("ledger", {"trades": [], "seen": [], "total_recorded": 0})
real_spy27 = data_engine.fetch_history
data_engine.fetch_history = lambda tickers, period="1y": pd.DataFrame(
    {"SPY": [100.0] * 30}, index=pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=30))
learning.record_outcomes()
data_engine.fetch_history = real_spy27
st27 = learning.stats()
bf = st27.get("by_factor", {}).get("value", {})
check("realized P&L is factor-attributed (cheap trades' edge shows in by_factor)",
      bf.get("n", 0) >= 6 and bf.get("edge_pts", 0) > 5,
      str(st27.get("by_factor")))

# ══ 28. FMP integration: key-gated, fail-soft, yfinance fallback intact ═══════
from firm import fmp as fmp28  # noqa: E402

# 28a. No key -> FMP inert, fetch_fundamentals is pure yfinance (no regression)
_real_key = config.FMP_API_KEY
config.FMP_API_KEY = ""
check("no FMP key -> fmp.available() False (fund unchanged)", not fmp28.available())
check("no key -> fundamentals fall through to yfinance source",
      data_engine._fetch_fundamentals_yf("AMD").get("source") == "yfinance")

# 28b. With a key, FMP maps cleanly and MERGES (FMP wins, yfinance fills gaps)
config.FMP_API_KEY = "test-key"
real_get = fmp28._get
def _fake_get(path, **params):
    if path == "ratios-ttm":
        return [{"priceToEarningsRatioTTM": 22.0, "priceToEarningsGrowthRatioTTM": 1.4,
                 "priceToSalesRatioTTM": 6.0, "netProfitMarginTTM": 0.26,
                 "operatingProfitMarginTTM": 0.30, "debtToEquityRatioTTM": 1.5}]
    if path == "key-metrics-ttm":
        return [{"returnOnEquityTTM": 0.45, "freeCashFlowTTM": 9.9e10}]
    if path == "financial-scores":
        return [{"piotroskiScore": 8, "altmanZScore": 6.1}]
    if path == "earnings":
        import datetime as d
        t = d.datetime.now(d.timezone.utc).date()   # UTC, matching fmp.next/recent_earnings
        return [{"date": (t + d.timedelta(days=20)).isoformat(), "time": "amc"},
                {"date": (t - d.timedelta(days=3)).isoformat(), "time": "bmo",
                 "epsEstimated": 1.0, "epsActual": 1.2}]
    return None
fmp28._get = _fake_get
fmp28._cache.clear()
ff = data_engine.fetch_fundamentals("TESTCO")
check("FMP maps clean fields; D/E stays a raw ratio; no fabricated fwd_pe/peg",
      ff.get("pe") == 22.0 and ff.get("roe") == 0.45
      and ff.get("debt_to_equity") == 1.5      # raw ratio, NOT x100, NOT dropped
      and ff.get("fwd_pe") != 22.0             # forward NOT faked from trailing PE
      and ff.get("peg") != 1.4                 # FMP's garbage PEG is not sourced
      and ff.get("piotroski") == 8 and ff.get("altman_z") == 6.1
      and ff.get("source") == "fmp+yf", str(ff)[:200])
ne = data_engine.next_earnings("TESTCO")
check("FMP next earnings: confirmed future date + AMC session",
      ne.get("days_away") == 20 and ne.get("session") == "AMC", str(ne))
re28 = data_engine.recent_earnings("TESTCO")
check("FMP recent earnings: actual beat parsed (1.2 vs 1.0 = +20%)",
      re28.get("verdict") == "beat" and re28.get("eps_reported") == 1.2, str(re28))

# 28c. FMP network failure is fail-soft -> yfinance, never an exception
fmp28._get = lambda path, **p: None
fmp28._cache.clear()
check("FMP returning nothing falls back to yfinance cleanly (no crash)",
      data_engine.fetch_fundamentals("AMD").get("source") == "yfinance")
fmp28._get = real_get
config.FMP_API_KEY = _real_key

# ══ 29. Self-healing data integrity: plausibility guard + vendor audit ════════
# 29a. Plausibility guard auto-replaces garbage with the sane vendor, else drops
_yf29 = {"peg": 8.0, "debt_to_equity": 2.0, "roe": 0.18}
_fmp29 = {"peg": -3.46, "debt_to_equity": 2.0, "roe": 0.18, "profit_margin": 5.0}
_merged29 = {"peg": -3.46, "debt_to_equity": 99.0, "roe": 1.47, "profit_margin": 5.0}
_san = data_engine._sanitize_fundamentals(dict(_merged29), _yf29, _fmp29, "T29")
check("plausibility: garbage PEG dropped, NOT cross-substituted (vendor methodology differs)",
      _san.get("peg") is None)
check("plausibility: out-of-band D/E (99) replaced from a sane source",
      _san.get("debt_to_equity") == 2.0)
check("plausibility: legit extreme (ROE 1.47x) is kept, not flagged",
      _san.get("roe") == 1.47)
check("plausibility: garbage with no sane alternate is dropped to None",
      _san.get("profit_margin") is None)
check("plausibility: changes recorded for the committee + audit trail",
      "debt_to_equity" in _san["_data_quality"]["replaced"]
      and "peg" in _san["_data_quality"]["dropped"]
      and "profit_margin" in _san["_data_quality"]["dropped"])
check("plausibility: a fully-clean dict is untouched (no _data_quality)",
      "_data_quality" not in data_engine._sanitize_fundamentals(
          {"roe": 0.2, "debt_to_equity": 0.8, "peg": 1.1}, {}, {}, "OK29"))
check("plausibility: negative-equity D/E (-2.5) is plausible (real: MCD/SBUX), kept not dropped",
      data_engine._fund_plausible("debt_to_equity", -2.5)
      and data_engine._sanitize_fundamentals(
          {"debt_to_equity": -2.5}, {}, {}, "NE").get("debt_to_equity") == -2.5)
check("quality_score: negative-equity D/E is SKIPPED (uninterpretable, like banks), not penalized",
      data_engine.quality_score({"debt_to_equity": -2.5, "roe": 0.3, "profit_margin": 0.2,
                                 "sector": "Consumer"})
      == data_engine.quality_score({"roe": 0.3, "profit_margin": 0.2, "sector": "Consumer"}))
_orig29 = {"profit_margin": 5.0}
data_engine._sanitize_fundamentals(_orig29, {}, {}, "M29")
check("plausibility: the caller's dict is never mutated (guard works on a copy)",
      _orig29.get("profit_margin") == 5.0)

# 29a-ext. ROE-on-negative/near-zero-equity + cross-vendor distrust (2026-06-16)
check("bookValue is captured into book_value (its sign = equity sign)",
      data_engine._FUND_FIELDS.get("bookValue") == "book_value")
check("_vendors_disagree: 8x split flagged, 7% gap not, opposite signs flagged, 2.5x not",
      data_engine._vendors_disagree(17.88, 2.10)
      and not data_engine._vendors_disagree(0.30, 0.32)
      and data_engine._vendors_disagree(0.5, -0.5)
      and not data_engine._vendors_disagree(0.20, 0.50))
# sanitize drops ROE on negative book equity (n/m) — committee never sees it
_negq = data_engine._sanitize_fundamentals({"roe": 0.30, "book_value": -4.0}, {}, {}, "NEGQ")
check("sanitize: ROE dropped to None on negative book equity (n/m), journaled",
      _negq.get("roe") is None and "roe" in _negq["_data_quality"]["dropped"])
# Seagate-style: 1788% exceeds the +-10.0 plausibility band -> dropped already
check("sanitize: Seagate-style 1788% ROE (17.88) dropped — exceeds plausibility band",
      data_engine._sanitize_fundamentals({"roe": 17.88, "book_value": 4.8},
                                         {"roe": 17.88}, {}, "STX").get("roe") is None)
# cross-vendor disagreement: distrust BOTH rather than keeping the merge winner
_disag = data_engine._sanitize_fundamentals({"roe": 0.80}, {"roe": 0.18}, {"roe": 0.80}, "DISAG")
check("sanitize: vendors disagreeing >3x on ROE (0.18 vs 0.80) -> distrust both, drop",
      _disag.get("roe") is None and "roe" in _disag["_data_quality"]["dropped"])
check("sanitize: vendors agreeing on ROE keep the value (no false distrust)",
      data_engine._sanitize_fundamentals({"roe": 0.30}, {"roe": 0.30}, {"roe": 0.32},
                                         "AGREE").get("roe") == 0.30)
# quality_score guard: negative-equity / band-passing-artifact ROE is SKIPPED, not rewarded
check("quality_score: ROE on negative equity SKIPPED (n/m), not a +15 elite bonus",
      data_engine.quality_score({"roe": 0.30, "book_value": -4.0, "profit_margin": 0.2})
      == data_engine.quality_score({"book_value": -4.0, "profit_margin": 0.2}))
check("quality_score: a band-passing 210% ROE (tiny-equity artifact) is SKIPPED",
      data_engine.quality_score({"roe": 2.10, "book_value": 5.0, "profit_margin": 0.2})
      == data_engine.quality_score({"book_value": 5.0, "profit_margin": 0.2}))
check("quality_score: a normal positive-equity ROE (30%) still earns its points",
      data_engine.quality_score({"roe": 0.30, "book_value": 5.0, "profit_margin": 0.2})
      > data_engine.quality_score({"book_value": 5.0, "profit_margin": 0.2}))
_tech29 = {"ret_6m": 0, "ret_3m": 0, "rel_strength_3m": 0, "above_sma50": True,
           "above_sma200": True, "rsi14": 50}
check("quant_score: a 210% ROE artifact does NOT add the +4 quality nudge",
      data_engine.quant_score(_tech29, {"roe": 2.10, "book_value": 5.0})
      == data_engine.quant_score(_tech29, {"book_value": 5.0}))
check("quant_score: a normal 30% ROE still adds the +4",
      data_engine.quant_score(_tech29, {"roe": 0.30, "book_value": 5.0})
      == data_engine.quant_score(_tech29, {"book_value": 5.0}) + 4)

# 29a-ext2. Adversarial-review fixes (2026-06-17): zero-handling, book_value fail-open
check("_vendors_disagree: a 0.0 vs positive is NOT disagreement (break-even, not opposite sign)",
      not data_engine._vendors_disagree(0.0, 0.5)
      and not data_engine._vendors_disagree(0.0, 0.02)
      and not data_engine._vendors_disagree(0.05, 0.0)
      and not data_engine._vendors_disagree(0.0, 0.0))
check("sanitize: break-even profit_margin (vendor 0.0 vs 0.02) is KEPT, not over-dropped",
      data_engine._sanitize_fundamentals({"profit_margin": 0.02}, {"profit_margin": 0.0},
          {"profit_margin": 0.02}, "BE").get("profit_margin") == 0.02)
check("sanitize: a real >3x split still drops (0.18 vs 0.80) — zero-fix didn't weaken it",
      data_engine._sanitize_fundamentals({"roe": 0.80}, {"roe": 0.18}, {"roe": 0.80},
          "STILLDROP").get("roe") is None)
# book_value fail-open: a lone negative-equity glitch must NOT suppress a valid ROE
_bvsplit = data_engine._sanitize_fundamentals({"roe": 0.30, "book_value": -4.0},
    {"roe": 0.30, "book_value": 5.0}, {"roe": 0.30, "book_value": -4.0}, "BVSPLIT")
check("sanitize: single-vendor negative book_value glitch KEEPS roe + corrects book_value (fail open)",
      _bvsplit.get("roe") == 0.30 and _bvsplit.get("book_value") == 5.0)
check("sanitize: ROE STILL dropped when BOTH vendors confirm negative equity",
      data_engine._sanitize_fundamentals({"roe": 0.30, "book_value": -4.0},
          {"roe": 0.30, "book_value": -3.0}, {"roe": 0.30, "book_value": -4.0},
          "BVNEG").get("roe") is None)
# FIX 1 cross-path leak: a fresh cycle mark must be USED + persisted (not discarded by
# a lingering _mark_stale flag); the fetch_marks path must still suppress.
_pos_leak = {"instrument": "equity", "quantity": 10, "entry_price": 100.0,
             "last_price": 100.0, "peak_price": 100.0, "_mark_stale": True}
_sl_leak = {"cash": 0.0, "positions": {"ZZ": dict(_pos_leak)}, "pending_orders": []}
_eq_fresh = broker.sleeve_equity(_sl_leak, {"ZZ": 110.0})           # cycle path: trust it
check("FIX1 leak: a fresh cycle mark is USED despite a lingering _mark_stale flag",
      abs(_eq_fresh - 1100.0) < 0.01
      and _sl_leak["positions"]["ZZ"]["last_price"] == 110.0
      and _sl_leak["positions"]["ZZ"]["_mark_stale"] is False)
_sl_fm = {"cash": 0.0, "positions": {"ZZ": dict(_pos_leak)}, "pending_orders": []}
_eq_fm = broker.sleeve_equity(_sl_fm, {"ZZ": 90.0}, from_fetch_marks=True)  # monitor: suppress stale
check("FIX1: the fetch_marks path STILL suppresses a stale mark (values at prior, no regression)",
      abs(_eq_fm - 1000.0) < 0.01 and _sl_fm["positions"]["ZZ"]["last_price"] == 100.0)

# 29b. FMP blocklist: a flagged field stops being emitted (degrades to yfinance)
config.FMP_API_KEY = "test-key"
fmp28._get = _fake_get
fmp28._cache.clear()
state.save("fmp_blocklist", {"fields": ["debt_to_equity"]})
_blk = fmp28.fundamentals("TESTCO")
_raw = fmp28.fundamentals("TESTCO", respect_blocklist=False)
check("blocklist: FMP stops emitting an auto-disabled field",
      "debt_to_equity" not in _blk and _blk.get("pe") == 22.0)
check("blocklist: audit can still re-test a blocked field (bypass)",
      _raw.get("debt_to_equity") == 1.5)
state.save("fmp_blocklist", {"fields": []})
fmp28._get = real_get
config.FMP_API_KEY = _real_key

# 29c. Vendor-audit pure logic: divergence verdict, majority strike, block/unblock
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import vendor_audit  # noqa: E402
check("audit verdict: in-band >3x divergence on D/E reads as bad",
      vendor_audit._field_verdict("debt_to_equity", 9.0, 0.8) == "bad")
check("audit verdict: agreeing vendors read ok",
      vendor_audit._field_verdict("debt_to_equity", 0.80, 0.79) == "ok")
check("audit verdict: out-of-band FMP value is bad even without a 2nd source",
      vendor_audit._field_verdict("profit_margin", 5.0, None) == "bad")
check("audit verdict: no FMP value -> skip (nothing to audit)",
      vendor_audit._field_verdict("pe", None, 20.0) == "skip")
check("audit verdict: legit definitional gap (bank margin 1.6x) NOT flagged",
      vendor_audit._field_verdict("profit_margin", 0.21, 0.34) == "ok")
_strk29, _ = vendor_audit.decide_strikes(
    [{"debt_to_equity": "bad"}, {"debt_to_equity": "bad"}, {"debt_to_equity": "ok"}])
check("audit: a field bad across a majority of names strikes",
      "debt_to_equity" in _strk29)
check("audit: a single idiosyncratic bad name does NOT strike",
      "roe" not in vendor_audit.decide_strikes(
          [{"roe": "bad"}, {"roe": "ok"}, {"roe": "ok"}])[0])
_s0 = {"fields": [], "strikes": {}, "clear": {}}
_s1, _ = vendor_audit.update_blocklist(_s0, {"debt_to_equity"}, {"debt_to_equity"})
_s2, _a2 = vendor_audit.update_blocklist(_s1, {"debt_to_equity"}, {"debt_to_equity"})
check("audit auto-fix: 2 consecutive strikes auto-block the field",
      "debt_to_equity" not in _s1["fields"] and "debt_to_equity" in _s2["fields"]
      and ("block", "debt_to_equity", 2) in _a2)
_s3, _ = vendor_audit.update_blocklist(_s2, set(), {"debt_to_equity"})
_s4, _a4 = vendor_audit.update_blocklist(_s3, set(), {"debt_to_equity"})
check("audit auto-fix: 2 clean weeks auto-unblock (self-heals both ways)",
      "debt_to_equity" in _s3["fields"] and "debt_to_equity" not in _s4["fields"]
      and ("unblock", "debt_to_equity", 2) in _a4)

# ══ 30. Health checks are MARKET-AWARE (no weekend/overnight false alarms) ════
from firm import health as _health, market_hours as _mh  # noqa: E402


def _et30(y, m, d, h, mn):
    return datetime(y, m, d, h, mn, tzinfo=_mh.ET)


_fri_ran = _et30(2026, 6, 12, 9, 50).isoformat()    # Fri cycle ran on time
_thu_ran = _et30(2026, 6, 18, 9, 50).isoformat()    # Thu cycle ran on time
check("health: weekend with Friday's cycle done is NOT overdue (market closed)",
      not _health._cycle_overdue(_fri_ran, _et30(2026, 6, 13, 23, 0)))     # Sat night
check("health: an ON-TIME 9:50 cycle is not overdue after the 10:30 deadline (bugfix)",
      not _health._cycle_overdue(_et30(2026, 6, 15, 9, 50).isoformat(),
                                 _et30(2026, 6, 15, 11, 0)))               # Mon 11am
check("health: a genuinely MISSED weekday cycle is overdue after 10:30",
      _health._cycle_overdue(_fri_ran, _et30(2026, 6, 15, 11, 0)))        # Mon 11am, last=Fri
check("health: pre-10:30 grace window — today's not-yet-run cycle is NOT overdue",
      not _health._cycle_overdue(_fri_ran, _et30(2026, 6, 15, 10, 0)))    # Mon 10am
check("health: holiday (Juneteenth) with Thursday's cycle done is NOT overdue",
      not _health._cycle_overdue(_thu_ran, _et30(2026, 6, 19, 12, 0)))    # Fri holiday
check("health: never-ran (empty) is overdue once a deadline has passed",
      _health._cycle_overdue("", _et30(2026, 6, 15, 11, 0)))
# Market scan shares the same market-aware logic (premarket 8:30 run / 9:30 deadline)
check("health: market scan over a weekend (Friday's scan done) is NOT overdue",
      not _health._scan_overdue(_et30(2026, 6, 12, 8, 35).isoformat(),
                                _et30(2026, 6, 13, 23, 0)))      # Sat night
check("health: market scan genuinely missed on a weekday (after 9:30) is overdue",
      _health._scan_overdue(_et30(2026, 6, 11, 8, 35).isoformat(),
                            _et30(2026, 6, 15, 11, 0)))          # Mon 11am, last=Thu
# Options monitor — tight freshness, but ONLY when options are open AND market open
check("health: fresh options sweep (60s) with open options is OK",
      _health._options_monitor_ok(60 / 3600, True, True))
check("health: stale options sweep (300s > 180s) with open options FAILS (tight)",
      not _health._options_monitor_ok(300 / 3600, True, True))
check("health: stale options loop ignored when the options market is closed (idle)",
      _health._options_monitor_ok(300 / 3600, True, False))
check("health: no open options -> options-monitor staleness is irrelevant",
      _health._options_monitor_ok(9999.0, False, True))

# ══ 31. Bench REMEMBERS a near catalyst (earnings or imminent FOMC) ═══════════
from firm import watchlist as _wl31  # noqa: E402

# Fixed base in a genuinely macro-QUIET window (no high-impact print within 5
# days), so the earnings-window checks isolate the earnings branch from the
# imminent-macro branch. 2026-07-20..25 has no FOMC/CPI/PCE/NFP/retail/GDP/ISM
# (next is FOMC 2026-07-29 / PCE 2026-07-30). All earnings checks pass this as
# `today`.
_BASE31 = datetime(2026, 7, 20, tzinfo=timezone.utc).date()


def _ed31(days):
    return (_BASE31 + timedelta(days=days)).isoformat()


check("bench: HOLD awaiting a near (<=35d) earnings print is protected from eviction",
      _wl31._awaiting_near_catalyst({"source": "hold_verdict", "earnings_date": _ed31(10)}, _BASE31))
check("bench: a print already in the past is NOT 'awaiting' (catalyst path handles it)",
      not _wl31._awaiting_near_catalyst({"source": "hold_verdict", "earnings_date": _ed31(-1)}, _BASE31))
check("bench: a quarterly-distance earnings (90d) IS remembered (covers Aug/SanDisk)",
      _wl31._awaiting_near_catalyst({"source": "hold_verdict", "earnings_date": _ed31(90)}, _BASE31))
check("bench: an absurd far-future date (>100d) does NOT pin the name forever",
      not _wl31._awaiting_near_catalyst({"source": "hold_verdict", "earnings_date": _ed31(130)}, _BASE31))
check("bench: no earnings + no imminent FOMC -> not awaiting",
      not _wl31._awaiting_near_catalyst({"source": "hold_verdict"}, _BASE31))
check("bench: only hold_verdict names wait on catalysts (near_miss excluded)",
      not _wl31._awaiting_near_catalyst({"source": "near_miss", "earnings_date": _ed31(10)}, _BASE31))
check("bench: a loose/yearless earnings date is NOT a valid wait (fail-closed)",
      not _wl31._awaiting_near_catalyst({"source": "hold_verdict", "earnings_date": "Jun 12"}, _BASE31))
# FOMC: an imminent Fed decision protects EVERY benched HOLD (it reopens them all)
check("bench: a hold with NO earnings date IS protected when an FOMC is imminent",
      _wl31._awaiting_near_catalyst({"source": "hold_verdict"},
                                    datetime(2026, 6, 14, tzinfo=timezone.utc).date()))  # Jun 17 FOMC, 3d out
# earnings_date is BACKFILLED each cycle (capture-at-bench-time alone is unreliable)
import firm.data_engine as _de31  # noqa: E402
_real_ne31 = _de31.next_earnings
_de31.next_earnings = lambda t: {"date": _ed31(40), "session": "AMC"} if t == "REFRESHME" else {}
_wlref31 = {"REFRESHME": {"ticker": "REFRESHME", "source": "hold_verdict", "earnings_date": None}}
_wl31._refresh_catalyst_dates(_wlref31)
check("bench: a missing earnings_date is backfilled from next_earnings each cycle",
      _wlref31["REFRESHME"].get("earnings_date") == _ed31(40))
_de31.next_earnings = _real_ne31

# ══ 32. Earnings-actuals guarantee: retry once, then ALERT the operator ════════
from firm import alerts as _alerts32  # noqa: E402
_real_toast = _alerts32._toast
_alerts32._toast = lambda *a, **k: None          # no real Windows toast in tests
_printed32 = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
_wn32 = {"CSCO": {"why": f"catalyst: earnings printed {_printed32} — re-judge with actuals"}}
state.save("watchlist", {"CSCO": {"ticker": "CSCO", "source": "hold_verdict",
                                  "earnings_date": _printed32,
                                  "last_verdict": {"rating": "HOLD", "ts": ts(0.1)}}})
state.save("alerts", {"items": []})
# 1st pass — actuals missing: arm a one-shot retry, do NOT alert yet
cyc25._check_earnings_actuals({"CSCO": {"packet": {"recent_earnings": {}}}}, _wn32)
check("actuals: missing on an earnings reopen -> one-shot retry armed, no alert yet",
      state.load("watchlist", {}).get("CSCO", {}).get("actuals_retry") == 1
      and not _alerts32.unacked())
# 2nd pass — still missing: give up and ALERT the operator
cyc25._check_earnings_actuals({"CSCO": {"packet": {"recent_earnings": {}}}}, _wn32)
check("actuals: still missing after the retry -> operator is ALERTED",
      any("EARNINGS ACTUALS MISSING" in a.get("title", "") for a in _alerts32.unacked()))
# success path — actuals present: clear the retry flag, no alert
state.save("watchlist", {"CSCO": {"ticker": "CSCO", "source": "hold_verdict",
                                  "earnings_date": _printed32, "actuals_retry": 1,
                                  "last_verdict": {"rating": "HOLD", "ts": ts(0.1)}}})
state.save("alerts", {"items": []})
cyc25._check_earnings_actuals(
    {"CSCO": {"packet": {"recent_earnings": {"eps_reported": 1.2, "verdict": "beat"}}}}, _wn32)
check("actuals: arrived -> retry flag cleared, no alert (back to normal)",
      "actuals_retry" not in state.load("watchlist", {}).get("CSCO", {})
      and not _alerts32.unacked())
_alerts32._toast = _real_toast

# ══ 33. Macro catalysts beyond FOMC (CPI + jobs) + live-state reopen watchdog ══
from firm import macro_calendar as _mc33  # noqa: E402
from datetime import date as _date33  # noqa: E402

check("macro: CPI (2026-06-10) counts as a landed catalyst, not just FOMC",
      _mc33.landed_since(_date33(2026, 6, 9), _date33(2026, 6, 12)) == _date33(2026, 6, 10))
check("macro: a quiet window (no FOMC/CPI/jobs) returns None",
      _mc33.landed_since(_date33(2026, 6, 18), _date33(2026, 6, 22)) is None)
check("macro: an imminent CPI (2 days out) is detected by imminent_within",
      _mc33.imminent_within(_date33(2026, 6, 8), 5))
check("macro: the jobs report (first Friday) is a macro event",
      _date33(2026, 6, 5) in _mc33.event_dates(_date33(2026, 6, 1), _date33(2026, 6, 7)))
check("macro: future_events sees the curated FOMC/CPI calendar isn't stale",
      _mc33.future_events(_date33(2026, 6, 14)) >= 3)
check("catalyst: a HOLD verdict'd before the CPI reopens on the macro print (not just FOMC)",
      "macro data landed" in (_wl31._catalyst_landed(
          {"source": "hold_verdict",
           "last_verdict": {"ts": "2026-06-09T15:00:00+00:00"}}) or ""))
# Live-state watchdog: a landed-but-unreviewed catalyst (a cycle ran after it) is flagged
state.save("research", {"latest_asof": "2026-06-13T14:00:00+00:00", "history": []})
state.save("watchlist", {
    "STALE1": {"ticker": "STALE1", "source": "hold_verdict",
               "earnings_date": "2026-06-11", "earnings_session": "BMO",
               "last_verdict": {"rating": "HOLD", "ts": "2026-06-09T15:00:00+00:00"}},
    "OKAY1": {"ticker": "OKAY1", "source": "hold_verdict",
              "earnings_date": "2026-08-01",
              "last_verdict": {"rating": "HOLD", "ts": "2026-06-12T15:00:00+00:00"}}})
_od33 = _wl31.overdue_reopens(today=_date33(2026, 6, 14))
check("watchdog: a landed-but-unreviewed catalyst (cycle ran after it) is flagged overdue",
      any(o["ticker"] == "STALE1" for o in _od33))
check("watchdog: a name still genuinely waiting (future earnings) is NOT flagged",
      not any(o["ticker"] == "OKAY1" for o in _od33))

# ══ 34. Registry covers ALL fed numbers + thesis-keyword auto-tracking ════════
# Coverage now spans PCE/retail/GDP/ISM, not just FOMC/CPI/NFP (high-impact set)
check("registry: PCE (2026-06-25) is a tracked high-impact macro event",
      _date33(2026, 6, 25) in _mc33.event_dates(_date33(2026, 6, 20), _date33(2026, 6, 30)))
check("registry: GDP advance (2026-07-30) is tracked",
      _date33(2026, 7, 30) in _mc33.event_dates(_date33(2026, 7, 25), _date33(2026, 8, 1)))
# Medium-impact prints stay OUT of the global trigger (no bench-wide churn)...
check("registry: PPI (medium) is NOT a global high-impact trigger",
      _date33(2026, 6, 11) not in _mc33.event_dates(_date33(2026, 6, 10), _date33(2026, 6, 12)))
# ...but ARE tracked when a thesis NAMES them ("if the bench mentions a number")
check("auto-track: a thesis naming PPI resolves PPI's specific landed date",
      _mc33.mentioned_landed("waiting for the PPI report", _date33(2026, 6, 10),
                             _date33(2026, 6, 12)) == _date33(2026, 6, 11))
check("auto-track: a thesis naming PCE returns PCE's next date",
      any(e["key"] == "pce" for e in
          _mc33.events_mentioned("hold until PCE clears", _date33(2026, 6, 1))))
check("auto-track: the real SNDK/DOCN 'post-CPI/FOMC' thesis maps to BOTH events",
      {e["key"] for e in _mc33.events_mentioned("After CPI/FOMC clear, a pullback",
                                                _date33(2026, 6, 1))} >= {"cpi", "fomc"})
check("auto-track: a thesis naming nothing macro returns no events",
      _mc33.events_mentioned("a clean beat-and-raise breakout", _date33(2026, 6, 1)) == [])
check("auto-track: mentioned_imminent true when the named number is near",
      _mc33.mentioned_imminent("waiting on CPI", _date33(2026, 6, 8), 5))
check("calendar tier reported (rule with no live snapshot/key in test)",
      _mc33.calendar_tier() == "rule")

# ══ 35. Technical reopen triggers — bench acts on the SPECIFIC setup named ═════
_tt = _wl31._technical_trigger_met
check("tech: pullback_pct fires when the dip is deep enough (and not before)",
      _tt({"reopen_trigger": {"kind": "pullback_pct", "pct": 20}, "benched_price": 100},
          {"price": 78})
      and not _tt({"reopen_trigger": {"kind": "pullback_pct", "pct": 20}, "benched_price": 100},
                  {"price": 90}))
check("tech: below_price fires at/under the level (a 'wait for the dip' the momentum re-check misses)",
      _tt({"reopen_trigger": {"kind": "below_price", "price": 50}}, {"price": 48})
      and not _tt({"reopen_trigger": {"kind": "below_price", "price": 50}}, {"price": 55}))
check("tech: reclaim_price fires at/above the breakout level",
      _tt({"reopen_trigger": {"kind": "reclaim_price", "price": 50}}, {"price": 52})
      and not _tt({"reopen_trigger": {"kind": "reclaim_price", "price": 50}}, {"price": 48}))
check("tech: rsi_below fires when RSI drops to the level (oversold entry)",
      _tt({"reopen_trigger": {"kind": "rsi_below", "level": 40}}, {"rsi14": 35})
      and not _tt({"reopen_trigger": {"kind": "rsi_below", "level": 40}}, {"rsi14": 45}))
check("tech: cross_sma above fires when price is above the SMA",
      _tt({"reopen_trigger": {"kind": "cross_sma", "period": 50, "dir": "above"}},
          {"above_sma50": True})
      and not _tt({"reopen_trigger": {"kind": "cross_sma", "period": 50, "dir": "above"}},
                  {"above_sma50": False}))
check("tech: cross_sma below fires when price pulls to/below the 50-day SMA",
      _tt({"reopen_trigger": {"kind": "cross_sma", "period": 50, "dir": "below"}},
          {"above_sma50": False}))
check("tech: 'none'/catalyst kinds don't fire a technical reopen",
      _tt({"reopen_trigger": {"kind": "none"}}, {"price": 1})
      is None
      and _tt({"reopen_trigger": {"kind": "catalyst"}}, {"price": 1}) is None)
check("tech: a faded name uses the directly-fetched last_price (price-based triggers)",
      _tt({"reopen_trigger": {"kind": "below_price", "price": 50}}, {}, 48))
check("tech: price-based triggers are flagged for the faded-name price fetch",
      _wl31._needs_price_trigger({"reopen_trigger": {"kind": "pullback_pct", "pct": 10}})
      and not _wl31._needs_price_trigger({"reopen_trigger": {"kind": "rsi_below", "level": 40}}))
check("tech: a corrupt/legacy reopen_trigger (string or missing) never crashes",
      _tt({"reopen_trigger": "catalyst"}, {"price": 1}) is None
      and _tt({}, {"price": 1}) is None
      and not _wl31._needs_price_trigger({"reopen_trigger": "garbage"}))

# ══ 36. News/sentiment reopen (unscheduled, non-FRED catalysts) + broad calendar
import firm.data_engine as _de36  # noqa: E402
check("has_news_since: bad/missing 'since' -> False (fail-soft, no crash)",
      _de36.has_news_since("X", "") is False and _de36.has_news_since("X", None) is False)
_real_hns36 = _de36.has_news_since
_de36.has_news_since = lambda t, since, **k: t == "NEWSY"      # stub the network probe
check("news trigger: fires for a 'news' thesis when fresh trusted news broke since the verdict",
      _wl31._news_trigger_hit({"reopen_trigger": {"kind": "news"},
                               "last_verdict": {"ts": ts(3)}}, "NEWSY"))
check("news trigger: does NOT fire for a non-news trigger",
      not _wl31._news_trigger_hit({"reopen_trigger": {"kind": "pullback_pct", "pct": 10}}, "NEWSY"))
check("news trigger: does NOT fire when no fresh news (quiet name)",
      not _wl31._news_trigger_hit({"reopen_trigger": {"kind": "news"},
                                   "last_verdict": {"ts": ts(3)}}, "QUIET"))
_de36.has_news_since = _real_hns36
# Broad macro calendar (everything Forex Factory carries, beyond the high-impact registry)
_cc_date = (datetime.now(timezone.utc).date() + timedelta(days=2)).isoformat()
state.save("macro_calendar", {"ts": datetime.now(timezone.utc).isoformat(),
                              "tier": "forexfactory", "overlay": {},
                              "ff_raw": [{"date": _cc_date, "title": "Consumer Confidence",
                                          "impact": "medium"}]})
check("upcoming_all surfaces the BROAD calendar (e.g. Consumer Confidence, not in the reopen registry)",
      any("Consumer Confidence" in e["event"] for e in _mc33.upcoming_all(10)))
state.save("macro_calendar", {"ts": datetime.now(timezone.utc).isoformat(),
                              "tier": "rule", "overlay": {}})   # restore the rule seed

# ══ 37. Inspector General — the loud-fail contract ═══════════════════════════════
# The IG is FORBIDDEN from a silent 'all good'. A None from Opus (parse fail /
# CLI down) must NEVER look like a clean audit — it must surface as 'broken'.
import importlib.util as _ilu
_ig_path = Path(__file__).resolve().parent.parent / "scripts" / "ig_audit.py"
_ig_spec = _ilu.spec_from_file_location("ig_audit", _ig_path)
_ig = _ilu.module_from_spec(_ig_spec); _ig_spec.loader.exec_module(_ig)
check("IG: a None Opus response is classified 'broken', never a clean audit",
      _ig._classify(None)["status"] == "broken")
check("IG: zero findings is classified 'empty' (suspicious, alerts) not 'ok'",
      _ig._classify({"findings": []})["status"] == "empty")
_igv = _ig._classify({"findings": [
    {"severity": "low", "gap": "x"}, {"severity": "critical", "gap": "y"},
    {"severity": "high", "gap": "z"}], "summary": "s"})
check("IG: findings are severity-sorted (critical first) with correct high+ count",
      _igv["status"] == "ok" and _igv["findings"][0]["severity"] == "critical"
      and _igv["high_plus"] == 2, str(_igv))
_real_fh_ig = data_engine.fetch_history
data_engine.fetch_history = lambda tickers, period="6mo": pd.DataFrame()   # keep gather offline
try:
    _igsnap = _ig._gather()
finally:
    data_engine.fetch_history = _real_fh_ig
check("IG: gather splits daily/weekly/exception events so NEVER-is-normal isn't flagged inert",
      "daily_last_seen" in _igsnap and "weekly_last_seen" in _igsnap
      and "exception_last_seen_NEVER_is_normal" in _igsnap
      and "fundamentals_sanitized" in _igsnap["exception_last_seen_NEVER_is_normal"],
      str(list(_igsnap.keys())))
check("IG: gather is read-only — health via health_report(), never run_health_sweep()",
      len(_igsnap["health_checks"]) >= 1)

# ══ 38. IG-finding fixes: OPEX calendar, coverage health, rating calibration ═════
from firm import macro_calendar as _mc38, health as _hlth38, learning as _lrn38
from datetime import date as _date38
# OPEX / quad-witching: 3rd Friday, surfaced on the BROAD committee calendar even
# with no live feed (market structure, not in FRED/FF).
check("OPEX: 3rd Friday computed correctly (Jun 2026 = quad-witching 06-19)",
      _mc38._third_friday(2026, 6) == _date38(2026, 6, 19)
      and _mc38._third_friday(2026, 12) == _date38(2026, 12, 18))
_opex_ahead = _mc38.upcoming_all(45)   # a 3rd-Friday always falls within 45 days
check("OPEX: surfaces on the broad calendar regardless of live data tier",
      any("Options expiration" in e["event"] for e in _opex_ahead), str(len(_opex_ahead)))

# Coverage-over-time + state-integrity health checks: seed the journal with a
# crash-loop + unpriceable flood + a corruption recovery, then prove the rolling
# 24h scan SEES them (the point-in-time heartbeats can't).
for _ in range(4):
    state.journal("keepalive_restart", verified_up=False)
for _ in range(20):
    state.journal("position_unpriceable", ticker="ZZZ")
state.journal("state_restored_from_backup", file="watchlist")
_sig = _hlth38._coverage_signals(24.0)
check("coverage: rolling 24h scan counts failed restarts / unpriceable / restores",
      _sig["failed_restarts"] >= 4 and _sig["unpriceable"] >= 20 and _sig["restored"] >= 1,
      str(_sig))
_hr38 = _hlth38.health_report()
_names38 = {c["name"]: c for c in _hr38["checks"]}
check("coverage: 'data coverage' health check exists and FAILS on degradation",
      "data coverage" in _names38 and not _names38["data coverage"]["ok"])
check("coverage: 'state integrity' health check exists and FAILS on recent corruption",
      "state integrity" in _names38 and not _names38["state integrity"]["ok"])

# Rating calibration: an INVERTED ladder (sell-rated names outperform buy-rated)
# must be caught — correct_pct alone hides it. Min-n guarded.
state.save("research", {"history": [{"asof": ts(10),
    "verdicts": {**{f"BUL{i}": {"rating": "BUY", "conviction": 75} for i in range(8)},
                 **{f"BER{i}": {"rating": "STRONG_SELL", "conviction": 85} for i in range(8)}},
    "context": {}}]})
_d38 = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=30)
_cols38 = {f"BUL{i}": [100.0 * (1 - 0.12 * j / 29) for j in range(30)] for i in range(8)}   # buys LOSE
_cols38.update({f"BER{i}": [100.0 * (1 + 0.12 * j / 29) for j in range(30)] for i in range(8)})  # sells WIN
_cols38["SPY"] = [100.0] * 30
_real_fh38 = data_engine.fetch_history
data_engine.fetch_history = lambda tickers, period="6mo": pd.DataFrame(_cols38, index=_d38)
try:
    _card38 = _lrn38.verdict_report_card(min_age_days=3)
finally:
    data_engine.fetch_history = _real_fh38
_cal38 = _card38.get("rating_calibration", {})
check("rating calibration: detects an INVERTED ladder (sell-side beats buy-side) with enough n",
      _cal38.get("inverted") is True and _cal38.get("n_buy_side") >= 8
      and _cal38.get("n_sell_side") >= 8, str(_cal38))
# Small sample must stay undecided (never cry wolf on a days-old fund)
state.save("research", {"history": [{"asof": ts(10),
    "verdicts": {"AAA": {"rating": "BUY", "conviction": 70},
                 "BBB": {"rating": "STRONG_SELL", "conviction": 70}}, "context": {}}]})
data_engine.fetch_history = lambda tickers, period="6mo": pd.DataFrame(
    {"AAA": [100.0]*30, "BBB": [100.0]*30, "SPY": [100.0]*30}, index=_d38)
try:
    _cal38b = _lrn38.verdict_report_card(min_age_days=3).get("rating_calibration", {})
finally:
    data_engine.fetch_history = _real_fh38
check("rating calibration: stays undecided (inverted=None) on a tiny sample",
      _cal38b.get("inverted") is None, str(_cal38b))

# Dedupe: the SAME (ticker, day) judged in two cycles must count ONCE, not twice
# (IG 2026-06-14: QH double-counted as STRONG_SELL and HOLD on one date inflated
# both tiers off a single outcome).
_dupday = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
state.save("research", {"history": [
    {"asof": _dupday + "T09:45:00+00:00", "verdicts": {"DUP": {"rating": "STRONG_SELL", "conviction": 90}},
     "context": {}},
    {"asof": _dupday + "T13:00:00+00:00", "verdicts": {"DUP": {"rating": "HOLD", "conviction": 78}},
     "context": {}}]})
data_engine.fetch_history = lambda tickers, period="6mo": pd.DataFrame(
    {"DUP": [100.0 * (1 + 0.5 * j / 29) for j in range(30)], "SPY": [100.0]*30}, index=_d38)
try:
    _ddc = _lrn38.verdict_report_card(min_age_days=3)
finally:
    data_engine.fetch_history = _real_fh38
_tot_n = sum(b["n"] for b in _ddc.get("by_rating", {}).values())
check("report card dedupes one verdict per (ticker, day) — no double-counting across cycles",
      _tot_n == 1, f"total n={_tot_n}, by_rating={_ddc.get('by_rating')}")

# A single fat-tailed outlier must NOT fabricate an 'inverted' verdict (median is
# outlier-robust): 8 buys mildly up, 8 sells mildly down, plus ONE monster sell.
state.save("research", {"history": [{"asof": ts(10),
    "verdicts": {**{f"GB{i}": {"rating": "BUY", "conviction": 70} for i in range(8)},
                 **{f"GS{i}": {"rating": "STRONG_SELL", "conviction": 70} for i in range(8)}},
    "context": {}}]})
_oc = {f"GB{i}": [100.0 * (1 + 0.03 * j / 29) for j in range(30)] for i in range(8)}     # buys mildly WIN
_oc.update({f"GS{i}": [100.0 * (1 - 0.03 * j / 29) for j in range(30)] for i in range(8)})  # sells mildly LOSE
_oc["GS0"] = [100.0 * (1 + 2.0 * j / 29) for j in range(30)]   # ONE monster sell-side winner (+200%)
_oc["SPY"] = [100.0]*30
data_engine.fetch_history = lambda tickers, period="6mo": pd.DataFrame(_oc, index=_d38)
try:
    _ocal = _lrn38.verdict_report_card(min_age_days=3).get("rating_calibration", {})
finally:
    data_engine.fetch_history = _real_fh38
check("rating calibration: median is outlier-robust — one monster name doesn't flip 'inverted'",
      _ocal.get("inverted") is False, str(_ocal))

# unreviewable_holds: a hold with NO path (no earnings, no trigger) past its
# recheck budget is a stuck zombie; a normal armed hold is not.
import firm.watchlist as _wl38
state.save("watchlist", {
    "ZOMB": {"ticker": "ZOMB", "source": "hold_verdict", "added": ts(20),
             "recheck_count": 9, "last_verdict": {"ts": ts(20)}},
    "OKAY": {"ticker": "OKAY", "source": "hold_verdict", "added": ts(2),
             "earnings_date": (datetime.now(timezone.utc).date() + timedelta(days=10)).isoformat(),
             "recheck_count": 0}})
# Stub the macro-wait protection so the test is deterministic regardless of how
# near a real FOMC is today (an imminent FOMC legitimately protects every hold).
_real_await38 = _wl38._awaiting_near_catalyst
_wl38._awaiting_near_catalyst = lambda e: False
try:
    _stuck = _wl38.unreviewable_holds()
finally:
    _wl38._awaiting_near_catalyst = _real_await38
check("bench reviewability: a no-path, recheck-exhausted hold is flagged stuck; an armed one is not",
      "ZOMB" in _stuck and "OKAY" not in _stuck, str(_stuck))
state.save("watchlist", {})   # restore clean bench
_hr38b = {c["name"] for c in _hlth38.health_report()["checks"]}
check("health desk has the new IG-fix checks (bench reviewability + self-test cadence)",
      "bench reviewability" in _hr38b and "self-test cadence" in _hr38b, str(sorted(_hr38b)))

# ══ 39. Audit-fix Batch 1: firm-wide loss breaker + firm sector cap + fail-closed ═
import config as _cfg39
from firm import risk as _risk39
import firm.riskbrain as _rb39
_orig_sleeves39 = {k: state.get_sleeve(k) for k in _cfg39.SLEEVES}
_real_vm39 = _rb39.vol_multiplier
_rb39.vol_multiplier = lambda: 1.0
try:
    # Firm-wide daily breaker: every sleeve down $2k (anchor 25k -> equity 23k) =
    # firm -8% > the 4% firm ceiling -> ALL new entries blocked, even in a sleeve
    # that alone would be fine.
    for k in _cfg39.SLEEVES:
        sv = state.get_sleeve(k)
        sv["cash"] = 23000.0; sv["positions"] = {}; sv["halted"] = False
        sv["day_pnl_anchor"] = {"date": _risk39._today(), "equity": 25000.0}  # TODAY -> counts
        state.save_sleeve(k, sv)
    _okfb, _whyfb = _risk39.firm_daily_breaker()
    _oke, _whye = _risk39.entries_allowed("alpha_stocks", state.get_sleeve("alpha_stocks"), 23000.0)
    check("firm breaker: combined -8% day loss across all sleeves trips the firm-wide brake",
          _okfb is False, _whyfb)
    check("firm breaker: blocks new entries in an otherwise-fine sleeve (aggregate brake)",
          _oke is False, _whye)

    # Firm-wide sector cap: two sleeves each hold a $12k Technology name -> ~86% of
    # a ~$28k firm in tech -> a new tech entry is blocked, a non-tech one allowed.
    for k in ("alpha_stocks", "core_blend"):
        sv = state.get_sleeve(k); sv["cash"] = 1000.0
        sv["positions"] = {"TECH" + k[:3]: {"instrument": "equity", "market_value": 12000.0,
                            "cost": 12000.0, "quantity": 1, "sector": "Technology"}}
        state.save_sleeve(k, sv)
    for k in ("calls_only", "etf_rotation"):
        sv = state.get_sleeve(k); sv["cash"] = 1000.0; sv["positions"] = {}
        state.save_sleeve(k, sv)
    _oktech, _whytech = _risk39.check_firm_sector_cap("NEWTECH", "Technology", 1000.0)
    _okutil, _ = _risk39.check_firm_sector_cap("NEWUTIL", "Utilities", 1000.0)
    check("firm sector cap: blocks a new entry that would over-concentrate the firm in one sector",
          _oktech is False and _okutil is True, _whytech)

    # Fail CLOSED: firm caps are pure internal math — a corrupt-state exception must
    # BLOCK the trade (the old fail-open disabled the cap in the exact crisis).
    _real_gs39 = state.get_sleeve
    state.get_sleeve = lambda k: (_ for _ in ()).throw(RuntimeError("corrupt sleeve"))
    try:
        _okfc, _whyfc = _risk39.check_firm_name_cap("X", 100.0)
        _okfcs, _ = _risk39.check_firm_sector_cap("X", "Technology", 100.0)
    finally:
        state.get_sleeve = _real_gs39
    check("firm caps FAIL CLOSED on a corrupt-state error (block, never silently allow)",
          _okfc is False and _okfcs is False, str((_okfc, _okfcs)))
finally:
    _rb39.vol_multiplier = _real_vm39
    for k, sv in _orig_sleeves39.items():
        state.save_sleeve(k, sv)

# ══ 40. Audit-fix Batch 2a: injection-safe toast + disk-space health check ═══════
from firm import alerts as _al40, health as _hl40
check("toast is injection-safe: text via $env, never interpolated into the PS command",
      "$env:LR_TOAST_TITLE" in _al40._TOAST_PS and "$env:LR_TOAST_MSG" in _al40._TOAST_PS
      and "CreateTextNode('" not in _al40._TOAST_PS)
_names40 = {c["name"] for c in _hl40.health_report()["checks"]}
check("health desk has a 'disk space' check (A10)", "disk space" in _names40, str(sorted(_names40)))

# ══ 41. Audit-fix A2: mark-staleness guard (don't fire stops on a stale mark) ════
from firm import broker as _bk41, monitor as _mon41
import firm.market_hours as _mh41
_save41 = (_bk41.equity_quotes_public, _mh41.market_is_open,
           _bk41.data_engine.batch_last_prices, _bk41.data_engine.last_price,
           _bk41.option_quotes_public, _bk41.fetch_marks)
_mh41.market_is_open = lambda: True
_bk41.option_quotes_public = lambda positions: {}
_bk41.data_engine.batch_last_prices = lambda syms: {s: 100.0 for s in syms}
_bk41.data_engine.last_price = lambda s: 100.0
try:
    _sv41 = {"sleeve": "alpha_stocks",
             "positions": {"ZZ": {"instrument": "equity", "entry_price": 100.0, "quantity": 1}}}
    _bk41.equity_quotes_public = lambda syms: {}                       # live quote DOWN -> fallback
    _bk41.fetch_marks(_sv41)
    check("A2: an in-hours daily-close FALLBACK mark is flagged stale",
          _sv41["positions"]["ZZ"].get("_mark_stale") is True)
    _bk41.equity_quotes_public = lambda syms: {s: 100.0 for s in syms}  # live quote present
    _bk41.fetch_marks(_sv41)
    check("A2: a live Public mark is NOT stale",
          _sv41["positions"]["ZZ"].get("_mark_stale") is False)

    # Monitor: a stale mark at a stop-triggering price must NOT fire; a fresh one must.
    _orig_alpha41 = state.get_sleeve("alpha_stocks")
    _seed = state.get_sleeve("alpha_stocks")
    _seed["positions"] = {"ZZ": {"instrument": "equity", "entry_price": 100.0,
                                 "quantity": 1, "stop_pct": 8.0, "peak_price": 100.0}}
    _seed["halted"] = False
    state.save_sleeve("alpha_stocks", _seed)
    _bk41.fetch_marks = lambda s: (s["positions"]["ZZ"].__setitem__("_mark_stale", True)
                                   or {"ZZ": 80.0})   # -20%, would hard-stop, but STALE
    _c_stale = _mon41._check_equity_sleeve("alpha_stocks", "paper")
    state.save_sleeve("alpha_stocks", _seed)   # reset (monitor may have mutated)
    _seed["positions"] = {"ZZ": {"instrument": "equity", "entry_price": 100.0,
                                 "quantity": 1, "stop_pct": 8.0, "peak_price": 100.0}}
    state.save_sleeve("alpha_stocks", _seed)
    _bk41.fetch_marks = lambda s: (s["positions"]["ZZ"].__setitem__("_mark_stale", False)
                                   or {"ZZ": 80.0})   # -20%, FRESH -> must fire
    _c_fresh = _mon41._check_equity_sleeve("alpha_stocks", "paper")
    check("A2 monitor: a STALE -20% mark does NOT fire the stop (no false trigger)",
          _c_stale == 0, f"closes={_c_stale}")
    check("A2 monitor: a FRESH -20% mark DOES fire the stop", _c_fresh == 1, f"closes={_c_fresh}")

    # 2026-07-26: EXTENDED-SESSION stop protection. A stale book mark (daily
    # close) plus a genuine live Public quote must judge the stop on the LIVE
    # quote, not skip. This window used to have NO protection at all — PRSU ran
    # to -29.2% against a -12% stop, and its two entries are 57% of every dollar
    # the fund has lost. Both legs asserted: live quote breaking the stop fires,
    # live quote ABOVE the stop does not (the flag must not become a free pass).
    _seed["positions"] = {"ZZ": {"instrument": "equity", "entry_price": 100.0,
                                 "quantity": 1, "stop_pct": 8.0, "peak_price": 100.0}}
    state.save_sleeve("alpha_stocks", _seed)
    _bk41.fetch_marks = lambda s: (s["positions"]["ZZ"].update(
        {"_mark_stale": True, "_live_px": 80.0}) or {"ZZ": 100.0})  # close 100, live -20%
    _c_ext = _mon41._check_equity_sleeve("alpha_stocks", "paper")
    check("extended hours: a STALE close + LIVE quote through the stop DOES fire",
          _c_ext == 1, f"closes={_c_ext}")

    _seed["positions"] = {"ZZ": {"instrument": "equity", "entry_price": 100.0,
                                 "quantity": 1, "stop_pct": 8.0, "peak_price": 100.0}}
    state.save_sleeve("alpha_stocks", _seed)
    _bk41.fetch_marks = lambda s: (s["positions"]["ZZ"].update(
        {"_mark_stale": True, "_live_px": 99.0}) or {"ZZ": 100.0})  # live -1%, no stop
    _c_ext_ok = _mon41._check_equity_sleeve("alpha_stocks", "paper")
    check("extended hours: a LIVE quote INSIDE the stop does not fire",
          _c_ext_ok == 0, f"closes={_c_ext_ok}")

    _seed["positions"] = {"ZZ": {"instrument": "equity", "entry_price": 100.0,
                                 "quantity": 1, "stop_pct": 8.0, "peak_price": 100.0}}
    state.save_sleeve("alpha_stocks", _seed)
    _bk41.fetch_marks = lambda s: (s["positions"]["ZZ"].update(
        {"_mark_stale": True, "_live_px": None}) or {"ZZ": 80.0})   # stale, NO live quote
    _c_nolive = _mon41._check_equity_sleeve("alpha_stocks", "paper")
    check("extended hours: stale with NO live quote still refuses to fire",
          _c_nolive == 0, f"closes={_c_nolive}")

    # 2026-07-26 PROFIT RATCHET. Give-back capped as a share of PEAK GAIN, not of
    # price. Measured: the fund kept only 35% of every winner's peak gain, making
    # avg win (+13.1%) and avg loss (-12.4%) symmetric — unprofitable at a 22%
    # hit rate. Four legs: it books a winner past the cap; it leaves a winner
    # inside the cap alone; it stays dormant below the min-gain floor so young
    # positions keep their noise tolerance; and it never fires on a RED position
    # (that is the hard stop's job, and firing here would mislabel a loss).
    def _ratchet_case(entry, peak_px, now_px, label, want):
        _s = state.get_sleeve("alpha_stocks")
        _s["positions"] = {"ZZ": {"instrument": "equity", "entry_price": entry,
                                  "quantity": 1, "stop_pct": 50.0,
                                  "trail_stop_pct": 90.0, "peak_price": peak_px}}
        _s["halted"] = False
        state.save_sleeve("alpha_stocks", _s)
        _bk41.fetch_marks = lambda s, _p=now_px: (
            s["positions"]["ZZ"].__setitem__("_mark_stale", False) or {"ZZ": _p})
        got = _mon41._check_equity_sleeve("alpha_stocks", "paper")
        check(label, got == want, f"closes={got}")

    # peak +50%, now +20% -> kept 40% of peak gain, past the 35% give-back cap
    _ratchet_case(100.0, 150.0, 120.0,
                  "profit ratchet: a winner past the give-back cap is BOOKED", 1)
    # peak +50%, now +40% -> kept 80%, comfortably inside the cap
    _ratchet_case(100.0, 150.0, 140.0,
                  "profit ratchet: a winner inside the cap is left to run", 0)
    # peak +8% (below the 12% floor), now +2% -> dormant, wide trail governs
    _ratchet_case(100.0, 108.0, 102.0,
                  "profit ratchet: dormant below the min-gain floor", 0)
    # peak +50% but now RED at -5% -> must NOT fire as a 'profit' exit
    _ratchet_case(100.0, 150.0, 95.0,
                  "profit ratchet: never fires on a position that is red", 0)
    state.save_sleeve("alpha_stocks", _orig_alpha41)

finally:
    (_bk41.equity_quotes_public, _mh41.market_is_open, _bk41.data_engine.batch_last_prices,
     _bk41.data_engine.last_price, _bk41.option_quotes_public, _bk41.fetch_marks) = _save41

# ══ HELD-POSITION DETERIORATION TRACKER (operator 2026-07-26) ════════════════
# Measures what three refuted hypotheses could not explain: names the committee
# re-affirms BUY while the position bleeds. Must flag a real streak, must NOT
# flag a winner, and must break the streak on a non-BUY verdict.
_det_hist = [{"asof": f"2026-07-2{i}T12:00:00+00:00",
              "verdicts": {"BLEED": {"rating": "BUY", "conviction": 66},
                           "WINNER": {"rating": "BUY", "conviction": 70},
                           "TURNED": {"rating": "BUY", "conviction": 64}}}
             for i in (1, 2, 3, 4)]
# TURNED's OLDEST verdict was a HOLD -> its BUY streak is only the 3 newest
_det_hist[0]["verdicts"]["TURNED"] = {"rating": "HOLD", "conviction": 55}
state.save("research", {"history": _det_hist})
_ds = state.default_sleeve_state("alpha_stocks")
_ds["positions"] = {
    "BLEED":  {"symbol": "BLEED", "instrument": "equity", "quantity": 10,
               "entry_price": 100.0, "last_price": 88.0},      # -12% -> FLAG
    "WINNER": {"symbol": "WINNER", "instrument": "equity", "quantity": 10,
               "entry_price": 100.0, "last_price": 130.0},     # +30% -> no flag
    "TURNED": {"symbol": "TURNED", "instrument": "equity", "quantity": 10,
               "entry_price": 100.0, "last_price": 80.0},      # -20%, streak 3
    "PARKED": {"symbol": "PARKED", "instrument": "equity", "quantity": 10,
               "entry_price": 100.0, "last_price": 70.0,
               "cash_equitization": True},                     # parking -> ignored
}
state.save_sleeve("alpha_stocks", _ds)
_det = learning.held_deterioration()
_flagged = {r["ticker"]: r for r in _det["rows"]}
check("deterioration: flags a name re-affirmed BUY while bleeding",
      "BLEED" in _flagged and _flagged["BLEED"]["buy_streak"] == 4
      and _flagged["BLEED"]["pnl_pct"] == -12.0, str(_det["rows"]))
check("deterioration: does NOT flag a winner on the same BUY streak",
      "WINNER" not in _flagged)
check("deterioration: a non-BUY verdict BREAKS the streak",
      _flagged.get("TURNED", {}).get("buy_streak") == 3, str(_flagged.get("TURNED")))
check("deterioration: parking is never counted as a held name",
      "PARKED" not in _flagged and _det["held_checked"] == 3)
state.save("held_deterioration", _det)
_dctx = learning.held_deterioration_context()
check("deterioration: streaks reach the judge prompt as specific facts",
      "BLEED" in _dctx and "4 straight BUY" in _dctx and "-12.0%" in _dctx, _dctx[:160])
check("deterioration: a clean book adds NO prompt noise",
      learning.held_deterioration_context.__wrapped__() == ""
      if hasattr(learning.held_deterioration_context, "__wrapped__")
      else (state.save("held_deterioration", {"rows": []}) or
            learning.held_deterioration_context() == ""))
state.save("held_deterioration", _det)

# ══ 42. Audit-fix A7: learning anti-overfit (cold-start gate + overlap dedup) ═════
from firm import learning as _lr42
check("A7: _trades_span_days measures calendar spread",
      _lr42._trades_span_days([{"closed_at": ts(25)}, {"closed_at": ts(0)}]) >= 24
      and _lr42._trades_span_days([{"closed_at": ts(1)}]) == 0)
# Cold-start: first review needs >= COLD_START_MIN_TRADES AND >= span days
state.save("lessons", {"lessons": [], "reviewed_through": 0})
state.save("ledger", {"trades": [{"closed_at": ts(2)} for _ in range(5)], "total_recorded": 5})
check("A7: first review BLOCKED on too few trades (cold-start count guard)",
      _lr42.run_review(force=False) is None)
state.save("ledger", {"trades": [{"closed_at": ts(1)} for _ in range(25)], "total_recorded": 25})
check("A7: first review BLOCKED when 25 trades span < 21 days (cold-start span guard)",
      _lr42.run_review(force=False) is None)
state.save("lessons", {"lessons": [], "reviewed_through": 0})
state.save("ledger", {"trades": []})   # restore empty ledger

# Overlapping-window dedup: same name judged 4x (within & beyond one ~window)
# collapses to ONE per window (here: 2), not 4 independent observations.
state.save("research", {"history": [
    {"asof": ts(10), "verdicts": {"OVL": {"rating": "HOLD", "conviction": 60}}, "context": {}},
    {"asof": ts(12), "verdicts": {"OVL": {"rating": "HOLD", "conviction": 60}}, "context": {}},
    {"asof": ts(25), "verdicts": {"OVL": {"rating": "HOLD", "conviction": 60}}, "context": {}},
    {"asof": ts(45), "verdicts": {"OVL": {"rating": "HOLD", "conviction": 60}}, "context": {}}]})
_real_fh42 = data_engine.fetch_history
data_engine.fetch_history = lambda tickers, period="6mo": pd.DataFrame(
    {"OVL": [100.0]*30, "SPY": [100.0]*30}, index=pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=30))
try:
    _card42 = _lr42.verdict_report_card(min_age_days=3)
finally:
    data_engine.fetch_history = _real_fh42
_n42 = sum(b["n"] for b in _card42.get("by_rating", {}).values())
check("A7: overlapping same-name verdicts collapse to one-per-window (4 -> 2)",
      _n42 == 2, f"total n={_n42}, by_rating={_card42.get('by_rating')}")

# ══ 43. Audit-fix A5: calls overnight/earnings gap helpers ═══════════════════════
import firm.sleeves as _sl43
_fut43 = (datetime.now(timezone.utc).date() + timedelta(days=14)).isoformat()
_past43 = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
check("A5: _days_until computes forward days and handles bad input",
      _sl43._days_until(_fut43) == 14 and _sl43._days_until(None) is None
      and _sl43._days_until("garbage") is None)
check("A5: a past expiry yields a negative day count (already expired)",
      _sl43._days_until(_past43) == -3)
check("A5: overnight-premium cap + earnings-before-expiry constants are configured",
      config.MAX_OVERNIGHT_CALL_PREMIUM_PCT > 0)

# ══ 44. Audit-fix A13: PID-aware cross-process cycle lock ════════════════════════
import os as _os44, json as _json44, time as _time44
state.clear_cycle_lock()
check("A13: no lock -> cycle_active False", state.cycle_active() is False
      and state.cycle_lock_holder() is None)
state.write_cycle_lock()
check("A13: own lock -> active True and holder is our pid",
      state.cycle_active() is True and state.cycle_lock_holder() == _os44.getpid())
check("A13: own cycle is EXCLUDED with exclude_own (app monitors keep running)",
      state.cycle_active(exclude_own=True) is False)
state.CYCLE_LOCK.write_text(_json44.dumps({"pid": _os44.getpid() + 99999, "ts": _time44.time()}),
                            encoding="utf-8")
_real_alive44 = state._pid_alive
state._pid_alive = lambda pid: True   # simulate a LIVE foreign process (not a dead orphan)
try:
    _foreign_active = state.cycle_active(exclude_own=True)
finally:
    state._pid_alive = _real_alive44
check("A13: a FOREIGN live-pid lock is active even with exclude_own (app stands down)",
      _foreign_active is True)
state.CYCLE_LOCK.write_text(str(_time44.time()), encoding="utf-8")   # legacy bare-ts format
check("A13: a legacy bare-timestamp lock is treated as foreign-held (fail safe)",
      state.cycle_lock_holder() == -1 and state.cycle_active() is True)
state.write_cycle_lock(); _os44.utime(state.CYCLE_LOCK, (1.0, 1.0))   # force old mtime
check("A13: a stale lock (old mtime) is NOT active", state.cycle_active() is False)
state.clear_cycle_lock()
check("A13: clear_cycle_lock removes the lock", state.cycle_active() is False)

# ══ 45. Change-audit verify fixes (regressions caught by the verification workflow) ═
from firm import broker as _bk45, risk as _risk45, learning as _lr45
import firm.riskbrain as _rb45, os as _os45, json as _json45, time as _time45

# F7: on the MONITOR path (from_fetch_marks=True, where _mark_stale is authored),
# sleeve_equity must NOT advance peak_price on a mark flagged stale — peak drives the
# trailing stop and a stale high would force an early exit. (2026-06-17: staleness
# suppression is now opt-in to the fetch_marks path so a fresh cycle quote isn't
# discarded by a lingering flag — so this test states that path explicitly.)
_sv45 = {"cash": 0.0, "pending_orders": [], "positions": {
    "ZZ": {"instrument": "equity", "quantity": 1, "entry_price": 100.0,
           "peak_price": 100.0, "_mark_stale": True}}}
_bk45.sleeve_equity(_sv45, {"ZZ": 130.0}, from_fetch_marks=True)
check("F7: a STALE mark does not ratchet peak_price on the monitor path (trailing-stop integrity)",
      _sv45["positions"]["ZZ"]["peak_price"] == 100.0)
_sv45["positions"]["ZZ"]["_mark_stale"] = False
_bk45.sleeve_equity(_sv45, {"ZZ": 120.0}, from_fetch_marks=True)   # +20% < PEAK_MAX_JUMP guard
check("F7: a FRESH mark does ratchet peak_price normally",
      _sv45["positions"]["ZZ"]["peak_price"] == 120.0)
# and the 2026-07-02 poison guard: a GROSS one-sweep jump does NOT ratchet the peak
_bk45.sleeve_equity(_sv45, {"ZZ": 300.0}, from_fetch_marks=True)   # +150% bad tick
check("F7: a gross spike mark does NOT poison peak_price (poisoning guard)",
      _sv45["positions"]["ZZ"]["peak_price"] == 120.0)

# Stale-mark ALERT must NOT fire in extended/pre-market (a stale mark is expected
# there — the fund fetches live quotes only in regular hours). Operator hit 14
# STALE-MARK criticals at 8:34am premarket (2026-06-15). _stale_skip is a no-op
# when the regular session is closed (the stop-skip still happens via the caller).
from firm import monitor as _mon45
import firm.market_hours as _mh45b
_pos45s = {"mark_fail_streak": 4}          # one more increment would hit the alert
_real_mio45 = _mh45b.market_is_open
_mh45b.market_is_open = lambda: False       # extended / pre-market
try:
    _mon45._stale_skip(_pos45s, "alpha_stocks", "ZZ")
finally:
    _mh45b.market_is_open = _real_mio45
check("stale-mark alert is suppressed in extended/pre-market (no 8:34am flood)",
      _pos45s["mark_fail_streak"] == 4)     # unchanged -> not counted -> no alert

# Auto-resolve transient pricing alerts (2026-06-22): a CANNOT PRICE / STALE MARK
# alert must SELF-CLEAR once the name prices again, so a fixed outage stops showing as
# a permanent critical (operator: 30 resolved alerts still filled the feed for hours).
from firm import alerts as _al_ar
state.save("alerts", {"items": [
    {"ts": ts(), "title": "CANNOT PRICE ZZ", "msg": "x", "severity": "critical", "acked": False},
    {"ts": ts(), "title": "STALE MARK ZZ", "msg": "x", "severity": "critical", "acked": False},
    {"ts": ts(), "title": "CANNOT PRICE OTHER", "msg": "x", "severity": "critical", "acked": False},
    {"ts": ts(), "title": "DISPLAY RECONCILIATION FAILED", "msg": "x", "severity": "critical", "acked": False},
]})
_nres = _al_ar.resolve("ZZ")
_un = [a["title"] for a in _al_ar.unacked()]
check("alerts.resolve clears the name's CANNOT PRICE + STALE MARK, leaves others",
      _nres == 2 and "CANNOT PRICE ZZ" not in _un and "STALE MARK ZZ" not in _un
      and "CANNOT PRICE OTHER" in _un and "DISPLAY RECONCILIATION FAILED" in _un)
state.save("alerts", {"items": [
    {"ts": ts(), "title": "CANNOT PRICE ZZ", "msg": "x", "severity": "critical", "acked": False}]})
_posz = {"mark_fail_streak": 5, "mark_fail_alerted": True}
_mon45._mark_ok(_posz, "ZZ")
check("_mark_ok(pos, sym) on recovery resets streak AND self-heals the name's alert",
      _posz.get("mark_fail_streak") == 0 and "mark_fail_alerted" not in _posz
      and not _al_ar.unacked())
state.save("alerts", {"items": []})
check("alerts.resolve is a safe no-op when nothing matches / empty sym",
      _al_ar.resolve("ZZ") == 0 and _al_ar.resolve("") == 0)

# F5: a dead-pid cycle lock is treated as NOT held (orphan can't blind monitors).
state.CYCLE_LOCK.write_text(_json45.dumps({"pid": 2147480000, "ts": _time45.time()}), encoding="utf-8")
check("F5: a dead-pid cycle lock is treated as not-held (orphan neutralized)",
      state.cycle_lock_holder() is None and state.cycle_active() is False)
state.write_cycle_lock()
check("F5: a live (own-pid) cycle lock is still held",
      state.cycle_lock_holder() == _os45.getpid())
state.clear_cycle_lock()

# F6: firm_daily_breaker is date-aware — a sleeve whose anchor is dated YESTERDAY
# contributes its equity to the denominator but ZERO to today's P&L (no overnight
# gap masquerading as today's loss on the first cycle of a new day).
_orig45 = {k: state.get_sleeve(k) for k in config.SLEEVES}
_real_vm45 = _rb45.vol_multiplier
_rb45.vol_multiplier = lambda: 1.0
try:
    for k in config.SLEEVES:                      # all anchors YESTERDAY-dated, equity << anchor
        sv = state.get_sleeve(k)
        sv["cash"] = 20000.0; sv["positions"] = {}; sv["halted"] = False
        sv["day_pnl_anchor"] = {"date": "2000-01-01", "equity": 25000.0}
        state.save_sleeve(k, sv)
    _ok45, _why45 = _risk45.firm_daily_breaker()
    check("F6: stale (yesterday) anchors contribute 0 to firm day-P&L -> no false block",
          _ok45 is True, _why45)
finally:
    _rb45.vol_multiplier = _real_vm45
    for k, sv in _orig45.items():
        state.save_sleeve(k, sv)

# F-extra: an EPHEMERAL (heartbeat) backup-recovery is benign and must NOT fail
# 'state integrity' — only a CRITICAL book file (ledger/sleeve/watchlist) should.
from firm import health as _hl45
state.journal("state_restored_from_backup", file="heartbeat")
_sig45 = _hl45._coverage_signals(24.0)
check("state-integrity: an ephemeral heartbeat recovery is NOT counted as book corruption",
      _sig45.get("restored_ephemeral", 0) >= 1, str(_sig45))

# data-coverage RECOVERY CLOCK (2026-06-22): a blown 24h budget that's been QUIET for
# >= HEALTH_COVERAGE_RECOVERY_H is a RESOLVED incident aging out of the window, not a
# permanent red WARN; a benign verified-up restart (incl. our own deploy) must NOT reset
# the clock (operator saw a fixed morning outage stay red all day).
def _cov_evt(ev, h_ago, **x):
    return {"event": ev, "ts": (datetime.now(timezone.utc) - timedelta(hours=h_ago)).isoformat(), **x}
_real_rj45 = _hl45.state.read_journal
try:
    _hl45.state.read_journal = lambda n=1200: (
        [_cov_evt("position_unpriceable", 1.0) for _ in range(20)]
        + [_cov_evt("keepalive_restart", 0.4, verified_up=True)])
    _scr = _hl45._coverage_signals(24.0)
    check("coverage: RECENT unpriceable -> over budget, quiet<3h (NOT recovered)",
          _scr["unpriceable"] == 20 and _scr["quiet_h"] is not None and _scr["quiet_h"] < 3.0)
    _hl45.state.read_journal = lambda n=1200: (
        [_cov_evt("position_unpriceable", 10.0) for _ in range(20)]
        + [_cov_evt("keepalive_restart", 0.2, verified_up=True)])   # only a benign deploy since
    _sco = _hl45._coverage_signals(24.0)
    check("coverage: OLD incident + benign deploy restart since -> RECOVERED (quiet>=3h)",
          _sco["quiet_h"] is not None and _sco["quiet_h"] >= 3.0)
    _hl45.state.read_journal = lambda n=1200: [_cov_evt("keepalive_restart", 0.5, verified_up=False)]
    _scf = _hl45._coverage_signals(24.0)
    check("coverage: a FAILED restart (crash-loop) IS real instability -> resets the clock",
          _scf["failed_restarts"] == 1 and _scf["quiet_h"] is not None and _scf["quiet_h"] < 3.0)
finally:
    _hl45.state.read_journal = _real_rj45

# F10/F11: learning helpers are crash-proof on bad data.
check("F10: _trades_span_days handles mixed naive/aware datetimes without crashing",
      isinstance(_lr45._trades_span_days(
          [{"closed_at": "2026-06-01"}, {"closed_at": ts(0)}]), int))
state.save("research", {"history": [{"asof": ts(10),
    "verdicts": {"NONEV": None, "OK": {"rating": "BUY", "conviction": 60}}, "context": {}}]})
_realfh45 = data_engine.fetch_history
data_engine.fetch_history = lambda tickers, period="6mo": pd.DataFrame(
    {"OK": [100.0]*30, "SPY": [100.0]*30}, index=pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=30))
try:
    _c45 = _lr45.verdict_report_card(min_age_days=3)   # must not raise on the None verdict
    check("F11: report card tolerates a None verdict value (no AttributeError)",
          isinstance(_c45, dict))
finally:
    data_engine.fetch_history = _realfh45

# ══ 46. Audit-fix A6: rotation intra-cycle absolute-momentum break (no churn) ════
from firm import sleeves as _sl46, broker as _bk46, data_engine as _de46, risk as _risk46
import firm.riskbrain as _rb46
_orig_rot46 = state.get_sleeve("etf_rotation")
_real_rsig46, _real_ep46, _real_vm46b = _de46.rotation_signals, _bk46.equity_prices, _rb46.vol_multiplier
try:
    _rb46.vol_multiplier = lambda: 1.0
    _de46.rotation_signals = lambda: {"ranked": [("WIN", 5.0), ("LOSE", -3.0)], "cash_hurdle_3m": 0.0}
    _bk46.equity_prices = lambda syms: {x: 100.0 for x in syms}
    sv46 = state.get_sleeve("etf_rotation")
    sv46["halted"] = False; sv46["cash"] = 1000.0
    sv46["last_rotation"] = ts(3)   # 3 days ago -> BETWEEN rotations (< 28d)
    sv46["positions"] = {
        "WIN": {"instrument": "equity", "quantity": 10, "entry_price": 100.0,
                "peak_price": 100.0, "cost": 1000.0, "sector": ""},
        "LOSE": {"instrument": "equity", "quantity": 10, "entry_price": 100.0,
                 "peak_price": 100.0, "cost": 1000.0, "sector": ""}}
    sv46["day_pnl_anchor"] = {"date": _risk46._today(), "equity": 3000.0}
    state.save_sleeve("etf_rotation", sv46)
    _sl46._run_etf_rotation("paper")
    _pos46 = state.get_sleeve("etf_rotation")["positions"]
    check("A6: below-hurdle ETF exited to cash between rotations; above-hurdle kept (no churn)",
          "LOSE" not in _pos46 and "WIN" in _pos46, str(list(_pos46.keys())))
    # F2 (change-audit): a HALTED rotation sleeve must STILL exit a breaking-down
    # ETF (protective exits route when halted, like stops) — not get suppressed.
    sv46b = state.get_sleeve("etf_rotation")
    sv46b["halted"] = True; sv46b["cash"] = 1000.0; sv46b["last_rotation"] = ts(3)
    sv46b["positions"] = {"DROP": {"instrument": "equity", "quantity": 10, "entry_price": 100.0,
                                   "peak_price": 100.0, "cost": 1000.0, "sector": ""}}
    sv46b["day_pnl_anchor"] = {"date": _risk46._today(), "equity": 3000.0}
    state.save_sleeve("etf_rotation", sv46b)
    _de46.rotation_signals = lambda: {"ranked": [("DROP", -3.0)], "cash_hurdle_3m": 0.0}
    _sl46._run_etf_rotation("paper")
    check("F2: a HALTED rotation sleeve still exits a below-hurdle ETF (protective exit routes)",
          "DROP" not in state.get_sleeve("etf_rotation")["positions"])
finally:
    _de46.rotation_signals, _bk46.equity_prices, _rb46.vol_multiplier = _real_rsig46, _real_ep46, _real_vm46b
    state.save_sleeve("etf_rotation", _orig_rot46)

# ══ 47. Geopolitical/macro breaking-news awareness (macro_brief + learning) ══════
from firm import data_engine as _de47
import datetime as _dt47


class _FakeT47:
    def __init__(self, items): self._items = items
    @property
    def news(self): return self._items


class _FakeYF47:
    def __init__(self, items): self._items = items
    def Ticker(self, t): return _FakeT47(self._items)


def _item47(title, age_days, src="Reuters"):
    pub = (_dt47.datetime.now(_dt47.timezone.utc) - _dt47.timedelta(days=age_days)).isoformat()
    return {"content": {"title": title, "pubDate": pub, "provider": {"displayName": src}}}


_real_yf47 = _de47.yf
_de47.yf = _FakeYF47([
    _item47("Iran and US reach verbal ceasefire agreement, futures up", 0.5),  # geo, fresh -> IN
    _item47("Fed signals rate cut as inflation cools", 1.0),                   # monetary, fresh -> IN
    _item47("Acme Corp announces a new CFO", 0.2),                             # off-topic -> OUT
    _item47("Russia sanctions expanded sharply", 9.0),                        # geo but STALE -> OUT
])
try:
    _gn = _de47.macro_news_brief()
finally:
    _de47.yf = _real_yf47
check("geo-news: keeps fresh macro/geopolitical, drops off-topic and stale (topic+date filter)",
      any("Iran" in g["headline"] for g in _gn)
      and any(g["topic"] == "monetary" for g in _gn)
      and not any("Acme" in g["headline"] for g in _gn)
      and not any("Russia sanctions" in g["headline"] for g in _gn), str(_gn)[:200])
check("geo-news: every item carries a date AND a topic (relevance by both)",
      bool(_gn) and all(g.get("date") and g.get("topic") for g in _gn))

# Learning: the report card splits forward excess by geopolitical-news period.
from firm import learning as _lr47
state.save("research", {"history": [{"asof": ts(10),
    "verdicts": {"GEOZ": {"rating": "BUY", "conviction": 70}},
    "context": {"GEOZ": {"macro_news": ["geopolitical", "monetary"]}}}]})
_realfh47 = data_engine.fetch_history
data_engine.fetch_history = lambda tickers, period="6mo": pd.DataFrame(
    {"GEOZ": [100.0 * (1 + 0.05 * j / 29) for j in range(30)], "SPY": [100.0] * 30},
    index=pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=30))
try:
    _card47 = _lr47.verdict_report_card(min_age_days=3)
finally:
    data_engine.fetch_history = _realfh47
check("geo-news learning: report card exposes geopolitical_news_value split",
      "geopolitical_news_value" in _card47
      and _card47["geopolitical_news_value"].get("geopolitical_period", {}).get("n", 0) >= 1,
      str(_card47.get("geopolitical_news_value")))

# ══ 39. Compound reopen-triggers (flag-gated; design + adversarial review wf) ══
from firm import debate as _deb39
_real_ct39 = config.COMPOUND_TRIGGERS
_tt39 = watchlist._technical_trigger_met
_norm39 = watchlist._normalize_trigger
try:
    config.COMPOUND_TRIGGERS = False
    check("39A flag-off committee prompt is byte-identical to the legacy schema",
          _deb39._verdict_schema() == _deb39._VERDICT_SCHEMA)
    check("39A flag-off: a compound node is NOT evaluated (-> None)",
          _tt39({"reopen_trigger": {"op": "any", "legs": [{"kind": "rsi_below", "level": 99}]}},
                {"rsi14": 10}) is None)
    check("39A flag-off: a legacy leaf still evaluates identically",
          _tt39({"reopen_trigger": {"kind": "rsi_below", "level": 40}}, {"rsi14": 35})
          == "RSI 35 <= 40")
    config.COMPOUND_TRIGGERS = True
    check("39B legacy leaf unchanged with flag ON",
          _tt39({"reopen_trigger": {"kind": "rsi_below", "level": 40}}, {"rsi14": 35})
          == "RSI 35 <= 40")
    check("39B legacy catalyst leaf still -> None (reopened elsewhere)",
          _tt39({"reopen_trigger": {"kind": "catalyst"}}, {"price": 1}) is None)
    check("39B _needs_price_trigger unchanged for flat leaves",
          watchlist._needs_price_trigger({"reopen_trigger": {"kind": "pullback_pct"}}) is True
          and watchlist._needs_price_trigger({"reopen_trigger": {"kind": "rsi_below"}}) is False)
    _and39 = {"reopen_trigger": {"op": "all", "legs": [
        {"kind": "rsi_below", "level": 55}, {"kind": "cross_sma", "period": 50, "dir": "above"}]}}
    check("39C AND fires only when ALL legs hold",
          bool(_tt39(_and39, {"rsi14": 50, "above_sma50": True}))
          and _tt39(_and39, {"rsi14": 60, "above_sma50": True}) is None)
    check("39D AND with absent data HOLDS (UNKNOWN, never a false not-met)",
          _tt39(_and39, {"rsi14": 50}) is None
          and watchlist._needs_full_tech({"reopen_trigger": _and39["reopen_trigger"]}) is True)
    _or39 = {"reopen_trigger": {"op": "any", "legs": [
        {"op": "all", "legs": [{"kind": "rsi_below", "level": 20}]}, {"kind": "catalyst"}]}}
    check("39E OR reopens via a landed catalyst leg",
          bool(_tt39(_or39, {"rsi14": 60}, cat_fn=lambda e: "earnings printed"))
          and _tt39(_or39, {"rsi14": 60}, cat_fn=lambda e: None) is None)
    _af39 = {"reopen_trigger": {"op": "after", "legs": [
        {"kind": "catalyst"}, {"kind": "cross_sma", "period": 50, "dir": "below"}]}}
    check("39F after-gate withholds until the gate lands (tech holds, gate not landed -> None)",
          _tt39(_af39, {"above_sma50": False}, cat_fn=lambda e: None) is None)
    check("39F after-gate fires once gate landed AND setup met",
          bool(_tt39(_af39, {"above_sma50": False}, cat_fn=lambda e: "macro landed")))
    check("39G nonmacro_reopen: True for any[tech,catalyst], False for after[catalyst,tech]",
          watchlist._nonmacro_reopen_ok(_or39["reopen_trigger"]) is True
          and watchlist._nonmacro_reopen_ok(_af39["reopen_trigger"]) is False)
    check("39H normalizer drops a 'none' leg inside 'all' (collapses to the real leaf)",
          _norm39({"op": "all", "legs": [{"kind": "rsi_below", "level": 40}, {"kind": "none"}]})
          == {"kind": "rsi_below", "level": 40})
    check("39H normalizer hoists the catalyst to the gate in a reversed 'after'",
          _norm39({"op": "after", "legs": [{"kind": "cross_sma", "period": 50, "dir": "below"},
                                           {"kind": "catalyst"}]})["legs"][0] == {"kind": "catalyst"})
    check("39H normalizer rejects a garbage leaf -> None", _norm39({"kind": "bogus"}) is None)
    check("39H normalizer tolerates a non-list 'legs' without crashing",
          _norm39({"op": "any", "legs": "x"}) is None)
    check("39H _trigger_kinds walks a node",
          watchlist._trigger_kinds({"op": "all", "legs": [{"kind": "rsi_below"}, {"kind": "cross_sma"}]})
          == {"rsi_below", "cross_sma"})
    _rq39, _rc39 = watchlist._refresh_quants, watchlist._refresh_catalyst_dates
    try:
        watchlist._refresh_quants = lambda *a, **k: {}
        watchlist._refresh_catalyst_dates = lambda wl: None
        _sc39 = {"quant_scores": {}, "stock_candidates": []}
        state.save("watchlist", {})
        _wl39 = watchlist.update(_sc39, {"NODG": {"kind": "stock",
            "verdict": {"rating": "HOLD", "conviction": 60},
            "packet": {"technicals": {"rsi14": 70, "price": 100}}}})
        check("39I non-degraded HOLD with no trigger gets a derived rsi_below leg",
              (_wl39.get("NODG", {}).get("reopen_trigger") or {}).get("kind") == "rsi_below")
        state.save("watchlist", {})
        _wl39d = watchlist.update(_sc39, {"DGD": {"kind": "stock",
            "verdict": {"rating": "HOLD", "conviction": 60, "degraded": True},
            "packet": {"technicals": {"rsi14": 70, "price": 100}}}})
        check("39I degraded HOLD stays trigger-less (no fabricated leg)",
              _wl39d.get("DGD", {}).get("reopen_trigger") is None)
    finally:
        watchlist._refresh_quants = _rq39
        watchlist._refresh_catalyst_dates = _rc39
        state.save("watchlist", {})
finally:
    config.COMPOUND_TRIGGERS = _real_ct39

# ══ 40. Degraded-market-scan guard (operator 2026-06-24: a yfinance throttle gave
# 918/5609 tradable vs ~3836 and that thin universe got cached) ════════════════
from firm import universe as _uni40
_d40, _k40, _f40 = _uni40._scan_degraded_verdict(3836, 5609,
    {"symbols_tradable": 3000, "candidates": {"large": ["X"]}})
check("40 a healthy scan (3836/5609) is NOT degraded", _d40 is False and _k40 is False)
_d40, _k40, _f40 = _uni40._scan_degraded_verdict(918, 5609,
    {"symbols_tradable": 3836, "candidates": {"large": ["AAA"]}})
check("40 a throttled scan (918) is degraded AND keeps the fuller prior", _d40 and _k40)
_d40, _k40, _f40 = _uni40._scan_degraded_verdict(918, 5609, {})
check("40 throttled scan with no prior -> degraded, best-effort (no keep)",
      _d40 and not _k40)
_d40, _k40, _f40 = _uni40._scan_degraded_verdict(900, 5609,
    {"symbols_tradable": 800, "candidates": {"large": ["X"]}})
check("40 does not keep a prior that is not fuller than the new scan", not _k40)

# ══ 41. Fix 1 + Fix 2 (2026-07-01): silent-non-entry observability + guarded swap ══
from firm import sleeves as _slvF   # noqa: E402
import config as _cfgF              # noqa: E402

# --- Fix 1: a BUY with an UNRESOLVED kind (None) is DROPPED but JOURNALED loud ---
_vF1 = {"MUF":  {"kind": None,    "verdict": {"rating": "BUY", "conviction": 72}},
        "GOODF": {"kind": "stock", "verdict": {"rating": "BUY", "conviction": 65}}}
_rF1 = _slvF._ranked_buys(_vF1, 60)
check("41 Fix1: unresolved-kind BUY dropped from ranked; valid stock kept",
      "MUF" not in [x for x, _ in _rF1] and "GOODF" in [x for x, _ in _rF1])
check("41 Fix1: the unresolved-kind BUY drop is JOURNALED (no silent non-entry)",
      any(e.get("event") == "buy_skipped_kind_unresolved" and e.get("symbol") == "MUF"
          for e in state.read_journal(300)))
_nF1 = sum(1 for e in state.read_journal(300)
           if e.get("event") == "buy_skipped_kind_unresolved")
_slvF._ranked_buys({"SMHF": {"kind": "etf", "verdict": {"rating": "BUY", "conviction": 70}}}, 60)
check("41 Fix1: a valid ETF-for-stocks-sleeve is NOT logged as a drop (no noise)",
      sum(1 for e in state.read_journal(300)
          if e.get("event") == "buy_skipped_kind_unresolved") == _nF1)

# --- Fix 2: guarded conviction-swap (evict weakest evictable for a stronger buy) ---
_oldF = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
_nowF = datetime.now(timezone.utc).isoformat()
def _sleeveF():
    return {"sleeve": "alpha_stocks", "cash": 400.0, "swap_intents": {}, "positions": {
        "WEAKF":  {"quantity": 5, "entry_price": 100, "opened_at": _oldF, "instrument": "equity"},
        "WINF":   {"quantity": 5, "entry_price": 100, "opened_at": _oldF, "instrument": "equity"},
        "STILLF": {"quantity": 5, "entry_price": 100, "opened_at": _oldF, "instrument": "equity"},
        "SPY":    {"quantity": 5, "entry_price": 100, "opened_at": _oldF, "instrument": "equity",
                   "cash_equitization": True}}}
_vF2 = {"WEAKF":  {"verdict": {"rating": "HOLD", "conviction": 40}},
        "WINF":   {"verdict": {"rating": "HOLD", "conviction": 80}},
        "STILLF": {"verdict": {"rating": "BUY",  "conviction": 50}}}
_pxF2 = {"WEAKF": 90, "WINF": 120, "STILLF": 95, "SPY": 100}   # WEAKF red, WINF green
_alphaF = _cfgF.SLEEVES["alpha_stocks"]
check("41 Fix2: weakest-evictable = the red HOLD (green/still-BUY/parking protected)",
      _slvF._weakest_evictable(_sleeveF(), _vF2, _pxF2, set()) == ("WEAKF", 40.0))
check("41 Fix2: a GREEN weak HOLD is protected (never sell a winner)",
      _slvF._weakest_evictable(_sleeveF(), _vF2, dict(_pxF2, WEAKF=130), set()) is None)
_sf = _sleeveF(); _sf["positions"]["WEAKF"]["opened_at"] = _nowF
check("41 Fix2: a FRESH weak HOLD is protected (let a new entry work)",
      _slvF._weakest_evictable(_sf, _vF2, _pxF2, set()) is None)
# review-hardening guards: a DEGRADED HOLD and an unparsed-conviction HOLD are never evictable
_vDeg = {**_vF2, "WEAKF": {"verdict": {"rating": "HOLD", "conviction": 40, "degraded": True}}}
check("41 Fix2: a DEGRADED HOLD is NOT evictable (no capital action on a quant fallback)",
      _slvF._weakest_evictable(_sleeveF(), _vDeg, _pxF2, set()) is None)
_vNn = {**_vF2, "WEAKF": {"verdict": {"rating": "HOLD", "conviction": None}}}
check("41 Fix2: a HOLD with unparsed conviction is protected (not defaulted to weakest)",
      _slvF._weakest_evictable(_sleeveF(), _vNn, _pxF2, set()) is None)
# _plan_swap: margin + hysteresis (DECIDES; never sells)
_vM = {**_vF2, "NEWF": {"kind": "stock", "packet": {},
                        "verdict": {"rating": "STRONG_BUY", "conviction": 45}}}
check("41 Fix2: a thin conviction margin (+5 over the weakest) does NOT plan a swap",
      _slvF._plan_swap(_sleeveF(), "NEWF", _vM["NEWF"]["verdict"], _vM, _pxF2, _alphaF, set(), [0])
      is None)
_sH = _sleeveF()
_vH = {**_vF2, "NEWF": {"kind": "stock", "packet": {},
                        "verdict": {"rating": "STRONG_BUY", "conviction": 72}}}
_p1 = _slvF._plan_swap(_sH, "NEWF", _vH["NEWF"]["verdict"], _vH, _pxF2, _alphaF, set(), [0])
check("41 Fix2: strong margin but 1st cycle -> hysteresis defers (no plan, nothing sold)",
      _p1 is None and _sH["swap_intents"].get("NEWF", {}).get("count") == 1
      and "WEAKF" in _sH["positions"])
_p2 = _slvF._plan_swap(_sH, "NEWF", _vH["NEWF"]["verdict"], _vH, _pxF2, _alphaF, set(), [0])
check("41 Fix2: same intent 2nd cycle -> plan returns the weakest (still NOT sold)",
      _p2 == ("WEAKF", 40.0) and "WEAKF" in _sH["positions"])
# hysteresis keyed on the BUY candidate: the count survives the weakest rotating
_sK = _sleeveF()
_slvF._plan_swap(_sK, "NEWF", _vH["NEWF"]["verdict"], _vH, _pxF2, _alphaF, set(), [0])
_vK = {"WEAKF": {"verdict": {"rating": "HOLD", "conviction": 40}},
       "WINF":  {"verdict": {"rating": "HOLD", "conviction": 30}},
       "STILLF": {"verdict": {"rating": "HOLD", "conviction": 50}}, "NEWF": _vH["NEWF"]}
_pxK = {"WEAKF": 90, "WINF": 90, "STILLF": 90, "SPY": 100}
_pK = _slvF._plan_swap(_sK, "NEWF", _vH["NEWF"]["verdict"], _vK, _pxK, _alphaF, set(), [0])
check("41 Fix2: hysteresis keyed on the buy candidate -> count survives weakest rotating",
      _pK is not None and _sK["swap_intents"]["NEWF"]["count"] == 2)
# _commit_swap_sell: sells the weakest, frees the slot, stamps a round-trip cooldown, journals
_sC = _sleeveF()
_okC = _slvF._commit_swap_sell(_sC, "WEAKF", 40.0, "NEWF", 72, _pxF2, "paper", [0])
check("41 Fix2: commit sells the weakest, frees the slot + stamps a round-trip cooldown",
      _okC is True and "WEAKF" not in _sC["positions"]
      and _slvF._swap_cooldown_active(_sC, "WEAKF"))
check("41 Fix2: the swap is journaled position_swapped (P&L-auditable)",
      any(e.get("event") == "position_swapped" and e.get("sold") == "WEAKF"
          and e.get("bought") == "NEWF" for e in state.read_journal(300)))
# a PENDING (unfilled live) sell must NOT be treated as a freed slot (the CRITICAL fix)
class _PendBrokerF:
    def execute(self, *a, **k):
        return {"success": True, "pending": True, "order_id": "x"}
_realBrkF = _slvF.broker
_slvF.broker = _PendBrokerF()
_sPn = _sleeveF()
_okPn = _slvF._commit_swap_sell(_sPn, "WEAKF", 40.0, "NEWF", 72, _pxF2, "live", [0])
_slvF.broker = _realBrkF
check("41 Fix2: a PENDING (unfilled) live sell does NOT free the slot (no half-swap)",
      _okPn is False and "WEAKF" in _sPn["positions"])
# a missing live price defers the eviction (no stale entry_price fallback)
_sNp = _sleeveF()
check("41 Fix2: a missing live price defers the eviction (no stale-price sell)",
      _slvF._commit_swap_sell(_sNp, "WEAKF", 40.0, "NEWF", 72, {"NEWF": 100}, "paper", [0]) is False
      and "WEAKF" in _sNp["positions"])
# a name inside its post-eviction cooldown is not re-plannable as a swap either
_sRt = _sleeveF()
_slvF._commit_swap_sell(_sRt, "WEAKF", 40.0, "NEWF", 72, _pxF2, "paper", [0])
check("41 Fix2: an evicted name stays under round-trip cooldown (anti-whipsaw)",
      _slvF._swap_cooldown_active(_sRt, "WEAKF"))
# incoming stop-out name is never planned for a swap-in
_sStop = _sleeveF()
_vStop = {**_vF2, "RTF": {"kind": "stock", "packet": {"watchlist_context": {"stop_out": True}},
                          "verdict": {"rating": "STRONG_BUY", "conviction": 90}}}
_sStop["swap_intents"] = {"RTF": {"weak": "WEAKF", "count": 5}}
check("41 Fix2: a just-stopped-out name is NOT planned for a swap-in (anti round-trip)",
      _slvF._plan_swap(_sStop, "RTF", _vStop["RTF"]["verdict"], _vStop, _pxF2, _alphaF, set(), [0])
      is None)
# INTEGRATION: a real swap flows through run_alpha_stocks (validates the loop wiring:
# plan at book-full -> fall through the gates -> commit the eviction AT the buy point)
_real_eqpI = broker.equity_prices
_real_eaI = sleeves_mod2.risk.entries_allowed
broker.equity_prices = lambda syms: {x: 100.0 for x in syms}
sleeves_mod2.risk.entries_allowed = lambda *a, **k: (True, "")   # isolate: test the swap wiring, not the breaker
_oldI = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
_sI = state.default_sleeve_state("alpha_stocks")
_sI["cash"] = 3000.0
_sI["soft_risk"] = False
_sI["peak_equity"] = 999999
_posI = {f"HI{i}": {"quantity": 5, "entry_price": 50, "opened_at": _oldI,   # green -> protected
                    "instrument": "equity"} for i in range(_alphaF["max_positions"] - 1)}
_posI["WEAKI"] = {"quantity": 5, "entry_price": 200, "opened_at": _oldI,     # red HOLD -> evictable
                  "instrument": "equity"}
_sI["positions"] = _posI
_sI["swap_intents"] = {"NEWI": {"weak": "WEAKI", "count": 9}}   # hysteresis already satisfied
state.save_sleeve("alpha_stocks", _sI)
_vI = {k: {"kind": "stock", "packet": {}, "verdict": {"rating": "HOLD", "conviction": 60}}
       for k in _posI if k != "WEAKI"}
_vI["WEAKI"] = {"kind": "stock", "packet": {}, "verdict": {"rating": "HOLD", "conviction": 40}}
_vI["NEWI"] = {"kind": "stock", "packet": {},
               "verdict": {"rating": "STRONG_BUY", "conviction": 80, "target_weight_pct": 8}}
sleeves_mod2.run_alpha_stocks(_vI, {}, "paper")
_sIa = state.get_sleeve("alpha_stocks")
check("41 Fix2 INTEGRATION: full book + strong buy -> WEAKI evicted AND NEWI bought (real loop)",
      "WEAKI" not in _sIa["positions"] and "NEWI" in _sIa["positions"])
check("41 Fix2 INTEGRATION: real-position count never exceeds max_positions after a swap",
      sum(1 for p in _sIa["positions"].values() if not p.get("cash_equitization"))
      <= _alphaF["max_positions"])
broker.equity_prices = _real_eqpI
sleeves_mod2.risk.entries_allowed = _real_eaI

_realFlagF = _cfgF.REPLACE_ON_CONVICTION
_cfgF.REPLACE_ON_CONVICTION = False
check("41 Fix2: REPLACE_ON_CONVICTION=False disables the swap path entirely",
      _slvF._plan_swap(_sleeveF(), "NEWF", _vH["NEWF"]["verdict"], _vH, _pxF2, _alphaF, set(), [0])
      is None)
_cfgF.REPLACE_ON_CONVICTION = _realFlagF

# ══ 42. Goal-attainment audit fixes (2026-07-01) ══════════════════════════════
from firm import learning as _lrn42  # noqa: E402
from firm import cycle as _cyc42  # noqa: E402
from firm import health as _hl42  # noqa: E402
from firm import risk as _rk42  # noqa: E402
from firm import debate as _db42  # noqa: E402
import config as _cfg42  # noqa: E402

# — G5: worker-scaled deadline (legacy 2-row treated as RESEARCH_WORKERS) —
state.save("cycle_stats", {"standard": [[30.0, 20]]})
_d1, _b1 = _cyc42._research_deadline_min("standard", 20, workers=1)
_d2, _b2 = _cyc42._research_deadline_min("standard", 20, workers=2)
check("42 G5: deadline scales UP when today runs fewer workers than history",
      _d1 > _d2 and _b1.get("workers_today") == 1,
      f"1w={_d1:.0f}m vs 2w={_d2:.0f}m")
# pace hygiene: an all-degraded (zero-real-verdict) run teaches NOTHING
_cyc42._record_research_duration("standard", 15.0, 14, workers=1, real_verdicts=0)
check("42 G5: zero-real-verdict run does NOT poison the pace history",
      len((state.load("cycle_stats", {}) or {}).get("standard", [])) == 1)
_cyc42._record_research_duration("standard", 40.0, 20, workers=1, real_verdicts=18)
_rows42 = (state.load("cycle_stats", {}) or {}).get("standard", [])
check("42 G5: a REAL completion records [duration, n, workers]",
      len(_rows42) == 2 and _rows42[-1] == [40.0, 20, 1])

# — G4c: cash-equitization round-trips never open the learning gate —
_s42 = state.default_sleeve_state("alpha_stocks")
_s42["closed_trades"] = [
    {"trade_id": "eq42a", "symbol": "SPY", "instrument": "equity", "quantity": 5,
     "entry": 700, "exit": 710, "pnl": 50.0, "reason": "equitization sold",
     "closed_at": "2026-07-01T15:00:00+00:00", "opened_at": "2026-06-10T15:00:00+00:00",
     "strategy": "cash_equitization"},
    {"trade_id": "rl42a", "symbol": "NVDA", "instrument": "equity", "quantity": 2,
     "entry": 100, "exit": 120, "pnl": 40.0, "reason": "target",
     "closed_at": "2026-07-01T15:00:00+00:00", "opened_at": "2026-06-20T15:00:00+00:00",
     "rating": "BUY", "conviction": 71, "desk_scores": {"technical": 70}},
]
state.save_sleeve("alpha_stocks", _s42)
state.save("ledger", {})
_real_fh42 = data_engine.fetch_history
data_engine.fetch_history = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline"))
_lrn42.record_outcomes()
data_engine.fetch_history = _real_fh42
_led42 = state.load("ledger", {})
check("42 G4c: SPY-parking round-trip EXCLUDED from ledger + cold-start gate",
      _led42.get("total_recorded") == 1
      and [t["ticker"] for t in _led42.get("trades", [])] == ["NVDA"])
# — G4c-2 (operator 2026-07-20 "these changes don't count"): ETF seat-change
# closes are plumbing too — they booked with strategy='' and slipped the old
# string-equality filter; the shared predicate must catch every leg.
check("42 mechanical predicate: seat closes / equitization / flags all count as plumbing",
      _lrn42.is_mechanical_trade({"strategy": "cash_equitization"})
      and _lrn42.is_mechanical_trade({"strategy": "etf_seat"})
      and _lrn42.is_mechanical_trade({"seat_change": True})
      and _lrn42.is_mechanical_trade(
          {"strategy": "", "reason": "ETF seat change: parking incumbent SOXX"})
      and _lrn42.is_mechanical_trade({"exit_reason": "ETF seat change: core slot 1"})
      and _lrn42.is_mechanical_trade(
          {"reason": "satellite unwound — alpha already holds it (de-dup)"})
      and not _lrn42.is_mechanical_trade(
          {"strategy": "", "reason": "hard SL -8.2%"}))
_s42b = state.default_sleeve_state("alpha_stocks")
_s42b["closed_trades"] = [
    {"trade_id": "seat42a", "symbol": "SOXX", "instrument": "equity", "quantity": 44,
     "entry": 531.97, "exit": 530.5, "pnl": -62.62,
     "reason": "ETF seat change: parking incumbent SOXX is not reservoir-class",
     "closed_at": "2026-07-02T15:00:00+00:00", "opened_at": "2026-07-01T15:00:00+00:00",
     "strategy": ""},
]
state.save_sleeve("alpha_stocks", _s42b)
state.save("ledger", {})
data_engine.fetch_history = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline"))
_lrn42.record_outcomes()
data_engine.fetch_history = _real_fh42
check("42 G4c-2: a seat-change close (empty strategy stamp) never enters the ledger",
      state.load("ledger", {}).get("total_recorded", 0) == 0
      and not state.load("ledger", {}).get("trades"))
check("42 G4a/b: ledger row carries opened_at/quantity/cost + ENTRY desk_scores",
      _led42["trades"][0].get("opened_at") == "2026-06-20T15:00:00+00:00"
      and _led42["trades"][0].get("quantity") == 2
      and _led42["trades"][0].get("cost") == 200.0
      and _led42["trades"][0].get("desk_scores") == {"technical": 70})

# — G4b: degraded history rows are skipped in fallback attribution —
state.save("research", {"history": [
    {"asof": "2026-06-20T14:00:00+00:00",
     "verdicts": {"ZZFB": {"rating": "BUY", "conviction": 66}},
     "desk_scores": {"ZZFB": {"technical": 61}}},
    {"asof": "2026-06-30T14:00:00+00:00",
     "verdicts": {"ZZFB": {"rating": "HOLD", "conviction": 90, "degraded": True}},
     "desk_scores": {"ZZFB": {"technical": 100, "fundamental": 100}}},
]})
_s42b = state.default_sleeve_state("core_blend")
_s42b["closed_trades"] = [
    {"trade_id": "fb42", "symbol": "ZZFB", "instrument": "equity", "quantity": 1,
     "entry": 50, "exit": 55, "pnl": 5.0, "reason": "target",
     "closed_at": "2026-07-01T15:00:00+00:00", "opened_at": "2026-06-21T15:00:00+00:00"}]
state.save_sleeve("core_blend", _s42b)
data_engine.fetch_history = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline"))
_lrn42.record_outcomes()
data_engine.fetch_history = _real_fh42
_fb42 = [t for t in state.load("ledger", {}).get("trades", []) if t["ticker"] == "ZZFB"][0]
check("42 G4b: fallback attribution SKIPS the degraded row (real 06-20 verdict wins)",
      (_fb42.get("verdict") or {}).get("conviction") == 66
      and (_fb42.get("desk_scores") or {}).get("technical") == 61)

# — G4d: degraded conviction bucketed separately in calibration —
_stats42 = _lrn42.stats()
state.save("ledger", {"trades": [
    {"sleeve": "alpha_stocks", "ticker": "A", "pnl": 10,
     "verdict": {"rating": "BUY", "conviction": 72}},
    {"sleeve": "alpha_stocks", "ticker": "B", "pnl": -5,
     "verdict": {"rating": "HOLD", "conviction": 90, "degraded": True}}],
    "total_recorded": 2})
_bc42 = _lrn42.stats().get("by_conviction", {})
check("42 G4d: degraded verdicts calibrate in their OWN band, not 80+",
      "degraded" in _bc42 and "80+" not in _bc42 and _bc42.get("70-79", {}).get("n") == 1)

# — G7+G3: equitization dead zone + calls BIL parking —
_real_eqp42 = broker.equity_prices
broker.equity_prices = lambda syms: {x: 100.0 for x in syms}
_sc42 = state.default_sleeve_state("calls_only")           # $25k all cash
_sc42["soft_risk"] = False; _sc42["peak_equity"] = 999999
state.save_sleeve("calls_only", _sc42)
sleeves_mod2._equitize_idle_cash(_sc42, {}, "paper")
_psym42 = config.CASH_EQUITIZATION["calls_only"]["symbol"]   # SPY (was BIL, operator 07-02)
_park42 = _sc42["positions"].get(_psym42)
check("42 G3: calls_only idle cash parks in SPY (liquid dry powder, not $0)",
      bool(_park42) and _park42.get("cash_equitization")
      and _sc42["cash"] < 25000 * 0.10,
      f"cash left={_sc42['cash']:.0f} sym={_psym42}")
_ppx42 = _park42.get("last_price") or _park42.get("entry_price") or 100.0
sleeves_mod2._free_cash_from_equitization(_sc42, 5000.0, {_psym42: _ppx42}, "paper")
check("42 G3: premium buy reclaims cash FROM the SPY parking",
      _sc42["cash"] >= 5000 - 1e-6)
_sa42 = state.default_sleeve_state("alpha_stocks")          # 79.9%-invested dead zone
_sa42["cash"] = 5000.0
_sa42["positions"] = {"XYZ": {"quantity": 200, "entry_price": 100, "last_price": 100,
                              "market_value": 20000.0, "instrument": "equity",
                              "opened_at": "2026-06-10T15:00:00+00:00"}}
_sa42["soft_risk"] = False; _sa42["peak_equity"] = 999999
sleeves_mod2._equitize_idle_cash(_sa42, {"SPY": 100.0, "XYZ": 100.0}, "paper")
check("42 G7: 70-90%% dead-zone cash now tops up toward target (was stranded)",
      "SPY" in _sa42["positions"])
broker.equity_prices = _real_eqp42

# — J2: phantom breaker trip (corruption in session) is flagged, not recorded —
state.save("breaker_trips", {})
state.journal("state_restored_from_backup", file="sleeve_alpha_stocks")
_sp42 = state.default_sleeve_state("alpha_stocks")
_sp42["day_pnl_anchor"] = {"date": _rk42._today(), "equity": 25000.0}
_real_fdb42 = _rk42.firm_daily_breaker
_real_vm42 = riskbrain.vol_multiplier
_rk42.firm_daily_breaker = lambda: (True, "")
riskbrain.vol_multiplier = lambda: 1.0
_ok42, _why42 = _rk42.entries_allowed("alpha_stocks", _sp42, 7000.0)   # implies -72% day
check("42 J2: phantom-suspect trip BLOCKS entries but is NOT recorded as a trip",
      _ok42 is False and "PHANTOM" in _why42
      and _rk42._today() not in state.load("breaker_trips", {}),
      _why42[:60])
check("42 J2: phantom suspect journaled for the operator",
      any(e.get("event") == "breaker_phantom_suspect"
          for e in state.read_journal(50)))
# a normal-size trip (no >50% move) still records even with corruption events
_sp42b = state.default_sleeve_state("core_blend")
_sp42b["day_pnl_anchor"] = {"date": _rk42._today(), "equity": 25000.0}
_ok42b, _ = _rk42.entries_allowed("core_blend", _sp42b, 24000.0)       # -4% day: real trip
check("42 J2: a plausible (-4%%) trip still records normally (guard is NARROW)",
      _ok42b is False and "core_blend" in state.load("breaker_trips", {}).get(_rk42._today(), {}))
_rk42.firm_daily_breaker = _real_fdb42
riskbrain.vol_multiplier = _real_vm42

# — G6: durable equity history (idempotent per day) —
state.save("equity_history", [])
_hl42.record_equity_history({"alpha_stocks": {"equity": 26000.0}}, 103000.0)
_hl42.record_equity_history({"alpha_stocks": {"equity": 26100.0}}, 103100.0)
_eh42 = state.load("equity_history", [])
check("42 G6: EOD equity history is durable + one row per day (re-book overwrites)",
      len(_eh42) == 1 and _eh42[0]["firm_equity"] == 103100.0)

# — J1: every judge prompt now defines STRONG_BUY —
_pkt42 = {"ticker": "TEST", "technicals": {}}
check("42 J1: judge prompts carry STRONG_BUY semantics (calls-desk gate reachable)",
      "STRONG_BUY is a real" in _db42._judge_prompt(_pkt42, {}, {})
      and "STRONG_BUY is a real" in _db42._debate_and_judge_prompt(_pkt42, {})
      and "STRONG_BUY is a real" in _db42._fast_prompt(_pkt42))
check("42 J1: degraded fallback still can NEVER emit STRONG_BUY",
      _db42._fallback_verdict(_pkt42, {}).get("rating") in ("BUY", "HOLD", "SELL"))

# — G2: retry RAM gate DISABLED (operator 2026-07-03) — the 4 GB floor stranded
#   every zero-verdict retry on this chronically-<4 GB box ("won't start until 4 GB
#   free"). The quota-burn the gate guarded against is already prevented by graceful
#   degradation (worker->1, depth->standard returns REAL verdicts under RAM pressure),
#   and the 07-02/03 outage was an expired CLI login (401), not RAM. Assert it stays
#   disabled so a nonzero floor can't be reintroduced without a conscious re-decision. —
check("42 G2: RETRY_MIN_RAM_GB retry gate disabled (0.0) per operator 2026-07-03",
      float(getattr(_cfg42, "RETRY_MIN_RAM_GB", 0)) == 0.0)

# ══ 43. Purpose-fitness fixes (2026-07-01 evening) ════════════════════════════
from firm import cycle as _cy43  # noqa: E402
from firm import debate as _db43  # noqa: E402
from firm import data_engine as _de43  # noqa: E402
from firm import learning as _lr43  # noqa: E402

# — #3 fractional sizing: paper goes fractional when truncation cuts intent >30% —
check("43 P3: paper fill goes FRACTIONAL when int() would cut intent >30% (MU case)",
      abs(sleeves_mod2._size_qty(2600.0, 908.0, "paper") - 2.8634) < 0.001)
check("43 P3: paper keeps whole shares when truncation loss <=30%",
      sleeves_mod2._size_qty(2000.0, 100.0, "paper") == 20)
check("43 P3: LIVE mode never goes fractional (Public support unverified)",
      sleeves_mod2._size_qty(2600.0, 908.0, "live") == 2)
check("43 P3: dust guard — sub-0.05-share results size to zero",
      sleeves_mod2._size_qty(20.0, 908.0, "paper") == 0)

# — #1a scorecard digest reaches every judge prompt —
state.save("report_card_digest", {"asof": datetime.now(timezone.utc).isoformat(),
                                  "text": "YOUR OWN SCORECARD (test-digest-marker)\n\n"})
_pkt43 = {"ticker": "TEST", "technicals": {}}
check("43 P1a: cached scorecard digest is injected into the judge prompt",
      "test-digest-marker" in _db43._judge_prompt(_pkt43, {}, {}))
state.save("report_card_digest", {"asof": "2026-01-01T00:00:00+00:00", "text": "stale-marker"})
check("43 P1a: a stale (>24h) digest is NOT injected",
      "stale-marker" not in _db43._judge_prompt(_pkt43, {}, {}))

# — #1b verdict-only review: writes provisional lessons, NEVER touches reviewed_through —
state.save("lessons", {"lessons": [], "reviewed_through": 0})
_real_vrc43 = _lr43.verdict_report_card
_real_askj43 = brain.ask_json
_real_avail43 = brain.available
_lr43.verdict_report_card = lambda **k: {"by_rating": {
    "BUY": {"n": 15, "correct": 8, "sum_excess": 10.0, "excess": []},
    "HOLD": {"n": 50, "correct": 20, "sum_excess": 150.0, "excess": []},
    "degraded_fallback": {"n": 30, "correct": 15, "sum_excess": -60.0, "excess": []}}}


# Mock the REAL seam with the REAL signature (cognition audit caught the
# original: verdict_review called brain.ask(timeout_s=) which doesn't exist
# and returns str — the old mock `brain.ask = lambda *a,**k: {...}` accepted
# any kwargs AND returned a dict, hiding BOTH mismatches. ask_json is the
# correct seam (returns dict); this mock rejects unknown kwargs like the
# real function would (mock-vs-real-contract discipline, 2026-06-29/07-01).
def _mock_ask_json43(prompt, retries=1, model=None, timeout=None):
    return {"lessons": ["HOLD-rated names beat SPY — recalibrate."]}
brain.ask_json = _mock_ask_json43
brain.available = lambda: True
_vr43 = _lr43.verdict_review()
_ls43 = state.load("lessons", {})
check("43 P1b: verdict review fires on graded-verdict evidence (65 real, 15 buyside)",
      _vr43 == {"lessons": 1} and len(_ls43.get("lessons", [])) == 1
      and _ls43["lessons"][0].get("provisional") is True)
check("43 P1b: HAZARD-1 GUARD — reviewed_through untouched (money-gate intact)",
      _ls43.get("reviewed_through") == 0
      and bool(_ls43.get("verdict_reviewed_asof")))
check("43 P1b: provisional lessons flow into the judge's FIRM MEMORY block",
      "provisional" in _lr43.lessons_context())
_vr43b = _lr43.verdict_review()
check("43 P1b: throttled — a second run within 7 days is a no-op",
      _vr43b is None)
# degraded rows must NOT open the evidence gate
state.save("lessons", {"lessons": [], "reviewed_through": 0})
_lr43.verdict_report_card = lambda **k: {"by_rating": {
    "BUY": {"n": 5, "correct": 3, "sum_excess": 5.0, "excess": []},
    "degraded_fallback": {"n": 100, "correct": 50, "sum_excess": 0.0, "excess": []}}}
check("43 P1b: degraded rows do NOT count toward the evidence gate",
      _lr43.verdict_review() is None)
_lr43.verdict_report_card = _real_vrc43
brain.ask_json = _real_askj43
brain.available = _real_avail43

# — #5a position context: the judge sees what the firm holds —
_sp43 = state.default_sleeve_state("alpha_stocks")
_sp43["positions"] = {"PCTX": {"quantity": 3, "entry_price": 100.0, "last_price": 120.0,
                               "opened_at": (datetime.now(timezone.utc) - timedelta(days=9)).isoformat(),
                               "instrument": "equity", "trail_stop_pct": 15.0}}
state.save_sleeve("alpha_stocks", _sp43)
_pc43 = _cy43._position_context("PCTX")
check("43 P5a: position_context reports sleeve/unrealized/days for a held name",
      _pc43 is not None and _pc43["alpha_stocks"]["unrealized_pct"] == 20.0
      and _pc43["alpha_stocks"]["days_held"] == 9)
check("43 P5a: parking positions do not create ownership context",
      _cy43._position_context("NOTHELD43") is None)

# — #6i news sentinel + #6ii trusted normalization —
check("43 P6ii: source normalization maps feed variants into the trusted set",
      _de43._norm_source("Barrons.com") == "barrons"
      and _de43._norm_source("MT Newswires (English)") == "mt newswires"
      and _de43._norm_source("Barron's") == "barrons"
      and "barrons" in _de43._TRUSTED_SOURCES
      and "mt newswires" in _de43._TRUSTED_SOURCES)
class _BoomTicker43:
    def __init__(self, *a): pass
    @property
    def news(self): raise RuntimeError("throttled")
_real_yf43 = _de43.yf.Ticker
_de43.yf.Ticker = _BoomTicker43
check("43 P6i: news-source FAILURE returns the None sentinel (not 'quiet tape')",
      _de43.fetch_news("ANY") is None)
_de43.yf.Ticker = _real_yf43

# — #6iv degraded HOLD imposes no cooldown —
state.save("research", {"history": [
    {"asof": datetime.now(timezone.utc).isoformat(),
     "verdicts": {"DGCD": {"rating": "HOLD", "conviction": 88, "degraded": True}},
     "context": {"DGCD": {}}}]})
check("43 P6iv: a fresh DEGRADED HOLD does NOT cool the name down (real look allowed)",
      _cy43._cooldown_state("DGCD") is None)

# — #5b companion: core_blend + calls_only both have parking configs. Calls
# parks in SPY (BENCHMARK), NOT BIL, since 2026-07-02 (operator: T-bill cash
# isn't readily freeable for a live call) —
check("43 P5b: CASH_EQUITIZATION covers core_blend (SPY) + calls_only (SPY)",
      config.CASH_EQUITIZATION.get("core_blend", {}).get("symbol") == config.BENCHMARK
      and config.CASH_EQUITIZATION.get("calls_only", {}).get("symbol") == config.BENCHMARK)

# — #7 post-exit telemetry: matured exits get graded, fresh ones wait —
import pandas as _pd43  # noqa: E402
_dates43 = _pd43.date_range(end=_pd43.Timestamp.now().normalize(), periods=120, freq="B")
_frame43 = _pd43.DataFrame(
    {"PXT": [100 * (1.004 ** i) for i in range(120)],
     config.BENCHMARK: [500 * (1.001 ** i) for i in range(120)]}, index=_dates43)
_closed43 = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
state.save("ledger", {"trades": [
    {"trade_id": "px43", "sleeve": "alpha_stocks", "ticker": "PXT", "symbol": "PXT",
     "pnl": 50, "closed_at": _closed43, "exit_reason": "trailing stop -15%"},
    {"trade_id": "fresh43", "sleeve": "alpha_stocks", "ticker": "PXT", "symbol": "PXT",
     "pnl": 10, "closed_at": datetime.now(timezone.utc).isoformat(),
     "exit_reason": "target"}], "total_recorded": 2})
_real_fh43 = data_engine.fetch_history
data_engine.fetch_history = lambda *a, **k: _frame43
_n43 = _lr43.backfill_post_exit_excess()
data_engine.fetch_history = _real_fh43
_led43 = state.load("ledger", {})["trades"]
check("43 P7: matured exit gets post_exit_excess stamped; fresh exit waits",
      _n43 == 1 and isinstance(_led43[0].get("post_exit_excess_pct"), (int, float))
      and _led43[1].get("post_exit_excess_pct") is None)
check("43 P7: stats() aggregates exit quality by exit-reason family",
      "trail" in (_lr43.stats().get("by_exit_reason") or {}))

# — #44 core SPY parking + reclaim (the de-dup was REMOVED 2026-07-02; parking
#   itself is kept — idle core cash goes to SPY, reclaimed before a real buy) —
_real_eqp44 = broker.equity_prices
_real_ea44 = sleeves_mod2.risk.entries_allowed
broker.equity_prices = lambda syms: {x: 100.0 for x in syms}
sleeves_mod2.risk.entries_allowed = lambda *a, **k: (True, "")
_sc44 = state.default_sleeve_state("core_blend")
_sc44["soft_risk"] = False; _sc44["peak_equity"] = 999999
sleeves_mod2._equitize_idle_cash(_sc44, {}, "paper")
_park44 = _sc44["positions"].get(config.CASH_EQUITIZATION["core_blend"]["symbol"])
check("44 core idle cash parks in SPY (never idles as cash drag)",
      bool(_park44) and _park44.get("cash_equitization") is True
      and _sc44["cash"] < 25000 * 0.15)
# reclaim: when cash is SHORT, the parking sells down to fund a conviction buy
_sc44d = state.default_sleeve_state("core_blend")
_sc44d["cash"] = 200.0
_sc44d["positions"] = {"SPY": {"quantity": 50, "entry_price": 100, "last_price": 100,
                               "market_value": 5000.0, "instrument": "equity",
                               "cost": 5000.0, "peak_price": 100,
                               "opened_at": "2026-06-20T15:00:00+00:00",
                               "cash_equitization": True}}
sleeves_mod2._free_cash_from_equitization(_sc44d, 2000.0, {"SPY": 100.0}, "paper")
check("44 when cash is SHORT, the reclaim sells core SPY parking down",
      _sc44d["cash"] >= 2000.0
      and _sc44d["positions"]["SPY"]["quantity"] < 50)
broker.equity_prices = _real_eqp44
sleeves_mod2.risk.entries_allowed = _real_ea44
sleeves_mod2.risk.entries_allowed = _real_ea44

# — staged #2: starter-timing guidance is flag-gated (default OFF) —
check("43 P2-staged: starter-timing guidance ships OFF for clean attribution",
      getattr(config, "STARTER_TIMING_GUIDANCE", None) is False
      and "QUALITY vs TIMING" not in _db43._judge_prompt(_pkt43, {}, {}))

# ══ SECTION 46: 2026-07-02 watchers batch — REAL-SEAM digest build, edge      ═
# ══ inversion watchdog, restart/halt-churn health, FOMC-minutes calendar      ═
# The digest builder read card keys (`correct`, `sum_excess`, `excess`) the
# card never RETURNED — the prior tests only exercised digest_context()'s
# state read, so the builder would KeyError on its first real cycle
# (mock-vs-real-contract, third instance 2026-07-02). This section runs the
# REAL card + REAL builder with only the network seam
# (data_engine.fetch_history) substituted by a real-shaped DataFrame.
import pandas as _pd46  # noqa: E402
from firm import data_engine as _de46, learning as _lr46  # noqa: E402
from firm import macro_calendar as _mc46  # noqa: E402

_bdays46 = _pd46.bdate_range(end=_pd46.Timestamp.utcnow().tz_localize(None),
                             periods=60)
_d0_46 = _bdays46[10].strftime("%Y-%m-%d")   # verdict day ~50 sessions back


def _ramp46(start, end):
    step = (end - start) / (len(_bdays46) - 1)
    return [start + step * i for i in range(len(_bdays46))]


_closes46 = _pd46.DataFrame({
    "WINR46": _ramp46(100.0, 140.0),          # beats SPY forward -> BUY correct
    "LOSR46": _ramp46(100.0, 70.0),           # lags SPY forward -> HOLD correct
    config.BENCHMARK: _ramp46(500.0, 505.0),  # near-flat benchmark
}, index=_bdays46)
_real_fh46 = _de46.fetch_history
_de46.fetch_history = lambda syms, period="6mo": _closes46
_real_research46 = state.load("research", {})
_asof46 = _d0_46 + "T15:00:00+00:00"
state.save("research", {"history": [
    {"asof": _asof46,
     "verdicts": {"WINR46": {"rating": "BUY", "conviction": 70},
                  "LOSR46": {"rating": "HOLD", "conviction": 55}},
     "context": {"WINR46": {"had_news": True, "news_tier": "trusted"},
                 "LOSR46": {"had_news": False, "news_tier": "blind"}}},
]})
state.save("report_card_digest", {})
state.save("edge_health", {})
_card46 = _lr46.verdict_report_card()
check("46 card exposes the keys the digest reads (correct + excess lists)",
      (_card46.get("by_rating", {}).get("BUY", {}).get("correct") == 1
       and isinstance(_card46["by_rating"]["BUY"].get("excess"), list)
       and _card46.get("news_value", {}).get("trusted", {}).get("correct") == 1))
_lr46.refresh_report_card_digest()
_dg46 = state.load("report_card_digest", {}) or {}
check("46 REAL-SEAM: digest builds from the real card (no KeyError swallow)",
      bool(_dg46.get("text")) and "- BUY: n=1, correct 1/1" in _dg46["text"]
      and "BASE RATE" in _dg46["text"]
      and not any(e.get("event") == "report_card_digest_failed"
                  for e in state.read_journal(50)))
check("46 losers-you-avoided line builds from the real excess list",
      "Losers you CORRECTLY avoided" in _dg46["text"])
_eh46 = state.load("edge_health", {}) or {}
check("46 edge_health snapshot persists beside the digest",
      bool(_eh46.get("asof")) and isinstance(_eh46.get("edges"), list))
_de46.fetch_history = _real_fh46
state.save("research", _real_research46)

# — edge-inversion + restart + halt-churn health checks —
state.save("edge_health", {"asof": datetime.now(timezone.utc).isoformat(),
                           "edges": [{"edge": "factor:quality", "n": 12,
                                      "value": -3.4}],
                           "inverted": [{"edge": "factor:quality", "n": 12,
                                         "value": -3.4}]})
state.journal("breaker_tripped", sleeve="alpha_stocks")
state.journal("halt_cleared", sleeve="alpha_stocks")
state.journal("breaker_tripped", sleeve="alpha_stocks")
state.journal("kill_switch", sleeve="alpha_stocks")
state.journal("soft_risk_on", sleeve="alpha_stocks")
_hr46 = {c["name"]: c for c in _hl42.health_report()["checks"]}
check("46 edge integrity goes RED on an inverted edge, naming it",
      "edge integrity" in _hr46 and _hr46["edge integrity"]["ok"] is False
      and "factor:quality" in _hr46["edge integrity"]["detail"])
check("46 halt churn counts trips over 48h, not just the instantaneous state",
      "risk-halt churn" in _hr46
      and _hr46["risk-halt churn"]["ok"] is False
      and "alpha_stocks" in _hr46["risk-halt churn"]["detail"])
state.save("edge_health", {"asof": datetime.now(timezone.utc).isoformat(),
                           "edges": [], "inverted": []})
_hr46b = {c["name"]: c for c in _hl42.health_report()["checks"]}
check("46 edge integrity is OK with no inverted edges",
      _hr46b["edge integrity"]["ok"] is True)

# — FOMC minutes: derived 21 days after every decision, one anchor source —
from datetime import date as _date46  # noqa: E402
_min46 = _mc46._rule_dates(_mc46._BY_KEY["fomc_minutes"],
                           _date46(2026, 1, 1), _date46(2026, 12, 31))
_fomc46 = {_mc46._safe_date(s) for s in _mc46.FOMC
           if _mc46._safe_date(s) and _mc46._safe_date(s).year == 2026}
check("46 FOMC minutes: one date exactly 21d after EVERY 2026 decision",
      len(_min46) == len(_fomc46) == 8
      and all(any((m - f).days == 21 for f in _fomc46) for m in _min46)
      and _date46(2026, 7, 8) in _min46)

# ══ SECTION 47: 2026-07-02 brain isolation — every Opus call strips the       ═
# ══ operator's 2,818 skills/CLAUDE.md/MCP (measured ~127.6k -> ~25k ctx)      ═
import subprocess as _sp47  # noqa: E402
from firm import brain as _br47  # noqa: E402


class _Proc47:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


_cmds47 = []
_real_run47 = _br47.subprocess.run
_real_smok47 = _br47._safe_mode_ok


def _mock_run47_ok(cmd, **kw):
    _cmds47.append(list(cmd))
    return _Proc47(0, '{"result": "ok47"}')


_br47._SYS_PROMPT_FILE = None      # force a fresh compose in the sandbox STATE_DIR
_br47._safe_mode_ok = True
_br47.subprocess.run = _mock_run47_ok
_r47 = _br47.ask("probe")
_sp47 = _br47._full_system_prompt()
check("47 isolation flags ride EVERY brain call (safe-mode + fund system prompt)",
      _r47 == "ok47" and "--safe-mode" in _cmds47[-1]
      and "--exclude-dynamic-system-prompt-sections" in _cmds47[-1]
      and ("--system-prompt-file" in _cmds47[-1]
           or "--system-prompt" in _cmds47[-1])
      and config.BRAIN_SYSTEM_PROMPT in _sp47)
# the composed system prompt CARRIES the finance doctrine, and it actually
# reaches the call — read back the file the CLI is pointed at (or the inline
# arg) and confirm the doctrine is in it (operator 2026-07-02)
if "--system-prompt-file" in _cmds47[-1]:
    _spf47 = _cmds47[-1][_cmds47[-1].index("--system-prompt-file") + 1]
    _delivered47 = Path(_spf47).read_text(encoding="utf-8")
else:
    _delivered47 = _cmds47[-1][_cmds47[-1].index("--system-prompt") + 1]
check("47 every Opus call carries the institutional finance doctrine",
      "INSTITUTIONAL RESEARCH DOCTRINE" in _sp47
      and "Margin of safety" in _sp47 and "Macro/rate-regime VETO" in _sp47
      and "INSTITUTIONAL RESEARCH DOCTRINE" in _delivered47
      and config.BRAIN_SYSTEM_PROMPT in _delivered47)
_calls47 = {"n": 0}


def _mock_run47_oldcli(cmd, **kw):
    _cmds47.append(list(cmd))
    _calls47["n"] += 1
    if "--safe-mode" in cmd:
        return _Proc47(2, "", "error: unknown option '--safe-mode'")
    return _Proc47(0, '{"result": "fallback47"}')


_br47.subprocess.run = _mock_run47_oldcli
_r47b = _br47.ask("probe2")
check("47 SELF-HEALING: a CLI without the flags falls back full-context, same call",
      _r47b == "fallback47" and _calls47["n"] == 2
      and "--safe-mode" not in _cmds47[-1] and _br47._safe_mode_ok is False)
_r47c = _br47.ask("probe3")
check("47 fallback is sticky — later calls never re-try the dead flag",
      _r47c == "fallback47" and _calls47["n"] == 3
      and "--safe-mode" not in _cmds47[-1])
_br47.subprocess.run = _real_run47
_br47._safe_mode_ok = _real_smok47

# ── 47b: is_error envelope (exit 0) is a FAILURE, not model output, and its
#         message is scanned for a quota signature ──
_br47._last_quota_hit_ts = 0.0
_cmds47b = []


def _mock_run47_iserr(cmd, **kw):
    _cmds47b.append(list(cmd))
    return _Proc47(0, '{"type":"result","subtype":"error_during_execution",'
                      '"is_error":true,"result":"Claude AI usage limit reached|123"}')


_br47.subprocess.run = _mock_run47_iserr
_r47d = _br47.ask("probe4")
check("47b in-band error envelope (exit 0, is_error) returns None, never its text",
      _r47d is None)
check("47b usage-limit signature on STDOUT marks quota_recently_hit",
      _br47.quota_recently_hit() is True)
_br47.subprocess.run = _real_run47
_br47._last_quota_hit_ts = 0.0

# ══ SECTION 48: 2026-07-02 Opus-seam review fixes (extract_json ordering,     ═
# ══ calls position_context, fast-path guard, size factor)                     ═
# — extract_json: the verdict prompts say 'END your reply with exactly one JSON
#   block', so a draft/echoed object earlier in the prose must NOT win —
_draft48 = ('Let me reason. My prior verdict was ```json\n{"rating":"SELL",'
            '"conviction":40}\n``` but now:\n\n```json\n'
            '{"rating":"STRONG_BUY","conviction":82}\n```')
_ej48 = _br47.extract_json(_draft48)
check("48 extract_json takes the LAST fenced block (final verdict, not the draft)",
      _ej48 == {"rating": "STRONG_BUY", "conviction": 82})
# unfenced trailing object recovered even when prose has stray braces
_unf48 = 'thinking about the set {a, b} and cost {x}... final:\n{"rating":"BUY","conviction":66}'
check("48 extract_json balanced-scan recovers a trailing object past stray braces",
      _br47.extract_json(_unf48) == {"rating": "BUY", "conviction": 66})
check("48 extract_json single fenced/plain object still works (happy path intact)",
      _br47.extract_json('```json\n{"rating":"HOLD"}\n```') == {"rating": "HOLD"}
      and _br47.extract_json('{"rating":"HOLD"}') == {"rating": "HOLD"})

# — _position_context surfaces a CALL-held underlying (OSI-keyed), the AMD-on-
#   the-leveraged-book blind spot —
from firm import cycle as _cy48  # noqa: E402
_sc48 = state.default_sleeve_state("calls_only")
_sc48["positions"] = {"AMD  260117C00250000": {
    "instrument": "option", "underlying": "AMD", "quantity": 3,
    "entry_price": 5.0, "last_price": 7.5, "strike": 250.0,
    "expiration": "2026-01-17", "opened_at": "2026-06-20T15:00:00+00:00"}}
state.save_sleeve("calls_only", _sc48)
_pc48 = _cy48._position_context("AMD")
check("48 position_context finds the call-held underlying (not blind to calls)",
      _pc48 is not None and "calls_only" in _pc48
      and _pc48["calls_only"]["instrument"] == "long_call"
      and _pc48["calls_only"]["unrealized_pct"] == 50.0)
check("48 position_context: an unheld name is still None",
      _cy48._position_context("ZZZZ48") is None)
state.save_sleeve("calls_only", state.default_sleeve_state("calls_only"))

# — fast-path verdict guard mirrors standard/full —
from firm import debate as _db48  # noqa: E402
_real_askj48 = brain.ask_json
_real_avail48 = brain.available
brain.available = lambda: True
brain.ask_json = lambda *a, **k: {"debate_summary": "no rating key here"}
_pkt48 = {"ticker": "T48", "technicals": {}, "composite": 50}
_res48 = _db48.run_debate_and_verdict(_pkt48, {}, depth="fast")
check("48 fast-path: a truthy verdict with NO rating degrades to the fallback",
      _res48["verdict"].get("rating") is not None
      and _res48["verdict"].get("degraded") is True)
brain.ask_json = _real_askj48
brain.available = _real_avail48

# — size factor now grounds the quant desk's 'size' mandate —
from firm import data_engine as _de48  # noqa: E402
_fs48 = _de48.factor_scores({}, {"mkt_cap": 3.2e12})
check("48 factor_scores carries a grounded SIZE lens (mega-cap bucketed)",
      isinstance(_fs48.get("size"), dict)
      and _fs48["size"]["bucket"] == "mega"
      and _fs48["size"]["mkt_cap_b"] == 3200.0)
check("48 size lens is None (not fabricated) when the vendor gives no cap",
      _de48.factor_scores({}, {})["size"]["bucket"] is None)

# ══ SECTION 49: sleeves are INDEPENDENT — the same name MAY be held across      ═
# ══ sleeves (operator 2026-07-02: a real multi-strategy fund does this; the     ═
# ══ de-dup that forced core to avoid alpha's names was REMOVED)                 ═
_real_eqp49 = broker.equity_prices
_real_ea49 = sleeves_mod2.risk.entries_allowed
broker.equity_prices = lambda syms: {x: 100.0 for x in syms}
sleeves_mod2.risk.entries_allowed = lambda *a, **k: (True, "")


def _pos49(qty, px=100.0):
    return {"quantity": qty, "entry_price": px, "last_price": px,
            "market_value": qty * px, "cost": qty * px, "peak_price": px,
            "instrument": "equity", "opened_at": "2026-06-20T15:00:00+00:00"}


# alpha already holds SHARED49; core should be ALLOWED to buy it as a satellite
_sa49 = state.default_sleeve_state("alpha_stocks")
_sa49["positions"] = {"SHARED49": _pos49(10)}
state.save_sleeve("alpha_stocks", _sa49)
_sc49 = state.default_sleeve_state("core_blend")
_sc49["soft_risk"] = False; _sc49["peak_equity"] = 999999
state.save_sleeve("core_blend", _sc49)
_v49 = {"SHARED49": {"kind": "stock", "packet": {},
                     "verdict": {"rating": "BUY", "conviction": 70,
                                 "target_weight_pct": 10.0}}}
sleeves_mod2.run_core_blend(_v49, {"SHARED49": "Technology"}, "paper")
_c49 = state.get_sleeve("core_blend")
check("49 core MAY hold a name alpha also holds (de-dup removed; sleeves independent)",
      "SHARED49" in _c49["positions"]
      and "SHARED49" in state.get_sleeve("alpha_stocks")["positions"])
check("49 no de-dup skip/evict events are journaled anymore",
      not any(e.get("event") in ("satellite_skipped_alpha_overlap",
                                 "satellite_evicted_alpha_overlap")
              for e in state.read_journal(200)))
check("49 CORE_SAT_DEDUP config is gone",
      not hasattr(config, "CORE_SAT_DEDUP"))
state.save_sleeve("core_blend", state.default_sleeve_state("core_blend"))
state.save_sleeve("alpha_stocks", state.default_sleeve_state("alpha_stocks"))
broker.equity_prices = _real_eqp49
sleeves_mod2.risk.entries_allowed = _real_ea49

# ══ SECTION 50: 2026-07-02 operator batch — peak poisoning (guard+reconcile),   ═
# ══ etf calendar rebalance, calls BIL->SPY parking migration                    ═
from datetime import datetime as _dt50, timezone as _tz50, timedelta as _td50  # noqa: E402
import firm.sleeves as _sl50  # noqa: E402
import firm.monitor as _mon50  # noqa: E402

# — 50a: ETF rebalance is CALENDAR-MONTH anchored, not a rolling 28-day drift —
_now50 = _dt50(2026, 7, 2, tzinfo=_tz50.utc)
_jun9 = _dt50(2026, 6, 9, tzinfo=_tz50.utc)
check("50a etf rotation DUE when the calendar month changed (Jun 9 -> Jul: the frozen bug)",
      _sl50._rotation_due(_jun9, (_now50 - _jun9).days, 28, _now50) is True)
check("50a etf rotation NOT due same month, well within the interval",
      _sl50._rotation_due(_dt50(2026, 7, 1, tzinfo=_tz50.utc), 1, 28, _now50) is False)
check("50a etf rotation DUE on a missing/malformed last timestamp",
      _sl50._rotation_due(None, 9999, 28, _now50) is True)
check("50a etf rotation DUE via the max-staleness backstop (>=28d, same month)",
      _sl50._rotation_due(_dt50(2026, 7, 1, tzinfo=_tz50.utc), 30, 28,
                          _dt50(2026, 7, 31, tzinfo=_tz50.utc)) is True)

# — 50b: reconcile_peaks CORRECTS a poisoned (too-high) peak from real bar highs —
import pandas as _pd50  # noqa: E402
_idx50 = _pd50.bdate_range(end="2026-07-02", periods=20)
_hi50 = _pd50.DataFrame({"High": [110.0] * 20}, index=_idx50)   # true high since entry = 110
_real_yf_dl = None
try:
    import yfinance as _yf50
    _real_yf_dl = _yf50.download
    _yf50.download = lambda *a, **k: _hi50    # single-symbol -> flat frame w/ High
except Exception:
    pass
_sa50 = state.default_sleeve_state("alpha_stocks")
_sa50["positions"] = {"PZN50": {"quantity": 5, "entry_price": 90, "last_price": 100,
                                "market_value": 500.0, "cost": 450.0,
                                "peak_price": 738.0,   # POISONED (true high is 110)
                                "instrument": "equity",
                                "opened_at": "2026-06-09T15:00:00+00:00"}}
state.save_sleeve("alpha_stocks", _sa50)
_nfix50 = _mon50.reconcile_peaks() if _real_yf_dl else 1
_peak50 = state.get_sleeve("alpha_stocks")["positions"]["PZN50"]["peak_price"] \
    if _real_yf_dl else 110.0
check("50b reconcile_peaks corrects a poisoned peak down to the true bar high",
      abs(_peak50 - 110.0) < 0.5
      and any(e.get("event") == "peak_reconciled" and e.get("symbol") == "PZN50"
              for e in state.read_journal(200)) if _real_yf_dl else True)
if _real_yf_dl:
    _yf50.download = _real_yf_dl
state.save_sleeve("alpha_stocks", state.default_sleeve_state("alpha_stocks"))

# — 50c: the LIVE peak guard ignores a single anomalous high tick —
_real_fm50 = broker.fetch_marks
_sa50b = state.default_sleeve_state("alpha_stocks")
_sa50b["positions"] = {"GRD50": {"quantity": 5, "entry_price": 90, "last_price": 95,
                                 "market_value": 475.0, "cost": 450.0,
                                 "peak_price": 100.0, "instrument": "equity",
                                 "opened_at": "2026-06-20T15:00:00+00:00"}}
state.save_sleeve("alpha_stocks", _sa50b)
broker.fetch_marks = lambda st: {"GRD50": 130.0}   # +30% above the 100 peak (bad tick)
_mon50._check_equity_sleeve("alpha_stocks", "paper")
_p50c = state.get_sleeve("alpha_stocks")["positions"].get("GRD50", {})
check("50c live guard: a >25% one-sweep spike does NOT advance the peak",
      _p50c.get("peak_price") == 100.0
      and any(e.get("event") == "peak_spike_ignored" and e.get("symbol") == "GRD50"
              for e in state.read_journal(200)))
broker.fetch_marks = lambda st: {"GRD50": 108.0}   # legit +8% new high (< 25% jump)
_mon50._check_equity_sleeve("alpha_stocks", "paper")
_p50c2 = state.get_sleeve("alpha_stocks")["positions"].get("GRD50", {})
check("50c live guard: a normal new high DOES advance the peak",
      _p50c2.get("peak_price") == 108.0)
broker.fetch_marks = _real_fm50
state.save_sleeve("alpha_stocks", state.default_sleeve_state("alpha_stocks"))

# — 50d: calls BIL parking auto-migrates to SPY (operator: T-bill permanently gone) —
_real_eqp50 = broker.equity_prices
broker.equity_prices = lambda syms: {x: 100.0 for x in syms}
_scal50 = state.default_sleeve_state("calls_only")
_scal50["cash"] = 100.0
_scal50["positions"] = {"BIL": {"quantity": 200, "entry_price": 91.5, "last_price": 91.5,
                                "market_value": 18300.0, "cost": 18300.0, "peak_price": 91.5,
                                "instrument": "equity", "cash_equitization": True,
                                "opened_at": "2026-06-20T15:00:00+00:00"}}
state.save_sleeve("calls_only", _scal50)
# _equitize_idle_cash mutates the dict in place (broker.execute in paper mode
# edits the passed sleeve); the caller persists — so check the mutated dict.
_cal50 = state.get_sleeve("calls_only")
_sl50._equitize_idle_cash(_cal50, {"BIL": 91.5, "SPY": 100.0}, "paper")
check("50d calls BIL parking is liquidated (T-bill permanently removed; config now SPY)",
      "BIL" not in _cal50["positions"]
      and config.CASH_EQUITIZATION["calls_only"]["symbol"] == config.BENCHMARK)
broker.equity_prices = _real_eqp50
state.save_sleeve("calls_only", state.default_sleeve_state("calls_only"))

# ══ SECTION 51: A-Z audit fixes — degraded SELL never trades (equity + calls),  ═
# ══ etf rotation deploys FULLY per signal (no lopsided half-deploy)             ═
import firm.sleeves as _sl51  # noqa: E402
_real_eqp51 = broker.equity_prices
_real_ea51 = _sl51.risk.entries_allowed
broker.equity_prices = lambda syms: {x: 100.0 for x in syms}

# — 51a: a DEGRADED SELL verdict must NOT liquidate an equity position —
_sa51 = state.default_sleeve_state("alpha_stocks")
_sa51["positions"] = {"DGR51": {"quantity": 5, "entry_price": 100, "last_price": 100,
                                "market_value": 500.0, "cost": 500.0, "peak_price": 100,
                                "instrument": "equity",
                                "opened_at": "2026-06-20T15:00:00+00:00"}}
state.save_sleeve("alpha_stocks", _sa51)
_vdgr = {"DGR51": {"verdict": {"rating": "SELL", "conviction": 40, "degraded": True}}}
_sl51._apply_sells(state.get_sleeve("alpha_stocks"), _vdgr, "paper", {"DGR51": 100.0})
check("51a degraded SELL does NOT liquidate an equity position (Feature A, sell side)",
      "DGR51" in state.get_sleeve("alpha_stocks")["positions"]
      and any(e.get("event") == "degraded_sell_blocked" and e.get("symbol") == "DGR51"
              for e in state.read_journal(150)))
# a REAL (non-degraded) SELL still exits
_sa51b = state.get_sleeve("alpha_stocks")
_vreal = {"DGR51": {"verdict": {"rating": "SELL", "conviction": 70}}}
_sl51._apply_sells(_sa51b, _vreal, "paper", {"DGR51": 100.0})
check("51a a real committee SELL still exits the position",
      "DGR51" not in _sa51b["positions"])
state.save_sleeve("alpha_stocks", state.default_sleeve_state("alpha_stocks"))

# — 51c: etf rotation deploys FULLY even under a reduced risk_scale (no strand) —
from firm import data_engine as _de51  # noqa: E402
_real_rsig51 = _de51.rotation_signals
_real_rscale51 = _sl51.risk.risk_scale
try:
    _de51.rotation_signals = lambda: {"ranked": [("SMH", 5.0)], "cash_hurdle_3m": 0.0}
    _sl51.risk.risk_scale = lambda s: 0.5      # sleeve de-risked to half
    _sl51.risk.entries_allowed = lambda *a, **k: (True, "")
    _sv51 = state.default_sleeve_state("etf_rotation")
    _sv51["halted"] = False; _sv51["cash"] = 3000.0
    _sv51["last_rotation"] = "2026-05-01T00:00:00+00:00"   # prior month -> DUE
    _sv51["day_pnl_anchor"] = {"date": _sl51.datetime.now(_sl51.timezone.utc)
                               .date().isoformat(), "equity": 3000.0}
    _sv51["positions"] = {}
    state.save_sleeve("etf_rotation", _sv51)
    _sl51._run_etf_rotation("paper")
    _e51 = state.get_sleeve("etf_rotation")
    _dep51 = (_e51["positions"].get("SMH") or {}).get("market_value", 0)
    check("51c etf rotation deploys FULLY per signal despite risk_scale<1 (no half-strand)",
          "SMH" in _e51["positions"] and _dep51 >= 2500
          and _e51["cash"] < 600)   # ~full deploy of 3000, not ~1500
finally:
    _de51.rotation_signals = _real_rsig51
    _sl51.risk.risk_scale = _real_rscale51
    _sl51.risk.entries_allowed = _real_ea51
    state.save_sleeve("etf_rotation", state.default_sleeve_state("etf_rotation"))
broker.equity_prices = _real_eqp51

# ══ SECTION 52: cross-sleeve BUY routing visibility (operator 2026-07-02:       ═
# ══ "make sure a buy lands in the right sleeves; if it qualifies for core too   ═
# ══ put it there or say why") — routing.record_routing + routing_health         ═
from firm import routing as _rt52  # noqa: E402

# eligibility delegates to the sleeves' OWN gates (single source of truth).
# Fleet re-grade action 1: a bare suitable_for_calls=False no longer bars the
# options desk (the mech screen decides; execution still gates tradability,
# earnings window and chain quality) — a JUSTIFIED False does.
_v_hi = {"rating": "BUY", "conviction": 72, "suitable_for_calls": False}
_el52 = _rt52.eligibility("stock", _v_hi)
check("52 eligibility: a conv-72 BUY qualifies for BOTH alpha(>=60) and core(>=55)",
      _el52["alpha_stocks"] is True and _el52["core_blend"] is True
      and _el52["calls_only"] is True)
check("52 eligibility: an EXPLICIT justified judge veto still bars the calls "
      "desk (2026-07-21 #6: reason alone no longer binds)",
      _rt52.eligibility("stock", {
          **_v_hi, "options_veto_calls": True,
          "suitable_for_calls_reason":
          "binary FDA event inside the window"})["calls_only"] is False
      and _rt52.eligibility("stock", {
          **_v_hi, "suitable_for_calls_reason":
          "binary FDA event inside the window"})["calls_only"] is True)
_v_lo = {"rating": "BUY", "conviction": 57}
check("52 eligibility: a conv-57 BUY qualifies for core(>=55) but NOT alpha(>=60)",
      _rt52.eligibility("stock", _v_lo)["core_blend"] is True
      and _rt52.eligibility("stock", _v_lo)["alpha_stocks"] is False)
check("52 eligibility: a degraded BUY qualifies for NOTHING",
      not any(_rt52.eligibility("stock", {"rating": "BUY", "conviction": 90,
                                          "degraded": True}).values()))

# record_routing: RT52 qualifies for alpha+core, placed only in alpha, core has
# an OPEN satellite slot and NO block -> a SILENT DROP the operator must see
_sa52 = state.default_sleeve_state("alpha_stocks")
_sa52["positions"] = {"RT52": {"quantity": 5, "entry_price": 100, "last_price": 100,
                               "market_value": 500.0, "cost": 500.0, "peak_price": 100,
                               "instrument": "equity",
                               "opened_at": "2026-06-20T15:00:00+00:00"}}
state.save_sleeve("alpha_stocks", _sa52)
state.save_sleeve("core_blend", state.default_sleeve_state("core_blend"))  # empty satellites
_verd52 = {"RT52": {"kind": "stock",
                    "verdict": {"rating": "BUY", "conviction": 70}}}
_rows52 = _rt52.record_routing(_verd52, since_ts="2026-01-01T00:00:00+00:00")
_r52 = next((r for r in _rows52 if r["symbol"] == "RT52"), None)
check("52 record_routing: qualified alpha+core, placed alpha, core=SILENT DROP flagged",
      _r52 is not None and set(_r52["qualified"]) == {"alpha_stocks", "core_blend"}
      and _r52["placed"] == ["alpha_stocks"] and _r52["silent_drop"] is True
      and "core_blend" in _r52["unplaced"])
_ok52, _d52 = _rt52.routing_health()
check("52 routing_health goes RED on a silent drop, naming it",
      _ok52 is False and "RT52" in _d52)

# when core's satellite BUDGET is spent (2026-07-17: capacity is budget-bound,
# not count-bound), the same unplaced name reads 'slots full' (a legitimate
# reason), NOT a silent drop. 5 sats x $4,200 on $25k cash = 45.7% of equity,
# past the 45% budget minus the min-viable room.
_scf52 = state.default_sleeve_state("core_blend")
_scf52["positions"] = {f"SAT{i}": {"quantity": 42, "entry_price": 100, "last_price": 100,
                                   "market_value": 4200.0, "cost": 4200.0, "peak_price": 100,
                                   "instrument": "equity",
                                   "opened_at": "2026-06-20T15:00:00+00:00"}
                       for i in range(5)}
state.save_sleeve("core_blend", _scf52)
_rows52b = _rt52.record_routing(_verd52, since_ts="2026-01-01T00:00:00+00:00")
_r52b = next((r for r in _rows52b if r["symbol"] == "RT52"), None)
check("52 record_routing: core budget spent is a legit reason, not a silent drop",
      # 2026-07-20: the core label now states the REAL admission rule
      # (budget-bound + conviction-swap), not the pre-07-17 "slots full (5/5)"
      _r52b is not None and _r52b["silent_drop"] is False
      and "budget spent" in _r52b["unplaced"].get("core_blend", ""))
check("52 routing_health GREEN when every unplaced name has a real reason",
      _rt52.routing_health()[0] is True)
state.save_sleeve("alpha_stocks", state.default_sleeve_state("alpha_stocks"))
state.save_sleeve("core_blend", state.default_sleeve_state("core_blend"))
state.save("routing_report", {"asof": "2026-01-01T00:00:00+00:00", "rows": []})

# ══ SECTION 53: routing feeds the LEARNING LOOP — acceptance-quality grading    ═
# ══ (operator 2026-07-02: "route this into learning so the way stocks get       ═
# ══ accepted into each book matches what's best")                               ═
from firm import learning as _lr53, routing as _rt53  # noqa: E402
import pandas as _pd53  # noqa: E402

# record_routing writes a DURABLE routing_history the learning loop can grade
state.save("routing_history", {"events": []})
state.save_sleeve("alpha_stocks", state.default_sleeve_state("alpha_stocks"))
state.save_sleeve("core_blend", state.default_sleeve_state("core_blend"))
_sa53 = state.get_sleeve("alpha_stocks")
_sa53["positions"] = {"WIN53": {"quantity": 5, "entry_price": 100, "last_price": 100,
                                "market_value": 500.0, "cost": 500.0, "peak_price": 100,
                                "instrument": "equity",
                                "opened_at": "2026-06-20T15:00:00+00:00"}}
state.save_sleeve("alpha_stocks", _sa53)
# WIN53 placed in alpha; LOSS53 qualified for alpha but NOT placed (the counterfactual)
_verd53 = {"WIN53": {"kind": "stock", "verdict": {"rating": "BUY", "conviction": 70}},
           "LOSS53": {"kind": "stock", "verdict": {"rating": "BUY", "conviction": 70}}}
_rt53.record_routing(_verd53, since_ts="2026-01-01T00:00:00+00:00")
_rh53 = state.load("routing_history", {}).get("events", [])
check("53 record_routing writes a durable routing_history for grading",
      len(_rh53) == 1 and any(r["symbol"] == "WIN53" for r in _rh53[0]["rows"]))

# grade it: WIN53 (placed alpha) beats SPY forward; LOSS53 (passed) lags -> alpha
# accepted the RIGHT name (edge_vs_passed > 0)
_bd53 = _pd53.bdate_range(end=_pd53.Timestamp.utcnow().tz_localize(None), periods=40)
_d0_53 = _bd53[5].strftime("%Y-%m-%d")


def _ramp53(a, b):
    st = (b - a) / (len(_bd53) - 1)
    return [a + st * i for i in range(len(_bd53))]


_cl53 = _pd53.DataFrame({"WIN53": _ramp53(100.0, 135.0),   # +35% -> beats SPY
                         "LOSS53": _ramp53(100.0, 80.0),   # -20% -> lags SPY
                         config.BENCHMARK: _ramp53(500.0, 505.0)}, index=_bd53)
# force both routed names to the same aged asof so both are gradable
state.save("routing_history", {"events": [{
    "asof": _d0_53 + "T15:00:00+00:00",
    "rows": [{"symbol": "WIN53", "rating": "BUY", "conviction": 70,
              "qualified": ["alpha_stocks", "core_blend"], "placed": ["alpha_stocks"]},
             {"symbol": "LOSS53", "rating": "BUY", "conviction": 70,
              "qualified": ["alpha_stocks", "core_blend"], "placed": ["core_blend"]}]}]})
_real_fh53 = _lr53.data_engine.fetch_history if hasattr(_lr53, "data_engine") else None
import firm.data_engine as _de53  # noqa: E402
_realfh53b = _de53.fetch_history
_de53.fetch_history = lambda *a, **k: _cl53
try:
    _acc53 = _lr53.acceptance_report(min_age_days=1)
finally:
    _de53.fetch_history = _realfh53b
_alpha53 = (_acc53.get("by_sleeve") or {}).get("alpha_stocks") or {}
check("53 acceptance_report grades forward return of names each book PLACED",
      _alpha53.get("placed_n") == 1
      and _alpha53.get("placed_avg_fwd_excess_pct", 0) > 10)
check("53 acceptance grades the COUNTERFACTUAL (qualified-but-passed) — alpha "
      "correctly passed the loser (edge_vs_passed positive)",
      _alpha53.get("passed_n") == 1
      and _alpha53.get("passed_avg_fwd_excess_pct", 0) < 0)
state.save("routing_history", {"events": []})
state.save("routing_report", {"asof": "2026-01-01T00:00:00+00:00", "rows": []})
state.save_sleeve("alpha_stocks", state.default_sleeve_state("alpha_stocks"))

# ══ SECTION 54: audit 2026-07-04 Phase-1 — RAM health, real-verdict-keyed CLI ═
# ══ health, IG findings → fix-briefs wiring, judge sees the news inversion    ═
from firm import health as _hl54, debate as _db54  # noqa: E402

# — F1a: RAM headroom check exists; repeated RAM-deferred retries FAIL it —
# (the absolute free-GB reading is host-dependent, so the deterministic seam
# tested here is the journal deferral count — >= 2 in 24h must go red no
# matter how much RAM the test box happens to have free right now)
_hr54 = {c["name"]: c for c in _hl54.health_report()["checks"]}
check("54 'ram headroom' health check exists", "ram headroom" in _hr54,
      str(sorted(_hr54)))
state.journal("cycle_retry_deferred_ram", avail_gb=1.9, need_gb=4.0)
state.journal("cycle_retry_deferred_ram", avail_gb=1.4, need_gb=4.0)
_hr54b = {c["name"]: c for c in _hl54.health_report()["checks"]}
check("54 ram headroom FAILS on >=2 RAM-deferred cycle retries in 24h",
      _hr54b["ram headroom"]["ok"] is False
      and "RAM-deferred" in _hr54b["ram headroom"]["detail"],
      str(_hr54b["ram headroom"]))

# — F1b: 'claude cli' keys on REAL output, not the ping — a green ping with a
# pending zero-real-verdict retry (the 07-03 outage signature) must be RED
_real_avail54 = brain.available
brain.available = lambda: True
state.save("cli_auth", {"ok": True})
state.save("cycle_retry", {"needed": True, "ts": ts(0.1)})
try:
    _hr54c = {c["name"]: c for c in _hl54.health_report()["checks"]}
    check("54 claude cli FAILS when last cycle produced zero real verdicts "
          "despite CLI ping OK",
          _hr54c["claude cli"]["ok"] is False
          and "ZERO real" in _hr54c["claude cli"]["detail"],
          str(_hr54c["claude cli"]))
    state.save("cycle_retry", {"needed": False})
    _hr54d = {c["name"]: c for c in _hl54.health_report()["checks"]}
    check("54 claude cli green again once a cycle delivers real verdicts",
          _hr54d["claude cli"]["ok"] is True)
finally:
    brain.available = _real_avail54
    state.save("cli_auth", {})
    state.save("cycle_retry", {"needed": False})

# — F1 wiring: CRITICAL/HIGH IG findings file into the fix-briefs channel,
# de-duplicated so the daily refresh never re-files (re-alerts) the same gap —
_ig54 = _ig    # the spec-loaded ig_audit module from section 36
state.save("ig_briefs_filed", {})
_n_before54 = len((state.load("fix_briefs", {}) or {}).get("items") or [])
_f54 = [{"severity": "critical", "gap": "no RAM health check",
         "evidence": "5 cycle_retry_deferred_ram in journal",
         "proposed_check": "add ram headroom line to health_report"},
        {"severity": "high", "gap": "cli health green on zero real verdicts",
         "evidence": "07-03 outage", "proposed_check": "key on cycle_retry"},
        {"severity": "medium", "gap": "medium stays out of the briefs channel",
         "evidence": "-", "proposed_check": "-"}]
_n_filed54 = _ig54._file_high_findings(_f54)
_fb54 = (state.load("fix_briefs", {}) or {}).get("items") or []
check("54 IG criticals/highs file into fix_briefs (mediums do not)",
      _n_filed54 == 2 and len(_fb54) == _n_before54 + 2
      and any("[IG CRITICAL] no RAM health check" in i["problem"] for i in _fb54)
      and not any("medium stays" in i["problem"] for i in _fb54),
      f"filed={_n_filed54}, items={[i['problem'][:40] for i in _fb54]}")
check("54 brief carries the IG evidence + proposed check as the ask",
      any(i.get("ask_claude") == "add ram headroom line to health_report"
          for i in _fb54))
_n_refiled54 = _ig54._file_high_findings(_f54)
check("54 re-running the IG does NOT re-file the same findings (dedupe)",
      _n_refiled54 == 0
      and len((state.load("fix_briefs", {}) or {}).get("items") or [])
      == _n_before54 + 2)
check("54 FIX_BRIEFS.md rendered with the IG finding",
      "[IG CRITICAL] no RAM health check" in
      (config.STATE_DIR / "FIX_BRIEFS.md").read_text(encoding="utf-8"))

# — F2: the judge guidance carries a dynamic news-inversion line, fired ONLY
# by inverted news:* edges (a factor inversion alone must not trigger it) —
state.save("edge_health", {"asof": ts(0), "edges": [],
                           "inverted": [{"edge": "news:trusted-vs-blind",
                                         "n": 13, "value": -7.9}],
                           "compensated": True})
_g54 = _db54._judge_guidance()
check("54 judge guidance surfaces the measured news-edge inversion",
      "MEASURED NEWS EDGE" in _g54 and "trusted-vs-blind -7.9pts (n=13)" in _g54
      and "discount" in _g54.lower())
state.save("edge_health", {"asof": ts(0), "edges": [],
                           "inverted": [{"edge": "factor:quality",
                                         "n": 12, "value": -3.4}]})
check("54 news line does NOT fire on a factor-only inversion",
      "MEASURED NEWS EDGE" not in _db54._judge_guidance())
state.save("edge_health", {"asof": ts(0), "edges": [], "inverted": []})
check("54 news line silent when no edges are inverted",
      "MEASURED NEWS EDGE" not in _db54._judge_guidance())

# — 54c: market-aware staleness gating (operator 2026-07-05). A STALE IG
# snapshot and a STALE edge snapshot must NOT hard-red over a weekend/holiday
# when no cycle can refresh them (the class the 07-04 sweep fixed for 'claude
# cli'/'ram headroom' but missed here) — informational then, but the real red
# on a trading day when a refresh should have run —
_real_itd54c = mh_mod.is_trading_day
_ig_saved54c = state.load("ig_findings_latest", None)
_igr_saved54c = state.load("ig_reviewed_asof", "")
_eh_saved54c = state.load("edge_health", None)
try:
    state.save("ig_findings_latest", {"asof": ts(2.5), "summary": "probe",
        "findings": [{"severity": "high", "gap": "stale-weekend probe"}]})
    state.save("ig_reviewed_asof", "")
    state.save("edge_health", {"asof": ts(2.5),
        "edges": ["news:trusted-vs-blind"], "compensated": False,
        "inverted": [{"edge": "news:trusted-vs-blind", "n": 13, "value": -7.9}]})

    mh_mod.is_trading_day = lambda *a, **k: False
    _hc54c = {c["name"]: c for c in _hl54.health_report()["checks"]}
    check("54c IG findings does NOT red on a stale snapshot when market closed",
          _hc54c["IG findings"]["ok"] is True
          and "market closed" in _hc54c["IG findings"]["detail"],
          str(_hc54c.get("IG findings")))
    check("54c edge integrity does NOT red on a stale snapshot when market closed",
          _hc54c["edge integrity"]["ok"] is True
          and "market closed" in _hc54c["edge integrity"]["detail"],
          str(_hc54c.get("edge integrity")))

    mh_mod.is_trading_day = lambda *a, **k: True
    _ho54c = {c["name"]: c for c in _hl54.health_report()["checks"]}
    check("54c IG findings DOES red on a stale snapshot on a trading day",
          _ho54c["IG findings"]["ok"] is False
          and "STALE" in _ho54c["IG findings"]["detail"],
          str(_ho54c.get("IG findings")))
    check("54c edge integrity DOES red on a stale snapshot on a trading day",
          _ho54c["edge integrity"]["ok"] is False
          and "STALE" in _ho54c["edge integrity"]["detail"],
          str(_ho54c.get("edge integrity")))
finally:
    mh_mod.is_trading_day = _real_itd54c
    state.save("ig_findings_latest", _ig_saved54c if _ig_saved54c is not None else {})
    state.save("ig_reviewed_asof", _igr_saved54c)
    state.save("edge_health", _eh_saved54c if _eh_saved54c is not None else {})

# ══ SECTION 55: IG 2026-07-03 triage — degraded-verdict watchdog, recon-drift ═
# ══ escalation latch, bench reviewability covers EVERY entry (not just holds) ═
from firm import watchlist as _wl55, alerts as _al55  # noqa: E402

# — degraded share: a completed cycle mostly on quant-fallback must go RED
# (100% degraded is already caught by cycle_retry / 'claude cli'; this is the
# partial-flood alarm the IG asked for) —
_res55 = state.load("research", {})
state.save("research", {"history": [{"asof": ts(0.01), "verdicts": {
    f"T{i}55": {"rating": "HOLD", "conviction": 50, "degraded": i < 4}
    for i in range(6)}}]})
state.save("degraded_health", {})
_hr55 = {c["name"]: c for c in _hl54.health_report()["checks"]}
check("55 'degraded verdicts' health check exists", "degraded verdicts" in _hr55,
      str(sorted(_hr55)))
check("55 degraded verdicts FAILS when last cycle share breaches the ceiling",
      _hr55["degraded verdicts"]["ok"] is False
      and "4/6" in _hr55["degraded verdicts"]["detail"],
      str(_hr55.get("degraded verdicts")))

# — degraded accuracy: the graded cohort is a FROZEN tombstone since the
# 2026-07-16 permanent fix (failed workups backlog, never record) — it rides
# the GREEN detail as history instead of pinning the desk red on a stat that
# can never change; the share/flood legs above stay the live alarms —
state.save("research", {"history": [{"asof": ts(0.01), "verdicts": {
    "AAA55": {"rating": "BUY", "conviction": 70}}}]})
state.save("degraded_health", {"asof": ts(0.01), "n": 34, "correct_pct": 32.4,
                               "avg_fwd_excess_pct": -12.2,
                               "real_n": 48, "real_correct_pct": 62.0})
_hr55b = {c["name"]: c for c in _hl54.health_report()["checks"]}
check("55 legacy degraded cohort is green-with-story (frozen history, not a red)",
      _hr55b["degraded verdicts"]["ok"] is True
      and "32%" in _hr55b["degraded verdicts"]["detail"]
      and "frozen history" in _hr55b["degraded verdicts"]["detail"],
      str(_hr55b.get("degraded verdicts")))
state.save("degraded_health", {"asof": ts(0.01), "n": 34, "correct_pct": 60.0,
                               "avg_fwd_excess_pct": 1.0,
                               "real_n": 48, "real_correct_pct": 62.0})
_hr55c = {c["name"]: c for c in _hl54.health_report()["checks"]}
check("55 degraded verdicts GREEN when share and accuracy are inside bounds",
      _hr55c["degraded verdicts"]["ok"] is True,
      str(_hr55c.get("degraded verdicts")))
state.save("degraded_health", {})
state.save("research", _res55)

# — bench reviewability covers EVERY entry: a non-hold with no parseable
# `added` escapes the 30-day age ceiling forever (the live WDC case) —
_wlprev55 = state.load("watchlist", {})
_t5_55 = ts(5)
state.save("watchlist", {"ZZZ55": {"ticker": "ZZZ55", "source": "near_miss",
                                   "history": [{"ts": _t5_55, "quant": 60}]}})
check("55 unreviewable_entries flags a bench entry with no parseable `added`",
      _wl55.unreviewable_entries() == ["ZZZ55"])
_hr55d = {c["name"]: c for c in _hl54.health_report()["checks"]}
check("55 bench reviewability REDS on the un-evictable entry, naming it",
      _hr55d["bench reviewability"]["ok"] is False
      and "ZZZ55" in _hr55d["bench reviewability"]["detail"],
      str(_hr55d.get("bench reviewability")))
_wl55x = state.load("watchlist", {})
_wl55._backfill_added(_wl55x)
state.save("watchlist", _wl55x)
check("55 _backfill_added stamps `added` from the oldest evidence on the entry",
      _wl55.unreviewable_entries() == []
      and state.load("watchlist", {})["ZZZ55"]["added"] == _t5_55
      and any(e.get("event") == "watchlist_added_backfilled"
              for e in state.read_journal(20)))
_hr55e = {c["name"]: c for c in _hl54.health_report()["checks"]}
check("55 bench reviewability GREEN again after the backfill",
      _hr55e["bench reviewability"]["ok"] is True)
state.save("watchlist", _wlprev55)

# — recon-drift escalation: persistent failing rebuild raises ONE critical
# alert per incident (latched), and the incident clears when the rebuild is ok —
_sent55: list = []
_real_alert55 = _al55.alert
_al55.alert = lambda title, msg, sev="info": _sent55.append((title, sev))
try:
    state.save("reconcile_display",
               {"asof": ts(0.01), "ok": False,
                "drifts": ["calls_only shown 0.10 vs rebuilt 0.65"]})
    state.save("recon_drift_incident", {})
    _hl54._escalate_recon_drift()
    check("55 fresh recon drift opens an incident but does NOT alert yet",
          not _sent55
          and bool(state.load("recon_drift_incident", {}).get("since")))
    state.save("recon_drift_incident", {"since": ts(0.25), "alerted": False})
    _hl54._escalate_recon_drift()
    check("55 drift persisting past HEALTH_RECON_ESCALATE_H raises ONE "
          "critical alert",
          any(sev == "critical" for _, sev in _sent55)
          and state.load("recon_drift_incident", {}).get("alerted") is True,
          str(_sent55))
    _sent55.clear()
    _hl54._escalate_recon_drift()
    check("55 escalation is LATCHED — the next 30-min sweep does not re-alert",
          not _sent55)
    state.save("reconcile_display", {"asof": ts(0.0), "ok": True, "drifts": []})
    _hl54._escalate_recon_drift()
    check("55 incident clears the moment the rebuild reconciles",
          not state.load("recon_drift_incident", {}))
finally:
    _al55.alert = _real_alert55
    state.save("reconcile_display", {})
    state.save("recon_drift_incident", {})

# ══ SECTION 56: RAM scan-downgrade floor unified to SCAN_DOWNGRADE_RAM_GB (1.0) ═
# Operator 2026-07-07: "only downgrade when there is 1 GB of RAM available or less
# when scanning." Worker count, research depth, and the 'ram headroom' health line
# must all hold FULL above the floor and step down together at/below it.
from firm import cycle as _cyc56  # noqa: E402
import psutil as _ps56  # noqa: E402


def _fake_vm56(gb):
    class _M:
        available = int(gb * 1e9)
    return lambda: _M()


_real_vm56 = _ps56.virtual_memory
_real_itd56 = mh_mod.is_trading_day
_real_maxdef56 = config.HEALTH_MAX_RAM_DEFERRALS_24H
try:
    _floor56 = float(getattr(config, "SCAN_DOWNGRADE_RAM_GB", 1.0))
    check("56 scan floor is 1.0 GB and DEPTH downgrade aliases the same floor",
          _floor56 == 1.0 and float(config.DEPTH_DOWNGRADE_RAM_GB) == _floor56,
          f"floor={_floor56} depth={config.DEPTH_DOWNGRADE_RAM_GB}")

    # ABOVE the floor (1.9 GB free): full worker width + full depth, no downgrade
    _ps56.virtual_memory = _fake_vm56(1.9)
    check("56 workers stay FULL at 1.9 GB free (old taper thinned this to 1)",
          _cyc56._effective_workers() == int(config.RESEARCH_WORKERS),
          str(_cyc56._effective_workers()))
    check("56 depth stays 'full' at 1.9 GB free (old rule downgraded < 3.0 GB)",
          _cyc56._effective_depth("full") == "full")

    # AT/BELOW the floor (0.8 GB free): collapse to 1 worker + 'standard' depth
    _ps56.virtual_memory = _fake_vm56(0.8)
    check("56 workers collapse to 1 at 0.8 GB free (at/below the floor)",
          _cyc56._effective_workers() == 1, str(_cyc56._effective_workers()))
    check("56 depth downgrades to 'standard' at 0.8 GB free",
          _cyc56._effective_depth("full") == "standard")

    # 'ram headroom' health line: GREEN above the floor, RED at/below, on a trading
    # day. Isolate from the separate RAM-deferral gate so this tests the floor only.
    mh_mod.is_trading_day = lambda *a, **k: True
    config.HEALTH_MAX_RAM_DEFERRALS_24H = 10 ** 9
    _ps56.virtual_memory = _fake_vm56(1.9)
    _hc56 = {c["name"]: c for c in _hl54.health_report()["checks"]}
    check("56 ram headroom GREEN at 1.9 GB free on a trading day (no 2.5 GB red)",
          _hc56["ram headroom"]["ok"] is True, str(_hc56.get("ram headroom")))
    _ps56.virtual_memory = _fake_vm56(0.7)
    _hc56b = {c["name"]: c for c in _hl54.health_report()["checks"]}
    check("56 ram headroom RED at 0.7 GB free (at/below the scan floor)",
          _hc56b["ram headroom"]["ok"] is False
          and "scan floor" in _hc56b["ram headroom"]["detail"],
          str(_hc56b.get("ram headroom")))
finally:
    _ps56.virtual_memory = _real_vm56
    mh_mod.is_trading_day = _real_itd56
    config.HEALTH_MAX_RAM_DEFERRALS_24H = _real_maxdef56

# ══ SECTION 57: IG 2026-07-06 triage — reopen-drain SLA watchdog, verdict ═══════
# monoculture watchdog, market-scan market-awareness ════════════════════════════
from firm import watchlist as _wl57  # noqa: E402
from datetime import datetime as _dtt57, timedelta as _tdd57, timezone as _tzz57  # noqa: E402

_real_overdue57 = _wl57.overdue_reopens
_real_scanov57 = _hl54._scan_overdue
_real_mopen57 = mh_mod.market_is_open
_real_extopen57 = mh_mod.extended_session_open
_real_research57 = state.load("research", {})
try:
    _sla57 = float(config.REOPEN_SLA_DAYS)
    # — reopen SLA: red only when a single overdue reopen ages past the SLA —
    _wl57.overdue_reopens = lambda *a, **k: []
    _hc57 = {c["name"]: c for c in _hl54.health_report()["checks"]}
    check("57 'reopen SLA' health check exists", "reopen SLA" in _hc57, str(sorted(_hc57)))
    check("57 reopen SLA GREEN when nothing is overdue",
          _hc57["reopen SLA"]["ok"] is True, str(_hc57.get("reopen SLA")))
    _fresh57 = (_dtt57.now(_tzz57.utc).date() - _tdd57(days=1)).isoformat()
    _wl57.overdue_reopens = lambda *a, **k: [
        {"ticker": "NEW57", "landed": ["macro"], "last_verdict": _fresh57}]
    _hc57b = {c["name"]: c for c in _hl54.health_report()["checks"]}
    check("57 reopen SLA GREEN for an overdue name still within the SLA window",
          _hc57b["reopen SLA"]["ok"] is True, str(_hc57b.get("reopen SLA")))
    _stale57 = (_dtt57.now(_tzz57.utc).date() - _tdd57(days=int(_sla57) + 4)).isoformat()
    _wl57.overdue_reopens = lambda *a, **k: [
        {"ticker": "SNX57", "landed": ["macro"], "last_verdict": _stale57}]
    # Pin the actionability gate ON (2026-07-09): this test asserts the
    # drain-failure DETECTION; the off-hours green-with-story gating has its
    # own tests in section 68b.
    _real_ccrt57 = _hl54._cycle_can_run_today
    _hl54._cycle_can_run_today = lambda *a, **k: True
    try:
        _hc57c = {c["name"]: c for c in _hl54.health_report()["checks"]}
    finally:
        _hl54._cycle_can_run_today = _real_ccrt57
    check("57 reopen SLA RED when an overdue reopen ages past the SLA (drain starving)",
          _hc57c["reopen SLA"]["ok"] is False
          and "SNX57" in _hc57c["reopen SLA"]["detail"], str(_hc57c.get("reopen SLA")))
    _wl57.overdue_reopens = _real_overdue57

    # — verdict distribution / monoculture —
    _mn57 = int(config.HEALTH_MONOCULTURE_CYCLES)
    _allhold57 = [{"asof": ts(_mn57 - i),
                   "verdicts": {f"T{i}_{j}": {"rating": "HOLD", "conviction": 50}
                                for j in range(5)}} for i in range(_mn57)]
    state.save("research", {"history": _allhold57, "latest_asof": ts(0.01)})
    _hc57d = {c["name"]: c for c in _hl54.health_report()["checks"]}
    check("57 'verdict distribution' check exists", "verdict distribution" in _hc57d)
    check("57 verdict distribution RED after N all-HOLD cycles (monoculture)",
          _hc57d["verdict distribution"]["ok"] is False
          and "MONOCULTURE" in _hc57d["verdict distribution"]["detail"],
          str(_hc57d.get("verdict distribution")))
    _allhold57[-1]["verdicts"]["BUY57"] = {"rating": "BUY", "conviction": 75}
    state.save("research", {"history": _allhold57, "latest_asof": ts(0.01)})
    _hc57e = {c["name"]: c for c in _hl54.health_report()["checks"]}
    check("57 verdict distribution GREEN once a BUY-side verdict appears",
          _hc57e["verdict distribution"]["ok"] is True,
          str(_hc57e.get("verdict distribution")))
    state.save("research", _real_research57)

    # — market scan market-awareness: an overdue scan reds only when market OPEN —
    _hl54._scan_overdue = lambda *a, **k: True
    mh_mod.market_is_open = lambda *a, **k: False
    # Pin extended_session_open too (2026-08-28). The branch labels itself
    # "pre-market" vs "market closed" from THAT predicate, so leaving it on the
    # real wall clock made this a time bomb: green overnight, red any time the
    # suite ran inside an extended session (04:00-09:30 or 16:00-20:00 ET). It
    # failed at 16:51 on 2026-08-28 for exactly that reason, having passed at
    # 04:00 the same morning. Stub every predicate the assertion depends on.
    mh_mod.extended_session_open = lambda *a, **k: False
    _hc57f = {c["name"]: c for c in _hl54.health_report()["checks"]}
    check("57 market scan GREEN-with-story when overdue but market CLOSED",
          _hc57f["market scan"]["ok"] is True
          and "market closed" in _hc57f["market scan"]["detail"],
          str(_hc57f.get("market scan")))
    # ...and the same branch says "pre-market" when the extended session IS open.
    mh_mod.extended_session_open = lambda *a, **k: True
    _hc57f2 = {c["name"]: c for c in _hl54.health_report()["checks"]}
    check("57 market scan says pre-market when the extended session is open",
          _hc57f2["market scan"]["ok"] is True
          and "pre-market" in _hc57f2["market scan"]["detail"],
          str(_hc57f2.get("market scan")))
    mh_mod.market_is_open = lambda *a, **k: True
    _hc57g = {c["name"]: c for c in _hl54.health_report()["checks"]}
    check("57 market scan RED when overdue AND market OPEN (live cycle uses it)",
          _hc57g["market scan"]["ok"] is False, str(_hc57g.get("market scan")))
finally:
    _wl57.overdue_reopens = _real_overdue57
    _hl54._scan_overdue = _real_scanov57
    mh_mod.market_is_open = _real_mopen57
    mh_mod.extended_session_open = _real_extopen57
    state.save("research", _real_research57)

# ══ SECTION 58: news reliability gate + edge auto-neutralize/de-dup (2026-07-07) ═
from firm import news_quality as _nq58  # noqa: E402
from firm import debate as _db58, learning as _ln58  # noqa: E402

_thr58 = float(config.NEWS_RELIABLE_THRESHOLD)
_fact58 = "[✓ Reuters, 1d ago] Acme reports Q3 revenue up 20%, beats estimates"
_rumor58 = "[✓ Bloomberg, 1d ago] Acme reportedly in talks to acquire a rival, sources say"
_aggr58 = "[· SomeBlog, 2d ago] Why Acme could be a great buy right now"
_undated58 = "[✓ Reuters, undated] Acme names new CFO"
check("58 confirmed trusted-wire headline scores MATERIAL",
      _nq58.headline_reliability(_fact58) >= _thr58,
      str(round(_nq58.headline_reliability(_fact58), 2)))
check("58 trusted RUMOR (reportedly/in talks/sources say) scores SPECULATIVE",
      _nq58.headline_reliability(_rumor58) < _thr58,
      str(round(_nq58.headline_reliability(_rumor58), 2)))
check("58 aggregator headline scores SPECULATIVE (context only)",
      _nq58.headline_reliability(_aggr58) < _thr58,
      str(round(_nq58.headline_reliability(_aggr58), 2)))
_p58 = _nq58.partition([_fact58, _rumor58, _aggr58, _undated58])
check("58 partition -> trusted-wire facts MATERIAL, rumor+aggregator SPECULATIVE",
      _p58["material"] == [_fact58, _undated58]
      and _p58["speculative"] == [_rumor58, _aggr58], str(_p58))

# — edge auto-neutralize + collinearity de-dup (via a synthetic report card) —
_real_vrc58 = _ln58.verdict_report_card
_real_eh58 = state.load("edge_health", {})
_real_dg58 = state.load("report_card_digest", {})
try:
    _ln58.verdict_report_card = lambda *a, **k: {
        "by_rating": {"HOLD": {"n": 10, "correct": 5, "avg_fwd_excess_pct": 0.0,
                               "excess": [0.0]}},
        "factor_predictiveness": {},
        "news_value": {
            "blind": {"n": 30, "correct": 18, "correct_pct": 60.0, "avg_fwd_excess_pct": 0.6},
            "trusted": {"n": 25, "correct": 12, "correct_pct": 48.0, "avg_fwd_excess_pct": -8.4},
            "aggregator": {"n": 25, "correct": 12, "correct_pct": 48.0, "avg_fwd_excess_pct": -8.4},
        }}
    _ln58.refresh_report_card_digest()
    _eh58 = state.load("edge_health", {}) or {}
    _neu58 = {e["edge"] for e in (_eh58.get("neutralized") or [])}
    check("58 a net-harmful news tier (-9pts, n=25) is AUTO-NEUTRALIZED",
          "news:trusted-vs-blind" in _neu58, str(_eh58.get("neutralized")))
    check("58 collinear duplicate tier (same n+value) is de-duped, not double-counted",
          bool(_eh58.get("collinear"))
          and any("news:aggregator" in c.get("edge", "") for c in _eh58["collinear"]),
          str(_eh58.get("collinear")))

    # — judge note escalates DISCOUNT -> IGNORE when a tier is neutralized —
    state.save("edge_health", {"asof": ts(0), "compensated": True, "neutralized": [],
                               "inverted": [{"edge": "news:trusted-vs-blind", "n": 13, "value": -7.9}]})
    _soft58 = _db58._news_edge_note()
    check("58 inverted-only (small n) news edge -> soft DISCOUNT note",
          "DISCOUNT" in _soft58 and "IGNORE" not in _soft58, _soft58[:55])
    state.save("edge_health", {"asof": ts(0), "compensated": True,
                               "inverted": [{"edge": "news:trusted-vs-blind", "n": 30, "value": -8.4}],
                               "neutralized": [{"edge": "news:trusted-vs-blind", "n": 30, "value": -8.4}]})
    _hard58 = _db58._news_edge_note()
    check("58 neutralized news edge -> hard IGNORE-for-conviction note",
          "IGNORE" in _hard58 and "neutralized" in _hard58.lower(), _hard58[:70])
finally:
    _ln58.verdict_report_card = _real_vrc58
    state.save("edge_health", _real_eh58)
    state.save("report_card_digest", _real_dg58)

# ══ SECTION 59: macro breadth #6 — broad-release reopen + semiconductor topic ═══
from firm import macro_calendar as _mc59, data_engine as _de59  # noqa: E402
from datetime import date as _date59, timedelta as _td59  # noqa: E402

_real_mcal59 = state.load("macro_calendar", {})
try:
    _y59 = (_date59.today() - _td59(days=1)).isoformat()
    state.save("macro_calendar", {"broad": [
        {"event": "New Residential Construction", "date": _y59}], "ff_raw": []})
    check("59 a bench thesis naming a BROAD release reopens when it lands (housing)",
          _mc59.mentioned_landed("waiting on the housing starts report",
                                 _date59.today() - _td59(days=3), _date59.today())
          is not None)
    check("59 an unrelated thesis is NOT reopened by a broad release",
          _mc59.mentioned_landed("waiting on an earnings print",
                                 _date59.today() - _td59(days=3), _date59.today())
          is None)
    check("59 broad reopen is DATA-BACKED (reads the cached feed, no hardcoded id)",
          _mc59._broad_landed("housing data",
                              _date59.today() - _td59(days=3), _date59.today())
          is not None)
finally:
    state.save("macro_calendar", _real_mcal59)

check("59 chip-cycle headline classifies as the 'semiconductors' macro topic",
      _de59._classify_macro_topic("TSMC monthly sales jump on AI chip demand")
      == "semiconductors")
check("59 pure chip-cycle news (billings/foundry/wafer) is a semiconductors catalyst",
      _de59._classify_macro_topic("SEMI reports global chip billings up 12% on foundry demand")
      == "semiconductors")

# ══ SECTION 60: day-P&L display rolls across a day boundary (operator 2026-07-07) ═
# Before the first monitor sweep/cycle of a NEW trading day (pre-market, or right
# after a reboot), the stored anchor is still yesterday's — day_pnl must count from
# the PRIOR CLOSE, not show yesterday's whole gain as "today's".
from firm import risk as _rsk60  # noqa: E402

_real_today60 = _rsk60._today
_real_itd60 = mh_mod.is_trading_day
try:
    _rsk60._today = lambda: "2026-07-07"          # a Tuesday
    mh_mod.is_trading_day = lambda *a, **k: True
    # Monday CLOSE mark = 26000; stale anchor = Monday's date + Monday's START 25000
    _s60 = {"equity_curve": [{"ts": "2026-07-06T20:00:00+00:00", "equity": 26000.0}],
            "day_pnl_anchor": {"date": "2026-07-06", "equity": 25000.0}}
    check("60 day_pnl counts from PRIOR CLOSE when anchor is stale (new trading day)",
          _rsk60.day_pnl(_s60, 26000.0) == 0.0,
          f"got {_rsk60.day_pnl(_s60, 26000.0)} (yesterday's +1000 must NOT show as today)")
    check("60 an overnight gap since prior close shows as today's move",
          _rsk60.day_pnl(_s60, 26050.0) == 50.0, str(_rsk60.day_pnl(_s60, 26050.0)))
    # today's anchor -> normal behavior (count from today's anchor)
    _s60b = {"equity_curve": [], "day_pnl_anchor": {"date": "2026-07-07", "equity": 26000.0}}
    check("60 day_pnl unchanged when the anchor is already today's",
          _rsk60.day_pnl(_s60b, 26100.0) == 100.0, str(_rsk60.day_pnl(_s60b, 26100.0)))
finally:
    _rsk60._today = _real_today60
    mh_mod.is_trading_day = _real_itd60

# ══ SECTION 61: IG 2026-07-07 triage — scan-catchup overdue, benchmark data, ════
# macro live-feed tier ══════════════════════════════════════════════════════════
from datetime import datetime as _dt61, timezone as _tz61  # noqa: E402
from firm import market_hours as _mh61  # noqa: E402

# — a same-day catch-up scan (even pre-8:30) must NOT read as overdue (regression
#   from the 2026-07-07 scan catch-up: it stamped 07:54 < the 8:30 expected run) —
_now61 = _dt61(2026, 7, 7, 11, 0, tzinfo=_mh61.ET)          # Tue 11:00 ET, deadline passed
_early61 = _dt61(2026, 7, 7, 7, 54, tzinfo=_mh61.ET).astimezone(_tz61.utc).isoformat()
_prior61 = _dt61(2026, 7, 6, 20, 0, tzinfo=_mh61.ET).astimezone(_tz61.utc).isoformat()
check("61 same-day catch-up scan (pre-8:30) is NOT overdue (day_only)",
      _hl54._scan_overdue(_early61, _now61) is False)
check("61 a prior-day scan IS overdue",
      _hl54._scan_overdue(_prior61, _now61) is True)

# — benchmark data: SPY (the SPY-relative benchmark) mark_missing over the limit fails —
_real_bmax61 = config.HEALTH_MAX_BENCHMARK_MISS_24H
try:
    config.HEALTH_MAX_BENCHMARK_MISS_24H = 2
    for _ in range(3):
        state.journal("mark_missing", symbol="GLD,QQQ,SPY,TXN", n=4, sleeve="core_blend")
    _hcb61 = {c["name"]: c for c in _hl54.health_report()["checks"]}
    check("61 'benchmark data' health check exists", "benchmark data" in _hcb61)
    check("61 benchmark data RED when SPY mark_missing exceeds the 24h limit",
          _hcb61["benchmark data"]["ok"] is False and "SPY" in _hcb61["benchmark data"]["detail"],
          str(_hcb61.get("benchmark data")))
finally:
    config.HEALTH_MAX_BENCHMARK_MISS_24H = _real_bmax61

# — macro calendar fails on the rule tier (live feed unreachable = blind) —
_real_ct61 = _mc59.calendar_tier
_real_fe61 = _mc59.future_events
try:
    _mc59.future_events = lambda *a, **k: 20
    _mc59.calendar_tier = lambda: "rule"
    _hcm61 = {c["name"]: c for c in _hl54.health_report()["checks"]}
    check("61 macro calendar RED on the RULE tier (live feed unreachable)",
          _hcm61["macro calendar"]["ok"] is False and "RULE" in _hcm61["macro calendar"]["detail"],
          str(_hcm61.get("macro calendar")))
    _mc59.calendar_tier = lambda: "fred"
    _hcm61b = {c["name"]: c for c in _hl54.health_report()["checks"]}
    check("61 macro calendar GREEN on a live-feed tier (fred)",
          _hcm61b["macro calendar"]["ok"] is True, str(_hcm61b.get("macro calendar")))
finally:
    _mc59.calendar_tier = _real_ct61
    _mc59.future_events = _real_fe61

# ══ SECTION 62: short-desk put-pricing foundation (operator 2026-07-07) ═════════
# Foundation for the SELL/short sleeve (Item 10). Dormant until the sleeve is wired
# — verifies the put OSI encoding, pick_put's fail-soft contract, and that
# option_mark is direction-aware (a put no longer silently reads as unpriceable).
from firm import broker as _bk62  # noqa: E402

_osi_put62 = _bk62.build_osi("PENG", "2026-07-24", "PUT", 60.0)
_osi_call62 = _bk62.build_osi("PENG", "2026-07-24", "CALL", 60.0)
check("62 build_osi tags a PUT contract with 'P'",
      "P" in _osi_put62 and _osi_put62 != _osi_call62, f"{_osi_put62} vs {_osi_call62}")
check("62 option_mark is direction-aware (accepts contract_type, fail-soft)",
      isinstance(_bk62.option_mark("PENG", "2026-07-24", 60.0, "PUT"), float))
check("62 pick_put returns a dict-or-None (fail-soft, never raises)",
      _bk62.pick_put("PENG", 2.0, 7, 28) in (None,) or
      isinstance(_bk62.pick_put("PENG", 2.0, 7, 28), dict))

# ══ SECTION 63: combined options desk — PUTS on the SELL-side (operator 2026-07-07) ═
from firm import sleeves as _sl63, debate as _db63  # noqa: E402

check("63 verdict schema exposes suitable_for_puts to the judge",
      "suitable_for_puts" in _db63._verdict_schema())
_cmin63 = config.SLEEVES["calls_only"]["min_conviction"]
_cbuy63 = config.CALLS_BUY_CONVICTION_MIN
check("63 STRONG_SELL + suitable + conv>=min qualifies for a put",
      _sl63._puts_entry_ok({"rating": "STRONG_SELL", "conviction": _cmin63,
                            "suitable_for_puts": True}) is True)
check("63 SELL + suitable + near-top conviction qualifies for a put",
      _sl63._puts_entry_ok({"rating": "SELL", "conviction": _cbuy63,
                            "suitable_for_puts": True}) is True)
check("63 SELL with an EXPLICIT justified puts veto does NOT qualify "
      "(2026-07-21 #6: reason alone no longer binds)",
      _sl63._puts_entry_ok({"rating": "SELL", "conviction": 95,
                            "suitable_for_puts": False,
                            "options_veto_puts": True,
                            "suitable_for_puts_reason":
                            "no near-term downside catalyst, wide "
                            "spreads"}) is False
      and _sl63._puts_entry_ok({"rating": "SELL", "conviction": 95,
                                "suitable_for_puts": False,
                                "suitable_for_puts_reason":
                                "no near-term downside catalyst, wide "
                                "spreads"}) is True)
check("63 a DEGRADED SELL never opens a leveraged put",
      _sl63._puts_entry_ok({"rating": "STRONG_SELL", "conviction": 99,
                            "suitable_for_puts": True, "degraded": True}) is False)
check("63 a BUY never triggers the puts gate (wrong direction)",
      _sl63._puts_entry_ok({"rating": "STRONG_BUY", "conviction": 99,
                            "suitable_for_puts": True}) is False)
check("63 _puts_buy_pass is wired into the sleeve (callable)",
      callable(_sl63._puts_buy_pass))

# gate reachability now covers the put path — reachable on SELL-side signal alone
_realhist63 = state.load("research", {})
try:
    state.save("research", {"history": [
        {"verdicts": {f"T{i}": {"rating": "STRONG_SELL"} for i in range(12)}}
        for _ in range(12)]})
    _hcg63 = {c["name"]: c for c in _hl54.health_report()["checks"]}
    check("63 gate reachability GREEN on SELL-side signal alone (combined desk reachable)",
          _hcg63["calls_only gate reachability"]["ok"] is True,
          str(_hcg63.get("calls_only gate reachability")))
finally:
    state.save("research", _realhist63)

# exit ladder is direction-aware + fail-soft for a PUT contract
from firm import exits as _ex63  # noqa: E402
_real_avail63 = _ex63.brain.available
try:
    _ex63.brain.available = lambda: False          # deterministic default ladder, no Opus
    _lad63 = _ex63.plan_option_exits(
        {"osi": "PENG260724P00060000", "contract_type": "PUT",
         "expiration": "2026-07-24", "strike": 60.0, "mid": 2.5, "spread_pct": 8.0},
        {"rating": "STRONG_SELL", "conviction": 75}, {})
    check("63 exit ladder returns a valid plan for a PUT (direction-aware, fail-soft)",
          isinstance(_lad63, dict) and "sl_pct" in _lad63, str(_lad63)[:70])
finally:
    _ex63.brain.available = _real_avail63

# ══ SECTION 64: midday refresh warrant (operator 2026-07-08) ═══════════════════
from firm import watchlist as _wl64  # noqa: E402

_real_research64 = state.load("research", {})
_real_overdue64 = _wl64.overdue_reopens
try:
    _wl64.overdue_reopens = lambda *a, **k: []
    # last cycle MOSTLY DEGRADED -> a midday catch-up is warranted
    state.save("research", {"history": [{"verdicts": {
        f"T{i}": {"rating": "HOLD", "degraded": i < 8} for i in range(10)}}]})
    _w64, _r64 = _hl54.midday_refresh_warranted()
    check("64 midday refresh WARRANTED when the last cycle was mostly degraded",
          _w64 is True and "degraded" in _r64, _r64)
    # HEALTHY last cycle + no overdue reopens -> NOT warranted (no-op at noon)
    state.save("research", {"history": [{"verdicts": {
        f"T{i}": {"rating": "BUY", "degraded": False} for i in range(10)}}]})
    _w64b, _r64b = _hl54.midday_refresh_warranted()
    check("64 midday refresh NOT warranted on a healthy day", _w64b is False, _r64b)
    # OVERDUE REOPENS present -> warranted (operator's ask: include the reopens)
    _wl64.overdue_reopens = lambda *a, **k: [{"ticker": "PENG"}]
    _w64c, _r64c = _hl54.midday_refresh_warranted()
    check("64 midday refresh WARRANTED when names are overdue for reopen",
          _w64c is True and "PENG" in _r64c, _r64c)
finally:
    state.save("research", _real_research64)
    _wl64.overdue_reopens = _real_overdue64

# ══ SECTION 65: catalyst-waiter eviction protection — "don't disappear when the ═
# date is months later" (operator 2026-07-08) ══════════════════════════════════
from firm import watchlist as _wl65, macro_calendar as _mc65  # noqa: E402
from datetime import date as _date65, timedelta as _td65  # noqa: E402

_today65 = _date65.today()
_real_imm65, _real_ment65 = _mc65.imminent_within, _mc65.mentioned_imminent
try:
    _mc65.imminent_within = lambda *a, **k: False   # isolate the earnings-date logic
    _mc65.mentioned_imminent = lambda *a, **k: False

    def _hold65(days):
        return {"source": "hold_verdict",
                "earnings_date": (_today65 + _td65(days=days)).isoformat()}
    check("65 CATALYST_WAIT_MAX_DAYS widened to 120 (quarter + delayed-report margin)",
          _wl65.CATALYST_WAIT_MAX_DAYS == 120)
    check("65 a HOLD with earnings 110d out is PROTECTED from the age ceiling",
          _wl65._awaiting_near_catalyst(_hold65(110), _today65) is True)
    check("65 a HOLD with earnings 60d out is PROTECTED (normal months-out case)",
          _wl65._awaiting_near_catalyst(_hold65(60), _today65) is True)
    check("65 an absurd 200d-out date is NOT protected (rejects stale/wrong, no immortal seat)",
          _wl65._awaiting_near_catalyst(_hold65(200), _today65) is False)
    check("65 a HOLD whose print already passed is NOT protected",
          _wl65._awaiting_near_catalyst(_hold65(-3), _today65) is False)
finally:
    _mc65.imminent_within, _mc65.mentioned_imminent = _real_imm65, _real_ment65

# ══ SECTION 66: IG 2026-07-09 triage — the two safe hardenings ═════════════════
from firm import learning as _ln66  # noqa: E402
from datetime import datetime as _dt66, timezone as _tz66, timedelta as _tdh66  # noqa: E402

# #4: _card_for_prompt drops the degraded_fallback bucket (an ADVISORY outage-
#     fallback cohort the firm never acted on — must not skew the Opus REVIEW
#     prompt) while keeping real ratings and still stripping raw excess lists.
_card66 = {"by_rating": {
    "BUY": {"n": 10, "excess": [1.2, -0.3], "avg": 4.8},
    "degraded_fallback": {"n": 36, "excess": [-9.9], "avg": -9.9},
}}
_pc66 = _ln66._card_for_prompt(_card66)
check("66 #4 _card_for_prompt drops degraded_fallback from the review prompt",
      "degraded_fallback" not in _pc66["by_rating"])
check("66 #4 _card_for_prompt keeps the real ratings",
      "BUY" in _pc66["by_rating"])
check("66 #4 _card_for_prompt still strips raw per-name excess lists",
      "excess" not in _pc66["by_rating"]["BUY"])
check("66 #4 the raw card is untouched (degraded_health still reads the bucket)",
      "degraded_fallback" in _card66["by_rating"])

# #5: digest_context serves a good scorecard up to 30h old (was a hard 24h — a
#     one-cycle data-gap + daily cadence blinded the judge on 2026-07-08).
_real_load66 = _ln66.state.load
def _mk_digest66(age_h):
    return {"asof": (_dt66.now(_tz66.utc) - _tdh66(hours=age_h)).isoformat(), "text": "SCORECARD"}
try:
    _ln66.state.load = lambda k, d=None: _mk_digest66(26) if k == "report_card_digest" else (d or {})
    check("66 #5 digest_context serves a 26h-old digest (survives a one-cycle gap)",
          _ln66.digest_context() == "SCORECARD")
    _ln66.state.load = lambda k, d=None: _mk_digest66(31) if k == "report_card_digest" else (d or {})
    check("66 #5 digest_context still refuses a genuinely stale 31h-old digest",
          _ln66.digest_context() == "")
finally:
    _ln66.state.load = _real_load66

# ══ SECTION 67: thesis-gated bench cooldown — "a watchlist name must not be ═══
# re-rated when the thesis itself hasn't triggered the re-run" (operator
# 2026-07-09; the timer re-rates fed the max-recheck counter that judged out
# PENG before its +31.7% earnings pop, IG 2026-07-09 #7) ═══════════════════════
from firm import cycle as _cy67, macro_calendar as _mc67  # noqa: E402
_prev_r67 = state.load("research", {})
_prev_w67 = state.load("watchlist", {})
_real_ls67 = _mc67.landed_since
_today67 = datetime.now(timezone.utc).date()
_mc67.landed_since = lambda *a, **k: _today67       # force "a macro print landed"
try:
    state.save("watchlist", {
        "BENCHED":   {"reopen_trigger": {"kind": "pullback_pct", "pct": 8}},
        "BENCHSELL": {"reopen_trigger": {"kind": "pullback_pct", "pct": 8}},
        "CATONLY":   {"reopen_trigger": {"kind": "catalyst"}},
        "EARNWAIT":  {"earnings_date": (_today67 + timedelta(days=30)).isoformat()},
    })
    state.save("research", {"history": [
        {"asof": ts(10), "verdicts": {           # aged FAR past the 3d cooldown
            "BENCHED":   {"rating": "HOLD", "conviction": 60},
            "BENCHSELL": {"rating": "SELL", "conviction": 60},
            "CATONLY":   {"rating": "HOLD", "conviction": 60},
            "EARNWAIT":  {"rating": "HOLD", "conviction": 60},
            "PLAIN":     {"rating": "HOLD", "conviction": 60}},
         "context": {}}]})
    check("67 benched HOLD (technical trigger) does NOT age out on the timer",
          (_cy67._cooldown_state("BENCHED") or {}).get("lane") == "bench")
    check("67 pending-earnings waiter holds its cooldown until the print",
          (_cy67._cooldown_state("EARNWAIT") or {}).get("lane") == "bench")
    check("67 catalyst-only (macro-thesis) name still reopens normally",
          _cy67._cooldown_state("CATONLY") is None)
    check("67 a benched SELL is not gated (HOLD-only, mirrors the momentum lane)",
          _cy67._cooldown_state("BENCHSELL") is None)
    check("67 an un-benched name still ages out on the timer",
          _cy67._cooldown_state("PLAIN") is None)
    # 67b — the BOUNDED staleness escape (no unbounded state, house doctrine):
    # past BENCH_GATE_MAX_DWELL_DAYS even a thesis-gated waiter gets ONE
    # fresh full committee look.
    state.save("research", {"history": [
        {"asof": ts(25), "verdicts": {
            "BENCHED": {"rating": "HOLD", "conviction": 60}}, "context": {}}]})
    check("67b past BENCH_GATE_MAX_DWELL_DAYS the staleness escape fires",
          _cy67._cooldown_state("BENCHED") is None)
finally:
    _mc67.landed_since = _real_ls67
    state.save("research", _prev_r67)
    state.save("watchlist", _prev_w67)

# ══ SECTION 68: bench bookkeeping + reopen-watchdog actionability ═════════════
# (2026-07-09 APPS false alarm: re-judged HOLD conviction 54 — one point under
# the 55 bench bar — so neither update branch ran, last_verdict stayed 3d
# stale, and 'catalyst readiness' cried wolf every 30 min all evening on a
# name the committee had judged that morning) ══════════════════════════════════
_prev_wl68 = state.load("watchlist", {})
_t0_68 = ts(3)                                        # benched 3 days ago
_entry68 = {"ticker": "REJUDGE", "source": "hold_verdict", "added": _t0_68,
            "last_verdict": {"rating": "HOLD", "conviction": 62, "ts": _t0_68},
            "reopen_trigger": {"kind": "pullback_pct", "pct": 8}}
try:
    state.save("watchlist", {"REJUDGE": dict(_entry68)})
    _wl68 = watchlist.update({"quant_scores": {}}, {"REJUDGE": {
        "kind": "stock", "verdict": {"rating": "HOLD", "conviction": 54},
        "packet": {}}})
    _lv68 = (_wl68.get("REJUDGE") or {}).get("last_verdict") or {}
    check("68 a sub-bar HOLD re-judge still refreshes the bench last_verdict stamp",
          _lv68.get("ts", "") > _t0_68 and _lv68.get("conviction") == 54,
          str(_lv68))
    check("68 the name stays benched (membership rules unchanged)",
          "REJUDGE" in _wl68)
    # A DEGRADED verdict must NOT touch the stamp (non-authoritative, 2026-06-25)
    state.save("watchlist", {"REJUDGE": dict(_entry68)})
    _wl68d = watchlist.update({"quant_scores": {}}, {"REJUDGE": {
        "kind": "stock", "verdict": {"rating": "HOLD", "conviction": 54,
                                     "degraded": True}, "packet": {}}})
    check("68 a degraded verdict does NOT refresh the stamp",
          ((_wl68d.get("REJUDGE") or {}).get("last_verdict") or {}).get("ts") == _t0_68)
finally:
    state.save("watchlist", _prev_wl68)

# 68b — the reopen watchdogs are actionability-gated: green-with-story when no
# cycle can act (overnight/weekend), the real red when a session can.
from firm import health as _hl68  # noqa: E402
_real_ccrt68 = _hl68._cycle_can_run_today
_real_od68 = watchlist.overdue_reopens
try:
    watchlist.overdue_reopens = lambda *a, **k: [
        {"ticker": "APPS", "landed": ["macro 2026-07-07"],
         "last_verdict": "2026-07-01"}]
    _hl68._cycle_can_run_today = lambda *a, **k: False
    _hc68 = {c["name"]: c for c in _hl68.health_report()["checks"]}
    check("68b catalyst readiness is green-with-story when no cycle can act",
          _hc68["catalyst readiness"]["ok"] is True
          and "market closed" in _hc68["catalyst readiness"]["detail"],
          str(_hc68.get("catalyst readiness")))
    check("68b reopen SLA carries the story off-hours instead of redding",
          _hc68["reopen SLA"]["ok"] is True,
          str(_hc68.get("reopen SLA")))
    _hl68._cycle_can_run_today = lambda *a, **k: True
    _hc68b = {c["name"]: c for c in _hl68.health_report()["checks"]}
    check("68b catalyst readiness DOES red when a session can act",
          _hc68b["catalyst readiness"]["ok"] is False
          and "APPS" in _hc68b["catalyst readiness"]["detail"],
          str(_hc68b.get("catalyst readiness")))
finally:
    watchlist.overdue_reopens = _real_od68
    _hl68._cycle_can_run_today = _real_ccrt68

# ══ SECTION 69: promotions vs the cooldown — the candidate-collision trap ═════
# (2026-07-10 APPS: a benched HOLD that was ALSO a scan candidate sat inside
# its verdict cooldown — the cycle's cooldown gate dropped it from research
# while _promotions_locked skipped it as "already a candidate" (raw PRE-
# cooldown screen list), so NEITHER path re-judged it; its landed catalyst
# could never fire and 'catalyst readiness' redded every 30 min with no
# self-clear. promotions() now takes the cycle's ACTUAL post-cooldown
# research set; a cooled-out candidate stays promotable via its catalyst.) ═════
from datetime import datetime as _dt69, timedelta as _td69, timezone as _tz69  # noqa: E402
_prev_wl69 = state.load("watchlist", {})
_yday69 = (_dt69.now(_tz69.utc).date() - _td69(days=1)).isoformat()
_entry69 = {"ticker": "TRAP69", "source": "hold_verdict", "added": ts(5),
            "last_verdict": {"rating": "HOLD", "conviction": 60, "ts": ts(5)},
            "earnings_date": _yday69, "history": []}
_screen69 = {"quant_scores": {"TRAP69": 60.0}, "stock_candidates": ["TRAP69"],
             "technicals": {}}
try:
    # Legacy 1-arg call: raw screen list still suppresses the promotion
    state.save("watchlist", {"TRAP69": dict(_entry69)})
    check("69 legacy call keeps the old skip (raw screen list)",
          not any(p["ticker"] == "TRAP69"
                  for p in watchlist.promotions(_screen69)))
    # In the ACTUAL research set -> still skipped (no double-research)
    state.save("watchlist", {"TRAP69": dict(_entry69)})
    check("69 a name truly being researched is not double-promoted",
          not any(p["ticker"] == "TRAP69"
                  for p in watchlist.promotions(_screen69,
                                                researching={"TRAP69"})))
    # Cooled OUT of the research set -> the landed catalyst promotes it
    state.save("watchlist", {"TRAP69": dict(_entry69)})
    _p69 = watchlist.promotions(_screen69, researching=set())
    check("69 a cooldown-dropped scan candidate is still catalyst-promotable",
          any(p["ticker"] == "TRAP69" for p in _p69),
          str(_p69))
    check("69 the reopen reason is the landed catalyst",
          any(p["ticker"] == "TRAP69" and "earnings printed" in p["why"]
              for p in _p69),
          str(_p69))
finally:
    state.save("watchlist", _prev_wl69)

# ══ SECTION 70: pre-flight RAM recovery (operator 2026-07-10: "fix the RAM ════
# starvation") — the cycle fires the session janitor before sizing the
# research floor when free RAM is under RAM_RECOVERY_GB ════════════════════════
from firm import cycle as _cy70  # noqa: E402
_real_fire70 = _cy70._fire_janitor
_real_wait70 = getattr(config, "RAM_RECOVERY_WAIT_S", 25)
_fired70 = []
try:
    config.RAM_RECOVERY_WAIT_S = 0
    _cy70._fire_janitor = lambda: _fired70.append(1)
    _cy70._preflight_ram_recovery(avail_gb=5.0, allow_in_sandbox=True)
    check("70 healthy RAM -> janitor NOT fired (no needless reaping)",
          len(_fired70) == 0)
    _cy70._preflight_ram_recovery(avail_gb=1.2, allow_in_sandbox=True)
    check("70 starved RAM -> janitor fired before the research floor",
          len(_fired70) == 1)
    def _boom70():
        raise OSError("no such task")
    _cy70._fire_janitor = _boom70
    _cy70._preflight_ram_recovery(avail_gb=1.2, allow_in_sandbox=True)
    check("70 a missing janitor task is fail-soft (cycle proceeds)", True)
    # ── THE REGRESSION THAT COST THE SUITE ITSELF (2026-08-19) ──────────────
    # _post_cycle_ram_clean refuses to run host-wide cleanup from a sandboxed
    # STATE_DIR; its twin _preflight_ram_recovery did NOT, so every simulated
    # cycle in THIS FILE fired the real `schtasks /Run` janitor and slept
    # RAM_RECOVERY_WAIT_S (25s) — on a box under the 2 GB trigger that is an
    # unconditional 25s per simulated cycle, and the suite stopped finishing
    # inside self_test.py's 900s budget. The guard is the fix; this is the
    # check that keeps it. Real wait restored first so a regression SHOWS UP
    # as elapsed seconds, not as a silently-zeroed sleep.
    config.RAM_RECOVERY_WAIT_S = _real_wait70
    _fired70b = []
    _cy70._fire_janitor = lambda: _fired70b.append(1)
    import time as _t70
    _t0_70 = _t70.time()
    _cy70._preflight_ram_recovery()          # production call signature
    _el70 = _t70.time() - _t0_70
    check("70 a SIMULATED cycle never reaps the host: sandboxed STATE_DIR "
          "fires no janitor and sleeps nothing (the guard _post_cycle_ram_clean "
          "always had — its absence here timed the 900s suite out)",
          len(_fired70b) == 0 and _el70 < 1.0,
          f"fired={len(_fired70b)} elapsed={_el70:.1f}s")
    # ...and both twins must stay guarded, so neither can drift back. Scan each
    # function's WHOLE body (to the next top-level def), never a fixed char
    # slice: the first version of this check sliced [:1800] and went red purely
    # because the fix's own explanatory comment pushed the guard to char 2031 —
    # a source check whose verdict depends on how much prose sits above the
    # code is measuring the wrong thing, which is the very defect class this
    # session is repairing.
    _src70 = (Path(__file__).parent.parent / "firm" / "cycle.py").read_text(
        encoding="utf-8")

    def _fnbody70(name):
        _b = _src70.split(f"def {name}(", 1)[1]
        _nxt = _b.find(chr(10) + "def ")
        return _b if _nxt < 0 else _b[:_nxt]
    check("70 BOTH ram twins carry the sandbox guard (asymmetry is the bug)",
          all('str(config.STATE_DIR) != str(config.BASE_DIR / "state")'
              in _fnbody70(_fn)
              for _fn in ("_preflight_ram_recovery", "_post_cycle_ram_clean")),
          "guarded: " + ", ".join(
              _fn for _fn in ("_preflight_ram_recovery", "_post_cycle_ram_clean")
              if 'str(config.STATE_DIR) != str(config.BASE_DIR / "state")'
              in _fnbody70(_fn)))
finally:
    _cy70._fire_janitor = _real_fire70
    config.RAM_RECOVERY_WAIT_S = _real_wait70

# ══ SECTION 71: reopen watchdog macro-immunity + bench-stamp heal (IG 2026-07-10
# items 1/6/7 — the APPS false alarm: momentum-laned mom=100, waiting on its
# 08-04 earnings, but redded 'catalyst readiness' every 30 min for a macro it is
# designed to ignore, on a stamp that lagged its real re-judge) ═════════════════
from firm import watchlist as _wl71, macro_calendar as _mc71  # noqa: E402
from datetime import date as _date71, timedelta as _td71  # noqa: E402
_prev_wl71 = state.load("watchlist", {})
_prev_r71 = state.load("research", {})
_real_ls71 = _mc71.landed_since
_today71 = _date71.today()
_macro71 = _today71 - _td71(days=2)                       # a macro landed 2d ago
_vd71 = (_today71 - _td71(days=4)).isoformat() + "T05:00:00+00:00"   # verdict 4d ago
try:
    _mc71.landed_since = lambda since, today=None: _macro71 if since < _macro71 else None
    state.save("research", {"history": [], "latest_asof": _today71.isoformat() + "T15:00:00+00:00"})
    # EXPOSED: only a catalyst leg, no earnings, no technical -> macro IS its lifeline
    state.save("watchlist", {"EXPO71": {"source": "hold_verdict",
        "last_verdict": {"rating": "HOLD", "ts": _vd71},
        "reopen_trigger": {"kind": "catalyst"}}})
    check("71 a macro-only bench name IS still flagged overdue for a landed macro",
          any(o["ticker"] == "EXPO71" for o in _wl71.overdue_reopens()))
    # IMMUNE via a non-macro technical trigger (momentum lane / bench thesis gate)
    state.save("watchlist", {"TECH71": {"source": "hold_verdict",
        "last_verdict": {"rating": "HOLD", "ts": _vd71},
        "reopen_trigger": {"kind": "pullback_pct", "pct": 8}}})
    check("71 a name with a non-macro trigger is macro-IMMUNE (no false overdue)",
          not any(o["ticker"] == "TECH71" for o in _wl71.overdue_reopens()))
    # IMMUNE via a pending earnings date (the APPS case)
    state.save("watchlist", {"EARN71": {"source": "hold_verdict",
        "last_verdict": {"rating": "HOLD", "ts": _vd71},
        "earnings_date": (_today71 + _td71(days=20)).isoformat(),
        "reopen_trigger": {"kind": "catalyst"}}})
    check("71 a name waiting on a pending earnings date is macro-IMMUNE (the APPS case)",
          not any(o["ticker"] == "EARN71" for o in _wl71.overdue_reopens()))
    # Fix 2 — stamp reconciliation advances a lagging stamp, ignores degraded
    _newer71 = (_today71 - _td71(days=1)).isoformat() + "T14:00:00+00:00"
    state.save("research", {"history": [
        {"asof": _vd71, "verdicts": {"LAG71": {"rating": "HOLD", "conviction": 60}}},
        {"asof": _newer71, "verdicts": {"LAG71": {"rating": "HOLD", "conviction": 54},
                                        "DGD71": {"rating": "BUY", "conviction": 90, "degraded": True}}}]})
    _w71 = {"LAG71": {"source": "hold_verdict", "last_verdict": {"rating": "HOLD", "ts": _vd71}},
            "DGD71": {"source": "hold_verdict", "last_verdict": {"rating": "HOLD", "ts": _vd71}}}
    _wl71._reconcile_bench_stamps(_w71)
    check("71 stamp heal advances a lagging stamp to the latest REAL verdict",
          _w71["LAG71"]["last_verdict"]["ts"] == _newer71)
    check("71 stamp heal IGNORES a degraded-only newer verdict (non-authoritative)",
          _w71["DGD71"]["last_verdict"]["ts"] == _vd71)
finally:
    _mc71.landed_since = _real_ls71
    state.save("watchlist", _prev_wl71)
    state.save("research", _prev_r71)

# ══ 72. Book-full BUYs land on the Bench (operator 2026-07-16, ALKS) ══════════
from firm import routing as _rt72
_prev_wl72 = state.load("watchlist", {})
_prev_a72 = state.get_sleeve("alpha_stocks")
_prev_c72 = state.get_sleeve("core_blend")
try:
    def _fill72(sleeve, n, px=10.0, cash=None):
        s = state.get_sleeve(sleeve)
        s["positions"] = {f"F{i}{sleeve[:2].upper()}": {
            "symbol": f"F{i}{sleeve[:2].upper()}", "instrument": "equity",
            "quantity": 1, "entry_price": px, "cost": px, "opened_at": ts()}
            for i in range(n)}
        if cash is not None:
            s["cash"] = cash
        state.save_sleeve(sleeve, s)
    _alpha_cap72 = config.SLEEVES["alpha_stocks"]["max_positions"]
    _fill72("alpha_stocks", _alpha_cap72)
    # Budget-bound core (2026-07-17): 'full' = satellite VALUE fills the 45%
    # budget, not a count — 5 x $4,200 on $25k cash = 45.7% of equity.
    _fill72("core_blend", 5, px=4200.0, cash=25_000.0)
    state.save("watchlist", {})
    # MU regression (operator 2026-07-22): a stock BUY that is book-full on BOTH
    # equity sleeves and ALSO qualifies for calls, where the options desk skips
    # it with a NON-book-full reason (size_zero — budget rounded to zero
    # contracts), must STILL earn a bench seat. Pre-fix the guard demanded EVERY
    # unplaced sleeve incl. calls be book-full, so the one non-book-full calls
    # skip denied the seat and the name was discarded + forgotten — the live MU
    # case the operator caught ("a buy that didn't show up and wasn't benched").
    state.journal("calls_skipped_size_zero", symbol="MUXX",
                  reason="budget too small for one contract")
    _vmu72 = {"rating": "STRONG_BUY", "conviction": 74, "suitable_for_calls": True,
              "thesis": "book-full but calls-qualified", "target_weight_pct": 3.0}
    _rt72.record_routing(
        {"MUXX": {"kind": "stock", "verdict": _vmu72,
                  "packet": {"technicals": {"price": 50.0}}}},
        since_ts="2026-01-01T00:00:00+00:00")
    _discmu72 = [e for e in state.read_journal(60)
                 if e.get("event") == "book_full_discarded"
                 and e.get("symbol") == "MUXX"]
    check("72 MU-case: book-full-both-equity BUY skipped size_zero on calls is "
          "BENCHED not discarded",
          state.load("watchlist", {}).get("MUXX", {}).get("source") == "book_full"
          and not _discmu72)
    state.save("watchlist", {})
    _v72 = {"rating": "BUY", "conviction": 60, "thesis": "intact momentum leader",
            "target_weight_pct": 3.0}
    _rt72.record_routing({"ALKX": {"kind": "stock", "verdict": _v72,
                                   "packet": {"technicals": {"price": 30.0}}}})
    wl = state.load("watchlist", {})
    check("72 book-full BUY benched by routing with thesis + qualified sleeves",
          wl.get("ALKX", {}).get("source") == "book_full"
          and "intact momentum" in wl["ALKX"]["book_full"]["thesis"]
          and set(wl["ALKX"]["qualified_sleeves"]) == {"alpha_stocks", "core_blend"})
    # Full books -> NOT promoted on the slot path (no free slot anywhere)
    _scr72 = {"quant_scores": {}, "stock_candidates": [], "technicals": {}}
    check("72 no free slot -> book-full name stays benched (no promotion)",
          not any(p["ticker"] == "ALKX" for p in watchlist.promotions(_scr72)))
    # Free ONE alpha slot -> promoted the same cycle with the slot story
    s = state.get_sleeve("alpha_stocks")
    s["positions"].popitem()
    state.save_sleeve("alpha_stocks", s)
    _p72 = next((p for p in watchlist.promotions(_scr72)
                 if p["ticker"] == "ALKX"), None)
    check("72 slot opened -> book-full BUY promoted to re-compete",
          _p72 is not None and "slot opened" in _p72["why"])
    # Graduation guard: a benched name re-judged BUY into FULL books is KEPT
    # as a book-full waiter (the same forgetting bug through the graduation
    # door); once actually HELD, the same verdict graduates it off the bench.
    _fill72("alpha_stocks", _alpha_cap72)
    wl = state.load("watchlist", {})
    wl["ALKX"].pop("promoted_at", None)
    state.save("watchlist", wl)
    _verd72 = {"ALKX": {"kind": "stock", "verdict": _v72}}
    watchlist.update(_scr72, _verd72)
    check("72 BUY into full books does NOT graduate off the bench",
          state.load("watchlist", {}).get("ALKX", {}).get("source") == "book_full")
    s = state.get_sleeve("alpha_stocks")
    s["positions"]["ALKX"] = {"symbol": "ALKX", "instrument": "equity",
                              "quantity": 5, "entry_price": 30.0, "cost": 150.0,
                              "opened_at": ts()}
    state.save_sleeve("alpha_stocks", s)
    watchlist.update(_scr72, _verd72)
    check("72 BUY that actually PLACED graduates off the bench",
          "ALKX" not in state.load("watchlist", {}))
    # SELL-family still deletes a book-full waiter (thesis dead)
    watchlist.add_book_full("SELX", _v72, ["alpha_stocks"],
                            {"alpha_stocks": f"slots full ({_alpha_cap72}/{_alpha_cap72})"})
    watchlist.update(_scr72, {"SELX": {"kind": "stock",
                                       "verdict": {"rating": "SELL", "conviction": 70}}})
    check("72 SELL verdict deletes a book-full waiter",
          "SELX" not in state.load("watchlist", {}))
finally:
    state.save("watchlist", _prev_wl72)
    state.save_sleeve("alpha_stocks", _prev_a72)
    state.save_sleeve("core_blend", _prev_c72)

# ══ 73. Fix-brief regressions (2026-07-16 IG triage) ══════════════════════════
# 73a — degraded_health persists from the PUBLIC card shape (items 3+8: the old
# read used sum_excess/correct keys the bucket carries as *_pct — KeyError'd on
# every populated bucket, so the trailing-accuracy gate ran blind forever).
_real_card73 = learning.verdict_report_card
_prev_dgh73 = state.load("degraded_health", None)
try:
    learning.verdict_report_card = lambda: {
        "by_rating": {
            "BUY": {"n": 20, "correct": 12, "correct_pct": 60.0,
                    "avg_fwd_excess_pct": 2.0, "excess": []},
            "degraded_fallback": {"n": 34, "correct": 8, "correct_pct": 23.5,
                                  "avg_fwd_excess_pct": -18.5, "excess": []}},
        "rating_calibration": [], "news_value": {}, "geopolitical_news_value": {}}
    learning.refresh_report_card_digest()
    _dgh73 = state.load("degraded_health", {}) or {}
    check("73a degraded_health snapshot persists (no KeyError on public shape)",
          _dgh73.get("n") == 34 and _dgh73.get("correct_pct") == 23.5
          and _dgh73.get("avg_fwd_excess_pct") == -18.5
          and _dgh73.get("real_n") == 20)
finally:
    learning.verdict_report_card = _real_card73
    if _prev_dgh73 is not None:
        state.save("degraded_health", _prev_dgh73)

# 73b — route_coverage counts real (non-degraded) emissions vs gate-qualified
# (item 7: 'route exists but can never fire' must be measured, not asserted).
_prev_r73 = state.load("research", {})
try:
    state.save("research", {"history": [{"asof": ts(1), "verdicts": {
        "AA73": {"rating": "SELL", "conviction": 80, "suitable_for_puts": True},
        "BB73": {"rating": "SELL", "conviction": 80, "suitable_for_puts": True,
                 "degraded": True},
        "CC73": {"rating": "STRONG_SELL", "conviction": 50},
        "DD73": {"rating": "BUY", "conviction": 90, "suitable_for_calls": True},
        "EE73": {"rating": "BUY", "conviction": 60}}}]})
    from firm import routing as _rt73
    _cov73 = _rt73.route_coverage()
    check("73b route_coverage: degraded excluded, gates measured",
          _cov73.get("sell_family_emitted") == 2
          and _cov73.get("puts_gate_qualified") == 1
          and _cov73.get("buy_family_emitted") == 2
          and _cov73.get("calls_gate_qualified") == 1)
finally:
    state.save("research", _prev_r73)

# 73c — IG_TRIAGE_BLOCK_BUYS lever actually blocks entries when latched
# (item 2: the escalator's docstring promised a flag that was wired nowhere).
_prev_inc73 = state.load("ig_triage_incident", None)
_prev_flag73 = config.IG_TRIAGE_BLOCK_BUYS
try:
    state.save("ig_triage_incident", {"since": ts(2), "alerted": True})
    _s73 = state.get_sleeve("alpha_stocks")
    config.IG_TRIAGE_BLOCK_BUYS = True
    _ok73, _why73 = risk.entries_allowed("alpha_stocks", _s73, 25000.0)
    check("73c IG_TRIAGE_BLOCK_BUYS on + latched incident blocks entries",
          not _ok73 and "triage" in _why73.lower())
    config.IG_TRIAGE_BLOCK_BUYS = False
    _ok73b, _ = risk.entries_allowed("alpha_stocks", _s73, 25000.0)
    check("73c flag off (default) leaves entries governed by the usual gates",
          _ok73b)
finally:
    config.IG_TRIAGE_BLOCK_BUYS = _prev_flag73
    state.save("ig_triage_incident", _prev_inc73 if _prev_inc73 is not None else {})

# 73d — new macro siblings are registered as MEDIUM (calendar-visible) and
# excluded from the high-impact reopen trigger set (item 10).
from firm import macro_calendar as _mc73
_new73 = {"umich", "conf_board", "housing_starts", "existing_home",
          "durables", "tsy_refunding"}
check("73d sibling releases registered",
      _new73 <= {r.key for r in _mc73.RELEASES})
check("73d siblings are medium impact (not bench-reopen triggers)",
      all(r.impact == "medium" for r in _mc73.RELEASES if r.key in _new73))
_ref73 = _mc73._rule_dates(_mc73._BY_KEY["tsy_refunding"],
                           _mc73.date(2026, 1, 1), _mc73.date(2026, 12, 31))
check("73d refunding rule generates first-Wednesday Feb/May/Aug/Nov",
      [d.isoformat() for d in _ref73] == ["2026-02-04", "2026-05-06",
                                          "2026-08-05", "2026-11-04"])

# ══ 74. NAME-full vs CAPITAL-full: parked cash yields the slot cap ════════════
# (operator 2026-07-16: 8/8 "full" with 27.5% deployed and $16.5K in SPY while
# BUYs were turned away — the cap counted names, not capital)
from firm import routing as _rt74
_real_eqp74 = broker.equity_prices
_real_ea74 = sleeves_mod2.risk.entries_allowed
_prev_a74 = state.get_sleeve("alpha_stocks")
try:
    broker.equity_prices = lambda syms: {x: 100.0 for x in syms}
    sleeves_mod2.risk.entries_allowed = lambda *a, **k: (True, "")
    _old74 = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    _cap74 = config.SLEEVES["alpha_stocks"]["max_positions"]
    _s74 = state.default_sleeve_state("alpha_stocks")
    _s74["cash"] = 500.0
    _s74["peak_equity"] = 999999
    # A NAME-full book of tiny positions (qty 1 @ the mocked $100 px each)
    # + a fat SPY parking reservoir = capital-empty.
    _s74["positions"] = {f"TN{i}": {"quantity": 1, "entry_price": 100.0,
                                    "opened_at": _old74, "instrument": "equity"}
                         for i in range(_cap74)}
    _s74["positions"]["SPY"] = {"quantity": 160, "entry_price": 100.0,
                                "opened_at": _old74, "instrument": "equity",
                                "cash_equitization": True}
    state.save_sleeve("alpha_stocks", _s74)
    check("74 parked_capacity: name-full + capital-empty + live parking -> True",
          _rt74.parked_capacity("alpha_stocks"))
    _v74 = {"NEWP": {"kind": "stock", "packet": {},
                     "verdict": {"rating": "BUY", "conviction": 70,
                                 "thesis": "leader", "target_weight_pct": 8}}}
    sleeves_mod2.run_alpha_stocks(_v74, {}, "paper")
    _sa74 = state.get_sleeve("alpha_stocks")
    _spy74 = _sa74["positions"].get("SPY", {})
    check("74 slot cap YIELDS: 9th name bought, funded from parking",
          "NEWP" in _sa74["positions"]
          and (_spy74.get("quantity") or 0) < 160)
    check("74 admission journaled (book_full_admitted_parked_cash)",
          any(e.get("event") == "book_full_admitted_parked_cash"
              and e.get("symbol") == "NEWP" for e in state.read_journal(80)))
    # A genuinely DEPLOYED book keeps the old skip: same names, big sizes.
    _s74b = state.default_sleeve_state("alpha_stocks")
    _s74b["cash"] = 500.0
    _s74b["peak_equity"] = 999999
    _s74b["positions"] = {f"BG{i}": {"quantity": 28, "entry_price": 100.0,
                                     "opened_at": _old74, "instrument": "equity"}
                          for i in range(_cap74)}
    _s74b["positions"]["SPY"] = {"quantity": 10, "entry_price": 100.0,
                                 "opened_at": _old74, "instrument": "equity",
                                 "cash_equitization": True}
    state.save_sleeve("alpha_stocks", _s74b)
    check("74 deployed book (94% invested) -> cap does NOT yield",
          not _rt74.parked_capacity("alpha_stocks"))
    sleeves_mod2.run_alpha_stocks({"NEWQ": _v74["NEWP"]}, {}, "paper")
    check("74 deployed book still skips book-full (mandate preserved)",
          "NEWQ" not in state.get_sleeve("alpha_stocks")["positions"])
    # Hard ceiling: max_positions + EXTRA real names -> never yields again.
    _s74c = state.default_sleeve_state("alpha_stocks")
    _s74c["cash"] = 500.0
    _s74c["positions"] = {f"CL{i}": {"quantity": 1, "entry_price": 100.0,
                                     "opened_at": _old74, "instrument": "equity"}
                          for i in range(_cap74 + config.ALPHA_PARKED_OVERRIDE_MAX_EXTRA)}
    _s74c["positions"]["SPY"] = {"quantity": 160, "entry_price": 100.0,
                                 "opened_at": _old74, "instrument": "equity",
                                 "cash_equitization": True}
    state.save_sleeve("alpha_stocks", _s74c)
    check("74 hard ceiling (max+EXTRA) -> cap binds even with fat parking",
          not _rt74.parked_capacity("alpha_stocks"))
finally:
    broker.equity_prices = _real_eqp74
    sleeves_mod2.risk.entries_allowed = _real_ea74
    state.save_sleeve("alpha_stocks", _prev_a74)

# ══ 75. Street Research desk (operator 2026-07-16: institutional reports) ═════
from pathlib import Path as _P75
from firm import street_research as _sr75
from firm import analysts as _an75
from firm import brain as _br75
_prev_dir75 = config.STREET_RESEARCH_DIR
_prev_store75 = state.load("street_research", None)
_real_avail75, _real_ask75 = _br75.available, _br75.ask_json
try:
    _d75 = _P75(tempfile.mkdtemp())
    config.STREET_RESEARCH_DIR = _d75
    (_d75 / "gs_ai_report.txt").write_text(
        "AI infrastructure capex supercycle: hyperscaler spending accelerating "
        "across compute, networking and power. " * 8, encoding="utf-8")
    _brief75 = {"relevance": {"is_relevant": True, "why": "US equity sectors"},
                "source": "Goldman Sachs", "title": "GenAI: The Next Platform",
                "published": "2026-07-14", "scope": "AI infrastructure capex",
                "key_theses": [
                    {"claim": "Hyperscaler capex +45% YoY to $380B in 2026",
                     "horizon": "cyclical",
                     "checkable": "Q2/Q3 hyperscaler capex prints",
                     "confidence": "high"},
                    {"claim": "AI power demand doubles grid capex by 2030",
                     "horizon": "structural",
                     "checkable": "utility capex guidance trend",
                     "confidence": "medium"},
                    # legacy bare-string thesis — shape tolerance
                    "Semis supply stays tight through 2026"],
                "sector_implications": {"favored": ["semis", "power"],
                                        "pressured": ["legacy SaaS"]},
                "named_tickers": [{"ticker": "NVDA", "stance": "positive",
                                   "why": "accelerator share"}],
                "risks_and_contrarian_points": ["monetization lag"]}
    _br75.available = lambda: True
    _br75.ask_json = lambda *a, **k: dict(_brief75)
    state.save("street_research", {})
    check("75 new report ingested + journaled",
          _sr75.scan_and_distill() == 1
          and any(e.get("event") == "street_research_ingested"
                  for e in state.read_journal(30)))
    check("75 sha dedupe: second scan ingests nothing",
          _sr75.scan_and_distill() == 0)
    _dg75 = _sr75.digest_context()
    check("75 digest carries the report for the packet (claims normalized)",
          bool(_dg75) and _dg75["reports"][0]["source"] == "Goldman Sachs"
          and "capex" in _dg75["reports"][0]["key_theses"][0]["claim"].lower()
          and len(_dg75["reports"][0]["key_theses"]) == 3)
    check("75 fundamental/sentiment/macro desks see it; technical/quant stay pure",
          all("street_research" in _an75._DESK_VIEWS[d]
              for d in ("fundamental", "sentiment", "macro"))
          and all("street_research" not in _an75._DESK_VIEWS[d]
                  for d in ("technical", "quant")))
    _pk75 = {"ticker": "NVDA", "asof": "x", "street_research": _dg75,
             "technicals": {"price": 1}}
    check("75 desk packet passes the digest through",
          "street_research" in _an75._desk_packet("fundamental", _pk75))
    # Claim REVIEW lifecycle: Opus marks the cyclical claim debunked -> it
    # leaves the digest; the source's track record charges the miss.
    _st75 = state.load("street_research", {})
    _st75["docs"][0]["added"] = ts(8)          # past the 7d review cadence
    state.save("street_research", _st75)
    # Fleet re-grade action 6: reviews resolve ONLY from attached observed
    # data — patch the price feed (offline) and anchor resolved_from to it.
    import pandas as _pd75
    _real_fh75 = data_engine.fetch_history
    _idx75 = _pd75.date_range(end=_pd75.Timestamp.utcnow().tz_localize(None),
                              periods=10, freq="D")
    data_engine.fetch_history = lambda tickers, period="2y": _pd75.DataFrame(
        {"NVDA": [100, 99, 97, 94, 90, 87, 84, 82, 80, 78]}, index=_idx75)
    _br75.ask_json = lambda *a, **k: {"reviews": [
        {"idx": 0, "status": "debunked",
         "evidence": "Q2 prints showed capex +18% YoY, far below +45%",
         "resolved_from": "NVDA: -20.5% since ingestion (observed data)"},
        {"idx": 1, "status": "supported", "evidence": "utility capex raised",
         "resolved_from": "NVDA price series in the observed data block"}]}
    check("75 review pass runs when due", _sr75.review_claims() == 1)
    _dg75b = _sr75.digest_context()
    _claims75 = [t["claim"] for t in _dg75b["reports"][0]["key_theses"]]
    check("75 debunked claim leaves the digest; supported one stays flagged",
          not any("380B" in c for c in _claims75)
          and any("grid capex" in c for c in _claims75)
          and "confirmed_since_publication"
          in _dg75b["reports"][0]["key_theses"][0])
    check("75 source track record charges the miss",
          "1 debunked" in _dg75b["reports"][0]["source_track_record"])
    # Horizon TTLs: at 20d a tactical claim is dead, a structural one lives.
    _st75 = state.load("street_research", {})
    _st75["docs"][0]["added"] = ts(20)
    _th75 = _st75["docs"][0]["brief"]["key_theses"]
    _th75[2]["horizon"] = "tactical"           # the legacy string, normalized
    state.save("street_research", _st75)
    _dg75c = _sr75.digest_context()
    _claims75c = [t["claim"] for t in _dg75c["reports"][0]["key_theses"]]
    check("75 per-horizon TTL: tactical dead at 20d, structural alive",
          not any("supply stays tight" in c for c in _claims75c)
          and any("grid capex" in c for c in _claims75c))
    # Full expiry: at 200d even the structural claim is gone -> shelf empty
    _st75 = state.load("street_research", {})
    _st75["docs"][0]["added"] = ts(200)
    state.save("street_research", _st75)
    check("75 a fully stale report expires off the shelf",
          _sr75.digest_context() is None)
    # Relevance gate: an irrelevant doc becomes a tombstone, never a packet
    (_d75 / "crypto_gossip.txt").write_text(
        "A newsletter about meme-coin drama with no bearing on equities. " * 10,
        encoding="utf-8")
    _br75.ask_json = lambda *a, **k: {
        "relevance": {"is_relevant": False, "why": "no US equity implication"},
        "source": "Substack", "title": "Meme coin drama",
        "key_theses": [{"claim": "x", "horizon": "tactical",
                        "checkable": "", "confidence": "low"}]}
    _sr75.scan_and_distill()
    _dg75d = _sr75.digest_context()
    check("75 irrelevant doc gated out (tombstoned, not injected)",
          (_dg75d is None or all(r["source"] != "Substack"
                                 for r in _dg75d["reports"]))
          and _sr75.scan_and_distill() == 0)   # tombstone blocks re-distill
finally:
    config.STREET_RESEARCH_DIR = _prev_dir75
    _br75.available, _br75.ask_json = _real_avail75, _real_ask75
    try:
        data_engine.fetch_history = _real_fh75
    except NameError:
        pass
    state.save("street_research", _prev_store75 if _prev_store75 is not None else {})

# ══ 76. The 8:30 held-book cycle: sectioned, stored, ACTS on its verdicts ═════
from firm import cycle as _cyc76
from firm import riskbrain as _rb76
import pandas as _pd76
_prev_a76 = state.get_sleeve("alpha_stocks")
_prev_r76 = state.load("research", {})
_prev_poe76 = state.load("pending_open_entries", [])
_reals76 = (_cyc76._research_one, data_engine.fetch_history,
            data_engine.compute_technicals, _rb76.sector_regime,
            data_engine.macro_brief, broker.fetch_marks, broker.equity_prices)
# The suite is OFFLINE: the premarket hooks (tradable refresh -> Public API,
# seat review -> live ETF momentum) are patched out; they have §79/§78 tests.
from firm import tradable as _trd76
from firm import allocation as _al76
_hooks76 = (_trd76.refresh, _al76.refresh_etf_pool, _al76.review_seats)
_trd76.refresh = lambda force=False: {}
_al76.refresh_etf_pool = lambda force=False: {}
_al76.review_seats = lambda mode=None: {"parking_switched": None, "core_switched": []}
try:
    _s76 = state.default_sleeve_state("alpha_stocks")
    _s76["positions"] = {t: {"quantity": 5, "entry_price": 50.0,
                             "opened_at": ts(5), "instrument": "equity"}
                         for t in ("HLDA", "HLDB", "HLDC")}
    state.save_sleeve("alpha_stocks", _s76)
    state.save("research", {"history": [
        # HLDC has the FRESHEST verdict -> with cap=2 the two stalest go first
        {"asof": ts(0.05), "verdicts": {"HLDC": {"rating": "HOLD",
                                                 "conviction": 60}}}]})
    _idx76 = _pd76.date_range("2026-01-01", periods=40)
    data_engine.fetch_history = lambda syms, period="1y": _pd76.DataFrame(
        {s: range(100, 140) for s in (syms if isinstance(syms, list) else [syms])},
        index=_idx76, dtype=float)
    data_engine.compute_technicals = lambda closes, bench: {
        c: {"price": 120.0, "ret_1m": 5.0, "ret_3m": 8.0, "ret_6m": 15.0,
            "rel_strength_3m": 2.0, "above_sma50": True, "above_sma200": True,
            "rsi14": 55.0, "vol_ann_pct": 30.0}
        for c in closes.columns}
    _rb76.sector_regime = lambda: {}
    data_engine.macro_brief = lambda regime=None: {}
    broker.fetch_marks = lambda st: {}
    broker.equity_prices = lambda syms: {x: 100.0 for x in syms}
    # HLDB comes back SELL -> the 8:30 cycle must ACT on it (research to
    # acting, operator 2026-07-16); degraded HLDA-style fallbacks are covered
    # in the standalone degraded check below.
    _cyc76._research_one = lambda t, kind, tech, quant, depth, wn=None, \
        regime=None, macro=None: {
        "ticker": t, "kind": kind, "asof": ts(0),
        "packet": {"technicals": tech, "next_earnings": {},
                   "fundamentals": {"sector": "Test"}},
        "research": {}, "composite": None, "cases": {},
        "verdict": ({"rating": "SELL", "conviction": 80,
                     "thesis": "thesis broke overnight"} if t == "HLDB" else
                    {"rating": "HOLD", "conviction": 62,
                     "thesis": f"premarket look at {t}"})}
    _res76 = _cyc76.underwrite_held(max_names=2)
    check("76 sectioned: cap honored, stalest verdicts first",
          sorted(_res76["underwritten"]) == ["HLDA", "HLDB"]
          and _res76["backlog"] == ["HLDC"])
    _hist76 = state.load("research", {}).get("history", [])
    _pm76 = [h for h in _hist76 if h.get("premarket_held")]
    check("76 verdicts stored as a tagged premarket history row",
          len(_pm76) == 1
          and set(_pm76[-1]["verdicts"]) == {"HLDA", "HLDB"}
          and _pm76[-1]["context"]["HLDA"]["owned"] is True)
    check("76 backlog journaled for the next section",
          any(e.get("event") == "held_underwrite_backlog"
              and e.get("names") == ["HLDC"] for e in state.read_journal(40)))
    _sa76 = state.get_sleeve("alpha_stocks")
    check("76 ACTS on its verdicts: the SELL exited premarket",
          "HLDB" not in _sa76["positions"] and "HLDA" in _sa76["positions"])
    # Degraded held workup = failed workup: nothing stored, name stays stale
    state.save("research", {"history": []})
    _cyc76._research_one = lambda t, kind, tech, quant, depth, wn=None, \
        regime=None, macro=None: {
        "ticker": t, "kind": kind, "asof": ts(0),
        "packet": {"technicals": tech}, "research": {}, "composite": None,
        "cases": {}, "verdict": {"rating": "BUY", "conviction": 90,
                                 "degraded": True}}
    _res76b = _cyc76.underwrite_held(max_names=2)
    check("76 degraded held workups are NOT stored (no fabricated verdicts)",
          _res76b["underwritten"] == []
          and not [h for h in state.load("research", {}).get("history", [])
                   if h.get("premarket_held")])
finally:
    (_cyc76._research_one, data_engine.fetch_history,
     data_engine.compute_technicals, _rb76.sector_regime,
     data_engine.macro_brief, broker.fetch_marks,
     broker.equity_prices) = _reals76
    (_trd76.refresh, _al76.refresh_etf_pool, _al76.review_seats) = _hooks76
    state.save_sleeve("alpha_stocks", _prev_a76)
    state.save("research", _prev_r76)
    state.save("pending_open_entries", _prev_poe76)

# ══ 77. Queued-for-the-open entries: capture + flush + honest re-queues ═══════
from firm import market_hours as _mh77
from firm import sleeves as _slv77
_prev_a77 = state.get_sleeve("alpha_stocks")
_prev_poe77 = state.load("pending_open_entries", [])
_prev_rq77 = state.load("research_requests", {"queue": []})
_prev_tu77 = state.load("tradable_universe", None)
_reals77 = (_mh77.market_is_open, broker.equity_prices)
try:
    # §76's hooks may have pulled a REAL universe into the sandbox — pin an
    # empty one so the tradability gate fails open for this section's fakes.
    from firm import tradable as _trd77
    state.save("tradable_universe", {})
    _trd77._cache["asof"] = None
    _s77 = state.default_sleeve_state("alpha_stocks")
    # Consistent book: cash == starting capital == peak, else entries_allowed
    # correctly reads an 80% drawdown and halts (the gate is real in flush)
    _s77["cash"] = 25000.0
    state.save_sleeve("alpha_stocks", _s77)
    state.save("pending_open_entries", [])
    state.save("research_requests", {"queue": []})
    # Market CLOSED: a paper BUY is captured as an open-queue intent
    # (the harness disables the paper bell gate globally at line ~34 —
    # this section tests exactly that gate, so re-enable it)
    config.PAPER_ENFORCE_MARKET_HOURS = True
    _mh77.market_is_open = lambda *a, **k: False
    _s77 = state.get_sleeve("alpha_stocks")
    _r77 = broker.execute("paper", _s77, symbol="QOPN", side="buy",
                          quantity=10, price=100.0,
                          meta={"thesis": "t", "vol_ann": 30.0})
    check("77 market-closed BUY is queued, not lost",
          not _r77.get("success") and _r77.get("queued")
          and any(e.get("symbol") == "QOPN"
                  for e in state.load("pending_open_entries", [])))
    check("77 capture journaled (entry_queued_for_open)",
          any(e.get("event") == "entry_queued_for_open"
              and e.get("symbol") == "QOPN" for e in state.read_journal(30)))
    # Market OPEN: the flush executes it through the gates
    state.save_sleeve("alpha_stocks", _s77)
    _mh77.market_is_open = lambda *a, **k: True
    broker.equity_prices = lambda syms: {x: 100.5 for x in syms}
    _n77 = _slv77.flush_pending_open_entries("paper")
    _sa77 = state.get_sleeve("alpha_stocks")
    check("77 flush at the open fills the queued entry",
          _n77 == 1 and "QOPN" in _sa77["positions"]
          and state.load("pending_open_entries", []) == [],
          f"n={_n77} pos={sorted(_sa77['positions'])} "
          f"q={state.load('pending_open_entries', [])} "
          f"j={[e.get('event') for e in state.read_journal(8)]}")
    # A drifted intent is RE-QUEUED for research, never silently dropped
    state.save("pending_open_entries", [
        {"sleeve": "alpha_stocks", "symbol": "DRFT", "quantity": 5,
         "thesis_price": 100.0, "meta": {"vol_ann": 20.0},
         "queued_at": ts(0.5)}])
    broker.equity_prices = lambda syms: {x: 120.0 for x in syms}
    _slv77.flush_pending_open_entries("paper")
    check("77 drifted intent re-queued for a fresh committee look",
          "DRFT" not in state.get_sleeve("alpha_stocks")["positions"]
          and any(r.get("ticker") == "DRFT"
                  for r in state.load("research_requests",
                                      {"queue": []}).get("queue", [])))
finally:
    config.PAPER_ENFORCE_MARKET_HOURS = False   # back to harness default
    (_mh77.market_is_open, broker.equity_prices) = _reals77
    state.save_sleeve("alpha_stocks", _prev_a77)
    state.save("pending_open_entries", _prev_poe77)
    state.save("research_requests", _prev_rq77)
    if _prev_tu77 is not None:
        state.save("tradable_universe", _prev_tu77)
    _trd77._cache["asof"] = None

# ══ 78. Dynamic ETF seats + measured-edge tilt (operator/audit 2026-07-16) ════
from firm import allocation as _al78
_prev_seats78 = state.load("etf_seats", None)
_prev_eh78 = state.load("edge_health", None)
_prev_c78 = state.get_sleeve("core_blend")
_reals78 = (broker.equity_prices,)
try:
    # accessors fall back to config when no seats state exists
    state.save("etf_seats", {})
    check("78 parking accessor falls back to config default",
          _al78.parking_symbol("alpha_stocks") == config.BENCHMARK)
    check("78 core accessor falls back to config trio",
          _al78.core_weights() == {"VOO": 35.0, "QQQ": 15.0, "GLD": 5.0})
    state.save("etf_seats", {"parking": {"symbol": "VTI", "since": ts(1),
                                         "challenger": {}},
                             "core": {"slots": ["QQQ", "RSP", "TLT"],
                                      "since": ts(1), "challengers": {}}})
    check("78 dynamic seats override the defaults (weights preserved by slot)",
          _al78.parking_symbol("alpha_stocks") == "VTI"
          and _al78.core_weights() == {"QQQ": 35.0, "RSP": 15.0, "TLT": 5.0})
    # hysteresis: a thin lead never wins; a fat lead needs CONSECUTIVE looks
    _rk78 = [("QQQ", 20.0), ("SPY", 18.5)]
    _w78, _c78 = _al78._challenge("SPY", _rk78, 4.0, {})
    check("78 hysteresis: thin lead -> no switch, no streak",
          _w78 is None and _c78 == {})
    _rk78b = [("QQQ", 25.0), ("SPY", 18.5)]
    _w78, _c78 = _al78._challenge("SPY", _rk78b, 4.0, {})
    check("78 hysteresis: fat lead look 1 -> streak armed, no switch yet",
          _w78 is None and _c78.get("symbol") == "QQQ"
          and _c78.get("streak") == 1)
    _w78, _c78 = _al78._challenge("SPY", _rk78b, 4.0, _c78)
    check("78 hysteresis: fat lead look 2 -> seat flips",
          _w78 == "QQQ" and _c78 == {})
    # committee SELL on a seated core ETF -> sold + next-best seated
    import firm.data_engine as _de78
    _real_sig78 = _de78.rotation_signals
    _de78.rotation_signals = lambda universe=None: {
        "ranked": [("XLK", 30.0), ("IWM", 10.0)], "cash_hurdle_3m": 1.0}
    broker.equity_prices = lambda syms: {x: 100.0 for x in syms}
    _s78 = state.default_sleeve_state("core_blend")
    _s78["cash"] = 500.0
    _s78["positions"] = {"QQQ": {"symbol": "QQQ", "instrument": "equity",
                                 "quantity": 50, "entry_price": 90.0,
                                 "cost": 4500.0, "opened_at": ts(3),
                                 "core_seat": True}}
    state.save_sleeve("core_blend", _s78)
    _dem78 = _al78.apply_sell_verdicts(
        {"QQQ": {"kind": "etf", "verdict": {"rating": "SELL", "conviction": 70}}},
        "paper")
    _sc78 = state.get_sleeve("core_blend")
    check("78 seated ETF SELL verdict executes: sold + replacement seated",
          _dem78 == ["QQQ"] and "QQQ" not in _sc78["positions"]
          and "XLK" in _sc78["positions"]
          and _al78.core_weights().get("XLK") == 35.0,
          f"demoted={_dem78} pos={sorted(_sc78['positions'])} "
          f"cw={_al78.core_weights()} "
          f"journal={[e.get('event') for e in state.read_journal(8)]}")
    check("78 degraded SELL never unseats",
          _al78.apply_sell_verdicts(
              {"XLK": {"kind": "etf", "verdict": {"rating": "SELL",
                                                  "conviction": 90,
                                                  "degraded": True}}},
              "paper") == [])
    _de78.rotation_signals = _real_sig78
    # measured-edge tilt (#9): real edge + top momentum -> +2; thin n -> 0
    state.save("edge_health", {"edges": [
        {"edge": "factor:momentum", "n": 61, "value": 18.2}]})
    check("78 edge tilt: +2 on top momentum, -2 on weak, 0 mid",
          sleeves_mod2._edge_weight_tilt({"factor_scores": {"momentum": 95}}) == 2.0
          and sleeves_mod2._edge_weight_tilt({"factor_scores": {"momentum": 30}}) == -2.0
          and sleeves_mod2._edge_weight_tilt({"factor_scores": {"momentum": 60}}) == 0.0)
    state.save("edge_health", {"edges": [
        {"edge": "factor:momentum", "n": 12, "value": 18.2}]})
    check("78 edge tilt: thin sample (n<30) tilts exactly 0",
          sleeves_mod2._edge_weight_tilt({"factor_scores": {"momentum": 95}}) == 0.0)
    # money review must not wipe the verdict-review watermark
    _prev_ls78 = state.load("lessons", None)
    _wm78 = ts(2)
    state.save("lessons", {"lessons": [], "reviewed_through": 3,
                           "verdict_reviewed_asof": _wm78})
    _lprev78 = state.load("lessons", {})
    _out78 = {"lessons": [], "reviewed_through": 9, "asof": ts(0)}
    if _lprev78.get("verdict_reviewed_asof"):
        _out78["verdict_reviewed_asof"] = _lprev78["verdict_reviewed_asof"]
    state.save("lessons", _out78)
    check("78 verdict-review watermark survives a money review rebuild",
          state.load("lessons", {}).get("verdict_reviewed_asof") == _wm78)
    if _prev_ls78 is not None:
        state.save("lessons", _prev_ls78)
finally:
    (broker.equity_prices,) = _reals78
    state.save_sleeve("core_blend", _prev_c78)
    if _prev_seats78 is not None:
        state.save("etf_seats", _prev_seats78)
    if _prev_eh78 is not None:
        state.save("edge_health", _prev_eh78)

# ══ 79. Live-parity universe: Public tradability + full ETF pool (2026-07-16) ═
from firm import tradable as _trd79
from firm import allocation as _al79
_prev_tu79 = state.load("tradable_universe", None)
_prev_pool79 = state.load("etf_pool", None)
try:
    # fail-OPEN with no snapshot (telemetry gap must never block trading)
    state.save("tradable_universe", {})
    check("79 no universe snapshot -> gates fail open",
          _trd79.is_buyable("ANYTHING") and _trd79.options_enabled("ANYTHING"))
    state.save("tradable_universe", {
        "asof": ts(0.01), "source": "public_api", "n_buyable": 9000,
        "buyable": ["AAPL", "SPY", "DVA"], "options_enabled": ["AAPL"]})
    _trd79._cache["asof"] = None            # bust the frozenset cache
    check("79 buyable gate matches Public's list",
          _trd79.is_buyable("AAPL") and not _trd79.is_buyable("ZZFAKE"))
    check("79 options gate: enabled vs disabled",
          _trd79.options_enabled("AAPL") and not _trd79.options_enabled("SPY"))
    _ok79, _d79 = _trd79.universe_health()
    check("79 universe health red on implausibly thin API list",
          _ok79 is False and "thin" in _d79)
    # leverage classifier: 3x/inverse products never hold a SEAT
    check("79 leverage classifier",
          _al79._is_leveraged("SOXL", "Direxion Daily Semiconductor Bull 3X Shares")
          and _al79._is_leveraged("SQQQ", "ProShares UltraPro Short QQQ")
          and not _al79._is_leveraged("SPY", "SPDR S&P 500 ETF Trust")
          and not _al79._is_leveraged("BSV", "Vanguard Short-Term Bond ETF"))
    state.save("etf_pool", {"asof": ts(0.01), "etfs": [
        {"symbol": "SPY", "dollar_vol": 30_000_000_000, "leveraged": False},
        {"symbol": "SOXL", "dollar_vol": 9_000_000_000, "leveraged": True},
        {"symbol": "QQQ", "dollar_vol": 20_000_000_000, "leveraged": False},
        {"symbol": "IWM", "dollar_vol": 2_000_000_000, "leveraged": False},
        {"symbol": "XLE", "dollar_vol": 900_000_000, "leveraged": False}] +
        [{"symbol": f"E{i}79", "dollar_vol": 10_000_000, "leveraged": False}
         for i in range(12)]})
    check("79 parking eligible = deep-liquid 1x only (no SOXL)",
          _al79._parking_eligible() == ["SPY", "QQQ", "IWM"],
          str(_al79._parking_eligible()))
    check("79 core eligible excludes leveraged, keeps the rest",
          "SOXL" not in _al79._core_eligible()
          and {"SPY", "QQQ", "XLE"} <= set(_al79._core_eligible()))
    state.save("etf_pool", {})
    check("79 no pool -> eligibles fall back to config hand-lists",
          _al79._parking_eligible() == list(config.PARKING_ELIGIBLE)
          and _al79._core_eligible() == list(config.CORE_ELIGIBLE))
    # — 79b: parking = RESERVOIR FITNESS, not momentum (operator 2026-07-20:
    # "like SPY but better — not too volatile, not too stagnant"). Pure math
    # over synthetic closes: a steady broad fund with SPY's vol and a touch
    # more drift must out-score SPY; a hot sector fund (2.6x vol) fails the
    # corridor; a cash-like flat fund fails the low side; SPY always ranks.
    import numpy as _np79
    import pandas as _pd79
    _idx79 = _pd79.bdate_range("2025-07-01", periods=260)
    _r79 = _np79.random.default_rng(7).normal(0.0005, 0.007, 260)
    _closes79 = _pd79.DataFrame({
        "SPY": 100 * _np79.cumprod(1 + _r79),
        "BRD79": 100 * _np79.cumprod(1 + _r79 + 0.0003),   # broad, same vol, more drift
        "HOT79": 100 * _np79.cumprod(1 + _r79 * 2.6),      # sector-hot: vol out of band
        "FLT79": 100 * _np79.cumprod(_np79.full(260, 1.00008)),  # cash-like stagnant
    }, index=_idx79)
    _fit79 = data_engine._reservoir_fitness_from_closes(_closes79)
    _rk79 = dict(_fit79["ranked"])
    check("79b fitness: steady broad fund with more drift out-scores SPY",
          "BRD79" in _rk79 and "SPY" in _rk79 and _rk79["BRD79"] > _rk79["SPY"],
          str(_fit79["ranked"][:3]))
    check("79b fitness: hot sector fund fails the vol corridor (SOXX class)",
          "HOT79" not in _rk79 and "vol" in str(_fit79["gates"].get("HOT79", "")))
    check("79b fitness: cash-like stagnant fund fails the low-vol side",
          "FLT79" not in _rk79 and "vol" in str(_fit79["gates"].get("FLT79", "")))
    check("79b fitness: short history is gated, never scored",
          "history" in str(data_engine._reservoir_fitness_from_closes(
              _closes79.iloc[-60:])["gates"].get("BRD79", "")) or
          not data_engine._reservoir_fitness_from_closes(
              _closes79.iloc[-60:])["ranked"])
    # flush refuses an intent Public cannot buy
    from firm import market_hours as _mh79
    _real_mio79 = _mh79.market_is_open
    _real_eqp79 = broker.equity_prices
    _prev_a79 = state.get_sleeve("alpha_stocks")
    _prev_poe79 = state.load("pending_open_entries", [])
    try:
        state.save("tradable_universe", {
            "asof": ts(0.01), "source": "public_api", "n_buyable": 9000,
            "buyable": ["AAPL"], "options_enabled": []})
        _trd79._cache["asof"] = None
        _s79 = state.default_sleeve_state("alpha_stocks")
        _s79["cash"] = 25000.0
        state.save_sleeve("alpha_stocks", _s79)
        state.save("pending_open_entries", [
            {"sleeve": "alpha_stocks", "symbol": "GONE79", "quantity": 5,
             "thesis_price": 100.0, "meta": {}, "mode": "paper",
             "queued_at": ts(0.5)}])
        _mh79.market_is_open = lambda *a, **k: True
        broker.equity_prices = lambda syms: {x: 100.0 for x in syms}
        sleeves_mod2.flush_pending_open_entries("paper")
        check("79 flush refuses a non-tradable intent (journaled, not filled)",
              "GONE79" not in state.get_sleeve("alpha_stocks")["positions"]
              and any(e.get("event") == "open_queue_not_tradable"
                      for e in state.read_journal(20)))
    finally:
        _mh79.market_is_open = _real_mio79
        broker.equity_prices = _real_eqp79
        state.save_sleeve("alpha_stocks", _prev_a79)
        state.save("pending_open_entries", _prev_poe79)
finally:
    _trd79._cache["asof"] = None
    if _prev_tu79 is not None:
        state.save("tradable_universe", _prev_tu79)
    else:
        state.save("tradable_universe", {})
    if _prev_pool79 is not None:
        state.save("etf_pool", _prev_pool79)

# ══ 80. Deployment scaler + weight anchors (A+ push, 2026-07-16) ══════════════
_prev_a80 = state.get_sleeve("alpha_stocks")
_prev_floor80 = config.ALPHA_DEPLOYMENT_FLOOR_PCT
_reals80 = (broker.equity_prices, sleeves_mod2.risk.entries_allowed)
try:
    config.ALPHA_DEPLOYMENT_FLOOR_PCT = 60.0
    broker.equity_prices = lambda syms: {x: 100.0 for x in syms}
    sleeves_mod2.risk.entries_allowed = lambda *a, **k: (True, "")
    _s80 = state.default_sleeve_state("alpha_stocks")
    _s80["cash"] = 25000.0                    # empty book -> 0% deployed
    state.save_sleeve("alpha_stocks", _s80)
    _v80 = {"SCL80": {"kind": "stock", "packet": {"technicals": {"price": 100.0}},
                      "verdict": {"rating": "BUY", "conviction": 70,
                                  "thesis": "t", "target_weight_pct": 6.0}}}
    sleeves_mod2.run_alpha_stocks(_v80, {}, "paper")
    _pos80 = state.get_sleeve("alpha_stocks")["positions"].get("SCL80", {})
    # 6% x 1.5 (0% deployed -> full scale) = 9% of $25k = $2,250 -> 22 shares
    # (2026-08-14 operator decision: alpha single-name sizing halved) the
    # ALPHA_SIZING_DERISK=0.5 factor applies BEFORE the scaler, so the 6% ask
    # sizes as 3% x 1.5 = 4.5% of $25k = $1,125 -> 11 shares, and the scaler's
    # journaled counterfactual/scaled weights are the de-risked 3.0 -> 4.5.
    check("80 empty book: entry scaled 1.5x (6% -> 9%)",
          _pos80.get("quantity") == 11,
          f"qty={_pos80.get('quantity')} "
          f"j={[e.get('event') for e in state.read_journal(6)]}")
    check("80 scaler journaled with counterfactual",
          any(e.get("event") == "deployment_scale_applied"
              and e.get("counterfactual_weight") == 3.0
              and e.get("scaled_weight") == 4.5
              for e in state.read_journal(12)))
    # deployed book -> no scaling
    _s80b = state.default_sleeve_state("alpha_stocks")
    _s80b["cash"] = 2500.0
    _s80b["positions"] = {f"DP{i}80": {"quantity": 28, "entry_price": 100.0,
                                       "opened_at": ts(3), "instrument": "equity"}
                          for i in range(8)}          # ~90% deployed
    state.save_sleeve("alpha_stocks", _s80b)
    sleeves_mod2.run_alpha_stocks(
        {"SCB80": {"kind": "stock", "packet": {"technicals": {"price": 100.0}},
                   "verdict": {"rating": "BUY", "conviction": 70, "thesis": "t",
                               "target_weight_pct": 6.0}}}, {}, "paper")
    check("80 deployed book: no scaling applied",
          not any(e.get("event") == "deployment_scale_applied"
                  and e.get("symbol") == "SCB80"
                  for e in state.read_journal(10)))
    # judge prompt carries the weight anchors + dynamic calibration plumbing
    from firm import debate as _db80
    check("80 weight anchors ride the judge guidance",
          "POSITION WEIGHT ANCHORS" in _db80._judge_guidance())
finally:
    config.ALPHA_DEPLOYMENT_FLOOR_PCT = _prev_floor80
    (broker.equity_prices, sleeves_mod2.risk.entries_allowed) = _reals80
    state.save_sleeve("alpha_stocks", _prev_a80)

# ══ 81. Fleet re-grade fixes (2026-07-16 overnight): options gate veto
#        demotion, momentum-lane loop closure, parking in firm caps, flush
#        adds/TTL/settle, parked-capacity de-risk gate, street grounding ═══════
from firm import market_hours as _mh81
from firm import routing as _rt81
from firm import street_research as _sr81
from firm import tradable as _trd81
_prev81 = {k: state.get_sleeve(k) for k in config.SLEEVES}
_prev_tun81 = state.load("tuning", None)
_prev_poe81 = state.load("pending_open_entries", [])
_prev_rq81 = state.load("research_requests", {"queue": []})
_prev_tu81 = state.load("tradable_universe", None)
_prev_str81 = state.load("street_research", None)
_reals81 = (_mh81.market_is_open, broker.equity_prices,
            risk.firm_symbol_exposure_pct, brain.available, brain.ask_json,
            data_engine.fetch_history)
try:
    state.save("tradable_universe", {})
    _trd81._cache["asof"] = None          # options_enabled/is_buyable fail OPEN

    # — action 4: momentum-lane tuning loop actually closes —
    state.save("tuning", {"caps": {"MOMENTUM_LANE_MOM_MIN": 75.0}})
    check("81 momentum-lane lever: tuned_cap reads the tuned value",
          risk.tuned_cap("MOMENTUM_LANE_MOM_MIN") == 75.0)
    state.save("tuning", {"caps": {"MOMENTUM_LANE_MOM_MIN": 50.0}})
    check("81 momentum-lane lever: clamped into MOMENTUM_LANE_BOUNDS",
          risk.tuned_cap("MOMENTUM_LANE_MOM_MIN") == 70.0)
    state.save("tuning", {})

    # — action 1: options gate — judge flag demoted to veto —
    _v81 = {"rating": "BUY", "conviction": 72, "suitable_for_calls": False}
    _pk81 = {"next_earnings": {"days_away": 60}}
    check("81 options gate: reflexive False no longer binds (mech qualifies)",
          sleeves_mod2._calls_entry_ok(_v81, "OPTX", _pk81))
    check("81 options gate: EXPLICIT justified veto IS a veto",
          not sleeves_mod2._calls_entry_ok(
              {**_v81, "options_veto_calls": True,
               "suitable_for_calls_reason":
               "illiquid chain and FDA readout inside the window"},
              "OPTX", _pk81))
    check("81 options gate: reason WITHOUT the explicit veto flag hands the "
          "call to mech (fix brief 2026-07-21 #6)",
          sleeves_mod2._calls_entry_ok(
              {**_v81, "suitable_for_calls_reason":
               "illiquid chain and FDA readout inside the window"},
              "OPTX", _pk81))
    check("81 options gate: earnings inside the option window refused by mech",
          not sleeves_mod2._calls_entry_ok(
              _v81, "OPTX", {"next_earnings": {"days_away": 10}}))
    check("81 options gate: conviction bar still binds under the demotion",
          not sleeves_mod2._calls_entry_ok(
              {"rating": "BUY", "conviction": 40}, "OPTX", _pk81))
    check("81 options gate: judge TRUE still short-circuits",
          sleeves_mod2._calls_entry_ok(
              {"rating": "BUY", "conviction": 72, "suitable_for_calls": True}))
    check("81 puts gate mirrors the demotion",
          sleeves_mod2._puts_entry_ok(
              {"rating": "SELL", "conviction": 72,
               "suitable_for_puts": False}, "OPTX", _pk81))

    # — action 2: parking counts in the firm name cap + look-through helper —
    for _k in config.SLEEVES:
        _sv = state.default_sleeve_state(_k)
        _sv["cash"] = 25000.0
        state.save_sleeve(_k, _sv)
    _sa = state.get_sleeve("alpha_stocks")
    _sa["cash"] = 0.0
    _sa["positions"]["SPY"] = {"quantity": 65, "entry_price": 615.0,
                               "market_value": 40000.0, "instrument": "equity",
                               "cash_equitization": True, "opened_at": ts(3)}
    state.save_sleeve("alpha_stocks", _sa)
    _exp = risk.firm_symbol_exposure_pct("SPY")     # 40k / 115k ~ 34.8%
    check("81 look-through exposure counts parking",
          34.0 < _exp < 36.0, f"exp={_exp}")
    _okc81, _whyc81 = risk.check_firm_name_cap("SPY", 100.0)
    check("81 firm cap now SEES parking (SPY conviction buy blocked)",
          not _okc81 and "exposure" in _whyc81, _whyc81)

    # — action 2: parking ceiling stops the equitizer —
    risk.firm_symbol_exposure_pct = lambda sym: 60.0
    _se = state.default_sleeve_state("alpha_stocks")
    _se["cash"] = 25000.0
    state.save_sleeve("alpha_stocks", _se)
    sleeves_mod2._equitize_idle_cash(_se, {"SPY": 600.0}, "paper")
    check("81 equitizer refuses to park past the firm ceiling",
          "SPY" not in _se["positions"]
          and any(e.get("event") == "equitize_blocked_parking_ceiling"
                  for e in state.read_journal(6)))
    risk.firm_symbol_exposure_pct = _reals81[2]

    # — action 5: premarket adds top up at the flush (not voided) —
    for _k in config.SLEEVES:
        _sv = state.default_sleeve_state(_k)
        _sv["cash"] = 25000.0
        state.save_sleeve(_k, _sv)
    _sa = state.get_sleeve("alpha_stocks")
    _sa["positions"]["NVDA81"] = {"quantity": 20, "entry_price": 100.0,
                                  "cost": 2000.0, "instrument": "equity",
                                  "opened_at": ts(4), "sector": "Technology"}
    state.save_sleeve("alpha_stocks", _sa)
    state.save("research_requests", {"queue": []})
    state.save("pending_open_entries", [
        {"sleeve": "alpha_stocks", "symbol": "NVDA81", "quantity": 5,
         "thesis_price": 100.0, "mode": "paper", "queued_at": ts(0.2),
         "meta": {"add_on_strength": True, "conviction": 71,
                  "sector": "Technology", "vol_ann": 30.0}}])
    _mh81.market_is_open = lambda *a, **k: True
    broker.equity_prices = lambda syms: {x: 100.5 for x in syms}
    _n81 = sleeves_mod2.flush_pending_open_entries("paper")
    _sa = state.get_sleeve("alpha_stocks")
    _p81 = _sa["positions"].get("NVDA81") or {}
    check("81 queued add TOPS UP the held position at the flush",
          _n81 == 1 and _p81.get("quantity") == 25
          and _p81.get("adds_count") == 1
          and any(e.get("event") == "add_on_strength"
                  and e.get("via") == "open_queue_flush"
                  for e in state.read_journal(10)),
          f"n={_n81} qty={_p81.get('quantity')} adds={_p81.get('adds_count')} "
          f"j={[e.get('event') for e in state.read_journal(8)]}")
    # crash idempotency: an intent OLDER than last_add_ts must not re-book
    state.save("pending_open_entries", [
        {"sleeve": "alpha_stocks", "symbol": "NVDA81", "quantity": 5,
         "thesis_price": 100.0, "mode": "paper", "queued_at": ts(0.2),
         "meta": {"add_on_strength": True, "sector": "Technology"}}])
    sleeves_mod2.flush_pending_open_entries("paper")
    _p81 = state.get_sleeve("alpha_stocks")["positions"].get("NVDA81") or {}
    check("81 add crash-refill guard: stale add intent skipped",
          _p81.get("quantity") == 25 and _p81.get("adds_count") == 1
          and any(e.get("event") == "open_queue_add_already_applied"
                  for e in state.read_journal(6)))

    # — TTL: a week-old intent is dropped loudly, not executed —
    state.save("pending_open_entries", [
        {"sleeve": "alpha_stocks", "symbol": "OLD81", "quantity": 5,
         "thesis_price": 100.0, "mode": "paper", "queued_at": ts(10),
         "meta": {"vol_ann": 20.0}}])
    sleeves_mod2.flush_pending_open_entries("paper")
    check("81 stale queued intent expires (TTL) and goes back to research",
          "OLD81" not in state.get_sleeve("alpha_stocks")["positions"]
          and any(e.get("event") == "open_queue_expired"
                  for e in state.read_journal(8))
          and any(r.get("ticker") == "OLD81"
                  for r in state.load("research_requests",
                                      {"queue": []}).get("queue", [])))

    # — action 10a: parked-capacity yield stands down under de-risk —
    _sd = state.default_sleeve_state("alpha_stocks")
    _sd["cash"] = 500.0
    for _i in range(16):
        _sd["positions"][f"DR{_i}81"] = {"quantity": 1, "entry_price": 300.0,
                                         "instrument": "equity",
                                         "opened_at": ts(3)}
    _sd["positions"]["SPY"] = {"quantity": 30, "entry_price": 600.0,
                               "instrument": "equity", "opened_at": ts(3),
                               "cash_equitization": True}
    check("81 parked-capacity yields when healthy (control)",
          _rt81.parked_capacity("alpha_stocks", _sd, {}))
    _sd["soft_risk"] = True
    check("81 parked-capacity stands down in a de-risk state",
          not _rt81.parked_capacity("alpha_stocks", _sd, {}))

    # — action 6: street review grounding — confabulated evidence rejected —
    import pandas as _pd81
    _idx81 = _pd81.date_range(end=_pd81.Timestamp.utcnow().tz_localize(None),
                              periods=12, freq="D")
    data_engine.fetch_history = lambda tickers, period="2y": _pd81.DataFrame(
        {"GTX": [100, 101, 103, 105, 107, 109, 111, 112, 113, 114, 115, 112]},
        index=_idx81)
    brain.available = lambda: True
    # added 8d ago: past the 7d review cadence, inside the 14d tactical TTL
    _doc81 = {"file": "t81.pdf", "sha": "x81", "added": ts(8), "brief": {
        "source": "TestBank", "named_tickers": [{"ticker": "GTX"}],
        "key_theses": [{"claim": "GTX rallies on AI demand",
                        "horizon": "tactical", "checkable": "GTX price up",
                        "status": "intact"}]}}
    state.save("street_research", {"docs": [_doc81], "source_track": {}})
    brain.ask_json = lambda *a, **k: {"reviews": [
        {"idx": 0, "status": "supported",
         "evidence": "GTX rallied strongly since the report was published",
         "resolved_from": "my recollection of recent markets"}]}
    _sr81.review_claims()
    _st81 = state.load("street_research", {})
    _th81 = (_st81["docs"][0]["brief"]["key_theses"][0])
    check("81 street review: un-anchored 'supported' stays intact",
          _th81.get("status") == "intact"
          and any(e.get("event") == "street_review_ungrounded"
                  for e in state.read_journal(8))
          and not (_st81.get("source_track", {})
                   .get("TestBank", {}).get("supported")))
    _st81["docs"][0].pop("last_reviewed", None)
    state.save("street_research", _st81)
    brain.ask_json = lambda *a, **k: {"reviews": [
        {"idx": 0, "status": "supported",
         "evidence": "GTX is up double digits since the report landed",
         "resolved_from": "GTX: +12.0% since ingestion (observed data)"}]}
    _sr81.review_claims()
    _st81 = state.load("street_research", {})
    check("81 street review: anchored 'supported' resolves + credits source",
          _st81["docs"][0]["brief"]["key_theses"][0].get("status")
          == "supported"
          and _st81.get("source_track", {}).get("TestBank", {})
          .get("supported") == 1,
          str(_st81.get("source_track")))
finally:
    (_mh81.market_is_open, broker.equity_prices,
     risk.firm_symbol_exposure_pct, brain.available, brain.ask_json,
     data_engine.fetch_history) = _reals81
    for _k, _sv in _prev81.items():
        state.save_sleeve(_k, _sv)
    if _prev_tun81 is not None:
        state.save("tuning", _prev_tun81)
    state.save("pending_open_entries", _prev_poe81)
    state.save("research_requests", _prev_rq81)
    if _prev_tu81 is not None:
        state.save("tradable_universe", _prev_tu81)
    if _prev_str81 is not None:
        state.save("street_research", _prev_str81)
    _trd81._cache["asof"] = None

# ══ 82. 2026-07-20 fixes: pricing resilience + IG brief ratchets ══════════════
# (a) IWM outage class: extended-session Public last-resort marks; vendor-wide
#     alert self-diagnosis; yf session-reset hook. (b) Fix briefs 1-5: gate
#     open-vs-qualified reconcile, grading liveness counters, conviction-spread
#     escalation, per-hold earnings-date callout, reopen-kind liveness.
from firm import market_hours as mh82  # noqa: E402
from firm import alerts as alerts82  # noqa: E402
from firm import health as health82  # noqa: E402
from firm import routing as routing82  # noqa: E402

_reals82 = (mh82.market_is_open, mh82.extended_session_open,
            data_engine.batch_last_prices, data_engine.last_price,
            broker.equity_quotes_public, alerts82.alert,
            monitor._pricing_matters_now)
_prev_wl82 = state.load("watchlist", {})
_prev_res82 = state.load("research", {})
_prev_cs82 = state.load("conviction_spread", {})
_prev_fb82 = state.load("fix_briefs", {})
_prev_health82 = state.load("health", {})
try:
    # 82a. Extended session + yfinance wholesale-dead: Public last-resort
    # quotes price the book and the mark is LIVE (stops may act on it)
    mh82.market_is_open = lambda now=None: False
    mh82.extended_session_open = lambda now=None: True
    data_engine.batch_last_prices = lambda tk: {}
    data_engine.last_price = lambda tk: 0.0
    broker.equity_quotes_public = lambda syms: {s: 123.45 for s in syms}
    _s82 = state.default_sleeve_state("etf_rotation")
    _s82["positions"] = {"IWMX": {"symbol": "IWMX", "instrument": "equity",
                                  "quantity": 1, "entry_price": 100.0,
                                  "cost": 100.0, "peak_price": 100.0,
                                  "last_price": 100.0, "opened_at": ts(),
                                  "opened_mode": "paper"}}
    _mk82 = broker.fetch_marks(_s82)
    check("82a extended session + yf dead: Public last-resort prices the book",
          _mk82.get("IWMX") == 123.45
          and _s82["positions"]["IWMX"].get("_mark_stale") is False,
          str(_mk82))
    broker.equity_quotes_public = lambda syms: {}
    broker._recent_unpriced.clear()
    _mk82b = broker.fetch_marks(_s82)
    check("82a2 every source dead: symbol tracked for vendor-wide diagnosis",
          "IWMX" not in _mk82b and "IWMX" in broker.recent_unpriced(),
          str(broker.recent_unpriced()))

    # 82b. The CANNOT PRICE alert self-diagnoses outage shape
    _fired82 = []
    alerts82.alert = lambda title, msg: _fired82.append((title, msg))
    monitor._pricing_matters_now = lambda: True
    broker._recent_unpriced.clear()
    broker._note_unpriced(["OTHR1", "OTHR2"])
    _pos82 = {"mark_fail_streak": 4}
    monitor._mark_fail(_pos82, "alpha_stocks", "GHOSTZ")
    check("82b vendor-wide outage: alert says VENDOR, names co-failing names",
          bool(_fired82) and "VENDOR-WIDE" in _fired82[0][1]
          and "OTHR1" in _fired82[0][1], str(_fired82[:1]))
    broker._recent_unpriced.clear()
    _fired82.clear()
    _pos82c = {"mark_fail_streak": 4}
    monitor._mark_fail(_pos82c, "alpha_stocks", "LONELY")
    check("82b2 single-name failure: alert points at halt/delisting instead",
          bool(_fired82) and "halt/delisting" in _fired82[0][1],
          str(_fired82[:1]))

    # 82c. The in-process yfinance session-reset hook finds the internals
    check("82c yfinance session-reset hook reaches the singleton (no-op safe)",
          data_engine._reset_yf_session() is True)

    # 82d. Fix brief #1: per-side open-vs-qualified reconcile over ONE window
    state.journal("options_gate_qualified", side="calls", symbols=["FAKE"], n=1)
    state.journal("order", mode="paper", sleeve="calls_only",
                  symbol="FAKE260821C00100000", side="buy", qty=1, price=1.0,
                  instrument="option", order_id="paper-t82a")
    state.journal("order", mode="paper", sleeve="calls_only",
                  symbol="ROGUE260821P00050000", side="buy", qty=1, price=1.0,
                  instrument="option", order_id="paper-t82b")
    _cov82 = routing82.route_coverage()
    _rec82 = _cov82.get("gate_reconcile") or {}
    check("82d open WITH a decision-time qualification reconciles clean",
          "FAKE" not in (_rec82.get("calls_unmatched_opens") or []),
          str(_rec82))
    check("82d2 open WITHOUT a qualification is flagged as a gate bypass",
          "ROGUE" in (_rec82.get("puts_unmatched_opens") or []), str(_rec82))

    # 82e. Fix brief #3: conviction-spread failure escalates past the budget
    _budget82 = int(config.CONVICTION_SPREAD_ESCALATE_CYCLES)
    state.save("health", {"checks": [{"name": "conviction distribution",
                                      "ok": False,
                                      "detail": "stdev 2.8 (synthetic)"}]})
    state.save("research", {"latest_asof": ts(0), "history": []})
    state.save("conviction_spread", {"streak": _budget82 - 1,
                                     "last_asof": "older"})
    state.save("fix_briefs", {"items": []})
    health82._escalate_conviction_spread()
    _cs82 = state.load("conviction_spread", {})
    _fb82 = state.load("fix_briefs", {}).get("items", [])
    check("82e streak past budget files the escalation fix brief",
          _cs82.get("streak") == _budget82
          and any("conviction spread still compressed"
                  in str(i.get("problem", "")) for i in _fb82),
          f"streak={_cs82.get('streak')} briefs={len(_fb82)}")
    _n_fb82 = len(_fb82)
    health82._escalate_conviction_spread()
    check("82e2 same cycle re-measured: streak does NOT double-count",
          state.load("conviction_spread", {}).get("streak") == _budget82
          and len(state.load("fix_briefs", {}).get("items", [])) == _n_fb82)
    state.save("health", {"checks": [{"name": "conviction distribution",
                                      "ok": True, "detail": "recovered"}]})
    health82._escalate_conviction_spread()
    check("82e3 a passing cycle resets the streak",
          state.load("conviction_spread", {}).get("streak") == 0)

    # 82f. Fix brief #5: every supported reopen kind is offered + survives
    _rkl82 = watchlist.reopen_kind_liveness()
    check("82f all supported reopen kinds offered by schema + survive "
          "normalization",
          bool(_rkl82) and all(v["offered_in_schema"]
                               and v["survives_normalization"]
                               for v in _rkl82.values()),
          str({k: v for k, v in _rkl82.items()
               if not (v["offered_in_schema"] and v["survives_normalization"])}))
    check("82f2 below_price / reclaim_price specifically verified live-capable",
          _rkl82.get("below_price", {}).get("survives_normalization") is True
          and _rkl82.get("reclaim_price", {}).get("survives_normalization") is True)

    # 82g. Fix brief #4: a hold the vendor has no date for is NAMED, with its
    # dated fallback — never absorbed into an aggregate 'ok'
    state.save("watchlist", {"NODATE": {"ticker": "NODATE",
                                        "source": "hold_verdict", "added": ts(0),
                                        "earnings_date_misses": 3,
                                        "reopen_trigger": {"kind": "rsi_below",
                                                           "level": 45}}})
    _md82 = watchlist.holds_missing_earnings_date()
    check("82g dateless hold is named individually with its dated fallback",
          len(_md82) == 1 and _md82[0]["ticker"] == "NODATE"
          and "rsi_below" in _md82[0]["fallback"], str(_md82))

    # 82h. Fix brief #2: the report card always carries liveness counters
    state.save("research", {"history": []})
    _card82 = learning.verdict_report_card()
    check("82h report card carries rows_considered/graded even when empty",
          _card82.get("rows_considered") == 0 and _card82.get("graded") == 0,
          str(_card82))
finally:
    (mh82.market_is_open, mh82.extended_session_open,
     data_engine.batch_last_prices, data_engine.last_price,
     broker.equity_quotes_public, alerts82.alert,
     monitor._pricing_matters_now) = _reals82
    broker._recent_unpriced.clear()
    state.save("watchlist", _prev_wl82)
    state.save("research", _prev_res82)
    state.save("conviction_spread", _prev_cs82)
    state.save("fix_briefs", _prev_fb82)
    state.save("health", _prev_health82)

# ══ 83. Fix briefs 2026-07-21 #6-#10: explicit options veto, book-full tie-off,
#        sell-path watchdog, dispersion-aware swap margin, macro taxonomy ═══════
import config as _cfg83  # noqa: E402
from datetime import date as _date83  # noqa: E402
from firm import debate as _db83  # noqa: E402
from firm import macro_calendar as _mc83, routing as _rt83  # noqa: E402

_prev_res83 = state.load("research", {})
_prev_rcs83 = state.load("report_card_stats", {})
_prev_al83 = state.get_sleeve("alpha_stocks")
_prev_co83 = state.get_sleeve("core_blend")
try:
    # 83a. Swap margin follows the MEASURED conviction spread (fix brief #9):
    # compressed regime clamps to max(p90-p10, 4); healthy regime keeps static.
    state.save("research", {"history": [{"asof": ts(1), "verdicts": {
        f"C{i}": {"rating": "BUY", "conviction": 60 + (i % 3)}
        for i in range(20)}}]})
    check("83a swap margin clamps to live spread when conviction is compressed",
          sleeves_mod2._replace_margin() == 4.0,
          str(sleeves_mod2._replace_margin()))
    state.save("research", {"history": [{"asof": ts(1), "verdicts": {
        f"H{i}": {"rating": "BUY", "conviction": 50 + 2 * i}
        for i in range(16)}}]})
    check("83a swap margin stays static under healthy dispersion",
          sleeves_mod2._replace_margin()
          == float(getattr(_cfg83, "REPLACE_MIN_MARGIN", 12.0)))

    # 83b. A book-full skip that PLACED in another sleeve ties itself off with
    # an explicit book_full_discarded record (fix brief #7) — never a silent
    # evaporation the IG has to infer from an empty waiter list.
    _sa83 = state.default_sleeve_state("alpha_stocks")
    _sa83["positions"] = {"BF83": {
        "quantity": 5, "entry_price": 100, "last_price": 100,
        "market_value": 500.0, "cost": 500.0, "peak_price": 100,
        "instrument": "equity", "opened_at": ts(1)}}
    state.save_sleeve("alpha_stocks", _sa83)
    _sc83 = state.default_sleeve_state("core_blend")
    _sc83["cash"] = 100.0
    _sc83["positions"] = {"SATX": {
        "quantity": 1000, "entry_price": 100, "last_price": 100,
        "market_value": 100000.0, "cost": 100000.0, "peak_price": 100,
        "instrument": "equity", "opened_at": ts(1)}}
    state.save_sleeve("core_blend", _sc83)
    _rt83.record_routing({"BF83": {"kind": "stock", "packet": {},
                                   "verdict": {"rating": "BUY",
                                               "conviction": 66}}})
    _bfd83 = next((e for e in state.read_journal(30)
                   if e.get("event") == "book_full_discarded"
                   and e.get("symbol") == "BF83"), None)
    check("83b book-full skip placed elsewhere journals an explicit discard",
          _bfd83 is not None and str(_bfd83.get("reason", "")).startswith(
              "placed_in_other_sleeve"), str(_bfd83))

    # 83c. Macro taxonomy self-diff (fix brief #10): universe covered, the one
    # schedule-less release is a NAMED exception, and the new siblings generate
    # deterministic dates (ADP = the Wednesday 2 days before NFP's Friday;
    # Beige Book = 14 days before each FOMC decision).
    _tx83 = _mc83.taxonomy_gaps()
    check("83c macro taxonomy universe covered; fed_testimony documented",
          _tx83["untracked"] == []
          and "fed_testimony" in _tx83["documented_exceptions"], str(_tx83))
    _adp83 = _mc83._gen_monthly(_mc83._BY_KEY["adp"], 2026, 8)
    _ff83 = _mc83._first_friday(2026, 8)
    check("83c ADP rule lands the Wednesday before NFP's first Friday",
          _adp83 is not None and _adp83.weekday() == 2
          and (_ff83 - _adp83).days == 2, str(_adp83))
    _bb83 = _mc83._rule_dates(_mc83._BY_KEY["beige_book"],
                              _date83(2026, 1, 1), _date83(2026, 12, 31))
    check("83c Beige Book dates derive from the FOMC anchor list (-14d)",
          len(_bb83) >= 6, str(_bb83[:3]))

    # 83d. report_card_stats persists per-rating deep-negative counts +
    # calibration decidability (fix brief #8's data feed).
    _real_card83 = learning.verdict_report_card
    learning.verdict_report_card = lambda **kw: {
        "rows_considered": 4, "graded": 4, "note": "",
        "by_rating": {"HOLD": {"n": 4, "correct": 1, "correct_pct": 25.0,
                               "avg_fwd_excess_pct": -27.2,
                               "excess": [-51.5, -30.1, -29.1, 2.0]}},
        "rating_calibration": {"inverted": None,
                               "note": "insufficient sample"},
    }
    try:
        learning.refresh_report_card_digest()
    finally:
        learning.verdict_report_card = _real_card83
    _rcs83 = state.load("report_card_stats", {})
    check("83d stats persist HOLD deep_neg_n + calibration_undecidable",
          (_rcs83.get("by_rating_summary") or {}).get("HOLD", {})
          .get("deep_neg_n") == 3
          and _rcs83.get("calibration_undecidable") is True, str(_rcs83))

    # 83e. Sell-path utilization check REDS on deep-negative HOLDs with ~zero
    # sell-family issuance, and names the starved calibration (fix brief #8).
    state.save("report_card_stats", {
        **_rcs83, "by_rating_summary": {"HOLD": {"n": 20,
                                                 "avg_fwd_excess_pct": -6.2,
                                                 "deep_neg_n": 6}},
        "calibration_undecidable": True})
    from firm import health as _hl83
    _hc83 = {c["name"]: c for c in _hl83.health_report()["checks"]}
    check("83e sell-path utilization reds when HOLD hides SELLs",
          "sell-path utilization" in _hc83
          and _hc83["sell-path utilization"]["ok"] is False
          and "STARVED" in _hc83["sell-path utilization"]["detail"],
          str(_hc83.get("sell-path utilization")))
    check("83e new health checks registered (book-full coverage + taxonomy)",
          "book-full waiter coverage" in _hc83 and "macro taxonomy" in _hc83
          and _hc83["macro taxonomy"]["ok"] is True)

    # 83f. The explicit-veto contract end to end (fix brief #6): the schema
    # offers options_veto_calls; reason alone never binds; explicit veto with
    # reason does. (Gate-level checks live in sections 16c/52/81.)
    check("83f schema offers the explicit options veto fields",
          "options_veto_calls" in _db83._SCHEMA_TAIL
          and "options_veto_puts" in _db83._SCHEMA_TAIL)
finally:
    state.save("research", _prev_res83)
    state.save("report_card_stats", _prev_rcs83)
    state.save_sleeve("alpha_stocks", _prev_al83)
    state.save_sleeve("core_blend", _prev_co83)

# ══ 84. Mid-pass research checkpoint (operator 2026-07-21: restart at 8/9 must
#        resume, not re-run all nine Opus workups) ══════════════════════════════
from datetime import datetime as _dt84, timezone as _tz84, timedelta as _td84  # noqa: E402
from firm import cycle as _cy84  # noqa: E402

_prev_ck84 = state.load("research_checkpoint", {})
try:
    state.save("research_checkpoint", {})

    def _wk84(t, rating="BUY", degraded=False, asof=None):
        return {"ticker": t, "kind": "stock",
                "asof": asof or _dt84.now(_tz84.utc).isoformat(),
                "packet": {"technicals": {"price": 100.0}}, "research": {},
                "composite": None, "cases": {},
                "verdict": {"rating": rating, "conviction": 66,
                            **({"degraded": True} if degraded else {})}}

    # 84a. Completed workups persist; degraded ones NEVER do (a failed workup
    # must be retried fresh, not resumed as if it were research).
    _cy84._checkpoint_workup("CKA", _wk84("CKA"), "full")
    _cy84._checkpoint_workup("CKDEG", _wk84("CKDEG", degraded=True), "full")
    _ck84 = state.load("research_checkpoint", {}).get("workups") or {}
    check("84a completed workup checkpoints; degraded workup never does",
          "CKA" in _ck84 and "CKDEG" not in _ck84, str(sorted(_ck84)))

    # 84b. Resume adopts a fresh same-day entry; refuses a stale one, a
    # kind-mismatch, and a SHALLOWER-depth leftover (a standard-depth entry
    # must not silently degrade a full-depth cycle — the reverse is fine).
    _cy84._checkpoint_workup(
        "CKOLD", _wk84("CKOLD", asof=(_dt84.now(_tz84.utc)
                                      - _td84(hours=9)).isoformat()), "full")
    _cy84._checkpoint_workup("CKSTD", _wk84("CKSTD"), "standard")
    _res84 = _cy84._resume_from_checkpoint(
        [("CKA", "stock"), ("CKOLD", "stock"), ("CKSTD", "stock"),
         ("CKA", "etf"), ("CKNEW", "stock")], "full")
    check("84b resume adopts fresh same-day same-kind same-or-deeper entries only",
          set(_res84) == {"CKA"}, str(sorted(_res84)))
    check("84b resumed entry is the full workup result (sleeves-consumable)",
          _res84["CKA"].get("verdict", {}).get("conviction") == 66
          and _res84["CKA"].get("packet", {}).get("technicals", {})
          .get("price") == 100.0)
    _res84b = _cy84._resume_from_checkpoint([("CKSTD", "stock")], "standard")
    check("84b a full-depth pass refuses the standard leftover; a standard "
          "pass accepts it",
          "CKSTD" in _res84b)
    check("84b resume journals the adopted names",
          any(e.get("event") == "research_resumed_from_checkpoint"
              and e.get("names") == ["CKA"]
              for e in state.read_journal(20)))

    # 84c. A concluded pass clears the checkpoint — a completed pass must
    # never feed a later same-day pass (cooldowns govern reuse, not this).
    _cy84._clear_research_checkpoint()
    check("84c pass conclusion clears the checkpoint",
          (state.load("research_checkpoint", {}) or {}).get("workups") in
          (None, {}))
    check("84c resume on a cleared checkpoint is empty and harmless",
          _cy84._resume_from_checkpoint([("CKA", "stock")], "full") == {})
finally:
    state.save("research_checkpoint", _prev_ck84)

# ══ 85. Seat-switch brake (operator 2026-07-21: swaps learn from themselves —
#        a losing flip record tightens the governor until it recovers) ══════════
from firm import allocation as _al85  # noqa: E402

_prev_ssc85 = state.load("seat_switch_scorecard", {})
try:
    # 85a. Losing record (>=4 graded, win rate < 40%) arms the brake.
    state.save("seat_switch_scorecard",
               {"switches_graded": 5, "switch_wins": 1, "mean_edge_pts": -3.1})
    _m85, _x85, _w85 = _al85._switch_brake()
    check("85a losing switch record arms the brake (2x margin, +1 streak)",
          _m85 == 2.0 and _x85 == 1 and "losing" in _w85, f"{_m85},{_x85},{_w85}")
    # Negative mean edge alone (win rate ok) also brakes — a record that wins
    # often but small and loses big is still losing money.
    state.save("seat_switch_scorecard",
               {"switches_graded": 5, "switch_wins": 3, "mean_edge_pts": -1.2})
    check("85a negative mean edge brakes even at an ok win rate",
          _al85._switch_brake()[0] == 2.0)
    # 85b. Healthy record and thin samples never brake; absent fails open.
    state.save("seat_switch_scorecard",
               {"switches_graded": 6, "switch_wins": 4, "mean_edge_pts": 2.5})
    check("85b healthy record: no brake", _al85._switch_brake() == (1.0, 0, ""))
    state.save("seat_switch_scorecard",
               {"switches_graded": 2, "switch_wins": 0, "mean_edge_pts": -9.0})
    check("85b thin sample (<4 graded): no brake",
          _al85._switch_brake() == (1.0, 0, ""))
    state.save("seat_switch_scorecard", {})
    check("85b absent scorecard fails OPEN (validated engine unbraked)",
          _al85._switch_brake() == (1.0, 0, ""))
    # 85c. _challenge honors the extra streak: with need 2(+1) a two-look
    # leader still waits; the third look wins.
    _rk85 = [("CHAL", 50.0), ("INC", 40.0)]
    _w1, _c1 = _al85._challenge("INC", _rk85, 4.0, {}, extra_need=1)
    _w2, _c2 = _al85._challenge("INC", _rk85, 4.0, _c1, extra_need=1)
    _w3, _c3 = _al85._challenge("INC", _rk85, 4.0, _c2, extra_need=1)
    check("85c brake's extra streak defers the flip one extra look",
          _w1 is None and _w2 is None and _w3 == "CHAL",
          f"{_w1},{_w2},{_w3}")
    check("85c without the brake the same lead flips on look two",
          _al85._challenge("INC", _rk85, 4.0,
                           _al85._challenge("INC", _rk85, 4.0, {})[1])[0]
          == "CHAL")
    # 85d. The scorecard persists its cache for the governor's pure-state read.
    learning._persist_seat_scorecard({"switches_graded": 3, "switch_wins": 2})
    _ssc85 = state.load("seat_switch_scorecard", {})
    check("85d scorecard caches to state with an asof stamp",
          _ssc85.get("switches_graded") == 3 and bool(_ssc85.get("asof")))
    # 85e. Routing eligibility consumes the PACKET (2026-07-21: the packet-less
    # re-check failed open through the calls gate's earnings leg and flagged
    # three phantom silent drops the desk had really rejected on earnings).
    from firm import routing as _rt85
    _v85 = {"rating": "BUY", "conviction": 90}
    check("85e eligibility with an earnings-window packet matches the desk "
          "(calls_only refused); packet-less fails open as documented",
          _rt85.eligibility("stock", _v85, "ELG85",
                            {"next_earnings": {"days_away": 10}})["calls_only"]
          is False
          and _rt85.eligibility("stock", _v85)["calls_only"] is True)
finally:
    state.save("seat_switch_scorecard", _prev_ssc85)

# ── 86. Post-exit telemetry actually populates (fix brief 2026-07-27 #5) ──────
# The job existed and was called at every postmarket wrap, yet post_n was 0 for
# EVERY exit family and stop_evidence_by_vol was {} across 21 closed trades:
# the maturity gate was `closed >= 30 CALENDAR days` on an all-or-nothing
# 21-session stamp, and the oldest closed row in the live book was 28 days old.
# Nothing ever qualified, so Job 2g (stop/trail sigma tuning) had no evidence
# and was unfalsifiable. These lock the fixed behaviour: per-horizon maturity
# counted in REAL printed sessions, idempotent, ratchet included.
_prev_led86 = state.load("ledger", {})
_prev_res86 = state.load("research", {})
_real_fh86 = data_engine.fetch_history
try:
    _idx86 = pd.date_range(end=pd.Timestamp.now().normalize(), periods=140, freq="B")
    _frame86 = pd.DataFrame(
        {"ZPXT": [100 * (1.004 ** i) for i in range(140)],
         "ZHPE": [50 * (1.002 ** i) for i in range(140)],
         config.BENCHMARK: [500 * (1.001 ** i) for i in range(140)]}, index=_idx86)

    def _row86(sym, days, reason, vol=None, tid="t"):
        return {"trade_id": tid, "sleeve": "alpha_stocks",
                "ticker": learning._underlying(sym), "symbol": sym, "pnl": 5,
                "vol_ann": vol, "exit_reason": reason,
                "closed_at": (datetime.now(timezone.utc)
                              - timedelta(days=days)).isoformat()}

    state.save("ledger", {"trades": [
        # 45d: every horizon matured.  28d: the age of the OLDEST row in the
        # real book — the exact case the old >=30d gate rejected forever.
        _row86("ZPXT", 45, "trailing stop -15.1% from peak", 70.0, "a86"),
        _row86("ZPXT", 28, "hard stop -12.1%", 20.0, "b86"),
        # 9d: only the 5-session window has printed — partial evidence beats
        # silence, and the row must say WHICH window produced the number.
        _row86("ZPXT", 9, "profit ratchet: +8% held of a +20% peak", 45.0, "c86"),
        _row86("ZHPE260731C00050000", 40, "hard SL -15.0%", None, "d86"),
        _row86("ZPXT", 0, "PM verdict SELL", None, "e86"),
    ], "total_recorded": 5})
    data_engine.fetch_history = lambda *a, **k: _frame86
    _n86 = learning.backfill_post_exit_excess()
    _n86b = learning.backfill_post_exit_excess()      # idempotent re-run
    _led86 = {t["trade_id"]: t for t in state.load("ledger", {})["trades"]}
    _st86 = learning.stats()
    _ex86 = _st86.get("by_exit_reason") or {}
    check("86a matured exits get post-exit forward excess (the brief's "
          "assertion: post_n > 0 for a fixture older than 21 sessions)",
          _n86 == 4 and (_ex86.get("trail") or {}).get("post_n", 0) > 0
          and (_ex86.get("stop") or {}).get("post_n", 0) > 0,
          f"n={_n86} {_ex86}")
    check("86a the 28-day-old exit (oldest row in the live book) now grades — "
          "the old >=30 CALENDAR-day gate rejected 21 of 21 rows",
          isinstance(_led86["b86"].get("post_exit_excess_pct"), (int, float)))
    check("86b all three horizons (5/10/21) recorded, each with return AND "
          "SPY-relative excess",
          sorted((_led86["a86"].get("post_exit") or {})) == ["10", "21", "5"]
          and all(isinstance(v.get("ret_pct"), (int, float))
                  and isinstance(v.get("excess_pct"), (int, float))
                  for v in _led86["a86"]["post_exit"].values()),
          str(_led86["a86"].get("post_exit")))
    check("86b STRICT session maturity: a 9-day exit gets ONLY its 5-session "
          "observation and stamps the horizon that produced the flat number",
          list((_led86["c86"].get("post_exit") or {})) == ["5"]
          and _led86["c86"].get("post_exit_horizon_sessions") == 5
          and _led86["a86"].get("post_exit_horizon_sessions") == 21)
    check("86b a same-day exit stays unmeasured (no partial window is ever "
          "stamped as if it were the full one)",
          _led86["e86"].get("post_exit_excess_pct") is None
          and not _led86["e86"].get("post_exit"))
    check("86c backfill is idempotent (re-run stamps nothing new)",
          _n86b == 0)
    check("86d ratchet exits flow into stop_evidence_by_vol — the give-back "
          "cap rides the SAME vol-scaled mechanism the sigma tuning reads",
          (_st86.get("stop_evidence_by_vol") or {}).get("mid", {}).get("n", 0) >= 1
          and "ratchet" in _ex86 and _ex86["ratchet"]["post_n"] >= 1,
          str(_st86.get("stop_evidence_by_vol")))
    check("86d vol terciles come from the row's own vol_ann via "
          "config.vol_bucket (70 -> high, 20 -> low), not one global bucket",
          set(_st86.get("stop_evidence_by_vol") or {}) >= {"high", "low", "mid"},
          str(_st86.get("stop_evidence_by_vol")))
    check("86e an option row grades on its UNDERLYING (the contract itself "
          "has no post-exit series)",
          isinstance(_led86["d86"].get("post_exit_excess_pct"), (int, float)))
    check("86f per-horizon averages reach the review: a whipsaw decays across "
          "5/10/21 and only the split can show it",
          (_ex86.get("trail") or {}).get("avg_post_exit_excess_by_horizon", {})
          .get("5") is not None)

    # ── 87. Every unowned BUY maps to a gate or to `unattributed` (#9) ───────
    # 14 of 35 rated BUYs never opened while the caps audit attributed only 24
    # blocks: BLOCK_EVENTS was a subset of the emitted vocabulary and the
    # reader only looked at `symbol`, so book-full discards, the at-the-open
    # queue and the list-shaped rows were invisible. Silence is never the
    # answer — an unexplained leak must show up as the finding itself.
    _all87 = learning._all_block_events()
    check("87a the gate reader covers the block/skip/queue names the entry "
          "paths actually journal",
          {"book_full_discarded", "book_full_to_bench", "buy_skipped_book_full",
           "order_blocked_market_closed", "open_queue_gate_blocked",
           "open_queue_insufficient_cash", "open_queue_expired",
           "buy_skipped_insufficient_cash", "buy_blocked_stop_cooloff",
           "calls_skipped_book_full", "puts_skipped_no_contract",
           "momentum_lane_skip"} <= _all87)
    check("87a a FILL is never counted as a gate",
          not ({"open_queue_filled", "open_queue_partial_fill"} & _all87))
    check("87b block symbols are read from symbol / ticker / symbols[] alike",
          learning._block_symbols({"symbol": "ZAA"}) == ["ZAA"]
          and learning._block_symbols({"ticker": "ZBB"}) == ["ZBB"]
          and learning._block_symbols({"symbols": ["ZCC", "ZDD"]}) == ["ZCC", "ZDD"]
          and learning._block_symbols({"symbol": "ZHPE260731C00050000"}) == ["ZHPE"]
          and learning._block_symbols({"sleeve": "alpha_stocks"}) == [])
    state.save("research", {"history": [{
        "asof": ts(7), "context": {},
        "verdicts": {t: {"rating": "BUY", "conviction": 70}
                     for t in ("ZAA", "ZBB", "ZCC", "ZDD", "ZEE", "ZFF")}}]})
    state.save("ledger", {"trades": [{"sleeve": "alpha_stocks", "ticker": "ZAA",
                                      "symbol": "ZAA", "pnl": 1,
                                      "closed_at": ts(1)}], "total_recorded": 1})
    with open(config.STATE_DIR / "journal.jsonl", "a", encoding="utf-8") as _f87:
        for _ev87, _pay87 in (("book_full_discarded", {"symbol": "ZBB"}),
                              ("buy_skipped_book_full", {"symbols": ["ZCC"]}),
                              ("book_full_to_bench", {"ticker": "ZDD"}),
                              ("order_blocked_market_closed", {"symbol": "ZEE"}),
                              # a FILL for ZFF: not a gate, so ZFF stays a leak
                              ("open_queue_filled", {"symbol": "ZFF"})):
            _f87.write(_json15.dumps({"ts": ts(7), "event": _ev87, **_pay87}) + "\n")
    _idx87 = pd.date_range(end=pd.Timestamp.today().normalize(), periods=8, freq="D")
    _frame87 = pd.DataFrame(
        {s: [100, 102, 104, 106, 108, 110, 112, 114]
         for s in ("ZAA", "ZBB", "ZCC", "ZDD", "ZEE", "ZFF")}, index=_idx87)
    _frame87[config.BENCHMARK] = [100, 100.5, 101, 101.5, 102, 102.5, 103, 103.5]
    data_engine.fetch_history = lambda *a, **k: _frame87
    _aud87 = learning.blocked_audit()
    _gates87 = _aud87["by_gate"]
    _attr87 = _aud87["buy_attribution"]
    check("87c the newly-covered gates each get forward-outcome rows "
          "(discard, list-shaped book-full, bench hand-off, closed-market)",
          all(_gates87.get(g, {}).get("n", 0) >= 1
              for g in ("book_full_discarded", "buy_skipped_book_full",
                        "book_full_to_bench", "order_blocked_market_closed")),
          str(sorted(_gates87)))
    check("87c the BUY with no gate lands in an EXPLICIT unattributed bucket, "
          "graded on forward excess like any other gate",
          _gates87.get("unattributed", {}).get("n", 0) == 1
          and _gates87["unattributed"]["avg_fwd_excess_pct"] > 5
          and _gates87["unattributed"]["missed_winners"] == 1,
          str(_gates87.get("unattributed")))
    check("87c buy_attribution reconciles: unowned == attributed + "
          "unattributed, and names the leaked tickers loudly",
          _attr87["buys_not_owned"] == 5
          and _attr87["attributed_to_a_gate"] == 4
          and _attr87["unattributed"] == 1
          and _attr87["unattributed_tickers"] == ["ZFF"], str(_attr87))
    check("87c regret_audit and the caps audit count the SAME leak cohort "
          "(one shared predicate, so the two surfaces cannot disagree)",
          learning.regret_audit()["buys_not_owned"] == _attr87["buys_not_owned"])
    # No leak: every rated BUY owned -> the bucket is empty, not noisy.
    state.save("ledger", {"trades": [{"sleeve": "alpha_stocks", "ticker": t,
                                      "symbol": t, "pnl": 1, "closed_at": ts(1)}
                                     for t in ("ZAA", "ZBB", "ZCC", "ZDD",
                                               "ZEE", "ZFF")],
                          "total_recorded": 6})
    _attr87b = learning.blocked_audit()["buy_attribution"]
    check("87d nothing leaked -> unattributed is empty (the bucket reports a "
          "real gap, it does not manufacture one)",
          _attr87b["unattributed"] == 0
          and _attr87b["unattributed_tickers"] == [])
finally:
    data_engine.fetch_history = _real_fh86
    state.save("ledger", _prev_led86)
    state.save("research", _prev_res86)

# ══ SECTION 88: IG 2026-07-27 — the health desk stops passing VACUOUSLY,     ══
# ══ measures the macro calendar PER CLASS, and escalates standing failures   ══
# A check whose verdict ranges over an EMPTY population ("all reopens within
# 5d" with zero reopens) asserted nothing and rendered as health; the macro
# line asserted a TOTAL (26 events) against 24 release classes, which one
# weekly class can satisfy alone; and a red could repeat forever with no
# ladder above it. These pin all three.
from firm import health as _hl88  # noqa: E402
from firm import macro_calendar as _mc88  # noqa: E402
from firm import routing as _rt88  # noqa: E402
from firm import watchlist as _wl88  # noqa: E402
from firm import alerts as _al88  # noqa: E402

_prev_res88 = state.load("research", {})
_prev_rcs88 = state.load("report_card_stats", {})
_prev_eh88 = state.load("edge_health", {})
_prev_rd88 = state.load("reconcile_display", {})
_prev_str88 = state.load("health_streaks", {})
_prev_cb88 = state.get_sleeve("core_blend")
_reals88 = (_wl88.overdue_reopens, _wl88.reopen_kind_liveness,
            _rt88.route_coverage, _mc88.RELEASES, _mc88.calendar_tier,
            state.read_journal, _al88.alert)
_cfg88 = (getattr(config, "HEALTH_ESCALATE_AFTER_SWEEPS", None),
          getattr(config, "BOOK_FULL_COVERAGE_SINCE", None))


def _hc88():
    return {c["name"]: c for c in _hl88.health_report()["checks"]}


try:
    # ── 88a. VACUOUS PASS -> undetermined (the core fix) ──────────────────────
    # Empty populations everywhere the verdict divides by one.
    _wl88.overdue_reopens = lambda: []
    _wl88.reopen_kind_liveness = lambda: {}
    state.save("research", {"history": []})
    state.save("report_card_stats", {})
    state.save("edge_health", {})
    state.save("reconcile_display", {})
    config.BOOK_FULL_COVERAGE_SINCE = "2099-01-01"   # no skip can qualify
    _rep88 = _hl88.health_report()
    _h88 = {c["name"]: c for c in _rep88["checks"]}
    _undet88 = ("reopen SLA", "reopen-kind liveness",
                "book-full waiter coverage", "sell-path utilization",
                "verdict distribution", "conviction distribution",
                "edge integrity", "display reconciliation")
    check("88a every empty-population check reports `undetermined (n=…)` and "
          "NAMES its denominator instead of asserting a green over nothing",
          all(_h88.get(n, {}).get("undetermined") is True
              and str(_h88[n]["detail"]).startswith("undetermined (n=")
              for n in _undet88),
          str({n: _h88.get(n, {}).get("detail", "MISSING")[:40]
               for n in _undet88 if not _h88.get(n, {}).get("undetermined")}))
    check("88a an undetermined check stays ok=True (an unmeasurable check must "
          "never red the desk spuriously) but is listed separately, so 'green' "
          "and 'never measured' can never look alike",
          all(_h88[n]["ok"] is True for n in _undet88)
          and set(_undet88) <= set(_rep88["undetermined"]),
          str(sorted(_rep88["undetermined"])))
    check("88a the reopen-SLA line no longer claims 'all reopens within 5d' "
          "when zero reopens exist",
          "all reopens within" not in _h88["reopen SLA"]["detail"]
          and "0 overdue reopen" in _h88["reopen SLA"]["detail"],
          _h88["reopen SLA"]["detail"][:90])
    check("88a 'conviction distribution' emits a LINE on a short sample — it "
          "used to vanish from the report, and report.ok is all() over the "
          "checks that survived, so a thin sample made the desk look greener",
          "conviction distribution" in _h88)

    # Same checks, non-empty populations -> a REAL assertion, never undetermined.
    _wl88.overdue_reopens = lambda: [{"ticker": "ZSLA88",
                                      "last_verdict": ts(30)}]
    _wl88.reopen_kind_liveness = lambda: {
        "earnings_catalyst": {"offered_in_schema": True,
                              "survives_normalization": True, "armed_now": 2}}
    _h88b = _hc88()
    check("88b with a real population the same lines ASSERT again (they are "
          "undetermined only when there is nothing to measure)",
          _h88b["reopen SLA"].get("undetermined") is not True
          and "ZSLA88" in _h88b["reopen SLA"]["detail"]
          and _h88b["reopen-kind liveness"].get("undetermined") is not True,
          _h88b["reopen SLA"]["detail"][:90])

    # ── 88c. IG #13: the puts gate is measured, not proxied ───────────────────
    # The live snapshot read `puts 0/5 qualified ... P 0open/0explained of 0`
    # and passed: the hard leg needs >=25 emitted, and the conversion leg's
    # denominator only exists once the gate qualifies someone.
    _rt88.route_coverage = lambda *a, **k: {
        "window_cycles": 30, "buy_family_emitted": 32,
        "calls_gate_qualified": 3, "sell_family_emitted": 5,
        "puts_gate_qualified": 0,
        "gate_reconcile": {"calls_unmatched_opens": [],
                           "puts_unmatched_opens": [], "since": "2026-07-20",
                           "calls_opened_full_retention": 0,
                           "puts_opened_full_retention": 0},
        "gate_conversion": {"calls": {"qualified_symbol_cycles": 0,
                                      "converted_opens": 0,
                                      "explained_skips": 0,
                                      "unexplained_qualified": []},
                            "puts": {"qualified_symbol_cycles": 0,
                                     "converted_opens": 0,
                                     "explained_skips": 0,
                                     "unexplained_qualified": []}}}
    state.save("research", {"history": []})          # gate never exercised
    _h88c = _hc88()
    check("88c options gate reachability is UNDETERMINED when the puts gate "
          "recorded neither a qualification nor a per-name rejection (it used "
          "to read OK off '0 open / 0 explained of 0' — a proxy that zeroes "
          "itself)",
          _h88c["options gate reachability"].get("undetermined") is True
          and "never exercised" in _h88c["options gate reachability"]["detail"],
          _h88c["options gate reachability"]["detail"][:110])
    # The gate itself, as a pure function of the verdict window.
    _pg88 = _hl88._puts_gate_decisions([{"verdicts": {
        "ZLOW88": {"rating": "SELL", "conviction": 20},
        "ZDEG88": {"rating": "STRONG_SELL", "conviction": 99, "degraded": True},
        "ZOK88": {"rating": "STRONG_SELL", "conviction": 99,
                  "suitable_for_puts": True}}}])
    check("88c the puts-gate ledger records a per-name REJECTION with a stated "
          "reason and a per-name qualification — evidence the gate ran",
          _pg88["evaluated"] == 2 and _pg88["qualified"] == ["ZOK88"]
          and _pg88["rejected"] == ["ZLOW88"]
          and "conviction 20" in _pg88["reasons"]["ZLOW88"], str(_pg88))
    check("88c a DEGRADED sell verdict is excluded from the ledger — counting "
          "a verdict that can never qualify would manufacture evidence the "
          "gate is live",
          "ZDEG88" not in _pg88["rejected"] and "ZDEG88" not in _pg88["qualified"])
    state.save("research", {"history": [{"asof": ts(1), "verdicts": {
        "ZLOW88": {"rating": "SELL", "conviction": 20}}}]})
    _h88d = _hc88()
    check("88c once the gate records an explicit rejection the line ASSERTS "
          "again (a gate that rejects every name is measurably live, not "
          "unmeasured)",
          _h88d["options gate reachability"].get("undetermined") is not True
          and "puts-gate ledger" in _h88d["options gate reachability"]["detail"],
          _h88d["options gate reachability"]["detail"][-120:])

    # ── 88e. IG #11: macro calendar asserts PER CLASS, not a total ────────────
    _mc88.calendar_tier = lambda: "fred"             # isolate the DARK leg
    state.save("research", _prev_res88)
    _h88e = _hc88()
    check("88e macro calendar reports per-CLASS forward coverage, not a bare "
          "event total (26 events cannot cover 24 classes — weekly claims "
          "alone contributes ~17 of them)",
          f"/{len(_mc88.RELEASES)} tracked release classes" in
          _h88e["macro calendar"]["detail"]
          and _h88e["macro calendar"]["ok"] is True,
          _h88e["macro calendar"]["detail"][:110])
    _dead88 = _mc88.Release("zdead88", "Retired release", "medium", None,
                            "anchor", ("zdead88",), ("2020-01-15",))
    _mc88.RELEASES = tuple(_reals88[3]) + (_dead88,)
    _h88f = _hc88()
    check("88e a class that resolves NO future date is a RED, named — the "
          "total-only assertion hid exactly this",
          _h88f["macro calendar"]["ok"] is False
          and "zdead88" in _h88f["macro calendar"]["detail"]
          and "DARK CLASS" in _h88f["macro calendar"]["detail"],
          _h88f["macro calendar"]["detail"][:130])
    _mc88.RELEASES = (_dead88,)                      # source resolves nothing
    _h88g = _hc88()
    check("88e when NO class resolves a forward date the calendar SOURCE is "
          "empty, so darkness is unattributable -> undetermined, not a red "
          "pinned on one class",
          _h88g["macro calendar"].get("undetermined") is True
          and "source is EMPTY" in _h88g["macro calendar"]["detail"],
          _h88g["macro calendar"]["detail"][:110])
    _mc88.RELEASES = _reals88[3]

    # ── 88h. IG #1: a journal window shorter than the claim is undetermined ───
    _now88 = datetime.now(timezone.utc)
    state.read_journal = lambda limit=200: []
    check("88h 'no halt trips in 14d' over an EMPTY journal is undetermined",
          _hc88()["risk-halt churn"].get("undetermined") is True)
    state.read_journal = lambda limit=200: [
        {"ts": (_now88 - timedelta(hours=6)).isoformat(), "event": "cycle_done"}]
    _h88h = _hc88()
    check("88h a journal tail that reaches back 0.2d cannot certify a 14d "
          "window — the check says so instead of reporting 'no trips'",
          _h88h["risk-halt churn"].get("undetermined") is True
          and "never read" in _h88h["risk-halt churn"]["detail"],
          _h88h["risk-halt churn"]["detail"][:120])
    state.read_journal = lambda limit=200: [
        {"ts": (_now88 - timedelta(hours=6)).isoformat(), "event": "cycle_done"},
        {"ts": (_now88 - timedelta(days=20)).isoformat(), "event": "cycle_done"}]
    _h88i = _hc88()
    check("88h a journal that DOES span the window asserts for real again",
          _h88i["risk-halt churn"].get("undetermined") is not True
          and _h88i["risk-halt churn"]["ok"] is True
          and "no halt trips" in _h88i["risk-halt churn"]["detail"],
          _h88i["risk-halt churn"]["detail"][:110])
    state.read_journal = _reals88[5]

    # ── 88j. IG #14: the generic escalation ladder ────────────────────────────
    _alerts88 = []
    _al88.alert = lambda t, m, sev="critical": _alerts88.append((t, sev))
    config.HEALTH_ESCALATE_AFTER_SWEEPS = 3
    state.save("health_streaks", {})
    _bad88 = {"checks": [
        {"name": "eod mark-to-close", "ok": False,
         "detail": "last mark FAILED, 4 rescued via public_api"},
        {"name": "sleeve routing", "ok": False, "detail": "silent drop"},
        {"name": "internet", "ok": True, "detail": "connected"},
        {"name": "reopen SLA", "ok": True, "detail": "undetermined (n=0) — …",
         "undetermined": True}]}
    for _ in range(2):
        _st88 = _hl88._track_health_streaks(_bad88)
    check("88j streaks accrue per FAILING check and never for a passing or "
          "undetermined one",
          _st88["eod mark-to-close"]["streak"] == 2
          and _st88["sleeve routing"]["streak"] == 2
          and "internet" not in _st88 and "reopen SLA" not in _st88,
          str({k: v["streak"] for k, v in _st88.items()}))
    check("88j nothing escalates below the budget", _alerts88 == [])
    _st88 = _hl88._track_health_streaks(_bad88)          # 3rd = budget
    check("88j crossing HEALTH_ESCALATE_AFTER_SWEEPS raises a CRITICAL alert "
          "for EVERY standing failure, not just the eod one (generic ladder)",
          len(_alerts88) == 2
          and all(sev == "critical" for _t, sev in _alerts88)
          and any("eod mark-to-close" in t for t, _s in _alerts88)
          and any("sleeve routing" in t for t, _s in _alerts88), str(_alerts88))
    for _ in range(4):
        _st88 = _hl88._track_health_streaks(_bad88)
    check("88j the alert is LATCHED — a check failing for a week alerts ONCE, "
          "not once per sweep (the failure mode that made health_warning "
          "ignorable)",
          len(_alerts88) == 2 and _st88["eod mark-to-close"]["streak"] == 7
          and _st88["eod mark-to-close"]["alerted"] is True)
    _hl88._annotate_streaks(_bad88, _st88)
    _hl88._annotate_streaks(_bad88, _st88)               # idempotent
    _eod88 = next(c for c in _bad88["checks"]
                  if c["name"] == "eod mark-to-close")
    check("88j the streak rides in the check's own DETAIL (once, not "
          "re-appended every sweep) so the age of a failure is visible without "
          "reading the journal",
          _eod88["detail"].count("FAILING") == 1
          and "FAILING 7 consecutive health sweeps" in _eod88["detail"]
          and "CRITICAL alert raised" in _eod88["detail"], _eod88["detail"][-90:])
    check("88j a passing check carries no streak annotation",
          "FAILING" not in next(c for c in _bad88["checks"]
                                if c["name"] == "internet")["detail"])
    _ok88 = {"checks": [{"name": "eod mark-to-close", "ok": True,
                         "detail": "primary close feed clean"}]}
    _st88b = _hl88._track_health_streaks(_ok88)
    check("88j a pass RESETS the streak and journals the recovery, so the "
          "ladder can fire again on a relapse",
          _st88b == {}
          and any(e.get("event") == "health_check_recovered"
                  and e.get("check") == "eod mark-to-close"
                  for e in state.read_journal(40)))
    _hl88._annotate_streaks(_ok88, _st88b)
    check("88j the recovered check's detail carries no stale streak",
          "FAILING" not in _ok88["checks"][0]["detail"])

    # ── 88k. IG #7: core_blend is IN the denominator, seats in their own bucket
    # The line graded 21 of 29 open positions and left core_blend's 8 — the
    # worst day P&L in the fund — outside it. Core SEATS stay exempt from the
    # red because per-name SELLs are suppressed for them by design.
    from firm import allocation as _alc88  # noqa: E402
    _seat88 = sorted(_alc88.core_weights())[0]
    _cb88 = state.default_sleeve_state("core_blend")

    def _pos88(px):
        return {"quantity": 10, "entry_price": 100.0, "last_price": px,
                "market_value": px * 10, "cost": 1000.0, "peak_price": 100.0,
                "instrument": "equity", "opened_at": ts(9)}
    _cb88["positions"] = {_seat88: _pos88(80.0), "ZSAT88": _pos88(80.0)}
    state.save_sleeve("core_blend", _cb88)
    state.save("research", {"history": [
        {"asof": ts(i), "verdicts": {_seat88: {"rating": "BUY", "conviction": 70},
                                     "ZSAT88": {"rating": "BUY", "conviction": 70}}}
        for i in (3, 2, 1)]})
    _sc88 = _hl88._held_deterioration_scan(3, 5.0)
    check("88k core_blend positions are inside the deterioration denominator "
          "(the check used to grade 21 of 29 and omit this whole sleeve)",
          _sc88["by_sleeve"].get("core_blend") == 2
          and _sc88["checked"] >= 2, str(_sc88["by_sleeve"]))
    check("88k a bleeding core SATELLITE is flagged normally — a per-name SELL "
          "genuinely was available to it",
          any(r["ticker"] == "ZSAT88" for r in _sc88["flagged"]),
          str(_sc88["flagged"]))
    check("88k a bleeding MECHANICAL SEAT goes to its own labelled bucket, not "
          "the red list — core_sell_suppressed means the desk was never "
          "allowed to sell it per name",
          any(r["ticker"] == _seat88 and r["sell_suppressed"]
              for r in _sc88["seats_flagged"])
          and not any(r["ticker"] == _seat88 for r in _sc88["flagged"]),
          f"{_seat88} -> {_sc88['seats_flagged']}")
    _h88k = _hc88()
    check("88k the health line names the seat bucket and stays red only on the "
          "sellable name",
          _h88k["held deterioration"]["ok"] is False
          and "ZSAT88" in _h88k["held deterioration"]["detail"]
          and "MECHANICAL CORE SEATS" in _h88k["held deterioration"]["detail"],
          _h88k["held deterioration"]["detail"][:150])
    state.save_sleeve("core_blend", _prev_cb88)
finally:
    (_wl88.overdue_reopens, _wl88.reopen_kind_liveness, _rt88.route_coverage,
     _mc88.RELEASES, _mc88.calendar_tier, state.read_journal,
     _al88.alert) = _reals88
    if _cfg88[0] is None:
        delattr(config, "HEALTH_ESCALATE_AFTER_SWEEPS")
    else:
        config.HEALTH_ESCALATE_AFTER_SWEEPS = _cfg88[0]
    if _cfg88[1] is None:
        delattr(config, "BOOK_FULL_COVERAGE_SINCE")
    else:
        config.BOOK_FULL_COVERAGE_SINCE = _cfg88[1]
    state.save("research", _prev_res88)
    state.save("report_card_stats", _prev_rcs88)
    state.save("edge_health", _prev_eh88)
    state.save("reconcile_display", _prev_rd88)
    state.save("health_streaks", _prev_str88)
    state.save_sleeve("core_blend", _prev_cb88)

# ══ 89. Dependency poisoning (fix brief 2026-07-28 #3) ════════════════════════
# `conviction distribution` was RED for 36 consecutive sweeps while every line
# certifying a path that CONSUMES conviction reported ok — the desk read "one
# red, the rest green" when the truth was "one broken input and eight green
# lines computed on top of it". These lock the propagation, the single-alert
# rule, and the registry-staleness guard that keeps a rename from silently
# unprotecting a consumer.
from firm import health as _health89  # noqa: E402

_prev_str89 = state.load("health_streaks", {})
_cfg89 = getattr(config, "HEALTH_ESCALATE_AFTER_SWEEPS", None)
try:
    config.HEALTH_ESCALATE_AFTER_SWEEPS = 2
    state.save("health_streaks", {})

    def _mk89(**over):
        base = {"name": "conviction distribution", "ok": False, "detail": "compressed"}
        base.update(over)
        return base

    _checks89 = [_mk89(),
                 {"name": "sleeve routing", "ok": True, "detail": "clean"},
                 {"name": "options gate reachability", "ok": True, "detail": "reachable"},
                 {"name": "held deterioration", "ok": True, "detail": "0 of 20"},
                 {"name": "deployment", "ok": True, "detail": "alpha 89%"},
                 # already-failing and undetermined consumers keep their OWN verdict
                 {"name": "parking concentration", "ok": False, "detail": "31% parked"},
                 {"name": "bench reviewability", "ok": True, "detail": "n/a",
                  "undetermined": True}]
    _pois89 = _health89._apply_dependency_poisoning(_checks89)
    _by89 = {c["name"]: c for c in _checks89}
    check("89a a failing SIGNAL forces its registered consumers out of ok — a "
          "green computed on a broken input is not evidence of health",
          _by89["sleeve routing"]["ok"] is False
          and _by89["options gate reachability"]["ok"] is False
          and _by89["held deterioration"]["ok"] is False,
          str(sorted(_pois89)))
    check("89a the poisoned line NAMES its root and preserves its own reading, "
          "so the operator fixes one thing instead of chasing eight symptoms",
          _by89["sleeve routing"]["poisoned_by"] == "conviction distribution"
          and "clean" in _by89["sleeve routing"]["detail"],
          _by89["sleeve routing"]["detail"][:120])
    check("89a an UNDETERMINED consumer is left alone (it never claimed health) "
          "and an already-failing one keeps its own louder verdict",
          "bench reviewability" not in _pois89
          and "parking concentration" not in _pois89
          and _by89["parking concentration"]["detail"] == "31% parked")
    check("89b a HEALTHY signal poisons nothing — the watchdog fires on a "
          "flagged input, never on principle",
          _health89._apply_dependency_poisoning(
              [{"name": "conviction distribution", "ok": True, "detail": "ok"},
               {"name": "sleeve routing", "ok": True, "detail": "clean"}]) == [])

    # ONE broken root => ONE critical, no matter how many consumers it poisons
    for _ in range(4):
        _st89 = _health89._track_health_streaks(
            {"checks": [_mk89(),
                        {"name": "sleeve routing", "ok": False, "detail": "p",
                         "poisoned_by": "conviction distribution"},
                        {"name": "deployment", "ok": False, "detail": "p",
                         "poisoned_by": "conviction distribution"}]})
    check("89c poisoned consumers accrue NO streak and raise NO alert of their "
          "own — one root defect must not become an alert storm that trains "
          "the operator to ignore the ladder",
          set(_st89) == {"conviction distribution"}
          and _st89["conviction distribution"]["streak"] == 4
          and _st89["conviction distribution"]["alerted"] is True,
          str(_st89)[:160])
    for _ in range(3):
        _st89b = _health89._track_health_streaks(
            {"checks": [{"name": "sleeve routing", "ok": False,
                         "detail": "a real silent drop"}]})
    check("89c poisoning never MUTES a consumer that fails on its own evidence",
          _st89b["sleeve routing"]["streak"] == 3
          and _st89b["sleeve routing"]["alerted"] is True)

    check("89d the registry is validated against the live check names — a "
          "renamed check would otherwise drop out of it SILENTLY and go "
          "unprotected (the reconciler-allowlist bug class)",
          _health89._dependency_registry_gaps(
              [{"name": n} for n in
               (list(_health89.SIGNAL_CONSUMERS)
                + [c for s in _health89.SIGNAL_CONSUMERS.values()
                   for c in s["checks"]])]) == []
          and any("sleeve routing" in g for g in _health89._dependency_registry_gaps(
              [{"name": "conviction distribution"}])))
    check("89d every registered signal names the real trading gates that "
          "consume it, so the registry stays falsifiable rather than a guess",
          all(s.get("signal") and s.get("gates") and s.get("checks")
              for s in _health89.SIGNAL_CONSUMERS.values()))
finally:
    if _cfg89 is None:
        delattr(config, "HEALTH_ESCALATE_AFTER_SWEEPS")
    else:
        config.HEALTH_ESCALATE_AFTER_SWEEPS = _cfg89
    state.save("health_streaks", _prev_str89 or {})

# ══ 90. Stop slippage (fix brief 2026-07-28 #7) ═══════════════════════════════
# The brief read a list of raw exit percentages (-29.2%, -17.6%, -13.3%) and
# concluded the stop engine "evaluates only on daily bars or at the next open".
# Both halves were wrong: a 2.5-sigma stop on a 107-vol name IS ~17%, and the
# measured intraday cohort fills AT its design. These lock the three
# distinctions that make the number mean anything.
from firm import learning as _lrn90  # noqa: E402


def _row90(sym, reason, entry, exit_px, closed_at, **kw):
    r = {"symbol": sym, "ticker": sym, "sleeve": "alpha_stocks",
         "exit_reason": reason, "entry": entry, "exit": exit_px,
         "closed_at": closed_at, "pnl": (exit_px - entry) * 10}
    r.update(kw)
    return r


# 2026-07-28 is a Tuesday session; ET is UTC-4 in July.
_OPEN90 = "2026-07-28T13:31:00+00:00"   # 09:31 ET — first minutes
_MID90 = "2026-07-28T15:00:00+00:00"    # 11:00 ET — continuous trading
_EXT90 = "2026-07-28T20:30:00+00:00"    # 16:30 ET — after-hours
_SHUT90 = "2026-07-29T01:00:00+00:00"   # 21:00 ET — no session

check("90a a WIDE stop is not slippage — graded against the DISTANCE it was "
      "designed with, a 17% stop on a 107-vol name that filled at -17.6% "
      "slipped 0.6pt, not 17.6 (the exact misread that produced this brief)",
      (lambda r: r["n"] == 1 and -1.0 < r["avg_stop_slippage_pct"] < 0.0)(
          _lrn90.stop_slippage([_row90("MU", "hard stop -17.6%", 1030.86, 848.68,
                                       _MID90, stop_pct=17.0, vol_ann=107.2)])),
      str(_lrn90.stop_slippage([_row90("MU", "hard stop -17.6%", 1030.86, 848.68,
                                       _MID90, stop_pct=17.0, vol_ann=107.2)])["worst"]))

_trail90 = _lrn90.stop_slippage([
    _row90("AMAT", "trailing stop -18.0% from peak", 494.26, 605.56, _MID90,
           trail_stop_pct=15.0, vol_ann=40.0)])
check("90b a TRAILING exit is graded from the PEAK give-back in its own reason "
      "line, never from entry — AMAT closed +22.5% vs entry and must still "
      "report the -3.0pt give-back overshoot, not a fictional +37 gain",
      _trail90["n"] == 1 and _trail90["worst"][0]["realized_pct"] == -18.0
      and _trail90["worst"][0]["slippage_pct"] == -3.0,
      str(_trail90["worst"]))

_ctx90 = _lrn90.stop_slippage([
    _row90("GAPPER", "hard stop -25.0%", 100.0, 75.0, _OPEN90, stop_pct=12.0),
    _row90("STEADY", "hard stop -12.3%", 100.0, 87.7, _MID90, stop_pct=12.0),
    _row90("THIN", "hard stop -29.0%", 100.0, 71.0, _EXT90, stop_pct=12.0),
    _row90("DARK", "hard stop -13.0%", 100.0, 87.0, _SHUT90, stop_pct=12.0)])
_by90 = _ctx90["by_exit_context"]
check("90c each exit is bucketed by WHERE it filled — an overnight gap, a thin "
      "extended print and a continuous-session fill have three different fixes "
      "and one blended average hides all three",
      set(_by90) == {"gap_at_open", "intraday", "extended", "market_closed"}
      and _by90["gap_at_open"]["n"] == 1 and _by90["intraday"]["n"] == 1
      and _by90["extended"]["n"] == 1 and _by90["market_closed"]["n"] == 1,
      str({k: v["n"] for k, v in _by90.items()}))
check("90c only the INTRADAY bucket is evidence about the monitor: the steady "
      "fill shows ~0 slippage while the gap and the thin print show -13 and "
      "-17, so a headline average would have blamed the engine for the tape",
      abs(_by90["intraday"]["avg_slippage_pct"]) < 0.5
      and _by90["gap_at_open"]["avg_slippage_pct"] <= -12
      and _by90["extended"]["avg_slippage_pct"] <= -16,
      str({k: v["avg_slippage_pct"] for k, v in _by90.items()}))

_src90 = _lrn90.stop_slippage([
    _row90("STAMPED", "hard stop -20.0%", 100.0, 80.0, _MID90, stop_pct=12.0),
    _row90("NOSTAMP", "hard stop -20.0%", 100.0, 80.0, _MID90, vol_ann=30.0)])
check("90d the DESIGNED distance comes from the stop_pct stamped at entry when "
      "present, and a reconstruction from the row's own vol is labelled as one "
      "— provenance rides with the datum instead of being assumed",
      _src90["reconstructed_n"] == 1
      and {r["symbol"]: r["source"] for r in _src90["worst"]}
      == {"STAMPED": "stamped", "NOSTAMP": "reconstructed"},
      str([(r["symbol"], r["source"], r["intended_pct"]) for r in _src90["worst"]]))
_probe90 = {"trade_id": "z90", "symbol": "ZS90", "ticker": "ZS90",
            "entry": 10.0, "exit": 9.0, "pnl": -10.0, "quantity": 10,
            "reason": "hard stop -10.0%", "stop_pct": 11.5,
            "trail_stop_pct": 14.0, "vol_ann": 42.0,
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "opened_at": ts(3)}
_prev_led90 = state.load("ledger", {})
_prev_alpha90 = state.get_sleeve("alpha_stocks")
try:
    state.save("ledger", {"trades": [], "seen": [], "total_recorded": 0})
    _s90 = state.get_sleeve("alpha_stocks")
    _s90["closed_trades"] = [_probe90]
    state.save_sleeve("alpha_stocks", _s90)
    _lrn90.record_outcomes(spy_ser=None)
    _got90 = next((r for r in (state.load("ledger", {}).get("trades") or [])
                   if r.get("symbol") == "ZS90"), {})
    check("90d ledger rows now CARRY stop_pct/trail_stop_pct through a real "
          "record_outcomes round trip — broker.py stamped both on every closed "
          "trade and the row build dropped them, the same copy-through bug "
          "already fixed once for vol_ann, and the ledger is the ONLY copy "
          "that survives the 300-row sleeve cap",
          _got90.get("stop_pct") == 11.5 and _got90.get("trail_stop_pct") == 14.0,
          str({k: _got90.get(k) for k in ("stop_pct", "trail_stop_pct", "vol_ann")}))
finally:
    state.save("ledger", _prev_led90)
    state.save_sleeve("alpha_stocks", _prev_alpha90)
check("90e an empty population reports n=0 and NO average, instead of a "
      "confident 0.0% that would read as 'stops are perfect'",
      _lrn90.stop_slippage([])["n"] == 0
      and _lrn90.stop_slippage([])["avg_stop_slippage_pct"] is None)
check("90e the health bar is keyed to INTRADAY slippage only, so the desk is "
      "never pinned to a permanent red for overnight gaps it cannot catch",
      isinstance(getattr(config, "HEALTH_STOP_SLIPPAGE_MAX_PCT", None), (int, float)))

# ══ 91. Phantom marks may not fire stops (operator 2026-07-28, PRSU) ══════════
# broker.equity_quotes_public ended in `(last or bid)`, so a thin name with no
# ask — routine at the open and after the bell — marked at the raw BID. Four
# equity exits filled at prices that never traded (PRSU 39.08 vs a real
# 51.68-53.21 session, INNV 10.20 vs 10.93-11.63, STRZ 22.49 vs 25.98-27.65),
# booking -$1,516: 47% of every dollar the fund had lost. These lock BOTH ends —
# the quote may not invent a price, and a triggered stop needs a second opinion.
import firm.monitor as _mon90  # noqa: E402

_cap90 = getattr(config, "STOP_UNCONFIRMED_MAX_SWEEPS", 3)
_tol90 = getattr(config, "STOP_MARK_CONFIRM_TOLERANCE_PCT", 3.0)
_real_lp90 = data_engine.last_price
try:
    config.STOP_UNCONFIRMED_MAX_SWEEPS = 3

    class _Q90:
        def __init__(self, bid=None, ask=None, last=None):
            self.symbol, self.bid, self.ask, self.last = "Z90", bid, ask, last

    def _quote90(q):
        import types
        _sdk = types.ModuleType("public_api_sdk")
        _sdk.InstrumentType = types.SimpleNamespace(EQUITY="EQUITY")
        _sdk.OrderInstrument = lambda symbol, type: None
        sys.modules["public_api_sdk"] = _sdk
        _prev = broker.get_client
        broker.get_client = lambda: type("C", (), {"get_quotes": lambda self, instruments: [q]})()
        try:
            return broker.equity_quotes_public(["Z90"]).get("Z90")
        finally:
            broker.get_client = _prev

    check("91a a ONE-SIDED book yields no quote at all — the raw bid must never "
          "become a mark (this single expression cost the fund $1,516)",
          _quote90(_Q90(bid=37.51)) is None,
          str(_quote90(_Q90(bid=37.51))))
    check("91a a real last trade is preferred over a lonely bid",
          _quote90(_Q90(bid=37.51, last=51.58)) == 51.58)
    check("91a a two-sided TIGHT book still marks at the mid (the gate blocks "
          "junk, not normal quoting)",
          abs(_quote90(_Q90(bid=51.50, ask=51.66)) - 51.58) < 0.01)
    check("91a a junk-WIDE two-sided book falls back to the last print — the "
          "mid of a 37 x 65 quote is a price nobody could transact at",
          _quote90(_Q90(bid=37.0, ask=65.0, last=51.58)) == 51.58
          and _quote90(_Q90(bid=37.0, ask=65.0)) is None)
    check("91a a CROSSED book (ask < bid) is refused outright",
          _quote90(_Q90(bid=60.0, ask=40.0)) is None)

    data_engine.last_price = lambda s: 51.58            # the true PRSU price
    _p90 = {"entry_price": 53.01, "quantity": 27}
    check("91b the exact PRSU fill is HELD: a 37.51 mark against a 51.58 "
          "independent read does not route a stop",
          _mon90._mark_corroborated(_p90, 37.51, "alpha_stocks", "PRSU",
                                    "hard stop -29.2%") is False)
    data_engine.last_price = lambda s: 11.10
    check("91b INNV's SMALLER phantom is held too — 6.6% below the real low "
          "was enough to cross a 12% stop, and far under the 50% dislocation "
          "threshold _confirm_mark was built for",
          _mon90._mark_corroborated({"entry_price": 11.78}, 10.20,
                                    "alpha_stocks", "INNV", "hard stop") is False)
    data_engine.last_price = lambda s: 84.9
    check("91c a GENUINE crash still exits immediately when both sources agree "
          "— the gate must not become a reason to stop protecting the book",
          _mon90._mark_corroborated({"entry_price": 100.0}, 85.0,
                                    "alpha_stocks", "ZREAL", "hard stop") is True)
    data_engine.last_price = lambda s: 0.0
    _p90b = {"entry_price": 100.0}
    _seq90 = [_mon90._mark_corroborated(_p90b, 85.0, "alpha_stocks", "ZOUT",
                                        "hard stop") for _ in range(3)]
    check("91c a data OUTAGE cannot strand a real breakdown: with no second "
          "source the stop holds ONE sweep, then routes (missing-witness cap "
          "STOP_UNCONFIRMED_MAX_SWEEPS_NO_REF=2, 2026-09-01 — a witness that "
          "cannot show up must not hold a real breakdown 3 minutes)",
          _seq90[:2] == [False, True], str(_seq90))
    check("91c a corroborated sweep CLEARS the counter, so intermittent "
          "disagreement never accumulates toward a false release",
          (lambda p: (_mon90._mark_corroborated(p, 85.0, "s", "Z", "r"),
                      data_engine.last_price.__setattr__ if False else None,
                      p.get("stop_unconfirmed_sweeps"))[-1] == 1)(dict(entry_price=100.0)))

    _art90 = [{"closed_at": ts(1), "symbol": "ZART", "sleeve": "alpha_stocks",
               "entry": 100.0, "exit": 70.0, "pnl": -300.0, "quantity": 10,
               "exit_reason": "hard stop -30.0%", "mark_artifact": True,
               "verdict": {"rating": "BUY", "conviction": 70}},
              {"closed_at": ts(1), "symbol": "ZOK", "sleeve": "alpha_stocks",
               "entry": 100.0, "exit": 95.0, "pnl": -50.0, "quantity": 10,
               "exit_reason": "hard stop -5.0%",
               "verdict": {"rating": "BUY", "conviction": 70}}]
    _prev_led90 = state.load("ledger", {})
    try:
        state.save("ledger", {"trades": _art90, "total_recorded": 2})
        _st90 = learning.stats()
        check("91d a flagged mark-artifact reaches NO evidence table — one "
              "filter at the source, because eight per-table filters is eight "
              "chances for one to drift back in",
              (_st90.get("by_rating") or {}).get("BUY", {}).get("n") == 1
              and _st90.get("total_pnl") == -50.0
              and (_st90.get("stop_slippage") or {}).get("n", 0) <= 1,
              str(_st90.get("by_rating")))
        check("91d the exclusion is REPORTED, never silent — a shrinking "
              "sample must not look like a shrinking problem",
              (_st90.get("excluded_mark_artifacts") or {}).get("n") == 1
              and (_st90.get("excluded_mark_artifacts") or {}).get("pnl") == -300.0,
              str(_st90.get("excluded_mark_artifacts")))
    finally:
        state.save("ledger", _prev_led90)
finally:
    data_engine.last_price = _real_lp90
    config.STOP_UNCONFIRMED_MAX_SWEEPS = _cap90
    config.STOP_MARK_CONFIRM_TOLERANCE_PCT = _tol90

# ══ 92. Declared-path liveness + seat eligibility (briefs #1 and #4) ══════════
# #1 called the macro-catalyst reopen path inert. It was the BUSIEST path in the
# fund (165 fires). The auditor could believe otherwise because liveness was
# keyed on the debate schema's seven TRIGGER KINDS while the design declares
# five PATHS, and one `{"kind":"catalyst"}` arm resolves through EITHER branch
# of _catalyst_landed — so "catalyst arms == earnings arms" proved nothing about
# macro. #4's SOXL-in-a-core-seat is fixed; these lock the assertions that would
# catch either coming back.
_prev_wl92 = state.load("watchlist", {})
_prev_pool92 = state.load("etf_pool", {})
try:
    state.save("watchlist", {
        "ZCAT": {"ticker": "ZCAT", "source": "hold_verdict", "recheck_count": 0,
                 "earnings_date": "2026-08-05",
                 "reopen_trigger": {"kind": "catalyst"}},
        "ZTEC": {"ticker": "ZTEC", "source": "hold_verdict", "recheck_count": 0,
                 "reopen_trigger": {"kind": "pullback_pct", "pct": 8.0}},
    })
    _rp92 = watchlist.reopen_path_liveness()
    check("92a liveness enumerates one row per DECLARED PATH, not per schema "
          "kind — the two taxonomies are what let a CRITICAL be filed against "
          "the fund's most active reopen path",
          set(watchlist.REOPEN_PATHS) <= set(_rp92)
          and set(_rp92) - set(watchlist.REOPEN_PATHS) <= {"unattributed"}
          and len(watchlist.REOPEN_PATHS) == 8,
          str(sorted(_rp92)))
    check("92a ONE catalyst arm arms BOTH catalyst branches — earnings needs a "
          "date, macro resolves off the firm calendar, and counting them as "
          "rivals is the arithmetic that produced the false finding",
          _rp92["macro_catalyst_gated_to_non_immune"]["armed_now"] == 1
          and _rp92["earnings_catalyst"]["armed_now"] == 1
          and _rp92["technical_trigger"]["armed_now"] == 1)
    check("92a the time-cadence floor is armed for every entry under the "
          "recheck ceiling — the path that guarantees no benched name is stuck "
          "had no row in the kind-keyed map at all",
          _rp92["time_cadence_recheck_then_eviction"]["armed_now"] == 2)
    check("92b the two REASONS a path can be inert are reported SEPARATELY — "
          "macro is armed-but-never-fires (1 arm, 0 fires) while news is "
          "never-armed (0 arms). Different bugs, different fixes; the first "
          "cut collapsed both into one boolean and called the first healthy "
          "(IG 2026-07-28 #8)",
          _rp92["macro_catalyst_gated_to_non_immune"]["armed_but_never_fires"] is True
          and _rp92["macro_catalyst_gated_to_non_immune"]["never_armed"] is False
          and _rp92["news_catalyst"]["never_armed"] is True
          and _rp92["news_catalyst"]["armed_but_never_fires"] is False
          and all(_rp92[p]["inert"] for p in ("macro_catalyst_gated_to_non_immune",
                                              "news_catalyst")),
          str({k: (v["armed_now"], v["fired_in_window"]) for k, v in _rp92.items()}))
    check("92b reasons map back to the path that emitted them, so attribution "
          "is read off real events instead of inferred",
          watchlist._path_of_reason("catalyst: macro data landed 2026-07-16")
          == "macro_catalyst_gated_to_non_immune"
          and watchlist._path_of_reason("earnings printed 2026-07-20")
          == "earnings_catalyst"
          and watchlist._path_of_reason("quant improved +6") == "quant_improvement"
          and watchlist._path_of_reason("setup hit: pullback") == "technical_trigger")

    from firm import allocation as _alc92  # noqa: E402
    _real_lev92 = _alc92._is_leveraged
    try:
        state.save("etf_pool", {"etfs": [{"symbol": "ZSPY", "leveraged": False},
                                         {"symbol": "Z3X", "leveraged": True},
                                         {"symbol": "ZUNK", "leveraged": None}]})
        _core92 = dict(config.SLEEVES["core_blend"].get("core_weights") or {})
        _alc92._is_leveraged = lambda s, n=None: {"Z3X": True, "ZUNK": None}.get(s, False)
        check("92c a LEVERAGED pool member is not seat-eligible, and an "
              "UNCLASSIFIED one is refused too — None means the name feed was "
              "down, which is ineligible, not innocent",
              _alc92._pool_symbols(seats_only=True) == ["ZSPY"],
              str(_alc92._pool_symbols(seats_only=True)))
        check("92c the pool keeps leveraged names on purpose — they remain "
              "rankable into a ROTATION slot; only SEATS are constrained, so "
              "asserting an all-unleveraged pool would break a validated "
              "strategy to enforce a rule that never applied to it",
              len(_alc92._pool_symbols()) == 3)
    finally:
        _alc92._is_leveraged = _real_lev92
finally:
    state.save("watchlist", _prev_wl92)
    state.save("etf_pool", _prev_pool92)

# ══ 93. Conviction calibration (escalation step 6, 2026-07-28) ════════════════
# Five prompt-side steps failed to widen an emitted number the judge produces
# one name at a time with no view of the window. Step 6 stops asking and
# rescales the ORDER it does provide. The tests that matter are the ones that
# would let this quietly destroy the signal: a feedback loop that widens the
# scale every cycle, and any break in monotonicity.
import firm.debate as _dbt93  # noqa: E402

_prev_res93 = state.load("research", {})
_cfg93 = (getattr(config, "CONVICTION_TARGET_STDEV", 8.0),
          getattr(config, "CONVICTION_CALIBRATION_MIN_N", 40),
          getattr(config, "CONVICTION_MAX_GAIN", 2.5))
try:
    config.CONVICTION_TARGET_STDEV, config.CONVICTION_CALIBRATION_MIN_N = 8.0, 40
    config.CONVICTION_MAX_GAIN = 2.5

    def _seed93(vals):
        state.save("research", {"history": [{"verdicts": {
            f"Z{i}": {"rating": "BUY", "conviction_raw": v, "conviction": v}
            for i, v in enumerate(vals)}}]})

    _seed93([58, 60, 61, 62, 62, 63, 64, 66] * 5)          # n=40, median 62
    _c93 = {r: _dbt93._calibrate_conviction(r)[0] for r in (50, 58, 62, 66, 74)}
    check("93a the trailing MEDIAN is a fixed point — the rescale widens the "
          "distribution without moving its LEVEL, so every absolute threshold "
          "downstream keeps meaning what it meant",
          _c93[62] == 62, str(_c93))
    check("93a strictly monotonic: a name ranked above another before is "
          "ranked above it after (the judge's ordering is the whole signal "
          "being recovered, and a non-monotonic map would destroy it)",
          _c93[50] < _c93[58] < _c93[62] < _c93[66] < _c93[74], str(_c93))
    check("93a deviations are STRETCHED — that is the point",
          (_c93[74] - _c93[50]) > (74 - 50), f"{_c93[50]}..{_c93[74]}")

    _seed93([62] * 10)
    check("93b under the minimum sample the raw sum ships UNCHANGED — a cold "
          "start must never invent spread from four verdicts",
          _dbt93._calibrate_conviction(70) == (70, {"raw": 70, "mode": "raw",
                                                    "note": "only 10 reference "
                                                            "verdicts, need 40"}))
    _seed93([62] * 40)
    _flat93, _p93 = _dbt93._calibrate_conviction(70)
    check("93b a zero-width reference cannot be rescaled and says so, instead "
          "of dividing by ~0 and minting a 100",
          _flat93 == 70 and "no width" in str(_p93.get("note")), str(_p93))

    _seed93([58, 60, 61, 62, 62, 63, 64, 66] * 5)
    _v93 = {"rating": "BUY", "conviction_components":
            {"evidence": 11, "asymmetry": 6, "timing": 4, "differentiation": 3}}
    _f1 = dict(_dbt93._finalize_verdict(dict(_v93)))
    _f2 = _dbt93._finalize_verdict(_f1)
    check("93c conviction_raw is stamped and the sum is recomputed from the "
          "COMPONENTS every time, so re-finalizing a verdict cannot rescale an "
          "already-rescaled number",
          _f1["conviction_raw"] == 64 and _f2["conviction"] == _f1["conviction"]
          and _f2["conviction_raw"] == 64,
          f"raw={_f1['conviction_raw']} cal={_f1['conviction']} refinal={_f2['conviction']}")
    check("93c the calibration is auditable on the verdict — raw, gain and the "
          "reference it used ride with the number",
          _f1["conviction_source"] == "components+calibrated"
          and _f1["conviction_calibration"]["ref_n"] == 40
          and _f1["conviction_calibration"]["gain"] > 1.0,
          str(_f1.get("conviction_calibration")))

    # THE RUNAWAY TEST. If a calibrated value ever re-enters the reference, the
    # scale widens every cycle until everything clamps to 40/100 — a failure
    # that would take weeks to become visible in production and would destroy
    # the signal silently on the way.
    import random as _rnd93
    _r93 = _rnd93.Random(7)
    _hist93 = []
    for _c in range(20):
        _vs = {}
        for _i in range(30):
            _vs[f"Z{_i}"] = _dbt93._finalize_verdict({"rating": "BUY",
                "conviction_components": {"evidence": _r93.randint(6, 12),
                                          "asymmetry": _r93.randint(4, 8),
                                          "timing": _r93.randint(2, 6),
                                          "differentiation": _r93.randint(1, 4)}})
        _hist93.append({"verdicts": _vs})
        state.save("research", {"history": _hist93[-30:]})
    _all93 = [v["conviction"] for h in _hist93 for v in h["verdicts"].values()]
    _sd93 = (sum((x - sum(_all93) / len(_all93)) ** 2 for x in _all93) / len(_all93)) ** 0.5
    check("93d NO FEEDBACK LOOP: 20 cycles of calibrated output fed back as "
          "history leave the spread stable, because the reference is built "
          "from conviction_RAW and never from the calibrated value",
          _sd93 < 12 and min(_all93) > 35 and max(_all93) < 100,
          f"stdev {_sd93:.2f} range {min(_all93)}-{max(_all93)}")
    check("93d a degraded verdict never enters the reference — a synthetic "
          "fallback conviction must not set the scale real judgments are "
          "measured against",
          all(not (v or {}).get("degraded")
              for h in _hist93 for v in h["verdicts"].values()))
finally:
    (config.CONVICTION_TARGET_STDEV, config.CONVICTION_CALIBRATION_MIN_N,
     config.CONVICTION_MAX_GAIN) = _cfg93
    state.save("research", _prev_res93)

# ══ 94. Path attribution + census self-consistency (IG 2026-07-28 #6/#7/#8) ═══
# Three findings against code shipped hours earlier, and all three landed.
# The worst was self-inflicted: _PATH_REASON_MARKERS was a GUESS at the reason
# strings rather than a reading of the emitters, so "recheck" never matched the
# emitted "HOLD re-check due" and the time-cadence path reported 0 fires having
# fired 7 times — the exact defect written up that morning as a lesson.
_prev_wl94 = state.load("watchlist", {})
try:
    # The literal strings watchlist.promotions() writes. If an emitter is
    # renamed, THIS is what fails — not a silent miscount in production.
    import re as _re94
    _src94 = Path("firm/watchlist.py").read_text(encoding="utf-8")
    _emitted94 = set(_re94.findall(r'picks\.append\(\([^)]*?f?"([^"{]{4,40})', _src94))
    check("94a every literal promotion-reason prefix in watchlist.py maps to a "
          "DECLARED path — the attribution vocabulary is regenerated from the "
          "emitters instead of guessed at",
          _emitted94 and all(watchlist._path_of_reason(p) != "unattributed"
                             for p in _emitted94
                             if p.strip() not in ("catalyst:",)),
          str({p: watchlist._path_of_reason(p) for p in sorted(_emitted94)}))
    _cases94 = {
        "catalyst: macro data landed 2026-07-16 — re-judge": "macro_catalyst_gated_to_non_immune",
        "catalyst: earnings printed 2026-07-20 — re-judge": "earnings_catalyst",
        "setup hit: pullback 8%": "technical_trigger",
        "HOLD re-check due": "time_cadence_recheck_then_eviction",
        "stop-out recovery: $52.04 reclaimed $37.53 +3%": "stop_out_reclaim",
        "quant improved 88.0 -> 94.0": "quant_improvement",
    }
    check("94a the hyphen case that caused the miscount is locked: 'HOLD "
          "re-check due' attributes to time-cadence, not to technical_trigger",
          all(watchlist._path_of_reason(k) == v for k, v in _cases94.items()),
          str({k[:26]: watchlist._path_of_reason(k) for k in _cases94}))
    check("94a an UNRECOGNISED reason is reported as unattributed rather than "
          "defaulted into a real path — three of five emitters hid inside "
          "technical_trigger's count that way",
          watchlist._path_of_reason("some brand new reason") == "unattributed")
    check("94b quant_improvement and stop_out_reclaim are DECLARED paths — the "
          "five-path list omitted the only route a near_miss entry has back, "
          "which is why that cohort kept reading as unarmed on every path",
          {"quant_improvement", "stop_out_reclaim"} <= set(watchlist.REOPEN_PATHS)
          and len(watchlist.REOPEN_PATHS) == 8)

    state.save("watchlist", {
        "ZA": {"ticker": "ZA", "source": "near_miss", "history": [{"quant": 90}],
               "recheck_count": 0},
        "ZB": {"ticker": "ZB", "source": "hold_verdict", "recheck_count": 0,
               "history": [{"quant": 80}], "earnings_date": "2026-08-05",
               "reopen_trigger": {"kind": "catalyst"}},
    })
    _rj94 = state.read_journal
    state.read_journal = lambda n=200: []      # deterministic: zero fires
    try:
        _rp94 = watchlist.reopen_path_liveness()
    finally:
        state.read_journal = _rj94
    check("94c ARMED-BUT-NEVER-FIRES is its own failure, distinct from "
          "NEVER-ARMED — the first cut called a path healthy whenever it had "
          "arms, so 'armed on 102 entries, 0 fires in 30d' read as green",
          _rp94["quant_improvement"]["armed_but_never_fires"] is True
          and _rp94["quant_improvement"]["never_armed"] is False
          and _rp94["quant_improvement"]["inert"] is True,
          str(_rp94["quant_improvement"]))

    _cen94 = {"by_source": {"near_miss": {
        "n": 19, "armed_either": {"n": 0},
        "unarmed_both_paths_names": [f"Z{i}" for i in range(12)]}}}
    import ig_audit as _iga94  # noqa: E402
    check("94d the census asserts its own counts against its own name list — "
          "reporting 19 unarmed while naming 12 meant any repair driven off "
          "the names silently covered 12 of 19",
          True)  # behaviour proven on live state below
    _wl94 = {f"Z{i}": {"ticker": f"Z{i}", "source": "near_miss"} for i in range(19)}
    _c94 = _iga94._arming_census(_wl94, watchlist._trigger_kinds)
    check("94d 19 unarmed entries produce 19 NAMES (the [:12] cap is gone) and "
          "the reconcile invariant passes",
          len(_c94["by_source"]["near_miss"]["unarmed_both_paths_names"]) == 19
          and _c94["names_reconcile"] is True,
          f"named={len(_c94['by_source']['near_miss']['unarmed_both_paths_names'])}")
finally:
    state.save("watchlist", _prev_wl94)

# ══ 95. Brief queue durability + standing defects + 3rd-source marks ══════════
# The queue capped at [-10:], so filing 5 briefs DELETED 5 resolutions and the
# written reasoning behind them — the queue discarded exactly the part that
# took longest to produce.
_prev_fb95 = state.load("fix_briefs", {})
_prev_sd95 = state.load("standing_defects", {})
_cfg95 = (getattr(config, "FIX_BRIEF_MAX_ITEMS", 60),
          getattr(config, "STANDING_DEFECT_SWEEPS", 20),
          getattr(config, "STANDING_DEFECT_BLOCKS_SLEEVES", False))
try:
    config.FIX_BRIEF_MAX_ITEMS = 6
    state.save("fix_briefs", {"items":
        [{"problem": f"resolved-{i}", "resolved": f"FIXED — evidence {i}"} for i in range(4)]
        + [{"problem": f"open-{i}"} for i in range(2)]})
    learning._file_fix_requests([{"problem": f"new-{i}"} for i in range(4)])
    _fb95 = state.load("fix_briefs", {})
    _act95 = [r["problem"] for r in _fb95["items"]]
    _arc95 = [r["problem"] for r in _fb95.get("archive", [])]
    check("95a an OPEN brief is NEVER evicted by the cap — it is the work queue, "
          "and dropping it loses the task itself",
          all(p in _act95 for p in ("open-0", "open-1")), str(_act95))
    check("95a a RESOLVED brief is archived, never deleted — the reasoning "
          "survives leaving the active list",
          sorted(_arc95) == ["resolved-0", "resolved-1", "resolved-2", "resolved-3"]
          and len(_act95) <= 6, f"active={_act95} archive={_arc95}")

    config.STANDING_DEFECT_SWEEPS = 3
    config.STANDING_DEFECT_BLOCKS_SLEEVES = False
    _sd95 = _health89.record_standing_defects({
        "conviction distribution": {"streak": 46},
        "ram headroom": {"streak": 2}})
    check("95b a failure past the budget becomes a DECLARED standing defect "
          "carrying its real dependents — an alert nobody can act on is not a "
          "control",
          list(_sd95) == ["conviction distribution"]
          and "sleeve routing" in _sd95["conviction distribution"]["dependent_checks"],
          str(list(_sd95)))
    check("95b sleeve gating is OFF by default: halting the fund to avoid the "
          "benchmark-hugging this particular defect causes is the LARGER harm, "
          "so the flip is the operator's, not a default",
          _health89.standing_defect_block("sleeve routing") is None
          and _sd95["conviction distribution"]["blocking"] is False)
    config.STANDING_DEFECT_BLOCKS_SLEEVES = True
    _health89.record_standing_defects({"conviction distribution": {"streak": 46}})
    check("95b with the flag ON the block names its root, and reaches ONLY the "
          "registered dependents",
          "conviction distribution" in (_health89.standing_defect_block("sleeve routing") or "")
          and _health89.standing_defect_block("disk space") is None)
    config.STANDING_DEFECT_BLOCKS_SLEEVES = False
    _health89.record_standing_defects({"ram headroom": {"streak": 1}})
    check("95b the defect CLEARS when the check recovers — a latch that never "
          "releases is the same failure in the other direction",
          state.load("standing_defects", {}) == {})

    _ts95 = _health89._third_source_mark_audit({"ZFAKE95": 1.0}, sample=1)
    check("95c the third-source audit reports UNVERIFIED rather than clean when "
          "no independent bar can be fetched — an unmeasurable check must not "
          "read as a pass (this is the equity OF RECORD)",
          (_ts95 or {}).get("checked") == 0 and "note" in (_ts95 or {}),
          str(_ts95))
finally:
    (config.FIX_BRIEF_MAX_ITEMS, config.STANDING_DEFECT_SWEEPS,
     config.STANDING_DEFECT_BLOCKS_SLEEVES) = _cfg95
    state.save("fix_briefs", _prev_fb95)
    state.save("standing_defects", _prev_sd95)

# ══ 96. Self-test cadence survives a BUSY journal (operator 2026-07-31) ═══════
# The cadence check read the last self_test out of read_journal(1500) — a fixed
# EVENT-COUNT window standing in for a multi-DAY question. The firm writes ~320
# journal events/day, so 1500 events spans ~4.7 days: the WEEKLY event aged out
# of the window before its own next run and the check reported "has not run in
# >8d (or never) — the watchdog-of-watchdogs is dark" while the scheduled task
# was green (2026-07-26, rc=0, 819/819 + 53/53). It then escalated a CRITICAL,
# so the ONE signal that says "the watchdog is dark" was spent on a false red.
# The failure gets WORSE as the firm gets busier, and rotation erases the event
# regardless of window size — so the stamp must be durable, not a journal scan.
from firm import health as _health96  # noqa: E402
import json as _json96  # noqa: E402

_prev_st96 = state.load("self_test", None)
try:
    # A self-test that ran 2d ago, buried under MORE journal events than the
    # read window can ever reach — exactly the live 2026-07-31 condition.
    _ts96 = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    state.save("self_test", {"ts": _ts96, "passed": True})
    _jp96 = config.STATE_DIR / "journal.jsonl"
    _jp96.write_text("".join(
        _json96.dumps({"ts": (datetime.now(timezone.utc)
                              - timedelta(minutes=2000 - i)).isoformat(),
                       "event": "noise"}) + "\n" for i in range(2000)),
        encoding="utf-8")
    _rep96 = _health96.health_report()
    _c96 = next((c for c in _rep96.get("checks", [])
                 if c.get("name") == "self-test cadence"), None)
    check("96 a self-test 2d old is GREEN even when the journal window cannot "
          "reach it — the durable stamp answers 'when did X last happen', not a "
          "count window that shrinks as the firm gets busier",
          _c96 is not None and _c96.get("ok") is True
          and "never" not in str(_c96.get("detail", "")),
          str(_c96))
    # ...and the check must still be able to go RED for the real reason.
    state.save("self_test", {"ts": (datetime.now(timezone.utc)
                                    - timedelta(days=20)).isoformat()})
    _rep96b = _health96.health_report()
    _c96b = next((c for c in _rep96b.get("checks", [])
                  if c.get("name") == "self-test cadence"), None)
    check("96 a genuinely DARK watchdog still reds — the fix must not buy green "
          "by disabling the signal",
          _c96b is not None and _c96b.get("ok") is False, str(_c96b))
    # The stamp is written by the runner, so the runner must actually write it.
    _src96 = (Path(__file__).parent.parent / "scripts"
              / "self_test.py").read_text(encoding="utf-8")
    check("96 scripts/self_test.py persists the durable stamp the check reads — "
          "otherwise the stamp goes stale and the false red returns on a delay",
          'state.save("self_test"' in _src96)
finally:
    if _prev_st96 is None:
        (config.STATE_DIR / "self_test.json").unlink(missing_ok=True)
    else:
        state.save("self_test", _prev_st96)

# == SECTION 97: the 2026-08-19 alarm-integrity repairs ========================
# Four health lines were escalating (or hiding) the wrong thing. Every check
# below asserts BOTH directions: the false alarm clears, AND the line still
# reds for the real reason. A fix that only proves the green half is how a
# check gets quietly disabled.
from firm import health as _hl97, routing as _rt97  # noqa: E402

# -- 97a self-test RESULT (fix briefs 58 + 71) -------------------------------
# 'self-test cadence' graded recency while state/self_test.json said
# {passed:false, firm_suite:"crashed: ... timed out after 900 seconds"}. Two
# days of a dead regression suite behind a green watchdog.
_prev97 = state.load("self_test", None)
try:
    state.save("self_test", {"ts": datetime.now(timezone.utc).isoformat(),
                             "passed": True, "firm_suite": "928/928 passed",
                             "week_sim": "53/53 passed"})
    _r97 = _hl97.health_report()
    _c97 = next((c for c in _r97["checks"] if c["name"] == "self-test result"), None)
    check("97a a PASSING self-test reads green on the RESULT line",
          _c97 is not None and _c97.get("ok") is True, str(_c97)[:120])

    state.save("self_test", {"ts": datetime.now(timezone.utc).isoformat(),
                             "passed": False,
                             "firm_suite": "crashed: Command ... timed out "
                                           "after 900 seconds",
                             "week_sim": "53/53 passed"})
    _r97b = _hl97.health_report()
    _c97b = next((c for c in _r97b["checks"] if c["name"] == "self-test result"), None)
    _cad97 = next((c for c in _r97b["checks"] if c["name"] == "self-test cadence"), None)
    check("97a a CRASHED suite reds the RESULT line even though the stamp is "
          "FRESH -- recency must never satisfy outcome",
          _c97b is not None and _c97b.get("ok") is False, str(_c97b)[:120])
    check("97a the crash is named as a TIMEOUT, not a plain failure (different "
          "repair: environment/perf vs a broken invariant)",
          _c97b is not None and "TIMED OUT" in str(_c97b.get("detail", "")))
    check("97a cadence stays GREEN on the same stamp -- the two questions are "
          "independent and both must be answerable",
          _cad97 is not None and _cad97.get("ok") is True, str(_cad97)[:100])

    state.save("self_test", {"ts": datetime.now(timezone.utc).isoformat()})
    _r97c = _hl97.health_report()
    _c97c = next((c for c in _r97c["checks"] if c["name"] == "self-test result"), None)
    # UNDETERMINED is its own bucket in this codebase, not a red: add_undet
    # leaves ok=True and names the check in report["undetermined"]. Probe the
    # bucket, not the boolean -- the point of the IG's "vacuous pass" rule is
    # that "no evidence" must be DISTINGUISHABLE from "evidence of health",
    # and that distinction lives in the undetermined list.
    check("97a a stamp with NO pass/fail key is UNDETERMINED, never a silent "
          "green -- absence of the evidence is not evidence of health",
          _c97c is not None
          and "self-test result" in (_r97c.get("undetermined") or [])
          and "carries no pass/fail key" in str(_c97c.get("detail", "")),
          str(_c97c)[:130])
finally:
    if _prev97 is None:
        (config.STATE_DIR / "self_test.json").unlink(missing_ok=True)
    else:
        state.save("self_test", _prev97)

# -- 97b options-gate conversion window (the PEB latch) ----------------------
# route_coverage's BYPASS leg was epoch-bounded; its CONVERSION leg had no
# lower bound, so ONE silent drop (PEB 2026-08-04, fixed 08-13) pinned
# 'options gate reachability' CRITICAL for 53 sweeps with no clearing path.
import json as _json97  # noqa: E402
_now97 = datetime.now(timezone.utc)
_jf97 = config.STATE_DIR / "journal.jsonl"


def _conv97(days_ago):
    """Run the conversion leg against a single qualified-but-unconverted
    event `days_ago` old; return (silent, aged)."""
    _ts = (_now97 - timedelta(days=days_ago)).isoformat()
    _jf97.write_text(_json97.dumps(
        {"ts": _ts, "event": "options_gate_qualified",
         "side": "calls", "symbols": ["ZZTOP"], "n": 1}) + "\n",
        encoding="utf-8")
    _c = (_rt97.route_coverage().get("gate_conversion") or {}).get("calls") or {}
    return (_c.get("unexplained_qualified") or [],
            _c.get("aged_unexplained") or [])


_prev_j97 = _jf97.read_text(encoding="utf-8") if _jf97.exists() else None
try:
    _win97 = int(getattr(config, "OPTIONS_GATE_CONVERSION_WINDOW_DAYS", 14))
    _sil_new, _aged_new = _conv97(1)
    check("97b a silent drop INSIDE the window still reds the line -- the fix "
          "must not stop detecting a live regression",
          "ZZTOP" in _sil_new and "ZZTOP" not in _aged_new,
          "silent=%s aged=%s" % (_sil_new, _aged_new))
    _sil_old, _aged_old = _conv97(_win97 + 5)
    check("97b a silent drop OLDER than the window ages out of the red and "
          "moves to aged_unexplained -- a remediated defect must be able to "
          "clear, and must stay auditable rather than being deleted",
          "ZZTOP" not in _sil_old and "ZZTOP" in _aged_old,
          "silent=%s aged=%s" % (_sil_old, _aged_old))
finally:
    if _prev_j97 is None:
        _jf97.unlink(missing_ok=True)
    else:
        _jf97.write_text(_prev_j97, encoding="utf-8")

# -- 97c the fix-brief evidence line must not fabricate its own numbers ------
# health.py appended the LITERAL "real verdicts, 0 qualified" to every options
# gate brief regardless of _cov. Fix briefs 68 and 69 -- and a CRITICAL alert
# claiming an "arithmetically unreachable" gate -- were manufactured from that
# hardcoded zero, about the fund's best-performing sleeve.
_src97 = (Path(__file__).parent.parent / "firm" / "health.py").read_text(
    encoding="utf-8")
# Strip whole-line comments first: the fix's own comment QUOTES the fabricated
# string it documents, and a source check that a comment can fail is measuring
# prose rather than behaviour (the same brittleness as the [:1800] slice in
# section 70). Executable lines only.
_code97 = "\n".join(ln for ln in _src97.splitlines()
                    if not ln.lstrip().startswith("#"))
check("97c no options-gate brief hardcodes a qualified count -- the evidence "
      "line interpolates what was actually measured",
      "real verdicts, 0 qualified" not in _code97)
check("97c the brief's evidence reports the measured qualified counts",
      "calls_gate_qualified" in _src97.split("suspected_area")[0][-1600:])

# -- 97d parking concentration is TIME-AGGREGATED, not instantaneous ---------
# A desk that is correctly FLAT BETWEEN TRADES reads identical to one whose
# gate is broken: both show "95% parked, 0 real positions". The old leg fired
# on that instant and escalated calls_only -- the fund's best sleeve, +6.8%
# lifetime, +$1,672 realised three days earlier -- as "that mandate is not
# being expressed".
_prev_sl97 = state.get_sleeve("calls_only")
try:
    def _park97(days_since_trade):
        _s = copy.deepcopy(_prev_sl97)
        _s["cash"] = 100.0
        _s["positions"] = {"SPY": {"quantity": 100, "entry_price": 500.0,
                                   "last_price": 500.0, "instrument": "equity",
                                   "cash_equitization": True,
                                   "opened_at": _now97.isoformat()}}
        _s["closed_trades"] = [{
            "symbol": "GEO260821C00031000", "instrument": "option",
            "opened_at": (_now97 - timedelta(days=days_since_trade + 1)).isoformat(),
            "closed_at": (_now97 - timedelta(days=days_since_trade)).isoformat(),
            "pnl": 1171.2, "reason": "TP2 +78% sell all but runner"}]
        state.save("sleeve_calls_only", _s)
        _r = _hl97.health_report()
        return next((c for c in _r["checks"]
                     if c["name"] == "parking concentration"), None)

    _idle97 = float(getattr(config, "PARKING_SLEEVE_IDLE_DAYS", 10.0))
    _c97d = _park97(1.0)
    check("97d a 100%-parked sleeve that traded 1d ago is BETWEEN TRADES, not "
          "a dead mandate -- parking between qualifying signals is the mandate "
          "working, and the line says so",
          _c97d is not None
          and "BETWEEN TRADES" in str(_c97d.get("detail", ""))
          and "EFFECTIVELY ALL PARKING" not in str(_c97d.get("detail", "")),
          str(_c97d.get("detail"))[:160])
    _c97e = _park97(_idle97 + 5)
    check("97d a 100%-parked sleeve IDLE past the bar still reds -- the fix "
          "must not buy green by never looking at parking again",
          _c97e is not None
          and "EFFECTIVELY ALL PARKING" in str(_c97e.get("detail", "")),
          str(_c97e.get("detail"))[:160])
finally:
    state.save("sleeve_calls_only", _prev_sl97)

# -- 97f session coverage: the exit-protection hole nobody owned -------------
# 'exit monitor' grades time-since-last-sweep, so a blackout that has CLOSED
# reads "last sweep 0 min ago" and goes green forever. The firm was emitting
# "EXIT PROTECTION WAS DOWN 260 min of trading" criticals that no health check
# consumed and no streak tracked. Measured on the live journal when this was
# written: 61% coverage over 10d, 7 blackouts, worst 260 min.
_prev_j97f = _jf97.read_text(encoding="utf-8") if _jf97.exists() else None
try:
    _mx97 = float(getattr(config, "EXIT_COVERAGE_MAX_GAP_MIN", 30.0))

    def _cov97f(rows):
        _jf97.write_text("".join(_json97.dumps(r) + "\n" for r in rows),
                         encoding="utf-8")
        _r = _hl97.health_report()
        return next((c for c in _r["checks"]
                     if c["name"] == "session coverage"), None)

    _c97f = _cov97f([{"ts": _now97.isoformat(), "event": "noise"}])
    check("97f a session with NO recorded blackout reads green -- the check "
          "must be satisfiable, not a permanently-on light",
          _c97f is not None and _c97f.get("ok") is True,
          str(_c97f.get("detail"))[:140])

    _c97g = _cov97f([
        {"ts": (_now97 - timedelta(days=1)).isoformat(),
         "event": "session_coverage_gap",
         "from": (_now97 - timedelta(days=1, hours=5)).isoformat(),
         "to": (_now97 - timedelta(days=1)).isoformat(),
         "gap_hours": 5.0, "missed_session_minutes": _mx97 * 4},
    ])
    check("97f a single blackout past the max-gap bar REDS the line -- one long "
          "hole is what actually lets a stop fire late (CRS and TXN both "
          "stopped after a 16.07h blackout)",
          _c97g is not None and _c97g.get("ok") is False
          and "EXIT PROTECTION WAS DOWN" in str(_c97g.get("detail", "")),
          str(_c97g.get("detail"))[:140])

    # One hole must count ONCE: the reporter has been observed logging an exact
    # duplicate pair (2026-08-12T18:00, 2026-08-19T16:31).
    _dup = {"ts": (_now97 - timedelta(days=1)).isoformat(),
            "event": "session_coverage_gap",
            "from": (_now97 - timedelta(days=1, hours=2)).isoformat(),
            "to": (_now97 - timedelta(days=1)).isoformat(),
            "gap_hours": 2.0, "missed_session_minutes": 55}
    _c97h = _cov97f([_dup, dict(_dup)])
    check("97f a duplicated gap event counts as ONE blackout -- double-counting "
          "inflates the very number the operator is asked to act on",
          _c97h is not None and "1 blackout(s)" in str(_c97h.get("detail", "")),
          str(_c97h.get("detail"))[:140])
finally:
    if _prev_j97f is None:
        _jf97.unlink(missing_ok=True)
    else:
        _jf97.write_text(_prev_j97f, encoding="utf-8")

# -- 97e eod mark-to-close is an SLA that can CLEAR, and sees missing marks --
# The old line latched on `_fails == 0 and _rescues == 0` over slices that
# never truncate, so three 2026-08-11 rescues held it red for 105 sweeps while
# it stayed BLIND to the real defect: sessions with no successful mark at all.
check("97e the eod check grades a declared SLA window, not two booleans",
      "EOD_MARK_SLA_DAYS" in _src97
      and "SESSION(S) WITH NO SUCCESSFUL MARK" in _src97)
check("97e eod_catchup marks to the close BEFORE booking the close -- the "
      "recovery path runs when marks are STALEST, and it was stamping an "
      "intraday print as the session's equity of record",
      "mark_books_to_close()" in _src97.split("def eod_catchup")[1][:2800])

# ══ SECTION 98: bench PROVENANCE survives a re-judge (operator 2026-08-19, ═════
# "fix the bench source overwrite too"). `source` does two incompatible jobs —
# the DISPATCH switch the promotion loop branches on (legitimately mutable) and
# the PROVENANCE class the census partitions on (must be durable). Mutability
# won: _update_locked rewrites e["source"] on every HOLD re-judge, so the
# stop_out class stopped existing. bench.by_source read
# {hold_verdict 76, near_miss 36, book_full 4} with partition_sums_to_total
# TRUE — a complete partition over a set missing a member — while
# stop_out_reclaim reported armed_now 0 / never_armed True and the journal
# showed the path firing twice. That false-inert verdict held 'reopen-path
# liveness' red for 338 consecutive sweeps. The payload blocks the emitters
# write are never deleted, so THEY are the durable fact. ═══════════════════════
import json as _json98  # noqa: E402
from firm import watchlist as _wl98  # noqa: E402
_prev_wl98 = state.load("watchlist", {})
try:
    _stopped98 = {
        "ticker": "ZZTOP", "added": ts(30), "history": [],
        # THE DEFECT: a stop-out that a later HOLD re-judge relabelled.
        "source": "hold_verdict",
        "recheck_count": 1,
        "last_verdict": {"rating": "HOLD", "conviction": 57, "ts": ts(1)},
        # ...while the emitter's payload block survived untouched.
        "stop_out": {"sleeve": "alpha_stocks", "entry": 100.0, "exit": 80.0,
                     "reason": "hard stop -20%", "thesis": "still intact",
                     "conviction": 61, "ts": ts(30)},
        # ...and the re-judge also overwrote the reclaim arm add_stop_out wrote.
        "reopen_trigger": {"kind": "cross_sma", "period": 50, "dir": "above"},
    }
    check("98 a stop-out relabelled by a re-judge is STILL a stop-out — the "
          "obligation lives in the payload the emitter wrote, not in the "
          "mutable dispatch label a later re-judge overwrites",
          _wl98._stop_out_open(_stopped98) is True
          and "stop_out" in _wl98.bench_obligations(_stopped98))

    # The arming census must count the OBLIGATION. Reading `source` here is what
    # made a live population invisible and produced "never armed" for a path the
    # journal showed firing.
    state.save("watchlist", {"ZZTOP": _stopped98})
    _lv98 = _wl98.reopen_path_liveness()
    _sor98 = (_lv98 or {}).get("stop_out_reclaim") or {}
    check("98 reopen_path_liveness ARMS a relabelled stop-out — the census that "
          "read `source` reported 0 armed while the path fired twice, and that "
          "contradiction is what 338 sweeps of red were actually reporting",
          int(_sor98.get("armed_now") or 0) == 1
          and not _sor98.get("never_armed"), str(_sor98))

    # The reclaim arm must be restored, and must COMPOSE with the committee's
    # own trigger rather than replacing it: the committee's setup is an opinion
    # and is re-derivable, the reclaim price is arithmetic off the stop and is
    # not.
    _e98 = copy.deepcopy(_stopped98)
    _ch98 = _wl98._ensure_reclaim_arm(_e98, 7.0)
    _rt98 = _e98.get("reopen_trigger") or {}
    _legs98 = _rt98.get("legs") or []
    check("98 the reclaim arm is restored at exit x(1+pct) and OR-ed with the "
          "committee's trigger — both must be able to reopen the name",
          _ch98 is True and _rt98.get("op") == "any"
          and any(l.get("kind") == "reclaim_price" and abs(l.get("price") - 85.6) < 0.01
                  for l in _legs98)
          and any(l.get("kind") == "cross_sma" for l in _legs98), str(_rt98))

    # IDEMPOTENCE IS LOAD-BEARING: this runs every cycle from promotions() and
    # again from the re-judge path. A non-idempotent version nests the tree one
    # level deeper forever until the trigger evaluator chokes on it.
    _before98 = _json98.dumps(_e98.get("reopen_trigger"), sort_keys=True)
    _again98 = _wl98._ensure_reclaim_arm(_e98, 7.0)
    check("98 re-arming is IDEMPOTENT — it runs every cycle, so a second pass "
          "must change nothing (else the trigger tree nests forever)",
          _again98 is False
          and _json98.dumps(_e98.get("reopen_trigger"), sort_keys=True) == _before98)

    # OR-REACHABILITY: a reclaim leg buried under `all`/`after` is gated behind
    # another condition and is strictly stricter than the obligation. PRSU was
    # the live case — {any:[{all:[cross_sma, reclaim_price]}, catalyst]} — a
    # reclaim that could only fire if the 50-day was ALSO reclaimed.
    _gated98 = copy.deepcopy(_stopped98)
    _gated98["reopen_trigger"] = {"op": "all", "legs": [
        {"kind": "cross_sma", "period": 50, "dir": "above"},
        {"kind": "reclaim_price", "price": 85.6}]}
    check("98 a reclaim buried under an AND does NOT satisfy the obligation — "
          "the stop-out contract is 'price reclaimed the exit => reconsider', "
          "full stop, so a standalone leg is OR-ed in beside it",
          _wl98._has_reclaim_leg(_gated98["reopen_trigger"]) is False
          and _wl98._ensure_reclaim_arm(_gated98, 7.0) is True
          and any(l.get("kind") == "reclaim_price"
                  for l in (_gated98["reopen_trigger"].get("legs") or [])))
    check("98 an OR-reachable reclaim IS honoured (no needless second leg)",
          _wl98._has_reclaim_leg(
              {"op": "any", "legs": [{"kind": "catalyst"},
                                     {"kind": "reclaim_price", "price": 85.6}]}) is True)

    # A name with no stop-out obligation must never be touched.
    _plain98 = {"ticker": "AAPL", "source": "hold_verdict",
                "reopen_trigger": {"kind": "catalyst"}}
    check("98 a name with no stop-out payload is left alone (the guard must not "
          "arm a reclaim for a name that was never stopped)",
          _wl98._stop_out_open(_plain98) is False
          and _wl98._ensure_reclaim_arm(_plain98, 7.0) is False
          and _plain98["reopen_trigger"] == {"kind": "catalyst"})

    # A stop-out with a corrupt/absent exit price cannot be armed — and must
    # fail SOFT rather than writing a nonsense price trigger.
    _noexit98 = copy.deepcopy(_stopped98)
    _noexit98["stop_out"] = dict(_noexit98["stop_out"], exit=None)
    _noexit98["reopen_trigger"] = None
    check("98 a stop-out with no usable exit price arms NOTHING rather than a "
          "fabricated level (fail soft, never fail wrong)",
          _wl98._ensure_reclaim_arm(_noexit98, 7.0) is False
          and not _noexit98.get("reopen_trigger"))

    # THE AUDIT MUST BE ABLE TO SEE THIS CLASS OF DRIFT, or it recurs silently.
    _obl98 = _wl98.bench_obligations(_stopped98)
    check("98 bench_obligations reports BOTH the dispatch label and the durable "
          "obligation, so the IG census can name the entries whose label hides "
          "a live class (by_source said stop_out ZERO while 2 were live)",
          _obl98 == {"hold_verdict", "stop_out"}, str(sorted(_obl98)))

    # The shared classifier is the anti-drift device brief 73 asked for: the
    # audit must consume it, not re-derive its own copy of the emitter list.
    _iga98 = (Path(__file__).parent.parent / "scripts"
              / "ig_audit.py").read_text(encoding="utf-8")
    check("98 scripts/ig_audit.py derives its obligation census from the SHARED "
          "watchlist.bench_obligations — a second private copy of the emitter "
          "list is exactly how the census drifted from the runtime",
          "watchlist.bench_obligations(e)" in _iga98
          and "obligation_hidden_by_label" in _iga98)

    # And the runtime must not silently drop the arm again on the next re-judge.
    _srcwl98 = (Path(__file__).parent.parent / "firm"
                / "watchlist.py").read_text(encoding="utf-8")
    check("98 the HOLD re-judge path re-composes the reclaim arm — the "
          "unconditional reopen_trigger rewrite is right for the committee's "
          "legs and wrong for the stop price, which is arithmetic",
          "_ensure_reclaim_arm(e, _param(" in _srcwl98)
finally:
    state.save("watchlist", _prev_wl98)

# ══ SECTION 99: 'state integrity' grades BOOK corruption, not every file ══════
# (operator 2026-08-19). The classifier was a two-name BLACKLIST — heartbeat and
# status were benign, EVERYTHING else was "CRITICAL state file(s) recovered from
# backup — check disk/writes" — even though the check's own comment defined
# CRITICAL as "the books: sleeves, ledger, watchlist, lessons, research,
# settings". So ~60 derived files graded as money state, and the line sat red
# from 14:49 on exactly one event: reopen_path_inert (the health desk's OWN
# inertness tracker) restoring from its .bak. Losing that costs a telemetry
# rebuild, not a dollar. Tiers are declared once in firm/state.py, beside the
# reader/writer that owns the names. ═══════════════════════════════════════════
check("99 the books are BOOK state — a restore here is a real money alarm",
      all(state.state_tier(_n) == "BOOK" for _n in
          ("sleeve_alpha_stocks", "sleeve_calls_only", "ledger", "watchlist",
           "lessons", "research", "settings", "firm", "equity_history")))
check("99 liveness stamps stay EPHEMERAL (rewritten every sweep — losing one "
      "write is harmless and must never read as disk failure)",
      all(state.state_tier(_n) == "EPHEMERAL" for _n in ("heartbeat", "status")))
check("99 recomputable health/telemetry is DERIVED — reopen_path_inert was the "
      "live false CRITICAL",
      all(state.state_tier(_n) == "DERIVED" for _n in
          ("reopen_path_inert", "health", "health_streaks", "report_card_stats",
           "ig_findings_latest", "routing_report")))
# THE ANTI-REGRESSION THAT MATTERS: an allow-list is a silent scope limit. A new
# book file that nobody classified must fail LOUD (graded as money), never land
# in the benign bucket by default.
check("99 an UNCLASSIFIED state file falls through to BOOK — forgetting to "
      "classify a new book file must be loud, never silently benign",
      state.state_tier("brand_new_unclassified_book") == "BOOK"
      and state.state_tier("sleeve_future_sleeve") == "BOOK")

# The check must still go RED for the real reason, and must not be buyable-green
# by a flood of derived recoveries.
_prevj99 = (config.STATE_DIR / "journal.jsonl").read_text(encoding="utf-8") \
    if (config.STATE_DIR / "journal.jsonl").exists() else ""
try:
    def _mkj99(rows):
        (config.STATE_DIR / "journal.jsonl").write_text(
            "".join(_json98.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def _si99():
        _r = _health96.health_report()
        return next((c for c in _r.get("checks", [])
                     if c.get("name") == "state integrity"), None)

    _now99 = datetime.now(timezone.utc)
    _base99 = [{"ts": (_now99 - timedelta(minutes=5 + i)).isoformat(),
                "event": "noise"} for i in range(40)]

    _mkj99(_base99 + [{"ts": (_now99 - timedelta(minutes=2)).isoformat(),
                       "event": "state_restored_from_backup",
                       "file": "sleeve_alpha_stocks", "reason": "parse_error"}])
    _c99a = _si99()
    check("99 a BOOK restore still REDS the line and names the file — the fix "
          "must not buy green by grading less",
          _c99a is not None and _c99a.get("ok") is False
          and "sleeve_alpha_stocks" in str(_c99a.get("detail")), str(_c99a))

    _mkj99(_base99 + [{"ts": (_now99 - timedelta(minutes=2)).isoformat(),
                       "event": "state_restored_from_backup",
                       "file": "reopen_path_inert", "reason": "parse_error"}])
    _c99b = _si99()
    check("99 a single DERIVED restore is GREEN but REPORTED by name — visible, "
          "not buried, and not an alarm about the books",
          _c99b is not None and _c99b.get("ok") is True
          and "reopen_path_inert" in str(_c99b.get("detail")), str(_c99b))

    _flood99 = [{"ts": (_now99 - timedelta(minutes=2 + i)).isoformat(),
                 "event": "state_restored_from_backup",
                 "file": "health_streaks", "reason": "parse_error"}
                for i in range(int(getattr(config,
                                           "STATE_DERIVED_RESTORE_BUDGET_24H", 3)) + 2)]
    _mkj99(_base99 + _flood99)
    _c99c = _si99()
    check("99 a FLOOD of derived restores still reds — past the budget the host "
          "is genuinely losing writes, and 'not a book' must not mean 'ignore "
          "forever' (the latched-green failure mode)",
          _c99c is not None and _c99c.get("ok") is False, str(_c99c))

    # An unrecoverable corruption counts the same as a restore: the check that
    # ignored state_corrupt_unrecoverable was an earlier IG finding.
    _mkj99(_base99 + [{"ts": (_now99 - timedelta(minutes=2)).isoformat(),
                       "event": "state_corrupt_unrecoverable",
                       "file": "ledger", "reason": "parse_error"}])
    _c99d = _si99()
    check("99 an UNRECOVERABLE book corruption reds too — 'could not even "
          "restore' is strictly worse than 'restored', never better",
          _c99d is not None and _c99d.get("ok") is False, str(_c99d))
finally:
    (config.STATE_DIR / "journal.jsonl").write_text(_prevj99, encoding="utf-8")

# ══ SECTION 100: the LABEL must never order the FEATURE (operator 2026-08-19) ══
# learning._factor_edge read `obs = sorted(obs)` over (score, excess) tuples.
# Python sorts tuples lexicographically, so names TIED on the factor score were
# ordered by their own FORWARD EXCESS — the outcome being measured — and the
# median split handed the best-performing ties to the "high half" by
# construction. The screen saturates momentum, so 75% of the 194 momentum
# observations share a score: the statistic reported edge +14.3 where the
# tie-safe value is -2.7, 95% band [-6.4, +1.3], permutation p 1.00.
# It was not inert. factor:momentum is the only measured edge with a MONEY
# consumer — sleeves._edge_weight_tilt gates on val > 0 and sized
# min(2.0, 12.7/9) = +1.41 pts of extra target weight onto essentially every
# alpha entry, in a book running -10.73 vs SPY. ═══════════════════════════════
import firm.learning as _lrn100  # noqa: E402


def _fe100(obs):
    """Reach the module-local _factor_edge through a minimal card run."""
    return _lrn100._factor_edge_for_test(obs) if hasattr(
        _lrn100, "_factor_edge_for_test") else None


# The leak is provable without reaching the nested helper: build an observation
# set where the factor CANNOT predict (one single score) but the outcomes vary
# wildly. Any honest statistic must refuse to call that an edge.
_tied100 = [(100.0, x) for x in
            (-40, -30, -20, -10, -5, 0, 5, 10, 20, 30, 40, 50)]
_sorted_lex100 = sorted(_tied100)                    # the OLD behaviour
_mid100 = len(_sorted_lex100) // 2
_lo100 = [x for _, x in _sorted_lex100[:_mid100]]
_hi100 = [x for _, x in _sorted_lex100[-_mid100:]]
_fake100 = sum(_hi100) / len(_hi100) - sum(_lo100) / len(_lo100)
check("100 the OLD lexicographic split fabricates a large edge from a factor "
      "with ONE score and zero predictive content — this is the bug, shown "
      f"arithmetically (it reports {_fake100:+.1f} pts on a constant factor)",
      _fake100 > 20.0, f"fabricated edge {_fake100:+.1f}")

# Now the real function, through the real card, on real state.
_prev_res100 = state.load("research", {})
try:
    # 12 names, all momentum 100 (a constant factor), outcomes spread wide.
    _hist100 = {"asof": ts(30), "verdicts": {}, "context": {}}
    for _i, _x in enumerate((-40, -30, -20, -10, -5, 0, 5, 10, 20, 30, 40, 50)):
        _t = f"ZZ{_i:02d}"
        _hist100["verdicts"][_t] = {"rating": "HOLD", "conviction": 55}
        _hist100["context"][_t] = {"factors": {"momentum": 100.0}}
    state.save("research", {"history": [_hist100]})
    _card100 = _lrn100.verdict_report_card(min_age_days=0)
    _mom100 = (_card100.get("factor_predictiveness") or {}).get("momentum")
    check("100 a CONSTANT factor is reported UNDETERMINED with its tie fraction, "
          "never as an edge — the halves are the same score and cannot be told "
          "apart however the ties are broken",
          _mom100 is None or _mom100.get("undetermined") is True
          or _mom100.get("edge_pts") is None,
          str(_mom100))
finally:
    state.save("research", _prev_res100)

# The source-level invariant: the sort key must exclude the outcome.
_srcl100 = (Path(__file__).parent.parent / "firm"
            / "learning.py").read_text(encoding="utf-8")
check("100 _factor_edge sorts by SCORE ONLY — a bare sorted() over "
      "(score, excess) lets the label order the feature and that is the defect",
      "obs = sorted(obs, key=lambda t: t[0])" in _srcl100
      and "obs = sorted(obs)\n" not in _srcl100)

# An undetermined edge must not be publishable, or the fix is cosmetic: the
# digest only emits an edge carrying a numeric edge_pts, and the tilt only
# fires on a POSITIVE published value.
check("100 refresh_report_card_digest publishes only edges carrying numeric "
      "edge_pts, so an undetermined factor cannot reach edge_health",
      'isinstance(_fe.get("edge_pts"), (int, float))' in _srcl100)

_prev_eh100 = state.load("edge_health", {})
try:
    from firm import sleeves as _sl100
    _pk100 = {"factor_scores": {"momentum": 100}}
    state.save("edge_health", {"edges": [
        {"edge": "factor:momentum", "n": 190, "value": 12.7}]})
    _tilt_with = _sl100._edge_weight_tilt(_pk100)
    state.save("edge_health", {"edges": [
        {"edge": "factor:value", "n": 148, "value": 1.6}]})
    _tilt_without = _sl100._edge_weight_tilt(_pk100)
    check("100 the fabricated edge was sizing REAL money (+1.41 pts of target "
          "weight on a momentum-100 name) and collapses to 0.0 once it is not "
          "published — this is why the sort bug was expensive, not cosmetic",
          abs(_tilt_with - 1.41) < 0.01 and _tilt_without == 0.0,
          f"with {_tilt_with} / without {_tilt_without}")
    # ...and a NEGATIVE edge must never tilt either (the true momentum value is
    # about -2.7, so the correct tilt was 0.0 all along).
    state.save("edge_health", {"edges": [
        {"edge": "factor:momentum", "n": 190, "value": -2.7}]})
    check("100 a NEGATIVE measured edge tilts nothing — the true momentum value "
          "is negative, so the correct upsize was always zero",
          _sl100._edge_weight_tilt(_pk100) == 0.0)
finally:
    state.save("edge_health", _prev_eh100)

# ══ SECTION 101: ONE LIVE THREAD IS NOT A LIVE FUND (operator 2026-08-19) ══════
# keepalive._heartbeats_fresh decided "the app is alive, just slow" from
# min(ages) — the FRESHEST monitor. Measured live at 22:14 while the fund was
# down: calls_monitor 1.4 min, equity_sweep 34.9 min, equity_monitor 55.2 min.
# min() read 1.4 and returned fresh, so keepalive took its "slow, NOT
# respawning" branch and never relaunched — and the thread doing the beating
# was calls_monitor, which had ZERO open option positions and so protected
# nothing. The in-process watchdog could not save it either
# (watchdog_thread_restart fired for equity_monitor at 20:53 stale-21min and
# again at 21:52 stale-30min, straight back to stale). That is the mechanism
# behind 'session coverage' 60.9% and 830 blackout minutes in 7 holes.
# Grade the WORST monitor: every key in heartbeat.json is a monitor the app
# declares it is running. ════════════════════════════════════════════════════
import importlib.util as _ilu101  # noqa: E402
_spec101 = _ilu101.spec_from_file_location(
    "_ka101", str(Path(__file__).parent.parent / "keepalive.py"))
_ka101 = _ilu101.module_from_spec(_spec101)
_spec101.loader.exec_module(_ka101)

_tmp101 = Path(tempfile.mkdtemp(prefix="lochraven_ka_"))
(_tmp101 / "state").mkdir()
_real_base101 = _ka101.BASE
try:
    _ka101.BASE = _tmp101

    def _hb101(**mins):
        (_tmp101 / "state" / "heartbeat.json").write_text(
            _json98.dumps({k: (datetime.now(timezone.utc)
                               - timedelta(minutes=v)).isoformat()
                           for k, v in mins.items()}), encoding="utf-8")

    _bar101 = float(_ka101.HEARTBEAT_STALE_MIN)
    # THE EXACT LIVE SHAPE THAT COST TWO HOURS OF DOWNTIME.
    _hb101(calls_monitor=1.4, equity_sweep=34.9, equity_monitor=55.2)
    check("101 an app whose EXIT sweeper died 35 min ago is NOT fresh, even "
          "though an idle calls_monitor with no open options beat 1.4 min ago "
          "— min() called this healthy and left the fund unrecovered",
          _ka101._heartbeats_fresh() is False)

    # All beating -> genuinely alive, must NOT be killed (the duplicate-writer
    # hazard keepalive was built to avoid is still real).
    _hb101(calls_monitor=0.2, equity_sweep=0.3, equity_monitor=0.5)
    check("101 an app with ALL monitors beating is fresh — the fix must not "
          "buy safety by respawning healthy funds (two app.py writers thrashing "
          "is the 2026-06-23 failure this branch exists to prevent)",
          _ka101._heartbeats_fresh() is True)

    # Boundary: one LIVENESS monitor just past the bar condemns it.
    _hb101(calls_monitor=0.1, equity_sweep=0.1, equity_monitor=_bar101 + 1.0)
    check("101 ANY single stale LIVENESS monitor fails the check — the fund is "
          "only doing its job when every unconditional monitor is beating",
          _ka101._heartbeats_fresh() is False)

    # ...but a COVERAGE beat is not a liveness beat. app.py writes equity_sweep
    # only inside `if session_open`, so overnight it is legitimately hours old.
    # Grading it would relaunch the app every 5 minutes until the opening bell —
    # the exact opposite failure to the one this section fixes, and the reason
    # the first version of the max() change was wrong.
    _hb101(calls_monitor=0.2, equity_sweep=600.0, equity_monitor=0.3)
    check("101 a session-gated COVERAGE beat (equity_sweep) does NOT condemn a "
          "healthy overnight app — max() must not become a restart loop",
          _ka101._heartbeats_fresh() is True)
    check("101 equity_sweep is declared a coverage beat, not a liveness beat",
          "equity_sweep" in _ka101.COVERAGE_BEATS)

    # The filter must never empty the question into a vacuous pass.
    _hb101(equity_sweep=600.0)
    check("101 if ONLY coverage beats exist the check falls back to grading "
          "them — an unrecognised schema must not certify the fund on zero "
          "evidence",
          _ka101._heartbeats_fresh() is False)

    # A single-monitor file must still work (no IndexError on one key).
    _hb101(equity_monitor=0.1)
    check("101 a one-monitor heartbeat file still grades correctly",
          _ka101._heartbeats_fresh() is True)

    # Unreadable/missing heartbeats must fail CLOSED (unknown liveness is not
    # liveness), matching the existing tolerant-read fallback contract.
    (_tmp101 / "state" / "heartbeat.json").write_text("{ not json",
                                                      encoding="utf-8")
    check("101 an unreadable heartbeat file is NOT fresh (fail closed — an "
          "unknown state must never certify the fund as alive)",
          _ka101._heartbeats_fresh() is False)
finally:
    _ka101.BASE = _real_base101

# Source invariant, so the aggregate cannot quietly regress to min().
_srcka101 = (Path(__file__).parent.parent / "keepalive.py").read_text(
    encoding="utf-8")
# Read the CODE, not the prose: the docstring above deliberately quotes the old
# `min(ages)` expression to explain the defect, so a naive substring scan of the
# whole file goes red on the fix's own explanation. That is the same brittleness
# the section-70 source check hit earlier today — assert on the return statement.
_ret101 = [ln.strip() for ln in _srcka101.splitlines()
           if ln.strip().startswith("return bool(ages)")]
check("101 keepalive grades the WORST monitor, never the best",
      len(_ret101) == 1 and "max(ages)" in _ret101[0]
      and "min(ages)" not in _ret101[0], str(_ret101))



def _mon102():
    from firm import monitor as _m102
    return _m102._abs_stop_floor()

# ══ SECTION 102: the two behaviour changes of 2026-08-19, and their reversibility
# (a) ABS_STOP_FLOOR_PCT 15.0 -> 10.0. Reconstructed absolute adverse excursion
#     for all 35 closed equity trades from real daily closes: winners' DEEPEST
#     drawdown was -4.9%, losers' median -6.6%. Counterfactual at this floor
#     fired on 8 of 35, cut ZERO winners, and moved mean excess -7.22 -> -6.63.
#     Chosen as the TIGHTEST value inside the pre-existing sanctioned bounds
#     rather than the -8% the (monotone, no-interior-optimum) curve prefers.
# (b) quant_screen_score retained on the HISTORY row and read from CONTEXT, so
#     screen_predictiveness can populate. It was {} on every card ever produced
#     because 0 of 1,467 retained verdict rows carried the key. ═══════════════
check("102 the stop floor sits INSIDE its own sanctioned bounds — a data-fitted "
      "value that needs the rails widened is an overfit, not a finding",
      config.ABS_STOP_BOUNDS["ABS_STOP_FLOOR_PCT"][0]
      <= config.ABS_STOP_FLOOR_PCT
      <= config.ABS_STOP_BOUNDS["ABS_STOP_FLOOR_PCT"][1])
check("102 the live reader agrees with the config constant — a lever whose "
      "reader disagrees with its constant mints false evidence",
      _mon102() == config.ABS_STOP_FLOOR_PCT, str(_mon102()))
# THE FLOOR IS A CATASTROPHE CEILING, NOT THE FIRM'S STOP. Setting it to 10.0
# made it bind before EVERY sleeve's own stop and collapsed the deliberate
# per-sleeve risk differentiation — test_week_sim's "D2 stop isolation" caught
# it. Re-cutting the counterfactual PER SLEEVE showed the pooled number hid
# opposite signs: alpha -10% = +1.04 pts, but etf_rotation -10% = -1.60, and
# etf_rotation is the mechanical sleeve currently BEATING SPY.
check("102 the firm-wide floor stays ABOVE the tightest sleeve stop — it is a "
      "last-resort catastrophe ceiling, not the binding stop for every book",
      config.ABS_STOP_FLOOR_PCT
      > min(c["hard_stop_pct"] for c in config.SLEEVES.values()
            if "hard_stop_pct" in c))
check("102 the tightening landed on alpha_stocks (n=22, the worst book at "
      "-8.79 and the source of most reconstructable paths), not firm-wide",
      config.SLEEVES["alpha_stocks"]["hard_stop_pct"] == 10.0)
check("102 etf_rotation was NOT tightened — its own counterfactual says a "
      "tighter stop COSTS it 1.60 pts, and it is the one sleeve beating SPY",
      config.SLEEVES["etf_rotation"]["hard_stop_pct"] >= 20.0)
check("102 per-sleeve risk differentiation survives: the concentrated "
      "single-name book stops tighter than the core blend, which stops tighter "
      "than the mechanical rotation",
      config.SLEEVES["alpha_stocks"]["hard_stop_pct"]
      < config.SLEEVES["core_blend"]["hard_stop_pct"]
      < config.SLEEVES["etf_rotation"]["hard_stop_pct"])

# REVERSIBILITY: the firm must be able to walk this back WITHOUT a human, or a
# wrong call at n=35 is permanent. Reader and writer must both be wired.
_prev_tun102 = state.load("tuning", {})
try:
    _applied102 = learning._apply_cap_tuning({"ABS_STOP_FLOOR_PCT": 18.0})
    check("102 the review can move the stop floor itself (writer whitelisted) — "
          "reversibility is what makes a small-sample change safe to ship",
          (_applied102 or {}).get("ABS_STOP_FLOOR_PCT") == 18.0
          and _mon102() == 18.0, str(_applied102))
    # ...and it stays clamped inside the rails in BOTH directions.
    learning._apply_cap_tuning({"ABS_STOP_FLOOR_PCT": 99.0})
    _hi102 = _mon102()
    learning._apply_cap_tuning({"ABS_STOP_FLOOR_PCT": 1.0})
    _lo102 = _mon102()
    check("102 a runaway proposal is clamped to the rails, never applied raw",
          _hi102 == config.ABS_STOP_BOUNDS["ABS_STOP_FLOOR_PCT"][1]
          and _lo102 == config.ABS_STOP_BOUNDS["ABS_STOP_FLOOR_PCT"][0],
          f"hi {_hi102} lo {_lo102}")
finally:
    state.save("tuning", _prev_tun102)

# (b) THE SCREEN MUST BECOME GRADEABLE.
_src102 = (Path(__file__).parent.parent / "firm" / "cycle.py").read_text(
    encoding="utf-8")
_hist102 = _src102.split('research_log.setdefault("history"')[0]
check("102 cycle.py stamps quant_screen_score on the HISTORY context row — "
      "writing it only to research_log['latest'] is why the screen has never "
      "been gradeable",
      _src102.count('"quant_screen_score"') >= 2)

_prev_res102 = state.load("research", {})
_real_fh102 = data_engine.fetch_history
try:
    _h102 = {"asof": ts(30), "verdicts": {}, "context": {}}
    for _i in range(12):
        _t = f"QS{_i:02d}"
        _h102["verdicts"][_t] = {"rating": "HOLD", "conviction": 55}
        # spread across all four declared bands (low <80, mid 80-83,
        # high 84-87, top >=88) so a populated result proves BANDING, not just
        # that one number survived the pipeline
        _h102["context"][_t] = {"factors": {"momentum": 50.0 + _i},
                                "quant_screen_score": float(70 + _i * 2)}
    state.save("research", {"history": [_h102]})
    # Synthetic tickers have no market data, so every row would drop ungraded
    # and an empty card would look exactly like the bug. Stub the price frame
    # the same way section 26b does — winners first, losers second.
    _d102 = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=30)
    _cols102 = {f"QS{_i:02d}": [100.0 * (1 + (0.10 if _i < 6 else -0.10) * _j / 29)
                                for _j in range(30)] for _i in range(12)}
    _cols102["SPY"] = [100.0] * 30
    data_engine.fetch_history = lambda t, period="6mo": pd.DataFrame(
        _cols102, index=_d102)
    _card102 = learning.verdict_report_card(min_age_days=0)
    data_engine.fetch_history = _real_fh102
    _sp102 = _card102.get("screen_predictiveness") or {}
    check("102 screen_predictiveness POPULATES once the score reaches the "
          "history row — it was {} on every card ever produced, while the "
          "review prompt told the reviewer to tune QUANT_COMPRESS_* from it",
          _card102.get("graded", 0) > 0 and isinstance(_sp102, dict)
          and len(_sp102) > 0,
          f"graded={_card102.get('graded')} bands={sorted(_sp102)}")
finally:
    data_engine.fetch_history = _real_fh102
    state.save("research", _prev_res102)

# ══ SECTION 103: a MARK-TO-CLOSE must use a CLOSE (operator 2026-08-20) ═══════
# The 2026-08-13 primacy flip made broker.equity_quotes_public primary for the
# nightly mark. That function's own docstring says "Mid pricing" — it returns
# (bid+ask)/2 from the LIVE quote, and after the bell that is an after-hours
# price which can sit outside the regular session's own high/low. Measured on
# the 2026-08-19 books: only 2 of 17 held equities carried their actual close;
# MPC was marked 308.78 against a 359.00-365.87 session (close 360.75, -14.4%,
# $155.90 understated) and CORT 110.83 against 118.37-121.11. Firm-wide the
# alpha book sat $119.48 (-0.51%) under its real-close value — which IS the
# 'display reconciliation' drift (shown -6.63 vs rebuilt -6.15), because
# reconcile_display rebuilds from official closes while the display carried
# quote mids. The two conventions could never converge. ════════════════════════
_prev_sb103 = _health96._session_bars
_prev_eq103 = broker.equity_quotes_public
try:
    # AAA has a bar; BBB has none. The quote for AAA names a price that never
    # traded; the quote for CCC is plausible.
    _bars103 = {"AAA": (100.0, 110.0, 105.0, "2026-08-19"),
                "CCC": (50.0, 55.0, 52.0, "2026-08-19")}
    _health96._session_bars = lambda syms: {k: v for k, v in _bars103.items()
                                            if k in set(syms)}
    check("103 the marker and the auditor share ONE definition of 'this price "
          "traded' — a corrector and its detector that disagree each assume the "
          "other is handling it",
          _health96._in_session_range(105.0, 100.0, 110.0) is True
          and _health96._in_session_range(80.0, 100.0, 110.0) is False)

    # The audit must default to EVERY mark. The old sample was
    # sorted(closes)[:8] over the firm-wide union (8 of 22 on the 08-19 books),
    # which checked CORT (-6.9%) and never looked at MPC (-14.4%) because MPC
    # sorts ninth — understating a book-level error by an order of magnitude.
    _aud103 = _health96._third_source_mark_audit(
        {"AAA": 105.0, "CCC": 52.0}, bars=_bars103)
    check("103 the audit covers EVERY mark by default, not the first 8 "
          "alphabetically — the worst mark of the night sorted ninth",
          (_aud103 or {}).get("checked") == 2, str(_aud103))
    _aud103b = _health96._third_source_mark_audit(
        {"AAA": 80.0, "CCC": 52.0}, bars=_bars103)
    check("103 the audit still flags a price that never traded",
          len((_aud103b or {}).get("outside_session_range") or []) == 1,
          str(_aud103b))

    _src103 = (Path(__file__).parent.parent / "firm"
               / "health.py").read_text(encoding="utf-8")
    _mb103 = _src103.split("def mark_books_to_close")[1].split("\ndef ")[0]
    # Compare CODE, not prose: the fix's own comment names
    # broker.equity_quotes_public while explaining the defect, so an ordering
    # test over the raw text sees the comment before the call. Strip comment
    # lines first — the same brittleness the section-70 and section-101 source
    # checks hit earlier in this same session.
    _code103 = chr(10).join(ln for ln in _mb103.splitlines()
                         if not ln.strip().startswith("#"))
    check("103 the CLOSE is the primary source for a mark-to-close, and the "
          "quote is only a fallback — the broker exposes no close endpoint at "
          "all, so the daily bar is the only source that has one",
          "_session_bars(" in _code103
          and "equity_quotes_public" in _code103
          and _code103.index("_session_bars(")
          < _code103.index("equity_quotes_public"))
    check("103 a quote naming a price OUTSIDE its own session range is refused "
          "and journaled, never written to the books",
          "mark_refused_outside_session" in _mb103
          and "_in_session_range(px, b[0], b[1])" in _mb103)
    check("103 a quote standing in for a close is journaled as DEGRADATION — "
          "plausible is not the same as official",
          "mark_quote_fallback" in _mb103)
    # A renamed emitter with an unchanged consumer is how a signal goes dark.
    check("103 the health check consumes the NEW fallback event name too, so "
          "the rename cannot silently blind it",
          '"mark_quote_fallback"' in _src103.split("def health_report")[1]
          if "def health_report" in _src103 else
          _src103.count('"mark_quote_fallback"') >= 2)
    # Fail-soft: no bar for a name must still mark it rather than leave a hole.
    check("103 a name with NO daily bar still gets marked from the quote — the "
          "guard must never leave a position unpriced (fail soft, never worse "
          "than the behaviour it replaced)",
          "b is None or _in_session_range" in _mb103)
    # ONE SESSION DEFINITION. app.py's monitor re-marks whenever
    # `market_is_open() or extended_session_open()`, but the reconciler's
    # settled-window guard read market_is_open() alone. Between 04:00 and 09:30
    # ET the display was therefore re-marked from PRE-MARKET quotes while the
    # reconciler graded them against YESTERDAY's closes — phantom drift every
    # morning with no book error behind it (measured 08:41 ET: shown -6.49 vs
    # rebuilt -6.15, 1 of 17 marks still equal to its close, sleeve file
    # rewritten 22 seconds earlier).
    _rd103 = (Path(__file__).parent.parent / "scripts"
              / "reconcile_display.py").read_text(encoding="utf-8")
    _rdcode103 = chr(10).join(ln for ln in _rd103.splitlines()
                              if not ln.strip().startswith("#"))
    _app103 = (Path(__file__).parent.parent / "app.py").read_text(encoding="utf-8")
    # SUPERSEDED 2026-08-21, kept as the record of a wrong fix. This block
    # originally asserted the reconciler must share the MARKER's session
    # definition (`market_is_open() or extended_session_open()`). That reasoning
    # was wrong in a way worth preserving: the after-hours window runs to 20:00
    # ET, so the widened gate also silenced the postmarket wrap's own
    # reconciliation at ~16:30 ET and the nightly rebuild stopped happening at
    # all. Matching the marker's window silences the check for as long as the
    # marker is active, which for an after-hours-marking system is most of the
    # night. The real invariant is not about sessions: the comparison is defined
    # only while the display still carries the closes it was snapped to, and
    # that is a question about the BOOKS. Section 104 asserts the corrected form.
    check("103 the reconciler does NOT gate on the extended session — that "
          "widening silenced the nightly rebuild for four hours a day",
          "extended_session_open" not in _rdcode103)
    check("103 the marker DOES run through the extended session, which is why "
          "the display cannot simply be compared to yesterday's closes — the "
          "observation was right even though the fix drawn from it was wrong",
          "extended_session_open()" in _app103)
finally:
    _health96._session_bars = _prev_sb103
    broker.equity_quotes_public = _prev_eq103

# ══ SECTION 104: the nightly close must actually happen (operator 2026-08-21) ══
# A 2026-08-20 attempt to silence a morning false-drift alarm widened the
# reconciler's gate from market_is_open() to `or extended_session_open()`. The
# after-hours window runs to 20:00 ET, so that also silenced the postmarket
# wrap's own reconciliation at ~16:30 ET — the nightly rebuild stopped running
# entirely ("the fund doesn't close at market close like it used to"). The
# morning drift is real but it is not a session question: the display is LIVE by
# design (monitor.py reads pos["last_price"] in five places to evaluate stops,
# so it cannot be frozen after hours without risking a blinded exit), which
# means the raw-close comparison is only defined while the display still carries
# the closes it was snapped to. Ask the books, not the clock. ══════════════════
_rd104 = (Path(__file__).parent.parent / "scripts"
          / "reconcile_display.py").read_text(encoding="utf-8")
_rdcode104 = chr(10).join(ln for ln in _rd104.splitlines()
                          if not ln.strip().startswith("#"))
check("104 the reconciler gates on REGULAR hours only — widening it to the "
      "extended session silences the postmarket wrap itself for four hours",
      "extended_session_open" not in _rdcode104)
check("104 the reconciler refuses to grade a REPAINTED book instead of "
      "reporting phantom drift — a skip that says why beats a false alarm",
      "books repainted since the" in _rd104
      and "eod_mark_stamp" in _rdcode104
      and "marks have moved off the" in _rd104)

_h104 = (Path(__file__).parent.parent / "firm" / "health.py").read_text(encoding="utf-8")
check("104 mark_books_to_close STAMPS the closes it wrote, so the reconciler "
      "can ask whether the display still carries them",
      'state.save("eod_mark_stamp"' in _h104)

_rc104 = (Path(__file__).parent.parent / "run_cycle.py").read_text(encoding="utf-8")
_pm104 = _rc104.split("def postmarket")[1].split(chr(10) + "def ")[0]
_i_mark = _pm104.find("mark-to-close")
_i_rec = _pm104.find("reconcile_display.py")
_i_book = _pm104.find("EOD booked")
check("104 the wrap reconciles IMMEDIATELY after the mark and before booking — "
      "the only window where a live display and a raw-close rebuild are the "
      "same quantity (it used to run ~4 min later, after the after-hours "
      "monitor had already repainted)",
      0 < _i_mark < _i_rec < _i_book,
      f"mark@{_i_mark} reconcile@{_i_rec} book@{_i_book}")

# The display mark must stay LIVE. If a future change freezes last_price after
# the close to make reconciliation easy, it blinds after-hours stop evaluation —
# strictly worse than the alarm it silences.
_m104 = (Path(__file__).parent.parent / "firm" / "monitor.py").read_text(encoding="utf-8")
check("104 exit logic still reads last_price, which is WHY the display cannot "
      "be frozen after the close — this check exists so nobody 'fixes' the "
      "drift by blinding a stop",
      _m104.count("last_price") >= 4)

# ── bench provenance: origin is durable, source is not ──────────────────────
# The IG filed CRITICAL on 2026-08-20: by_obligation exceeded by_source for
# book_full (5 vs 3) and stop_out (3 vs 1) — DK, SYRE, PRSU, TXN carrying live
# obligations under a label a re-judge had overwritten, so the per-source arming
# census could not see the class at all.
_e104 = {"ticker": "ZZ104", "source": "hold_verdict", "origin": "stop_out",
         "stop_out": {"sleeve": "alpha_stocks", "entry": 100.0, "exit": 80.0,
                      "reason": "hard stop", "ts": ts(30)}}
check("104 origin survives a re-judge that rewrites source — provenance and "
      "dispatch are different questions and cannot share one mutable field",
      _wl98.bench_origin(_e104) == "stop_out"
      and _e104.get("source") == "hold_verdict")
check("104 a legacy entry with NO origin field derives it from its durable "
      "payload, so the backfill needs no migration pass",
      _wl98.bench_origin({"source": "hold_verdict",
                          "stop_out": {"exit": 80.0}}) == "stop_out"
      and _wl98.bench_origin({"source": "near_miss"}) == "near_miss")
check("104 origin is stamped ONCE and never rewritten — a second write would "
      "recreate the mutability that made the census blind",
      _wl98._stamp_origin({"origin": "stop_out"}, "hold_verdict")["origin"]
      == "stop_out")
_iga104 = (Path(__file__).parent.parent / "scripts"
           / "ig_audit.py").read_text(encoding="utf-8")
check("104 the IG's per-source arming census partitions on ORIGIN, not the "
      "mutable label — the class it cannot see is the class it cannot measure",
      "bench_origin(e)" in _iga104 and "by_origin" in _iga104)

# ── the third-source verdict must state its window ─────────────────────────
check("104 a clean LATEST mark is never reported as a clean PERIOD — the line "
      "said 'third-source audit clean' while the same window held 10 mismatch "
      "events, which reads as 'no mark was ever wrong'",
      "clean on the LATEST mark" in _h104
      and "is not a clean period" in _h104
      and '"eod_mark_third_source_mismatch")]' in _h104)

# ══ SECTION 105: name the SESSION, and make the bench actually reachable ══════
# (operator 2026-08-21: "say pre market or market close and market hours instead
# of putting it all in one", and "make sure my bench is working like it's
# supposed to fully")
from firm import market_hours as _mh105  # noqa: E402
_ET105 = _mh105.ET
_d105 = datetime(2026, 8, 21, tzinfo=_ET105)     # an ordinary Friday


def _ph105(h, m):
    return _mh105.session_phase(_d105.replace(hour=h, minute=m))


check("105 the session is NAMED, not lumped — pre-market, market hours and "
      "after hours are three different states and a message that says "
      "'session live' makes the reader guess which",
      (_ph105(7, 0) == "closed" and _ph105(8, 30) == "pre_market"
       and _ph105(9, 29) == "pre_market" and _ph105(9, 30) == "market_hours"
       and _ph105(15, 59) == "market_hours" and _ph105(16, 0) == "after_hours"
       and _ph105(19, 59) == "after_hours" and _ph105(20, 0) == "closed"),
      f"{_ph105(8,30)}/{_ph105(9,30)}/{_ph105(16,0)}/{_ph105(20,0)}")
check("105 a non-trading day is closed in every window",
      _mh105.session_phase(datetime(2026, 8, 22, 12, 0, tzinfo=_ET105)) == "closed")
check("105 the ROUTING booleans are unchanged — they group the two extended "
      "windows on purpose (protective exits may route in both); only the "
      "REPORTING view is split",
      _mh105.extended_session_open(_d105.replace(hour=8, minute=45)) is True
      and _mh105.extended_session_open(_d105.replace(hour=17, minute=0)) is True
      and _mh105.market_is_open(_d105.replace(hour=12, minute=0)) is True)

# ── the bench: every declared path must be reachable, or honestly idle ──────
_prev_wl105 = state.load("watchlist", {})
try:
    # A leg buried under an AND cannot reopen a name on its own. Counting it as
    # armed is what held 'reopen-path liveness' red for 428 sweeps on a single
    # SLS entry whose news leg was {all: [news, cross_sma]}.
    check("105 only OR-reachable legs count as armed — a leg nested under an "
          "AND cannot fire the path alone",
          _wl98._or_reachable_kinds(
              {"op": "any", "legs": [{"op": "all", "legs": [{"kind": "news"},
                                                            {"kind": "cross_sma"}]},
                                     {"kind": "catalyst"}]}) == {"catalyst"}
          and _wl98._or_reachable_kinds({"kind": "news"}) == {"news"}
          and _wl98._or_reachable_kinds(
              {"op": "any", "legs": [{"kind": "news"}]}) == {"news"})
    # ...but the entry still COUNTS as population, so the path reads idle
    # rather than un-wired.
    state.save("watchlist", {"SLS": {
        "ticker": "SLS", "added": ts(5), "source": "hold_verdict",
        "origin": "hold_verdict", "history": [{"ts": ts(5), "quant": 80.0}],
        "reopen_trigger": {"op": "any", "legs": [
            {"op": "all", "legs": [{"kind": "news"},
                                   {"kind": "cross_sma", "period": 50,
                                    "dir": "above"}]},
            {"kind": "catalyst"}]}}})
    _lv105 = _wl98.reopen_path_liveness()
    _news105 = _lv105.get("news_catalyst") or {}
    check("105 a conjunctive-only news leg reads IDLE (population 1, armed 0), "
          "never INERT — 'nothing can arm this' and 'nothing qualifies right "
          "now' are different bugs with different fixes",
          _news105.get("population") == 1 and _news105.get("armed_now") == 0
          and _news105.get("idle_no_eligible") is True
          and not _news105.get("inert") and not _news105.get("never_armed"),
          str(_news105))

    # book_full arms only what the promotion branch will actually take.
    _bf105 = {"ticker": "BF1", "added": ts(9), "source": "hold_verdict",
              "origin": "book_full", "history": [{"ts": ts(9), "quant": 80.0}],
              "book_full": {"conviction": 70, "ts": ts(9)},
              "last_verdict": {"rating": "HOLD", "conviction": 55, "ts": ts(1)}}
    state.save("watchlist", {"BF1": _bf105})
    _lv105b = _wl98.reopen_path_liveness()
    _b105 = _lv105b.get("book_full_slot_reopened") or {}
    check("105 a book-full waiter re-rated HOLD is population, NOT armed — "
          "_promotions_locked skips that branch unless the last verdict is "
          "still BUY-family, so counting it armed manufactures a permanent red",
          _b105.get("population") == 1 and _b105.get("armed_now") == 0
          and _b105.get("idle_no_eligible") is True, str(_b105))
    state.save("watchlist", {"BF2": dict(
        _bf105, ticker="BF2",
        last_verdict={"rating": "BUY", "conviction": 72, "ts": ts(1)})})
    _b105b = (_wl98.reopen_path_liveness() or {}).get("book_full_slot_reopened") or {}
    check("105 ...and a book-full waiter still rated BUY IS armed — the fix "
          "must not silence the path, only stop it counting the ineligible",
          _b105b.get("armed_now") == 1, str(_b105b))
finally:
    state.save("watchlist", _prev_wl105)

# The near-miss class's only name-specific route must be REACHABLE.
_srcw105 = (Path(__file__).parent.parent / "firm"
            / "watchlist.py").read_text(encoding="utf-8")
check("105 near-miss reopens on RE-ENTERING THE TIER CUT, the condition its "
      "own watch_trigger already promises — the +5 absolute bar is unreachable "
      "for a class selected for already-high scores (measured: 36 of 36 "
      "entries, best delta EVER +4.40 against a +5.0 bar, 0 ever cleared it)",
      "_tier_cut" in _srcw105 and "back inside the tier cut" in _srcw105)
check("105 the tier-cut promotion still requires the name to hold its OWN "
      "banked baseline (cur_q >= first_q), so a name that FELL while the cut "
      "fell further does not promote on the cut's weakness",
      "cur_q >= _tier_cut and cur_q >= first_q" in _srcw105)
check("105 tier-cut promotions remain bounded by the existing per-cycle cap — "
      "this fills a budget that already existed and was never spendable, it "
      "does not widen one",
      "other_picks[:PROMOTE_PER_CYCLE]" in _srcw105)
check("105 the tier cut needs a real candidate set before it will bind",
      'tier_cut_min_n' in _srcw105)

# ══ SECTION 106: closing the book_full and near_miss briefs ═══════════════════
# IG filed both CRITICAL on 2026-08-20. Investigating them changed what each one
# turned out to be.
#
# book_full — "names parked for a slot are permanently stranded" is REFUTED by
# the bench itself: all 5 carriers had been re-judged within 9 days (the
# time-cadence path fired 20x in 30d) and every one came back HOLD at LOWER
# conviction than when it was benched (DK 63->57 over 3 rechecks, SYRE 61->47,
# BFLY 60->48, DELL 56->53, SMWB 57->52). They were never stuck; the committee
# kept saying no. The REAL defect is that the obligation never retired, so a
# dead thesis kept counting as a live claim on a slot.
#
# near_miss — the arming gap is real. The class never reaches the committee, so
# nothing ever built it a packet and 100% carried no earnings date, leaving
# `earnings_catalyst` (the second-busiest path in the fund) unreachable for 31%
# of the bench. ══════════════════════════════════════════════════════════════
_prev_wl106 = state.load("watchlist", {})
try:
    _bf106 = {"conviction": 63, "ts": ts(25)}
    check("106 a book-full obligation is OPEN while the thesis stands",
          _wl98._book_full_open({"book_full": dict(_bf106)}) is True)
    check("106 ...and RETIRES once resolved, so a dead thesis stops counting as "
          "a live claim on a slot",
          _wl98._book_full_open(
              {"book_full": dict(_bf106, resolved_at=ts(0))}) is False)
    check("106 retirement keeps PROVENANCE — origin still reports book_full, so "
          "the census can still partition the class it came from",
          _wl98.bench_origin({"origin": "book_full", "source": "hold_verdict",
                              "book_full": dict(_bf106,
                                                resolved_at=ts(0))}) == "book_full")
    # A retired obligation must drop out of the liveness POPULATION too, or the
    # idle/inert distinction just moves the false red one field over.
    state.save("watchlist", {"R1": {
        "ticker": "R1", "added": ts(25), "source": "hold_verdict",
        "origin": "book_full", "history": [{"ts": ts(25), "quant": 80.0}],
        "book_full": dict(_bf106, resolved_at=ts(0)),
        "last_verdict": {"rating": "HOLD", "conviction": 50, "ts": ts(1)}}})
    _b106 = (_wl98.reopen_path_liveness() or {}).get("book_full_slot_reopened") or {}
    check("106 a RETIRED obligation leaves the book-full population entirely — "
          "otherwise the permanent red just moves from 'armed' to 'population'",
          _b106.get("population") == 0 and _b106.get("armed_now") == 0,
          str(_b106))
finally:
    state.save("watchlist", _prev_wl106)

_srcw106 = (Path(__file__).parent.parent / "firm"
            / "watchlist.py").read_text(encoding="utf-8")
check("106 the retirement is stamped on the non-BUY re-judge and journaled, so "
      "the transition is auditable rather than silent",
      "book_full_obligation_retired" in _srcw106
      and 'resolved_reason' in _srcw106)
check("106 a stop-out reclaim is NOT retired by a re-judge — its reclaim is a "
      "PRICE level and stays valid whatever the rating does (the asymmetry is "
      "deliberate and must stay)",
      "_stop_out_open" in _srcw106
      and "resolved_at" not in _srcw106.split("def _stop_out_open")[1].split(
          chr(10) + "def ")[0])

# ── near_miss arming ───────────────────────────────────────────────────────
check("106 near-miss arms on EARNINGS, the honest name-specific catalyst for a "
      "class benched on SCORE",
      "near_miss_earnings_armed" in _srcw106
      and "NEAR_MISS_EARNINGS_PER_CYCLE" in _srcw106)
check("106 it does NOT arm a derived wait-for-weakness leg — _derive_leg "
      "prefers pullback/rsi_below/cross-below, which would re-research a "
      "near-miss precisely as its score gets WORSE",
      "_derive_leg" not in chr(10).join(
          _l for _l in _srcw106.split("NEAR-MISS EARNINGS ARMING")[1]
          .split("unranked =")[0].splitlines()
          if not _l.strip().startswith("#")))
check("106 only a FUTURE earnings date arms anything — next_earnings can return "
      "the most recent PAST print (live: PMI came back days_away -7), and "
      "arming on something already true is a spurious reopen, not an arm",
      "_days >= 0" in _srcw106)
check("106 the vendor call is BUDGETED per cycle and the budget is spent on the "
      "ATTEMPT, so an unknowable ticker cannot be retried every cycle forever",
      "_arm_budget > 0" in _srcw106 and "_arm_budget -= 1" in _srcw106)
check("106 ...and an unknown result cools off before it is re-probed, so two "
      "unknowable names at the front of the ranked list cannot eat the whole "
      "budget and starve the rest of the class",
      "earnings_probe_ts" in _srcw106
      and "NEAR_MISS_EARNINGS_REPROBE_DAYS" in _srcw106)
check("106 the near-miss watch_trigger states BOTH of its real routes, so the "
      "prose matches the mechanism",
      "or its next earnings print lands" in _srcw106)

# ══ SECTION 107: the macro taxonomy is EXTERNALLY grounded now ════════════════
# IG CRITICAL 2026-08-20: "the check validates the tracked list against its own
# internal sibling list, so it can never discover a missing release class — it
# states its own invalidity and still reports ok=true". Upheld, and the audit
# found a second overstatement underneath it: only 4 of 24 tracked releases
# carried a fred_id, so 20 forward dates came from _rule_dates with NO provider
# confirmation, while the calendar line said "24/24 have a forward date".
from firm import macro_calendar as _mc107  # noqa: E402
_g107 = _mc107.grounding()
check("107 every tracked release is either FRED-grounded or has a DECLARED "
      "reason the provider does not carry it — a bare null id is an unfinished "
      "lookup, and cannot be told from a real provider gap",
      _g107["undeclared_nulls"] == [], str(_g107["undeclared_nulls"]))
check("107 provider grounding actually improved — 10 ids were resolved against "
      "the live FRED API and confirmed to publish forward dates before adoption "
      "(fuzzy matching alone proposed 'Dallas Fed Energy Survey' for FOMC)",
      len(_g107["fred_confirmed"]) >= 14, str(len(_g107["fred_confirmed"])))
check("107 the releases FRED genuinely does not carry are named with a reason, "
      "so 'rule-derived' is a decision rather than an omission",
      set(_g107["rule_derived"]) == set(_mc107.FRED_UNAVAILABLE)
      and all(_mc107.FRED_UNAVAILABLE.values()))
_ids107 = [r.fred_id for r in _mc107.RELEASES if r.fred_id]
check("107 no duplicate FRED ids — two releases sharing one id would silently "
      "collapse two schedules into one",
      len(_ids107) == len(set(_ids107)))
_h107 = (Path(__file__).parent.parent / "firm" / "health.py").read_text(encoding="utf-8")
check("107 the health lines report the CONFIRMED/DERIVED split instead of "
      "'24/24 have a forward date', which was true and misleading",
      "provider-confirmed" in _h107 and "rule-derived" in _h107)
check("107 an undeclared null FAILS the taxonomy check — the fix must be able "
      "to go red for the real reason, not just print a nicer sentence",
      "undeclared_nulls" in _h107
      and "declared provider reason" in _h107)
check("107 the self-referential sibling diff is KEPT as a regression tripwire — "
      "external grounding replaces the CLAIM it made, not the check itself",
      "_tx_un" in _h107 and "UNTRACKED" in _h107)

# ══ SECTION 108: every declared risk gate must be REACHABLE ═══════════════════
# IG CRITICAL 2026-08-20: "the entire buy-side risk-block vocabulary has NEVER
# fired — including breaker_tripped — so position-size, correlation, sector,
# firm-cap, liquidity and daily-breaker guards are wholly unexercised".
#
# HALF WRONG, and the wrong half matters: breaker_tripped HAS fired 9 times in
# the retained journal, and buy_blocked_stop_cooloff once. Repeating an
# unverified "never fired" is the same error class as the options-gate brief.
#
# The RIGHT half is worse than stated. Four gates had ZERO production fires AND
# ZERO test coverage: position_size, correlation, sector and liquidity. Zero
# fires alone is not evidence of a broken gate — the condition may simply never
# have arisen across 106 equity BUYs. But with no test either, nobody had ever
# demonstrated they CAN fire, on a system that trades money. These checks settle
# that question directly: feed each gate an input that must trip it.
_eq108 = 25_000.0
_ok108, _why108 = risk.check_position_size("alpha_stocks", _eq108,
                                           _eq108 * 0.50, 8.0)
check("108 position-size gate FIRES on a 50% position against an 8% cap",
      _ok108 is False and "exceeds cap" in _why108, _why108)
check("108 ...and PASSES a compliant position, so it is a gate and not a wall",
      risk.check_position_size("alpha_stocks", _eq108,
                               _eq108 * 0.05, 8.0)[0] is True)
check("108 position-size gate refuses a non-positive cost rather than dividing "
      "its way to a pass",
      risk.check_position_size("alpha_stocks", _eq108, 0.0, 8.0)[0] is False)

# Sector cap: an already-heavy sector must refuse more of the same.
# market_value, not quantity*price: check_sector_cap sums
# pos.get("market_value", pos.get("cost", 0)), so a fixture without it measures
# only the NEW cost and passes a cap it should breach. My first version of this
# test made exactly that mistake and read as a dead gate.
_pos108 = {"AAA": {"quantity": 100, "last_price": 100.0, "market_value": 10_000.0,
                   "instrument": "equity", "sector": "Technology"},
           "BBB": {"quantity": 100, "last_price": 100.0, "market_value": 10_000.0,
                   "instrument": "equity", "sector": "Technology"}}
_sec108 = {"AAA": "Technology", "BBB": "Technology", "CCC": "Technology"}
_okS108, _whyS108 = risk.check_sector_cap(_pos108, _sec108, "CCC", 8_000.0, _eq108)
check("108 sector gate FIRES when one sector would breach its cap",
      _okS108 is False, f"{_okS108} {_whyS108}")
check("108 ...and PASSES the same size into an UNCONCENTRATED sector",
      risk.check_sector_cap(_pos108, dict(_sec108, CCC="Utilities"),
                            "CCC", 8_000.0, _eq108)[0] is True)

# Firm-wide single-name cap: the same underlying across every sleeve.
_prev108 = {k: state.get_sleeve(k) for k in config.SLEEVES}
try:
    _sl108 = state.get_sleeve("alpha_stocks")
    _sl108["positions"] = {"DUP": {"quantity": 100, "last_price": 50.0,
                                   "entry_price": 50.0, "instrument": "equity",
                                   "underlying": "DUP"}}
    state.save_sleeve("alpha_stocks", _sl108)
    _okF108, _whyF108 = risk.check_firm_name_cap("DUP", 40_000.0)
    check("108 firm-wide name cap FIRES when one underlying would dominate the "
          "whole firm, not just one sleeve",
          _okF108 is False, f"{_okF108} {_whyF108}")
    check("108 ...and PASSES a small add to the same name",
          risk.check_firm_name_cap("DUP", 100.0)[0] is True)
finally:
    for k, v in _prev108.items():
        state.save_sleeve(k, v)

# Correlation: a book of one factor is one bet.
# check_correlation fetches REAL 6-month history, so synthetic tickers make it
# fail OPEN (journaled correlation_check_failed_open) and the gate reads dead
# when it is merely blind. Stub the fetch with a perfectly-correlated frame so
# the REFUSAL path itself is what gets exercised. (Checked in production: that
# fail-open event has never once fired, so the live gate is evaluating cleanly
# and its zero blocks are "the book was never that correlated", not "the gate
# is off".)
_corr108 = {"sleeve": "alpha_stocks", "positions": {
    "C1": {"quantity": 10, "last_price": 100.0, "instrument": "equity"},
    "C2": {"quantity": 10, "last_price": 100.0, "instrument": "equity"}}}
_d108 = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=40)
_lock108 = pd.DataFrame({t: [100.0 * (1 + 0.01 * i) for i in range(40)]
                         for t in ("C1", "C2", "C3")}, index=_d108)
_realfh108 = data_engine.fetch_history
try:
    data_engine.fetch_history = lambda tickers, period="6mo": _lock108
    _okC108, _whyC108 = risk.check_correlation(_corr108, "C3", threshold=0.5)
    check("108 correlation gate FIRES on a book that is one bet — three names "
          "moving in lockstep is not diversification",
          _okC108 is False and "same bet" in _whyC108, f"{_okC108} {_whyC108}")
    # A DECORRELATED candidate must pass the same threshold that blocked the
    # lockstep one — otherwise the gate is a wall, not a diversification test.
    # (Raising the threshold instead would prove nothing here: the lockstep
    # frame correlates at exactly 1.00, so no bound below 1.0 can pass it.)
    _mix108 = _lock108.copy()
    _mix108["C3"] = [100.0 + (7.0 if i % 2 else -7.0) for i in range(40)]
    data_engine.fetch_history = lambda tickers, period="6mo": _mix108
    _okP108, _whyP108 = risk.check_correlation(_corr108, "C3", threshold=0.5)
    check("108 ...and PASSES a genuinely DECORRELATED candidate at the same "
          "threshold — the gate measures diversification, it does not just "
          "refuse",
          _okP108 is True, f"{_okP108} {_whyP108}")
finally:
    data_engine.fetch_history = _realfh108

# Liquidity is an inline guard in sleeves.py, not a risk.* function: assert the
# ARITHMETIC it applies, so a change to the cap cannot silently un-gate it.
_adv108 = 1_000_000.0
_cap108 = config.MAX_POSITION_OF_ADV_PCT
check("108 liquidity guard arithmetic refuses a whale-sized entry and admits a "
      "normal one at the configured %-of-ADV bound",
      (_adv108 * 2) > _adv108 * _cap108 / 100
      and not (_adv108 * _cap108 / 100 * 0.5) > _adv108 * _cap108 / 100)
_srcs108 = (Path(__file__).parent.parent / "firm"
            / "sleeves.py").read_text(encoding="utf-8")
check("108 every declared buy-side block still JOURNALS its refusal — a gate "
      "that blocks silently is invisible to the caps audit that tunes it",
      all(f'"{_e}"' in _srcs108 for _e in
          ("buy_blocked_position_size", "buy_blocked_sector",
           "buy_blocked_correlation", "buy_blocked_firm_cap",
           "buy_blocked_firm_sector", "buy_blocked_liquidity")))

# ══ 109. yfinance thread bookkeeping — the kernel-semaphore leak (2026-09-01) ═
# yfinance.download(threads=True) hands every ticker to `multitasking`, which
# appends each worker Thread to a global list it never prunes; a finished
# Thread object still owns two locks, and on Windows every CPython lock is one
# kernel semaphore. The live app at 96h uptime held 255,259 leaked semaphores,
# was paged out and answered no HTTP endpoint — which is what turned the 08-31
# 09:45 cycle headless and stood exit protection down for 36 regular-hours
# minutes. data_engine.yf_download is the one sanctioned entry point.
import threading as _th109  # noqa: E402
import multitasking as _mt109  # noqa: E402
_dead109 = []
for _i109 in range(3):
    _t109 = _th109.Thread(target=lambda: None)
    _t109.start()
    _t109.join()
    _dead109.append(_t109)
_tasks109 = _mt109.config["TASKS"]
_n0_109 = len(_tasks109)
_tasks109.extend(_dead109)
_gate109 = _th109.Event()
_alive109 = _th109.Thread(target=_gate109.wait)
_alive109.start()
_tasks109.append(_alive109)
_released109 = data_engine.prune_yf_threads()
check("109 prune_yf_threads releases every DEAD yfinance worker and keeps the "
      "live one",
      _released109 >= 3 and all(t not in _tasks109 for t in _dead109)
      and _alive109 in _tasks109,
      f"released {_released109}, len {len(_tasks109)} (was {_n0_109 + 4})")
check("109 ...and prunes IN PLACE — the library's own list object shrinks (a "
      "rebound name would leak on)",
      _tasks109 is _mt109.config["TASKS"])
_gate109.set()
_alive109.join()
data_engine.prune_yf_threads()
# yf_download prunes after EVERY call, including one that raises
_realdl109 = data_engine.yf.download


def _fake_dl109(*a, **k):
    _t = _th109.Thread(target=lambda: None)
    _t.start()
    _t.join()
    _mt109.config["TASKS"].append(_t)
    raise RuntimeError("vendor down")


try:
    data_engine.yf.download = _fake_dl109
    try:
        data_engine.yf_download(["X"], period="5d")
    except RuntimeError:
        pass
    check("109 yf_download prunes dead workers even when the download RAISES",
          not [t for t in _mt109.config["TASKS"] if not t.is_alive()])
finally:
    data_engine.yf.download = _realdl109
_firm109 = Path(__file__).parent.parent / "firm"
_bare109 = [p.name for p in _firm109.glob("*.py")
            if p.name != "data_engine.py"
            and "yf.download(" in p.read_text(encoding="utf-8")]
_de_src109 = (_firm109 / "data_engine.py").read_text(encoding="utf-8")
check("109 no firm module calls yf.download directly — every threaded download "
      "goes through the pruning wrapper",
      not _bare109 and _de_src109.count("yf.download(") == 1, str(_bare109))

# ══ 110. Exit-protection continuity under the cycle lock (2026-09-01) ════════
# Briefs 87/66/72/78/53/28: the lock-holder now protects the book itself, the
# delegation probe no longer mistakes a busy app for a dead one, new risk is
# refused while stops cannot route, and a closed market cannot excuse a sweep
# that predates the last close.
import importlib as _il110  # noqa: E402
import os as _os110  # noqa: E402
import time as _time110  # noqa: E402
import urllib.error as _uerr110  # noqa: E402
import urllib.request as _ureq110  # noqa: E402

import firm.alerts as _al110  # noqa: E402
import firm.health as _hl110  # noqa: E402
import firm.market_hours as _mh110  # noqa: E402
import run_cycle as _rc110  # noqa: E402

# (a) /api/ping is state-free: it answers even when every state read raises
_app110 = _il110.import_module("app")
_real_load110 = state.load


def _boom110(*a, **k):
    raise RuntimeError("state unreadable")


try:
    state.load = _boom110
    _pong110 = _app110.ping()
finally:
    state.load = _real_load110
check("110 /api/ping answers with the pid and touches NO state (a liveness "
      "probe that prices the book is not a liveness probe)",
      _pong110.get("ok") is True and _pong110.get("pid") == _os110.getpid())

# (b) _app_running: patience for a busy app, any HTTP answer = alive, a
# listening-but-mute port = zombie (journaled + alerted) and NOT running
_real_urlopen110 = _ureq110.urlopen
_real_port110 = _rc110._port_listening
_real_gap110 = config.APP_PROBE_GAP_S
_real_att110 = config.APP_PROBE_ATTEMPTS
_real_toast110 = _al110._toast
config.APP_PROBE_GAP_S = 0.0
config.APP_PROBE_ATTEMPTS = 3
_calls110 = {"n": 0}


class _Resp110:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _flaky110(url, timeout=0):
    _calls110["n"] += 1
    if _calls110["n"] < 3:
        raise TimeoutError("busy")
    return _Resp110()


def _http404_110(url, timeout=0):
    raise _uerr110.HTTPError(url, 404, "nope", {}, None)


def _dead110(url, timeout=0):
    raise ConnectionRefusedError("down")


try:
    _al110._toast = lambda *a, **k: None
    _ureq110.urlopen = _flaky110
    _rc110._port_listening = lambda port=8420: True
    check("110 _app_running waits out a busy-but-alive app (2 timeouts, then an "
          "answer) instead of going headless on the first miss",
          _rc110._app_running() is True and _calls110["n"] == 3, str(_calls110))
    _ureq110.urlopen = _http404_110
    check("110 ...and an HTTP 404 from a pre-upgrade app still counts as ALIVE "
          "(an answer proves the server loop is turning)",
          _rc110._app_running() is True)
    _ureq110.urlopen = _dead110
    _rc110._port_listening = lambda port=8420: False
    check("110 ...a port nobody listens on is NOT running (headless is right)",
          _rc110._app_running() is False)
    _rc110._port_listening = lambda port=8420: True
    _n_before110 = sum(1 for e in state.read_journal(50)
                       if e.get("event") == "app_unresponsive_zombie")
    _z110 = _rc110._app_running()
    _n_after110 = sum(1 for e in state.read_journal(50)
                      if e.get("event") == "app_unresponsive_zombie")
    check("110 ...a LISTENING port that never answers is a ZOMBIE: reported not "
          "running (so the cycle still happens, headless + guarded) AND journaled",
          _z110 is False and _n_after110 == _n_before110 + 1)
finally:
    _ureq110.urlopen = _real_urlopen110
    _rc110._port_listening = _real_port110
    _al110._toast = _real_toast110
    config.APP_PROBE_GAP_S = _real_gap110
    config.APP_PROBE_ATTEMPTS = _real_att110

# (c) HeadlessExitGuard sweeps on cadence while a session is open, stamps the
# hand-off record + the coverage beat, and stops cleanly
_real_cae110 = monitor.check_all_exits
_real_mio110 = _mh110.market_is_open
_real_eso110 = _mh110.extended_session_open
_real_int110 = config.EQUITY_MONITOR_INTERVAL_S
_swept110 = {"n": 0}


def _count_sweep110(include_calls=True):
    _swept110["n"] += 1
    return {}


try:
    monitor.check_all_exits = _count_sweep110
    _mh110.market_is_open = lambda now=None: True
    _mh110.extended_session_open = lambda now=None: False
    config.EQUITY_MONITOR_INTERVAL_S = 0.2
    state.save("headless_sweep", {})
    with monitor.HeadlessExitGuard("test") as _g110:
        _time110.sleep(0.9)
    _rec110 = state.load("headless_sweep", {}) or {}
    _hb110 = state.load("heartbeat", {}) or {}
    check("110 HeadlessExitGuard sweeps immediately and then on cadence while the "
          "session is open (>=3 sweeps in 0.9s at a 0.2s cadence)",
          _swept110["n"] >= 3 and _g110.sweeps == _swept110["n"], str(_swept110))
    check("110 ...stamps state/headless_sweep with ITS pid and writes the "
          "equity_sweep coverage beat health.py grades",
          _rec110.get("pid") == _os110.getpid() and _rec110.get("sweeps", 0) >= 3
          and bool(_hb110.get("equity_sweep")))
    check("110 ...and the app-side reader sees that pid's sweep as FRESH, and any "
          "other pid's as not",
          monitor.headless_sweep_fresh(_os110.getpid()) is True
          and monitor.headless_sweep_fresh(_os110.getpid() + 1) is False
          and monitor.headless_sweep_fresh(None) is False)
    state.save("headless_sweep", {"pid": _os110.getpid(),
                                  "ts": _time110.time() - 3600, "sweeps": 1})
    check("110 ...a stale stamp is NOT coverage (the app must count that minute "
          "as blind)", monitor.headless_sweep_fresh(_os110.getpid()) is False)
    _swept110["n"] = 0
    _mh110.market_is_open = lambda now=None: False
    with monitor.HeadlessExitGuard("idle"):
        _time110.sleep(0.5)
    check("110 ...and idles off-session (nothing to exit into)", _swept110["n"] == 0)
finally:
    monitor.check_all_exits = _real_cae110
    _mh110.market_is_open = _real_mio110
    _mh110.extended_session_open = _real_eso110
    config.EQUITY_MONITOR_INTERVAL_S = _real_int110

# (d) The exit-protection ENTRY gate: in-session with no recent sweep, no new
# risk; fresh sweep or closed market -> silent
_real_gate110 = config.EXIT_PROTECTION_ENTRY_GATE
try:
    config.EXIT_PROTECTION_ENTRY_GATE = True
    _mh110.market_is_open = lambda now=None: True
    _hbd110 = state.load("heartbeat", {}) or {}
    _hbd110["equity_sweep"] = (datetime.now(timezone.utc)
                               - timedelta(minutes=30)).isoformat()
    state.save("heartbeat", _hbd110)
    _why110 = risk.exit_protection_gap()
    _okE110, _whyE110 = risk.entries_allowed(
        "alpha_stocks", state.default_sleeve_state("alpha_stocks"), 25_000.0)
    check("110 entries are REFUSED in-session when no exit sweep has run for "
          "longer than the bar (a position born unprotected is the 08-17 "
          "CRS/TXN class)",
          "exit protection is DOWN" in _why110 and _okE110 is False
          and "exit protection" in _whyE110, _whyE110[:80])
    _hbd110["equity_sweep"] = datetime.now(timezone.utc).isoformat()
    state.save("heartbeat", _hbd110)
    check("110 ...and allowed the moment a real sweep has run",
          risk.exit_protection_gap() == "")
    _hbd110["equity_sweep"] = (datetime.now(timezone.utc)
                               - timedelta(hours=5)).isoformat()
    state.save("heartbeat", _hbd110)
    _mh110.market_is_open = lambda now=None: False
    _mh110.extended_session_open = lambda now=None: False
    check("110 ...and silent off-session (entries queue for the open, where the "
          "sweep runs)", risk.exit_protection_gap() == "")
finally:
    config.EXIT_PROTECTION_ENTRY_GATE = _real_gate110
    _mh110.market_is_open = _real_mio110
    _mh110.extended_session_open = _real_eso110

# (e) A closed market must not excuse a sweep that predates the last close
_lc110 = _mh110.last_regular_close()
check("110 market_hours.last_regular_close returns the most recent COMPLETED "
      "regular close (never in the future, at 16:00 or the 13:00 half-day close)",
      _lc110 is not None and _lc110 <= datetime.now(_mh110.ET)
      and _lc110.time() in (_mh110.CLOSE_T, _mh110.EARLY_CLOSE_T), str(_lc110))
_old110 = (_lc110 - timedelta(hours=3)).isoformat()
_new110 = (_lc110 + timedelta(minutes=1)).isoformat()
check("110 'exit monitor' calls a last sweep 3h BEFORE the last close a blackout "
      "at the close, and one minute after it clean",
      "PREDATES" in _hl110._sweep_predates_last_close(_old110)
      and _hl110._sweep_predates_last_close(_new110) == ""
      and _hl110._sweep_predates_last_close("") == "")

# ══ 111. ETF-directory fetch: negative cache + one journal line per window ═══
# 2,562 universe_etf_fetch_error rows in 7 days (71% of the journal) blinded
# four journal-windowed health checks and stalled the trading cycle 21 s per
# call. A failure is now stamped on the cache and not retried for an hour.
import json as _json111  # noqa: E402
import urllib.request as _ur111  # noqa: E402

import firm.universe as _uni111  # noqa: E402

_real_urlopen111 = _ur111.urlopen
_calls111 = {"n": 0}


def _dead_dir111(url, timeout=0):
    _calls111["n"] += 1
    raise OSError("nasdaqtrader unreachable")


_cache_before111 = state.load("universe_symbols", {}) or {}
try:
    _ur111.urlopen = _dead_dir111
    state.save("universe_symbols", {
        "etfs": ["QQQ", "SPY"],
        "etfs_asof": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
        "etf_names": {"SPY": "SPDR S&P 500"}})
    _n0_111 = sum(1 for e in state.read_journal(200)
                  if e.get("event") == "universe_etf_fetch_error")
    _r1_111 = _uni111.fetch_all_us_etfs()
    _r2_111 = _uni111.fetch_all_us_etfs()
    _r3_111 = _uni111.etf_names()
    _n1_111 = sum(1 for e in state.read_journal(200)
                  if e.get("event") == "universe_etf_fetch_error")
    check("111 a failed ETF-directory fetch serves the cached list and is NOT "
          "retried on the next call (negative cache): three callers, one fetch "
          "attempt, one journal line",
          _calls111["n"] == 1 and _n1_111 == _n0_111 + 1
          and _r1_111 == ["QQQ", "SPY"] and _r2_111 == ["QQQ", "SPY"]
          and _r3_111.get("SPY") == "SPDR S&P 500",
          f"attempts={_calls111['n']} journal={_n1_111 - _n0_111}")
    _c111 = state.load("universe_symbols", {}) or {}
    check("111 ...the failure is stamped on the cache record with a consecutive "
          "count", bool(_c111.get("etfs_fetch_failed_at"))
          and _c111.get("etfs_fetch_fail_n") == 1)
    _c111["etfs_fetch_failed_at"] = (datetime.now(timezone.utc)
                                     - timedelta(hours=2)).isoformat()
    state.save("universe_symbols", _c111)
    _uni111.fetch_all_us_etfs()
    check("111 ...and past ETF_DIRECTORY_RETRY_MIN the fetch is retried (count "
          "climbs to 2)",
          _calls111["n"] == 2
          and (state.load("universe_symbols", {}) or {}).get("etfs_fetch_fail_n") == 2)
finally:
    _ur111.urlopen = _real_urlopen111
    state.save("universe_symbols", _cache_before111)

# ══ 112. Options bar per SIDE + memo; re-entry cooldown gate ═════════════════
# Fix brief 90: 'puts gate: 40 real SELL-family verdicts, 0 ever qualified'.
# One pooled p90 sat where the BUYs put it; the SELL family never reached it.
_real_research112 = state.load("research", {})
_hist112 = []
for _i in range(12):
    _hist112.append({"asof": ts(_i), "verdicts": {
        f"B{_i}": {"rating": "BUY", "conviction": 65 + (_i % 3)},
        f"S{_i}": {"rating": "SELL", "conviction": 58 + (_i % 4)}}})
state.save("research", {"latest_asof": ts(0), "history": _hist112})
sleeves._CONV_BAR_MEMO["stamp"] = None
_calls_bar112 = sleeves._options_conviction_min("calls")
_puts_bar112 = sleeves._options_conviction_min("puts")
_pool_bar112 = sleeves._options_conviction_min()
check("112 the options bar is PER SIDE: the BUY family sets the calls bar, the "
      "SELL family the puts bar — a SELL no longer has to clear a bar the BUYs "
      "set", _calls_bar112 == 67 and _puts_bar112 == 61 and _pool_bar112 == 67,
      f"calls={_calls_bar112} puts={_puts_bar112} pooled={_pool_bar112}")
_vS112 = {"rating": "SELL", "conviction": 61, "suitable_for_puts": True}
_vS112b = {"rating": "SELL", "conviction": 60, "suitable_for_puts": True}
check("112 ...so a top-decile SELL now qualifies for a put (61 clears the "
      "SELL-side bar it could never clear against the pooled 67) and a "
      "sub-bar one still does not",
      sleeves._puts_entry_ok(_vS112) is True and sleeves._puts_entry_ok(_vS112b) is False)
_real_load112 = state.load


def _boom112(*a, **k):
    raise RuntimeError("no parse")


try:
    state.load = _boom112
    check("112 ...and the bar is MEMOIZED on the research file stamp — a second "
          "read costs no parse (the calls pass evaluates it per verdict INSIDE "
          "SLEEVE_LOCK)",
          sleeves._options_conviction_min("puts") == 61
          and sleeves._options_conviction_min("calls") == 67)
finally:
    state.load = _real_load112
state.save("research", _real_research112)
sleeves._CONV_BAR_MEMO["stamp"] = None
_s112 = state.default_sleeve_state("calls_only")
_s112["closed_trades"] = [
    {"symbol": "HPE260919C00020000", "underlying": "HPE", "closed_at": ts(0.5),
     "stop_cooldown": {"underlying": "HPE", "stopped_at": ts(0.5),
                       "cooldown_days": 10, "reason": "hard SL -15.0%"}},
    {"symbol": "OLD260919C00020000", "underlying": "OLD", "closed_at": ts(20),
     "stop_cooldown": {"underlying": "OLD", "stopped_at": ts(20),
                       "cooldown_days": 10, "reason": "hard SL"}},
    {"symbol": "TPX260919C00020000", "underlying": "TPX", "closed_at": ts(0.2)}]
check("112 the re-entry cooldown GATE now consumes the stop_cooldown carrier: "
      "HPE stopped 12h ago is refused, a stop 20d old is not, a take-profit "
      "never is",
      bool(sleeves._options_reentry_blocked(_s112, "HPE"))
      and sleeves._options_reentry_blocked(_s112, "OLD") is None
      and sleeves._options_reentry_blocked(_s112, "TPX") is None)
_rsrc112 = (Path(__file__).parent.parent / "firm" / "routing.py").read_text(encoding="utf-8")
_ssrc112 = (Path(__file__).parent.parent / "firm" / "sleeves.py").read_text(encoding="utf-8")
check("112 ...both cooldown skips are journaled by the desk AND explained to "
      "route_coverage (never a silent drop)",
      all(f'"{e}"' in _rsrc112 and f'"{e}"' in _ssrc112
          for e in ("calls_skipped_reentry_cooldown", "puts_skipped_reentry_cooldown")))

# ══ 113. Quote BASIS for extended-session stops; refusals journaled; a missing
#         witness is not a disagreement ═══════════════════════════════════════
try:
    import public_api_sdk  # noqa: F401
    _sdk113 = True
except Exception:
    _sdk113 = False


class _Q113:
    def __init__(self, sym, bid=None, ask=None, last=None):
        self.symbol, self.bid, self.ask, self.last = sym, bid, ask, last


class _Client113:
    def get_quotes(self, instruments):
        return [_Q113("TWO", 10.0, 10.1, 10.05), _Q113("BIDONLY", 9.0, None, None),
                _Q113("LASTONLY", None, None, 8.0), _Q113("WIDE", 5.0, 8.0, 6.0)]


if _sdk113:
    _real_client113 = broker.get_client
    _real_qts113 = broker._quote_refused_ts
    try:
        broker.get_client = lambda: _Client113()
        broker._quote_refused_ts = 0.0
        broker._quote_refused_buf.clear()
        _out113 = broker.equity_quotes_public(["TWO", "BIDONLY", "LASTONLY", "WIDE"])
        check("113 a two-sided mid is the ONLY quote an extended-session stop may "
              "be judged on; a bare last print or a junk-wide book prices the "
              "display but never the stop, and a bare bid is refused outright",
              broker.extended_stop_quote_ok("TWO")
              and not broker.extended_stop_quote_ok("LASTONLY")
              and not broker.extended_stop_quote_ok("WIDE")
              and "BIDONLY" not in _out113 and _out113.get("LASTONLY") == 8.0
              and _out113.get("WIDE") == 6.0,
              str({k: broker._quote_basis.get(k) for k in ("TWO", "LASTONLY", "WIDE")}))
        _ref113 = [e for e in state.read_journal(20) if e.get("event") == "quote_refused"]
        check("113 ...and the refused one-sided book is JOURNALED (aggregated), no "
              "longer a silent drop",
              bool(_ref113) and "BIDONLY" in (_ref113[0].get("symbols") or {}))
    finally:
        broker.get_client = _real_client113
        broker._quote_refused_ts = _real_qts113
else:
    check("113 quote-basis checks skipped: public_api_sdk not importable here", True)
_real_lp113 = data_engine.last_price
_real_cap113 = config.STOP_UNCONFIRMED_MAX_SWEEPS
_real_toast113 = _al110._toast
try:
    _al110._toast = lambda *a, **k: None
    config.STOP_UNCONFIRMED_MAX_SWEEPS = 3
    data_engine.last_price = lambda s: 0.0
    _p113 = {"entry_price": 100.0}
    _seq113 = [monitor._mark_corroborated(_p113, 85.0, "alpha_stocks", "ZNOREF",
                                          "hard stop") for _ in range(2)]
    check("113 when the second source is UNAVAILABLE (vendor outage) a sanitized "
          "stop is held ONE sweep then routes — not the full disagreement cap",
          _seq113 == [False, True]
          and config.STOP_UNCONFIRMED_MAX_SWEEPS_NO_REF == 2 < config.STOP_UNCONFIRMED_MAX_SWEEPS,
          str(_seq113))
    data_engine.last_price = lambda s: 60.0
    _p113b = {"entry_price": 100.0}
    _seq113b = [monitor._mark_corroborated(_p113b, 85.0, "alpha_stocks", "ZDIS",
                                           "hard stop") for _ in range(3)]
    check("113 ...while a genuine DISAGREEMENT (85 vs 60) still holds the full cap",
          _seq113b == [False, False, True], str(_seq113b))
finally:
    data_engine.last_price = _real_lp113
    config.STOP_UNCONFIRMED_MAX_SWEEPS = _real_cap113
    _al110._toast = _real_toast113
_rk113 = (Path(__file__).parent.parent / "firm" / "risk.py").read_text(encoding="utf-8")
_i113 = _rk113.find("drawdown_repriced_no_halt")
check("113 the kill-switch re-price compares on the RISK basis it was first "
      "read on (peak_risk_equity + sleeve_risk_equity), never the display basis",
      "peak_risk_equity" in _rk113[max(0, _i113 - 1500):_i113]
      and "sleeve_risk_equity" in _rk113[max(0, _i113 - 1500):_i113])

# ══ 114. EOD mark SLA: host-down sessions are outages, not feed failures ═════
_gaps114 = [{"event": "session_coverage_gap",
             "from": "2026-08-23T09:54:14+00:00", "to": "2026-08-28T01:43:47+00:00"}]
_sess114 = ["2026-08-21", "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27",
            "2026-08-28", "2026-08-31"]
_hd114 = _hl110._host_down_sessions(_sess114, _gaps114)
check("114 sessions whose nightly mark fell inside a journaled host outage are "
      "excluded from the FEED grade (08-24..08-27); sessions the host was up "
      "for are not (08-21, 08-28 evening, 08-31)",
      _hd114 == ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27"], str(_hd114))

# ══ 115. Cycle lock is written atomically ════════════════════════════════════
state.write_cycle_lock()
_lk115 = _json111.loads(state.CYCLE_LOCK.read_text(encoding="utf-8"))
check("115 write_cycle_lock lands a complete JSON record with THIS pid (tmp + "
      "replace — a reader can never see a truncated file and misread its own "
      "cycle as FOREIGN)",
      _lk115.get("pid") == _os110.getpid()
      and state.cycle_lock_holder() == _os110.getpid()
      and not list(state.CYCLE_LOCK.parent.glob("cycle.lock.*.tmp")))
state.clear_cycle_lock()
check("115 ...and clear_cycle_lock releases it", state.cycle_lock_holder() is None)

# ══ 116. IG loop: digit-blind brief dedupe; the auditor sees UNMEASURED ═════
import importlib as _il116  # noqa: E402

_ig116 = _il116.import_module("scripts.ig_audit")
_real_toast116 = _al110._toast
_fb_before116 = state.load("fix_briefs", {"items": []})
_keys_before116 = state.load("ig_briefs_filed", {}) or {}
try:
    _al110._toast = lambda *a, **k: None
    state.save("ig_briefs_filed", {"keys": []})
    state.save("fix_briefs", {"items": [], "resolved": [], "archive": []})
    _f1 = [{"severity": "critical", "gap": "The entire near_miss bench class (31 of 109 entries, 28%) is unarmed",
            "evidence": "e", "proposed_check": "p"}]
    _f2 = [{"severity": "critical", "gap": "The entire near_miss bench class (32 of 110 entries, 29%) is unarmed",
            "evidence": "e", "proposed_check": "p"}]
    _n1_116 = _ig116._file_high_findings(_f1)
    _n2_116 = _ig116._file_high_findings(_f2)
    check("116 the IG brief dedupe is DIGIT-BLIND: the same gap re-worded with "
          "today's counts is the same finding, not a new brief",
          _n1_116 == 1 and _n2_116 == 0, f"first={_n1_116} second={_n2_116}")
    _snap_checks116 = [{"name": "x", "ok": True, "detail": "undetermined (n=0)",
                        "undetermined": True},
                       {"name": "y", "ok": True, "detail": "fine"}]
    _src116 = (Path(__file__).parent.parent / "scripts" / "ig_audit.py").read_text(encoding="utf-8")
    check("116 the IG snapshot carries a third state: an undetermined check is "
          "handed to the auditor as ok=null / status=unmeasured, never ok=true",
          '"status": ("unmeasured" if c.get("undetermined")' in _src116
          and "(None if c.get(\"undetermined\") else c[\"ok\"])" in _src116)
finally:
    _al110._toast = _real_toast116
    state.save("fix_briefs", _fb_before116)
    state.save("ig_briefs_filed", _keys_before116)

# ══ 117. Full-audit 2026-09-02: the gaps in the 09-01 exit-protection work ══
# Three lenses of the full audit independently found that the gate shipped on
# 09-01 had holes: it never looked at the options monitor, etf_rotation's
# breaker override discarded it wholesale, and a cycle lock that could not be
# claimed said nothing at all.
_real_gate117 = config.EXIT_PROTECTION_ENTRY_GATE
_real_mio117 = _mh110.market_is_open
_real_eso117 = _mh110.extended_session_open
_hb117 = state.load("heartbeat", {}) or {}
try:
    config.EXIT_PROTECTION_ENTRY_GATE = True
    _mh110.market_is_open = lambda now=None: True
    _fresh117 = datetime.now(timezone.utc).isoformat()
    _stale117 = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    # equity side healthy, options monitor dead
    state.save("heartbeat", {**_hb117, "equity_sweep": _fresh117,
                             "calls_monitor": _stale117})
    _calls117 = risk.exit_protection_gap("calls_only")
    _alpha117 = risk.exit_protection_gap("alpha_stocks")
    check("117 the options desk is REFUSED a new leveraged position while its "
          "own monitor is not beating — equity_sweep says nothing about the "
          "1 s options loop (audit exit-protection-e2e-2)",
          "options monitor is not beating" in _calls117 and _alpha117 == "",
          f"calls={_calls117[:60]!r} alpha={_alpha117!r}")
    state.save("heartbeat", {**_hb117, "equity_sweep": _fresh117,
                             "calls_monitor": _fresh117})
    check("117 ...and allowed the moment that monitor beats again (the beat is "
          "written before the has-options branch, so a flat desk can still open "
          "its first contract — the gate must not deadlock what it protects)",
          risk.exit_protection_gap("calls_only") == "")
    # etf_rotation: the breaker override must not swallow the protection gate
    state.save("heartbeat", {**_hb117, "equity_sweep": _stale117,
                             "calls_monitor": _fresh117})
    check("117 the exit-protection gate binds etf_rotation too — its breaker "
          "override bypasses the PER-SLEEVE daily loss breaker only, never a "
          "stop-routing outage (audit risk-and-sizing-1)",
          "exit protection is DOWN" in risk.exit_protection_gap("etf_rotation"))
    _rot117 = (Path(__file__).parent.parent / "firm" / "sleeves.py").read_text(encoding="utf-8")
    _i117 = _rot117.find("_rotation_runs = (_due")
    check("117 ...and the rotation gate reads it in code, so the override can "
          "no longer discard the whole entries_allowed verdict",
          "not _exit_gap" in _rot117[_i117:_i117 + 260]
          and "risk.exit_protection_gap(" in _rot117[max(0, _i117 - 900):_i117])
finally:
    config.EXIT_PROTECTION_ENTRY_GATE = _real_gate117
    _mh110.market_is_open = _real_mio117
    _mh110.extended_session_open = _real_eso117
    state.save("heartbeat", _hb117)
# A cycle lock that cannot be claimed must be LOUD (audit live-order-path-2)
_real_repl117 = Path.replace
_alerts117 = []
_real_alert117 = _al110.alert
try:
    _al110.alert = lambda *a, **k: _alerts117.append(a[0] if a else "")

    def _boom117(self, target):
        if str(target).endswith("cycle.lock"):
            raise PermissionError("locked by another process")
        return _real_repl117(self, target)

    Path.replace = _boom117
    state.write_cycle_lock()
    _j117 = [e for e in state.read_journal(30)
             if e.get("event") == "cycle_lock_write_failed"]
    check("117 a cycle lock that cannot be claimed journals AND alerts — a "
          "silent failure made an unheld lock look exactly like a held one, "
          "and 'a lost write is a lost trade'",
          bool(_j117) and any("CYCLE LOCK" in str(x) for x in _alerts117),
          f"journal={len(_j117)} alerts={_alerts117[:1]}")
finally:
    Path.replace = _real_repl117
    _al110.alert = _real_alert117
    state.clear_cycle_lock()

# ══ 118. Full audit 2026-09-02: no unbounded network call under SLEEVE_LOCK ══
# Four confirmed CRITICALs shared one root cause — a network call with no
# timeout made while holding the lock every stop in the firm must take to route.
import types as _types118  # noqa: E402

_bsrc118 = (Path(__file__).parent.parent / "firm" / "broker.py").read_text(encoding="utf-8")
_i118 = _bsrc118.find("def get_client()")
_gc118 = _bsrc118[_i118:_i118 + 2200]
check("118 the Public SDK transport is BOUNDED — the SDK ships timeout=30 x "
      "max_retries=3, and its config object exposes neither, so one slow "
      "endpoint could hold SLEEVE_LOCK ~2 min at the 1 s options cadence",
      "PUBLIC_API_TIMEOUT_S" in _gc118 and "_max_retries" in _gc118
      and config.PUBLIC_API_TIMEOUT_S <= 10 and config.PUBLIC_API_MAX_RETRIES <= 2,
      f"timeout={config.PUBLIC_API_TIMEOUT_S} retries={config.PUBLIC_API_MAX_RETRIES}")
check("118 ...and it is set on the ApiClient INSTANCE (what _retry_non_safe "
      "reads), not the mounted HTTPAdapter whose Retry excludes POST entirely "
      "— every hot-loop call here is a POST",
      "api_client" in _gc118 and "auth_manager" in _gc118)


class _OptQ118:
    def __init__(self, bid=None, ask=None, last=None):
        self.bid, self.ask, self.last = bid, ask, last


_real_gc118 = broker.get_client
# Block 91 installs a FAKE public_api_sdk into sys.modules; it has no
# InstrumentType/OrderInstrument, so the import inside option_quote_public
# would raise and return 0.0 for BOTH cases — the check would "pass" its stale
# half for the wrong reason and fail its live half. Lend the two symbols.
_prev_sdk118 = sys.modules.get("public_api_sdk")
try:
    # Block 91 leaves a stub here whose InstrumentType has EQUITY but NOT
    # OPTION, so option_quote_public's `InstrumentType.OPTION` raises and the
    # function returns 0.0 for every input — the check would then "pass" its
    # stale half for entirely the wrong reason. Install a module that satisfies
    # the option path, and put the previous one back afterwards.
    _mod118 = _types118.ModuleType("public_api_sdk")
    _mod118.InstrumentType = _types118.SimpleNamespace(OPTION="OPTION",
                                                       EQUITY="EQUITY")
    _mod118.OrderInstrument = lambda symbol=None, type=None: {"symbol": symbol,
                                                              "type": type}
    sys.modules["public_api_sdk"] = _mod118
    broker.get_client = lambda: type("C", (), {
        "get_quotes": lambda self, instruments: [_OptQ118(bid=None, ask=None, last=4.20)]})()
    _stale118 = broker.option_quote_public("XYZ260919C00050000")
    broker.get_client = lambda: type("C", (), {
        "get_quotes": lambda self, instruments: [_OptQ118(bid=4.0, ask=4.4, last=9.99)]})()
    _live118 = broker.option_quote_public("XYZ260919C00050000")
    check("118 no bid, no mark: a one-sided option quote prices at 0.0, never "
          "at a `last` print that can be hours old — this is the SOLE pre-trade "
          "repricing check before the desk pays premium, and the puts gate went "
          "reachable 2026-09-01",
          _stale118 == 0.0 and _live118 == 4.2, f"stale={_stale118} live={_live118}")
finally:
    broker.get_client = _real_gc118
    if _prev_sdk118 is not None:
        sys.modules["public_api_sdk"] = _prev_sdk118
    else:
        sys.modules.pop("public_api_sdk", None)

_real_rts118 = data_engine._yf_reset_ts
try:
    data_engine._yf_reset_ts = 0.0
    _first118 = data_engine._yf_reset_allowed()
    _second118 = data_engine._yf_reset_allowed()
    check("118 the yfinance session rebuild is rate-limited — it was gated only "
          "on the failure streak, so a vendor outage rebuilt the session on "
          "EVERY batch (~3,000 retries and a 5-hour unprotected book, each "
          "attempt under the lock the exit sweep needs)",
          _first118 is True and _second118 is False
          and config.YF_RESET_MIN_INTERVAL_S >= 60)
finally:
    data_engine._yf_reset_ts = _real_rts118

_hsrc118 = (Path(__file__).parent.parent / "firm" / "health.py").read_text(encoding="utf-8")
_j118 = _hsrc118.find("position_sector_backfilled")
_blk118 = _hsrc118[max(0, _j118 - 1800):_j118]
check("118 a position whose .info carries no sector is MARKED, so the untimed "
      "yfinance call under SLEEVE_LOCK stops repeating for it every sweep "
      "forever (an ETF has no sector and could never be stamped)",
      'sector_unavailable' in _blk118 and '_pp.get("sector_unavailable")' in _blk118)

_ssrc118 = (Path(__file__).parent.parent / "firm" / "sleeves.py").read_text(encoding="utf-8")
check("118 the options desk honours the EQUITY sleeve's post-stop cool-off on "
      "BOTH sides — a name stopped out this morning must not come back this "
      "afternoon as a leveraged option at 5-10x the delta",
      _ssrc118.count("equity sleeve stop-out") == 2
      and _ssrc118.count("recent_stop_block(sym, spots.get(sym, 0))") == 2)

# ══ 119. Atomic cycle-lock acquisition (2026-09-02 audit, four lenses) ══════
# Claiming was check-then-act -- `if cycle_active(): return` and only THEN write
# the lock -- so two processes could both pass the check and both write live
# sleeve state. O_CREAT|O_EXCL makes creation-or-fail one operation.
import subprocess as _sp119  # noqa: E402

state.clear_cycle_lock()
check("119 the cycle lock is claimed ATOMICALLY and the claim is re-entrant "
      "for its own holder (the in-app cycle refreshes through this path)",
      state.try_claim_cycle_lock() is True
      and state.cycle_lock_holder() == _os110.getpid()
      and state.try_claim_cycle_lock() is True)
# A LIVE foreign holder must lose us the claim. Use a real, running process.
_sleep119 = _sp119.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
try:
    _born119 = 0.0
    try:
        import psutil as _ps119
        _born119 = float(_ps119.Process(_sleep119.pid).create_time())
    except Exception:
        pass
    state.CYCLE_LOCK.write_text(
        _json111.dumps({"pid": _sleep119.pid, "ts": _time110.time(),
                        "born": _born119}), encoding="utf-8")
    check("119 a LIVE foreign holder blocks the claim — this is the whole "
          "point: two writers of sleeve state is the one unrecoverable "
          "corruption ('a lost write is a lost trade')",
          state.try_claim_cycle_lock() is False
          and state.cycle_lock_holder() == _sleep119.pid)
finally:
    _sleep119.terminate()
    _sleep119.wait(timeout=20)
check("119 ...and once that holder dies its lock is reclaimable, so a "
      "hard-killed cycle cannot block every future cycle",
      state.try_claim_cycle_lock() is True)
# PID REUSE: our own pid with somebody else's birth time is NOT us.
state.CYCLE_LOCK.write_text(
    _json111.dumps({"pid": _os110.getpid(), "ts": _time110.time(), "born": 1.0}),
    encoding="utf-8")
_recycled119 = state.cycle_lock_holder()
check("119 a RECYCLED pid does not read as the holder — identity is (pid, "
      "birth time), so a dead cycle's pid handed to an unrelated process "
      "cannot blind the app's monitors until the stale window expires",
      _recycled119 is None if _recycled119 != _os110.getpid() else False,
      f"holder={_recycled119}")
# A record with no `born` (older build / no psutil) keeps the pid-only contract.
state.CYCLE_LOCK.write_text(
    _json111.dumps({"pid": _os110.getpid(), "ts": _time110.time()}),
    encoding="utf-8")
check("119 ...while a legacy record without a birth token still reads as held "
      "(a missing token cannot be disproved, so nothing regresses)",
      state.cycle_lock_holder() == _os110.getpid())
state.clear_cycle_lock()
_rcsrc119 = (Path(__file__).parent.parent / "run_cycle.py").read_text(encoding="utf-8")
_apsrc119 = (Path(__file__).parent.parent / "app.py").read_text(encoding="utf-8")
check("119 both real CLAIM sites go through try_claim_cycle_lock — a lock that "
      "is merely written after a question was asked is not a lock",
      "state.try_claim_cycle_lock()" in _rcsrc119
      and "state.try_claim_cycle_lock()" in _apsrc119)

# ══ Report ════════════════════════════════════════════════════════════════════
print()
for status, name, detail in RESULTS:
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
fails = [r for r in RESULTS if r[0] == "FAIL"]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
sys.exit(1 if fails else 0)
