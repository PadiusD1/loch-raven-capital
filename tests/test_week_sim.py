"""Loch Raven Capital — WEEK-IN-THE-LIFE integration simulator.

Five simulated trading days drive the firm's REAL decision code end to end —
sleeves, monitor, risk, watchlist, learning, and the live-order plumbing —
fully OFFLINE in a sandboxed state dir (never touches state/). Prices come
from a scripted PRICES dict the scenario advances; Opus is a scripted dict;
the Public.com client is a mock implementing the live-path interface.

    py -3.11 tests\\test_week_sim.py

Complements tests/test_firm.py (unit-level safety properties) with a
multi-day narrative:
  D1 committee buys (alpha + core + rotation)  D2 gap skip + hard stop-out
  D3 SELL exit + trailing-stop profit          D4 day breaker + learning review
  D5 rotation roll + market-hours gate         LIVE pending/settle/chase/flips
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Isolate ALL state into throwaway sandboxes BEFORE importing firm ──────────
import config  # noqa: E402
config.RESEARCH_AUTO_REFRESH = False

WEEK_DIR = Path(tempfile.mkdtemp(prefix="lochraven_week_"))
LIVE_DIR = Path(tempfile.mkdtemp(prefix="lochraven_weeklive_"))
config.STATE_DIR = WEEK_DIR
assert "lochraven_week_" in str(config.STATE_DIR)   # never the real books
# Frictionless fills for exact-value assertions (slippage has its own suite)
config.PAPER_SLIPPAGE = {"equity_bps": 0.0, "option_spread_frac": 0.0,
                         "option_fee_per_contract": 0.0}
config.PAPER_ENFORCE_MARKET_HOURS = False   # the gate gets its own D5 section
# Exit-protection entry gate (2026-09-01) reads the real heartbeat clock; the
# sim scripts entries at any hour with no sweep loop — off here, its own
# section lives in test_firm (§110).
config.EXIT_PROTECTION_ENTRY_GATE = False
# The deployment scaler (re-grade 2026-07-16) deliberately up-sizes entries in
# an under-deployed book — this sim scripts EXACT quantities, so pin it off;
# the scaler has its own unit section in test_firm (§80).
config.ALPHA_DEPLOYMENT_FLOOR_PCT = 0.0

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import firm.brain as brain  # noqa: E402
import firm.broker as broker  # noqa: E402
import firm.data_engine as data_engine  # noqa: E402
import firm.market_hours as mh  # noqa: E402
from firm import alerts, learning, monitor, risk, riskbrain, sleeves, state  # noqa: E402

RESULTS = []
VIOLATIONS = []   # cross-beat invariants accumulated after every step


def check(name, ok, detail=""):
    RESULTS.append(("PASS" if ok else "FAIL", name, detail))


# ══ Scripted world ═════════════════════════════════════════════════════════════
# The scenario advances this dict; every mocked price source reads from it.
PRICES = {
    "NVDA": 100.0, "AMD": 100.0, "MSFT": 100.0, "TSLA": 100.0, "AVGO": 100.0,
    "ORCL": 100.0,
    "VOO": 500.0, "QQQ": 480.0, "GLD": 250.0, "SMH": 580.0, "XLK": 180.0,
    "TLT": 90.0, "XLE": 85.0, "BIL": 91.5, "SPY": 600.0,
}
SECTORS = {"NVDA": "Technology", "AMD": "Technology",
           "MSFT": "Communication Services", "TSLA": "Consumer Cyclical",
           "AVGO": "Technology", "ORCL": "Technology"}
ROTATION = {"ranked": [("SMH", 42.0), ("XLK", 33.0), ("QQQ", 28.0),
                       ("IWM", 9.0), ("GLD", 6.0), ("TLT", -2.0)],
            "cash_hurdle_3m": 1.0}


def _px_map(symbols):
    return {s: PRICES[s] for s in symbols if s in PRICES}


def fake_fetch_history(tickers, period="2y"):
    """Deterministic independent random walks ending at PRICES — exercises the
    real correlation / bench-audit math offline with near-zero pairwise corr."""
    import hashlib
    if isinstance(tickers, str):
        tickers = [tickers]
    idx = pd.bdate_range(end=datetime.now().date(), periods=140)
    data = {}
    for t in tickers:
        seed = int(hashlib.md5(t.encode()).hexdigest()[:8], 16)
        rng = np.random.RandomState(seed)
        rets = rng.normal(0.0, 0.01, len(idx))
        cum = np.cumsum(rets)
        data[t] = PRICES.get(t, 100.0) * np.exp(cum - cum[-1])
    return pd.DataFrame(data, index=idx)


def fake_build_packet(ticker, tech=None, quant=60.0):
    t = dict(tech or {})
    t.setdefault("price", PRICES.get(ticker, 100.0))
    t.setdefault("vol_ann_pct", 30.0)
    t.setdefault("avg_dollar_vol", 5e8)
    return {"ticker": ticker,
            "asof": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "quant_screen_score": quant, "technicals": t,
            "fundamentals": {"ticker": ticker, "sector": SECTORS.get(ticker, "")},
            "news_headlines": ["(scripted offline packet)"],
            "next_earnings": {"date": "2026-12-15", "days_away": 188}}


def fake_screen():
    return {"asof": datetime.now(timezone.utc).isoformat(),
            "technicals": {t: {"price": p, "vol_ann_pct": 30.0}
                           for t, p in PRICES.items()},
            "stock_candidates": ["NVDA", "AMD", "MSFT"],
            "etf_candidates": ["SMH", "XLK", "QQQ"],
            "quant_scores": {t: 70.0 for t in PRICES}, "scan_info": {}}


# Price sources — all read the mutable PRICES dict
data_engine.batch_last_prices = lambda tickers: _px_map(tickers)
data_engine.last_price = lambda t: PRICES.get(t, 0.0)
data_engine.fetch_history = fake_fetch_history
data_engine.build_packet = fake_build_packet
data_engine.screen_universe = fake_screen
data_engine.rotation_signals = lambda: {"ranked": list(ROTATION["ranked"]),
                                        "cash_hurdle_3m": ROTATION["cash_hurdle_3m"]}
broker.equity_quotes_public = lambda symbols: _px_map(symbols)
broker.option_quotes_public = lambda positions: {}
broker.option_quote_public = lambda osi: PRICES.get(osi, 0.0)
# broker.fetch_marks / broker.equity_prices stay REAL — they route through the
# mocked quote sources above (better integration coverage)

# Brain — scripted Opus
brain.available = lambda: True
brain.ask_json = lambda p, **kw: {"lessons": [
    {"lesson": "Hard stops on >12% gaps preserved capital", "evidence": "NVDA D2"},
    {"lesson": "Trailing stops convert open gains into booked P&L", "evidence": "MSFT D3"}]}

# Determinism / hygiene
riskbrain.vol_multiplier = lambda: 1.0
alerts._toast = lambda *a, **k: None
mh.market_is_open = lambda now=None: True   # scenario steps override per beat
mh.is_trading_day = lambda now=None: True   # every sim beat is a trading day
                                            # (anchor-roll gate, 2026-07-04)


def ts(days_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def jlines(dirpath=None):
    p = (dirpath or config.STATE_DIR) / "journal.jsonl"
    if not p.exists():
        return []
    return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def jevents(since=0, name=None, dirpath=None):
    out = []
    for ln in jlines(dirpath)[since:]:
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if name is None or e.get("event") == name:
            out.append(e)
    return out


def sweep_books(tag):
    """Cross-beat invariants: cash never negative, positions never malformed."""
    for sl in config.SLEEVES:
        s = state.get_sleeve(sl)
        if s["cash"] < -1e-6:
            VIOLATIONS.append(f"{tag}: {sl} cash {s['cash']:.2f}")
        for sym, p in s["positions"].items():
            if (not isinstance(p.get("entry_price"), (int, float))
                    or not isinstance(p.get("quantity"), (int, float))):
                VIOLATIONS.append(f"{tag}: {sl} {sym} missing entry_price/quantity")


def V(ticker, rating, conviction, weight=10.0, thesis="", pkt_price=None, vol=30.0):
    pk = fake_build_packet(ticker, {
        "price": pkt_price if pkt_price is not None else PRICES.get(ticker, 100.0),
        "vol_ann_pct": vol})
    return {"kind": "stock",
            "verdict": {"rating": rating, "conviction": conviction,
                        "target_weight_pct": weight, "thesis": thesis,
                        "suitable_for_calls": False},
            "packet": pk}


# ══ D1 — committee BUYs 3 names; alpha + core + rotation deploy ═══════════════
verdicts_d1 = {
    "NVDA": V("NVDA", "STRONG_BUY", 85, thesis="accelerator cycle intact"),
    "AMD": V("AMD", "BUY", 72, thesis="server share gains"),
    "MSFT": V("MSFT", "BUY", 66, thesis="copilot monetization"),
}
# The week-sim's exact-dollar assertions test cross-sleeve stop ISOLATION with
# shared names and unparked core cash. The satellite de-dup was REMOVED
# 2026-07-02 (sleeves are independent; the same name MAY be held across them),
# so no flag is needed; just disable core SPY parking so the dollar math stays
# exact (parking behavior is covered in test_firm §44).
config.CASH_EQUITIZATION.pop("core_blend", None)
sleeves.run_alpha_stocks(verdicts_d1, SECTORS, "paper")
sleeves.run_core_blend(verdicts_d1, SECTORS, "paper")
sleeves.run_etf_rotation("paper")

a = state.get_sleeve("alpha_stocks")
# (2026-08-14 operator decision: alpha single-name sizing halved / calls floor
# 60) ALPHA_SIZING_DERISK=0.5 halves every per-name target until the screen
# re-validates: the 10% verdict weight sizes at 5% -> 12 shares (was 25), and
# the SPY top-up absorbs the freed capital (31 shares; the 90%-invested target
# truncates to whole shares, leaving $2,800 cash instead of $2,500).
check("D1 alpha opened the 3 committee names at scripted prices",
      set(a["positions"]) == {"NVDA", "AMD", "MSFT", "SPY"}
      and all(a["positions"][k]["entry_price"] == 100.0
              and a["positions"][k]["quantity"] == 12
              for k in ("NVDA", "AMD", "MSFT"))
      and a["positions"]["SPY"].get("cash_equitization"),
      str({k: (p['entry_price'], p['quantity']) for k, p in a['positions'].items()}))
check("D1 alpha cash decreased consistently (cash + cost == capital, "
      "residue equitized into SPY to ~90% deployed)",
      abs(a["cash"] + sum(p["cost"] for p in a["positions"].values()) - 25_000.0) < 0.01
      and abs(a["cash"] - 2_800.0) < 0.01, f"cash={a['cash']}")
nv = a["positions"].get("NVDA", {})
check("D1 positions carry rating/conviction/depth/thesis/stop meta",
      nv.get("rating") == "STRONG_BUY" and nv.get("conviction") == 85
      and nv.get("depth") == "standard" and "accelerator" in nv.get("thesis", "")
      # from config, not a copy of the constant (2026-08-19) — the property is
      # "the position records the sleeve's stops", not "the stop is 12%"
      and nv.get("stop_pct") == config.SLEEVES["alpha_stocks"]["hard_stop_pct"]
      and nv.get("trail_stop_pct") == config.SLEEVES["alpha_stocks"]["trail_pct"])

c = state.get_sleeve("core_blend")
check("D1 core opened strategic core + 3 conviction satellites",
      set(c["positions"]) == {"VOO", "QQQ", "GLD", "NVDA", "AMD", "MSFT"},
      str(sorted(c["positions"])))
check("D1 core cash decreased consistently",
      abs(c["cash"] + sum(p["cost"] for p in c["positions"].values()) - 25_000.0) < 0.01
      and abs(c["cash"] - 5_290.0) < 0.01, f"cash={c['cash']}")
check("D1 core satellite carries the entry verdict too",
      c["positions"].get("AMD", {}).get("rating") == "BUY"
      and c["positions"].get("AMD", {}).get("conviction") == 72)

r = state.get_sleeve("etf_rotation")
check("D1 rotation deployed into the 3 momentum winners",
      set(r["positions"]) == {"SMH", "XLK", "QQQ"} and r.get("last_rotation"),
      str(sorted(r["positions"])))
check("D1 rotation positions stamped with strategy + signal",
      r["positions"].get("SMH", {}).get("strategy") == "dual_momentum"
      and r["positions"].get("SMH", {}).get("rank") == 1)
check("D1 journal recorded every fill",
      len(jevents(0, "order")) >= 12, f"orders={len(jevents(0, 'order'))}")
sweep_books("D1")

# ══ D2 — +8% gap is SKIPPED by the drift guard; -13% fires the hard stop ══════
jm = len(jlines())
PRICES["TSLA"] = 108.0     # gapped +8% overnight; thesis price was 100
sleeves.run_alpha_stocks(
    {"TSLA": V("TSLA", "BUY", 80, thesis="ev cycle", pkt_price=100.0)},
    SECTORS, "paper")
drift_ev = jevents(jm, "buy_skipped_price_drift")
check("D2 drift guard skips the +8% gap (no chase)",
      "TSLA" not in state.get_sleeve("alpha_stocks")["positions"]
      and drift_ev and drift_ev[-1]["symbol"] == "TSLA"
      and abs(drift_ev[-1]["drift_pct"] - 8.0) < 0.1,
      str(drift_ev[-1] if drift_ev else None))

PRICES["NVDA"] = 87.0      # -13% from entry: past the 12% hard stop
monitor.check_all_exits(include_calls=False)
a = state.get_sleeve("alpha_stocks")
last = a["closed_trades"][-1]
check("D2 hard stop fires at -13% and books the loss",
      "NVDA" not in a["positions"] and last["symbol"] == "NVDA"
      # (2026-08-14 operator decision: alpha single-name sizing halved) 12
      # shares x -$13 = -$156 (was 25 x -$13 = -$325); the stop MECHANISM is
      # unchanged and still asserted.
      and last["pnl"] == -156.0 and last["reason"].startswith("hard stop"),
      str(last))
wl = state.load("watchlist", {})
check("D2 stopped name lands on the bench with its thesis (stop_out)",
      wl.get("NVDA", {}).get("source") == "stop_out"
      and "accelerator" in wl.get("NVDA", {}).get("stop_out", {}).get("thesis", ""))
check("D2 stop isolation: core (-13% < its 15% stop) still holds NVDA",
      "NVDA" in state.get_sleeve("core_blend")["positions"])
sweep_books("D2")

# ══ D3 — re-research SELL exits; +20% winner fades into a trailing-stop profit ═
PRICES["AMD"] = 96.0
sell_v = {"AMD": {"kind": "stock",
                  "verdict": {"rating": "STRONG_SELL", "conviction": 78}}}
sleeves.run_alpha_stocks(sell_v, SECTORS, "paper")
# alpha + core close AMD back-to-back inside one Windows clock tick — the
# per-trade trade_id (broker._book_sell) must keep BOTH ledger rows; this
# same-tick double close used to need a sleep workaround before the fix
sleeves.run_core_blend(sell_v, SECTORS, "paper")
a = state.get_sleeve("alpha_stocks")
check("D3 SELL verdict exits the held name (alpha)",
      "AMD" not in a["positions"]
      and a["closed_trades"][-1]["reason"] == "PM verdict STRONG_SELL"
      # (2026-08-14 operator decision: alpha sizing halved) 12 x -$4 = -$48
      # (was 25 x -$4 = -$100); the SELL-exit path itself is unchanged.
      and a["closed_trades"][-1]["pnl"] == -48.0)
check("D3 SELL verdict exits the satellite too (core)",
      "AMD" not in state.get_sleeve("core_blend")["positions"]
      and state.get_sleeve("core_blend")["closed_trades"][-1]["pnl"] == -88.0)

PRICES["MSFT"] = 120.0     # winner runs +20%
monitor.check_all_exits(include_calls=False)
check("D3 winner at +20% rides (peak armed, no exit)",
      state.get_sleeve("alpha_stocks")["positions"]
      .get("MSFT", {}).get("peak_price") == 120.0)
PRICES["MSFT"] = 101.5     # fades -15.4% off peak: past the 15% trail
monitor.check_all_exits(include_calls=False)
a = state.get_sleeve("alpha_stocks")
last = a["closed_trades"][-1]
# 2026-07-26: the exit is still asserted (same symbol, same +37.5 profit); only
# the MECHANISM changed. At 101.5 the position has handed back 92.5% of a +20%
# peak gain, so the profit ratchet now books it before the 15% price-trail does.
# (2026-08-14 operator decision: alpha sizing halved) the +37.5 above became
# +18.0 — 12 shares x +$1.50 (was 25 x +$1.50); ratchet mechanism unchanged.
check("D3 the fade books the profit (ratchet or trail)",
      "MSFT" not in a["positions"] and last["symbol"] == "MSFT"
      and last["pnl"] == 18.0
      and ("trailing stop" in last["reason"]
           or "profit ratchet" in last["reason"]), str(last))
# CORE TOO — deliberate change of a previously-asserted behaviour. This line used
# to assert that core's wider 18% trail kept holding MSFT here. That is exactly
# the pathology the ratchet exists to end: giving back 92.5% of a peak gain is
# indefensible in ANY sleeve, and tolerating it is why avg win and avg loss came
# out symmetric (+13.1% / -12.4%) on a 22% hit rate. Sleeve trail differentiation
# is still real and still tested — see the case below, at a peak too small for the
# ratchet to engage, where core's wider trail does hold and alpha's does not.
check("D3 core is protected by the ratchet too (no 92% give-back in any sleeve)",
      "MSFT" not in state.get_sleeve("core_blend")["positions"])

# Sleeve-specific trails STILL differentiate, below the ratchet's min-gain floor:
# an +8% peak never arms the ratchet (floor 12%), so the price-trail governs and
# alpha's 15% exits while core's 18% holds — the original D3 intent, preserved.
import copy as _copy_d3
_snap_d3 = {_s: _copy_d3.deepcopy(state.get_sleeve(_s))
            for _s in ("alpha_stocks", "core_blend")}   # week-sim is a STATEFUL
# narrative — this probe must leave the book byte-identical or every downstream
# cash/equity assertion shifts underneath it (caught exactly that way).
for _sl_t, _tr in (("alpha_stocks", 15.0), ("core_blend", 18.0)):
    _st = state.get_sleeve(_sl_t)
    _st["positions"]["TRLX"] = {"symbol": "TRLX", "instrument": "equity",
                                "quantity": 10, "entry_price": 100.0,
                                "cost": 1000.0, "peak_price": 108.0,
                                "stop_pct": 50.0, "trail_stop_pct": _tr,
                                "opened_mode": "paper"}
    state.save_sleeve(_sl_t, _st)
PRICES["TRLX"] = 91.5      # -15.3% off the +8% peak: past alpha's 15%, inside core's 18%
monitor.check_all_exits(include_calls=False)
check("D3 sleeve-specific trails still differentiate below the ratchet floor",
      "TRLX" not in state.get_sleeve("alpha_stocks")["positions"]
      and "TRLX" in state.get_sleeve("core_blend")["positions"])
for _s, _v in _snap_d3.items():
    state.save_sleeve(_s, _v)
PRICES.pop("TRLX", None)

# Re-deploy so D4 has a book to break
PRICES["TSLA"] = 100.0
sleeves.run_alpha_stocks(
    {"TSLA": V("TSLA", "BUY", 80, weight=18.0, thesis="gap resolved, reset"),
     "AVGO": V("AVGO", "BUY", 75, weight=18.0, thesis="ai networking")},
    SECTORS, "paper")
a = state.get_sleeve("alpha_stocks")
_inv_val = sum(p["quantity"] * PRICES.get(k, p["entry_price"])
               for k, p in a["positions"].items())
check("D3 re-deploy: alpha holds TSLA + AVGO at 18% weights (residue parked)",
      {"TSLA", "AVGO"}.issubset(set(a["positions"]))
      and set(a["positions"]) - {"TSLA", "AVGO"} <= {"SPY"}
      # (2026-08-14 operator decision: alpha sizing halved) the 18% asks size
      # at an effective 9% -> 22 shares (was 44); residue still parks.
      and a["positions"]["TSLA"]["quantity"] == 22
      and a["cash"] / (a["cash"] + _inv_val) < 0.15,
      f"cash={a['cash']} positions={sorted(a['positions'])}")
rot_before = dict(state.get_sleeve("etf_rotation")["positions"])
sleeves.run_etf_rotation("paper")
check("D3 mid-week rotation call does NOT rotate (cadence respected)",
      set(state.get_sleeve("etf_rotation")["positions"]) == set(rot_before))
sweep_books("D3")

# ══ D4 — day boundary rolls; a breaker-crossing day trips it; exits still work ══
# (was "-4% day"; ~-3.1% since the 2026-08-14 alpha sizing halving — see the
# rescaled shock below, which keeps the 3% sleeve breaker genuinely crossed)
a = state.get_sleeve("alpha_stocks")
yesterday_et = (datetime.now(risk._ET) - timedelta(days=1)).strftime("%Y-%m-%d")
a["day_pnl_anchor"] = {"date": yesterday_et, "equity": 99_999.0}
state.save_sleeve("alpha_stocks", a)
monitor.check_all_exits(include_calls=False)   # new day: anchor must roll
a = state.get_sleeve("alpha_stocks")
check("D4 day-roll: anchor resets to TODAY at current equity (not stale)",
      a["day_pnl_anchor"]["date"] == risk._today()
      # (2026-08-14 operator decision: alpha sizing halved) book arithmetic:
      # cash 614 + TSLA 22x100 + AVGO 22x100 + SPY 33x600 = 24,814 (was 24,612.5).
      and abs(a["day_pnl_anchor"]["equity"] - 24_814.0) < 0.01,
      str(a["day_pnl_anchor"]))

jm = len(jlines())
# (2026-08-14 operator decision: alpha sizing halved) SHOCK RESCALED, not just
# re-pinned: with 22-share positions the old -13%/-10% script lost only -$506
# on the day — under the 3% sleeve breaker limit (-$744.42 of the 24,814
# anchor) — and the breaker path would have gone UNTESTED. A safety path must
# never go unexercised, so the TSLA leg deepens to -25%: realized 22 x -$25 =
# -$550 plus AVGO's held 22 x -$10 = -$220 -> day -$770, past the limit, while
# AVGO stays inside its 12% stop and the firm-wide 4% breaker still does NOT
# trip (so the per-sleeve "circuit breaker" reason below stays the one asserted).
PRICES["TSLA"] = 75.0      # -25% -> stop
PRICES["AVGO"] = 90.0      # -10% -> held; together ~-3.1% on the day
PRICES["NVDA"] = 84.0      # core's NVDA now -16% -> core stop
monitor.check_all_exits(include_calls=False)
a = state.get_sleeve("alpha_stocks")
check("D4 exits still work while the day bleeds (TSLA stop executed)",
      "TSLA" not in a["positions"] and "AVGO" in a["positions"]
      and a["closed_trades"][-1]["reason"].startswith("hard stop"))
check("D4 core stop fires at -16% and re-benches the name",
      "NVDA" not in state.get_sleeve("core_blend")["positions"]
      and state.load("watchlist", {}).get("NVDA", {})
      .get("stop_out", {}).get("sleeve") == "core_blend")

sleeves.run_alpha_stocks({"ORCL": V("ORCL", "BUY", 70)}, SECTORS, "paper")
blocked = jevents(jm, "entries_blocked")
check("D4 alpha day breaker trips: entry refused + journaled",
      "ORCL" not in state.get_sleeve("alpha_stocks")["positions"]
      and any(e.get("sleeve") == "alpha_stocks"
              and "circuit breaker" in e.get("reason", "") for e in blocked),
      str([e.get("reason", "")[:60] for e in blocked]))
check("D4 breaker trip recorded for the review",
      bool(jevents(jm, "breaker_tripped")))

learning.record_outcomes()
# Since 2026-07-01 (goal audit fix 6) the MONITOR SWEEP harvests closes into the
# ledger as they happen, so this later explicit call returns ~0 NEW — assert the
# LEDGER's accumulated content, not one call's return value. Equitization
# round-trips are deliberately excluded (fix 4c), so count real trades only.
_n_led = len(state.load("ledger", {}).get("trades", []))
check("D4 ledger holds >=5 closed trades (sweep-time + cycle-end harvest)",
      _n_led >= 5, f"n={_n_led}")
ledger = state.load("ledger", {})
nv_row = next((t for t in ledger.get("trades", [])
               if t["ticker"] == "NVDA" and t["sleeve"] == "alpha_stocks"), None)
check("D4 ledger row carries the verdict-at-entry attribution",
      nv_row is not None
      and (nv_row.get("verdict") or {}).get("rating") == "STRONG_BUY"
      and (nv_row.get("verdict") or {}).get("conviction") == 85
      and nv_row.get("depth") == "standard", str(nv_row)[:160])

# A7 cold-start guard (2026-06-14 audit): the FIRST review must NOT fire on a tiny
# few-day sample — its founding lessons would overfit one regime.
check("D4 cold-start guard blocks the FIRST review on a too-small/too-short sample",
      learning.run_review() is None)
# Simulate an established (post-cold-start) fund so this exercises the ONGOING gate.
_ls_cs = state.load("lessons", {"lessons": [], "reviewed_through": 0})
_ls_cs["reviewed_through"] = 1
state.save("lessons", _ls_cs)
rev = learning.run_review()        # ongoing gate: 5 new trades >= 5, no force needed
lessons = state.load("lessons", {})
check("D4 review runs off the trade gate and saves 2 lessons",
      rev and len(rev["lessons"]) == 2
      and len(lessons.get("lessons", [])) == 2,
      str(lessons.get("lessons"))[:120])
check("D4 reviewed_through advances via the monotonic counter",
      lessons.get("reviewed_through") == ledger.get("total_recorded")
      and ledger.get("total_recorded", 0) >= 6,
      f"reviewed={lessons.get('reviewed_through')} total={ledger.get('total_recorded')}")
check("D4 lessons injected into prompt context",
      "FIRM MEMORY" in learning.lessons_context())
sweep_books("D4")

# ══ D5 — rotation_days elapsed -> rotate; paper buys respect the bell ═════════
r = state.get_sleeve("etf_rotation")
r["last_rotation"] = ts(30)        # 30 days > rotation_days (28): due
state.save_sleeve("etf_rotation", r)
ROTATION["ranked"] = [("GLD", 51.0), ("TLT", 44.0), ("XLE", 38.0),
                      ("SMH", 4.0), ("XLK", 2.0), ("QQQ", 1.5)]
PRICES["SMH"] = 600.0              # old winner exits at a profit
jm = len(jlines())
sleeves.run_etf_rotation("paper")
r = state.get_sleeve("etf_rotation")
check("D5 rotation rolls into the new scripted winners",
      set(r["positions"]) == {"GLD", "TLT", "XLE"}, str(sorted(r["positions"])))
smh_close = next((t for t in r["closed_trades"] if t["symbol"] == "SMH"), None)
check("D5 rotation exit books the gain with strategy attribution",
      smh_close is not None and smh_close["pnl"] == 280.0
      and smh_close["strategy"] == "dual_momentum"
      and smh_close["momentum_score"] == 42.0, str(smh_close))
sig_ev = jevents(jm, "rotation_signal")
check("D5 rotation signal journaled with targets",
      sig_ev and "GLD" in sig_ev[-1].get("targets", []))

# Market-hours gate: paper buy refused after the bell, exit always allowed
config.PAPER_ENFORCE_MARKET_HOURS = True
_real_ext = mh.extended_session_open
mh.market_is_open = lambda now=None: False
mh.extended_session_open = lambda now=None: False   # pin: else this depends on run time
gate = state.default_sleeve_state("alpha_stocks")
res = broker.execute("paper", gate, symbol="GATE", side="buy", quantity=1, price=10.0)
check("D5 paper buy refused while the market is closed",
      not res["success"] and "closed" in res.get("error", ""))
gate["positions"]["GATE"] = {"symbol": "GATE", "instrument": "equity",
                             "quantity": 1, "entry_price": 10.0, "cost": 10.0,
                             "peak_price": 10.0, "opened_mode": "paper"}
# CHANGED 2026-07-25 (was "D5 paper exit still allowed while the market is
# closed"). Paper sells now mirror the live gate: a paper exit into a closed
# market books a fill live could never get. Real incident behind the change —
# OSCR and CON stopped out on SATURDAY at Friday's closing marks, teaching the
# exit-ladder designer that stops are cheaper than they are.
res = broker.execute("paper", gate, symbol="GATE", side="sell", quantity=1, price=9.0)
check("D5 paper exit REFUSED on a fully closed market (mirrors live)",
      not res["success"] and "closed" in res.get("error", ""))
mh.extended_session_open = lambda now=None: True
res = broker.execute("paper", gate, symbol="GATE", side="sell", quantity=1, price=9.0)
check("D5 paper equity exit still allowed in the extended session", res["success"])
config.PAPER_ENFORCE_MARKET_HOURS = False
mh.extended_session_open = _real_ext
mh.market_is_open = lambda now=None: True
sweep_books("D5")


# ══ LIVE-mode plumbing (fresh sandbox) ════════════════════════════════════════
class MockPublicClient:
    """Implements the slice of the Public.com SDK client that firm/broker.py's
    live path touches: get_portfolio, place_order, get_order, cancel_order,
    get_quotes. Order states are scripted by the scenario."""

    def __init__(self):
        self.orders = {}
        self.cancelled = []

    def get_portfolio(self):
        bp = SimpleNamespace(cash_only_buying_power=100_000.0,
                             options_buying_power=50_000.0)
        return SimpleNamespace(buying_power=bp, positions=[])

    def place_order(self, req):
        oid = str(req.order_id)
        self.orders[oid] = SimpleNamespace(order_id=oid, status="NEW",
                                           filled_quantity=0.0, average_price=0.0)
        return self.orders[oid]

    def get_order(self, oid):
        return self.orders[oid]

    def cancel_order(self, oid):
        self.cancelled.append(oid)

    def get_quotes(self, instruments=None, **kw):
        out = []
        for ins in instruments or []:
            px = PRICES.get(getattr(ins, "symbol", ""), 0.0)
            out.append(SimpleNamespace(symbol=getattr(ins, "symbol", None),
                                       bid=px or None, ask=px or None, last=px,
                                       instrument=ins))
        return out


config.STATE_DIR = LIVE_DIR
mock_client = MockPublicClient()
broker.get_client = lambda: mock_client
mh.market_is_open = lambda now=None: True
state.set_settings(mode="live")

ls = state.default_sleeve_state("alpha_stocks")
res = broker.execute("live", ls, symbol="NVDA", side="buy", quantity=10,
                     price=100.0, meta={"thesis": "live plumbing test"})
check("LIVE buy goes PENDING (never assumed filled) with cash reserved",
      res.get("success") and res.get("pending")
      and abs(ls["cash"] - 24_000.0) < 0.01
      and ls["pending_orders"][0]["reserved"] == 1_000.0,
      f"cash={ls['cash']} pending={len(ls['pending_orders'])}")

oid = ls["pending_orders"][0]["order_id"]
mock_client.orders[oid].status = "CANCELLED"      # DAY order: partial then out
mock_client.orders[oid].filled_quantity = 4.0
mock_client.orders[oid].average_price = 99.5
settled = broker.settle_pending(ls)
pos = ls["positions"].get("NVDA", {})
check("LIVE partial fill books ONLY the filled part at the ACTUAL avg price",
      settled == 1 and pos.get("quantity") == 4.0
      and pos.get("entry_price") == 99.5 and not ls["pending_orders"],
      str(pos)[:120])
check("LIVE unfilled remainder refunded exactly",
      abs(ls["cash"] - 24_602.0) < 0.01, f"cash={ls['cash']}")
check("LIVE position tagged with its opening mode",
      pos.get("opened_mode") == "live")

res = broker.execute("live", ls, symbol="NVDA", side="sell", quantity=4,
                     price=95.0, meta={"reason": "protective stop"})
oid2 = ls["pending_orders"][0]["order_id"]
check("LIVE protective sell goes pending and locks the position",
      res.get("pending") and ls["positions"]["NVDA"].get("exit_pending") == oid2)
ls["pending_orders"][0]["placed_at"] = (
    datetime.now(timezone.utc) - timedelta(seconds=broker.EXIT_CHASE_S + 50)).isoformat()
settled = broker.settle_pending(ls)
check("LIVE unfilled exit gets cancel-chased after EXIT_CHASE_S",
      settled == 0 and mock_client.cancelled == [oid2]
      and ls["pending_orders"][0].get("cancel_sent")
      and bool(jevents(0, "exit_chase_cancel", LIVE_DIR)))
mock_client.orders[oid2].status = "CANCELLED"      # broker confirms the cancel
settled = broker.settle_pending(ls)
check("LIVE cancelled exit settles clean: lock released, shares intact",
      settled == 1 and "exit_pending" not in ls["positions"]["NVDA"]
      and ls["positions"]["NVDA"]["quantity"] == 4.0
      and abs(ls["cash"] - 24_602.0) < 0.01)
state.save_sleeve("alpha_stocks", ls)

import app as app_mod  # noqa: E402  (import-safe: threads only start in __main__)

res = app_mod.update_settings(app_mod.SettingsUpdate(mode="paper"))
check("LIVE->paper mode flip refused while positions are open",
      getattr(res, "status_code", 200) == 409
      and bool(jevents(0, "mode_flip_refused", LIVE_DIR)))
state.set_settings(mode="paper")                   # simulate a paper firm
res = app_mod.update_settings(app_mod.SettingsUpdate(mode="live"))
check("paper->LIVE mode flip refused too (books must be flat)",
      getattr(res, "status_code", 200) == 409)

res = broker.execute("paper", ls, symbol="NVDA", side="sell", quantity=4, price=95.0)
check("cross-mode sell refused (live position, paper order)",
      not res["success"] and "mode" in res.get("error", ""))
sweep_books("LIVE")


# ══ FINAL INVARIANTS across the whole sim ═════════════════════════════════════
config.STATE_DIR = WEEK_DIR
learning.record_outcomes()         # pick up the D5 rotation closes
ledger = state.load("ledger", {})
rows = {(t.get("symbol"), t.get("closed_at")) for t in ledger.get("trades", [])}
missing, total_closed = [], 0
for sl in config.SLEEVES:
    s = state.get_sleeve(sl)
    for t in s.get("closed_trades", []):
        # Cash-equitization round-trips are deliberately NOT ledgered (goal
        # audit fix 4c, 2026-07-01): parking plumbing must never open the
        # 20-trade cold-start gate or dilute the win-rate stats.
        if t.get("strategy") == "cash_equitization":
            continue
        total_closed += 1
        if (t["symbol"], t.get("closed_at")) not in rows:
            missing.append(f"{sl}:{t['symbol']}")
check("FINAL every closed trade has a matching ledger row",
      total_closed >= 9 and not missing,
      f"closed={total_closed} missing={missing}")
check("FINAL no sleeve cash ever went negative; no malformed position",
      not VIOLATIONS, str(VIOLATIONS)[:200])

for label, d in (("week", WEEK_DIR), ("live", LIVE_DIR)):
    lines = jlines(d)
    parsed = 0
    for ln in lines:
        try:
            json.loads(ln)
            parsed += 1
        except json.JSONDecodeError:
            pass
    check(f"FINAL {label} journal.jsonl parses line-by-line as JSON",
          lines and parsed == len(lines), f"{parsed}/{len(lines)}")

bad_pos = []
for d in (WEEK_DIR, LIVE_DIR):
    config.STATE_DIR = d
    for sl in config.SLEEVES:
        s = state.get_sleeve(sl)
        for sym, p in s["positions"].items():
            if (not isinstance(p.get("entry_price"), (int, float))
                    or not isinstance(p.get("quantity"), (int, float))):
                bad_pos.append(f"{d.name}:{sl}:{sym}")
config.STATE_DIR = WEEK_DIR
check("FINAL no position object missing entry_price/quantity", not bad_pos,
      str(bad_pos))

# ══ Report ════════════════════════════════════════════════════════════════════
print()
for status, name, detail in RESULTS:
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
fails = [x for x in RESULTS if x[0] == "FAIL"]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
sys.exit(1 if fails else 0)
