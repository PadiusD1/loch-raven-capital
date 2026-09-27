"""Loch Raven Capital — E2E PRODUCTION-READINESS suite.

ONE end-to-end test that answers two questions the operator actually asks:

  1. "Does the firm RUN the way it is supposed to — 100% of the time, in LIVE
      and in DEMO (paper) mode?"  -> it drives the REAL decision -> execution ->
      settle -> risk -> state code path (the same functions run_cycle() calls at
      steps 75/82/88/93, PLUS the live pending->settle->book cycle) in BOTH modes
      and asserts they converge on the same book; only the fill timing differs.
  2. "Is what's INSIDE the config file actually the BEST/safest thing for the
      fund to run on?" -> it asserts the *content* of config.py against the
      firm's own invariants (universe sanity, risk-cap bounds, sleeve budgets,
      the calls ladder, cash-equitization floors), not just that code imports.

Runs FULLY OFFLINE in throwaway state sandboxes — never touches the real books,
no Opus calls, no market data. Re-run after ANY change to sleeves/risk/config:

    py -3.11 tests\\test_e2e_production.py

Anchor regressions (operator 2026-07-02, live journal evidence):
  A) A DUE monthly ETF rotation was frozen because the per-sleeve daily LOSS
     breaker kept deferring it every cycle -> the sleeve held June's SMH/XLK/EEM
     into July with last_rotation stuck at Jun 9.  §3 locks that a due mechanical
     rebalance runs THROUGH the per-sleeve loss breaker; §4 keeps the legacy
     defer selectable; §4b proves a true FIRM-WIDE crash brake is still honored.
  B) In LIVE mode the rotation's sells go pending, so the same-pass buys were
     starved AND last_rotation advanced anyway -> the live sleeve stranded in
     cash for a month.  §5 proves the rotation now HOLDS last_rotation until the
     buy leg funds and SELF-CORRECTS across the pending->settle cycle.
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Isolate ALL state into a throwaway sandbox BEFORE importing firm ─────────
import config  # noqa: E402

config.STATE_DIR = Path(tempfile.mkdtemp(prefix="lochraven_e2e_"))
assert "lochraven_e2e_" in str(config.STATE_DIR)   # NEVER the real books
config.PAPER_SLIPPAGE = {"equity_bps": 0.0, "option_spread_frac": 0.0,
                         "option_fee_per_contract": 0.0}
config.PAPER_ENFORCE_MARKET_HOURS = False
config.TRANSCRIPT_HEADLESS = False

import firm.broker as broker  # noqa: E402
import firm.data_engine as data_engine  # noqa: E402
import firm.market_hours as mh  # noqa: E402
from firm import alerts, riskbrain, sleeves, state, risk  # noqa: E402

# Deterministic macro snapshot so nothing reaches for the network.
state.save("macro_calendar", {"ts": datetime.now(timezone.utc).isoformat(),
                              "tier": "rule", "overlay": {}})

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(("PASS" if bool(ok) else "FAIL", name, detail))


def iso(dt):
    return dt.isoformat()


# ══ Scripted offline world ════════════════════════════════════════════════════
PRICES = {
    "SMH": 580.0, "XLK": 180.0, "EEM": 65.0, "GLD": 250.0, "TLT": 90.0,
    "XLU": 75.0, "QQQ": 480.0, "IWM": 230.0, "XLE": 85.0, "BIL": 91.5,
    "SPY": 600.0, "VOO": 500.0, "NVDA": 100.0, "AMD": 100.0, "MSFT": 100.0,
}
# A DUE-rotation signal whose top-3 (GLD/TLT/XLU) are DISTINCT from the held
# June book (SMH/XLK/EEM), so a real rotation VISIBLY changes the holdings.
ROTATION_SIGNAL = {
    "ranked": [("GLD", 40.0), ("TLT", 35.0), ("XLU", 30.0),
               ("SMH", 12.0), ("XLK", 10.0), ("EEM", -3.0)],
    "cash_hurdle_3m": 1.0,
}


def _px_map(symbols):
    return {s: PRICES[s] for s in symbols if s in PRICES}


# All price/signal sources read the mutable dicts above — fully offline.
data_engine.batch_last_prices = lambda tickers: _px_map(tickers)
data_engine.last_price = lambda t: PRICES.get(t, 0.0)
data_engine.rotation_signals = lambda: {"ranked": list(ROTATION_SIGNAL["ranked"]),
                                         "cash_hurdle_3m": ROTATION_SIGNAL["cash_hurdle_3m"]}
broker.equity_prices = lambda syms: _px_map(syms)
broker.equity_quotes_public = lambda symbols: _px_map(symbols)
riskbrain.vol_multiplier = lambda: 1.0          # deterministic breaker width
alerts._toast = lambda *a, **k: None            # no Windows toasts in tests
mh.market_is_open = lambda now=None: True


def prev_month_last_day_iso():
    """Last day of the previous calendar month, UTC ISO. A DIFFERENT (year,
    month) than today with a small day-gap -> the rotation is due via the MONTH
    change, not the staleness backstop."""
    now = datetime.now(timezone.utc)
    return iso(now.replace(day=1, hour=12, minute=0, second=0, microsecond=0)
               - timedelta(days=1))


def seed_due_rotation_sleeve(*, breaker_tripped: bool, halted: bool = False,
                             opened_mode: str = "paper"):
    """A fresh etf_rotation sleeve holding LAST month's book (SMH/XLK/EEM),
    optionally seeded so the per-sleeve daily loss breaker is TRIPPED today.
    `opened_mode` tags the positions so a LIVE run can actually close them
    (the cross-mode guard refuses to close a paper position with a live order).
    Returns the saved sleeve dict."""
    s = state.default_sleeve_state("etf_rotation")

    def _pos(sym, qty, entry, cost, peak):
        return {"symbol": sym, "instrument": "equity", "quantity": qty,
                "entry_price": entry, "cost": cost, "peak_price": peak,
                "last_price": PRICES[sym], "opened_at": prev_month_last_day_iso(),
                "reason": "rotation entry", "opened_mode": opened_mode}

    s["positions"] = {
        "SMH": _pos("SMH", 14.0, 560.0, 7840.0, 600.0),
        "XLK": _pos("XLK", 45.0, 175.0, 7875.0, 185.0),
        "EEM": _pos("EEM", 120.0, 66.0, 7920.0, 71.0),
    }
    s["cash"] = 120.0
    s["last_rotation"] = prev_month_last_day_iso()
    s["halted"] = halted
    if halted:
        s["halt_reason"] = "seeded halt (test)"
    held_eq = broker.sleeve_equity(s, _px_map(list(s["positions"])))
    # Anchor ABOVE current equity so day P&L is red enough to trip the 2.5%
    # per-sleeve breaker; peak just above anchor so the -20% kill never fires.
    anchor_eq = held_eq + 1_900.0 if breaker_tripped else held_eq
    today = datetime.now(timezone.utc).astimezone(risk._ET).strftime("%Y-%m-%d")
    s["day_pnl_anchor"] = {"date": today, "equity": anchor_eq}
    s["peak_equity"] = anchor_eq
    state.save_sleeve("etf_rotation", s)
    return s


def journal_has(event, dirpath=None):
    p = (dirpath or config.STATE_DIR) / "journal.jsonl"
    return p.exists() and f'"event": "{event}"' in p.read_text(encoding="utf-8")


def cur_month(iso_ts):
    try:
        d = datetime.fromisoformat(iso_ts).astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        return (d.year, d.month) == (now.year, now.month)
    except (ValueError, TypeError, AttributeError):
        return False


# ══ §1  CONFIG-CONTENT INVARIANTS — "is what's in the file the best to run on?"
S = config.SLEEVES

check("§1 all four sleeves are configured",
      set(S) == {"alpha_stocks", "core_blend", "calls_only", "etf_rotation"},
      str(sorted(S)))
check("§1 every sleeve has a positive paper capital + live BP fraction",
      all(S[k]["paper_capital"] > 0 and 0 < S[k]["live_pct_of_bp"] <= 1.0 for k in S))
check("§1 live BP fractions sum to <= 100% (can't over-allocate the account)",
      sum(S[k]["live_pct_of_bp"] for k in S) <= 1.0 + 1e-9,
      f"sum={sum(S[k]['live_pct_of_bp'] for k in S)}")
check("§1 every sleeve has a positive daily breaker % and drawdown kill %",
      all(S[k].get("day_breaker_pct", 0) > 0 and S[k].get("kill_dd_pct", 0) > 0
          for k in S))
check("§1 firm daily breaker sits ABOVE the widest EQUITY sleeve breaker "
      "(catches correlated bleed, not one sleeve)",
      config.FIRM_DAILY_LOSS_BREAKER_PCT >= max(
          S["alpha_stocks"]["day_breaker_pct"], S["core_blend"]["day_breaker_pct"],
          S["etf_rotation"]["day_breaker_pct"]),
      f"firm={config.FIRM_DAILY_LOSS_BREAKER_PCT}")
er = S["etf_rotation"]
check("§1 rotation top_n is >=1 and fits inside the rotation universe",
      1 <= er["top_n"] <= len(config.ROTATION_UNIVERSE), f"top_n={er['top_n']}")
check("§1 rotation cadence is sane (roughly monthly, 1..60 days)",
      1 <= er["rotation_days"] <= 60, f"rotation_days={er['rotation_days']}")
check("§1 rotation momentum blend weights sum to 1.0",
      abs(sum(er["mom_blend"]) - 1.0) < 1e-9, f"mom_blend={er['mom_blend']}")
check("§1 rotation universe ⊆ the defined ETF universe, no duplicates",
      set(config.ROTATION_UNIVERSE) <= set(config.ETF_UNIVERSE)
      and len(config.ROTATION_UNIVERSE) == len(set(config.ROTATION_UNIVERSE)))
check("§1 cash ETF is set and is a DISTINCT fallback (not itself rotated)",
      config.CASH_ETF and config.CASH_ETF not in config.ROTATION_UNIVERSE,
      f"cash_etf={config.CASH_ETF}")
c = S["calls_only"]
check("§1 calls exit ladder is monotonic (SL>0 < TP1 < TP2 < TP3) and DTE valid",
      c["hard_sl_pct"] > 0 and c["tp1_pct"] < c["tp2_pct"] < c["tp3_pct"]
      and 0 < c["tp1_sell_pct"] <= 100 and c["min_dte"] <= c["max_dte"])
cap_ok, cap_bad = True, []
for key, (lo, hi) in config.CAP_BOUNDS.items():
    val = getattr(config, key, None)
    if val is None or not (lo <= val <= hi):
        cap_ok = False
        cap_bad.append(f"{key}={val} not in [{lo},{hi}]")
check("§1 every default risk cap sits inside its learnable CAP_BOUNDS",
      cap_ok, "; ".join(cap_bad))
eq_ok = all(0 <= v["min_invested_pct"] <= v["target_invested_pct"] <= 100
            for v in config.CASH_EQUITIZATION.values())
check("§1 cash-equitization floors are ordered (0 <= min <= target <= 100)", eq_ok)
check("§1 basic tradability + buffer floors are positive",
      config.MIN_PRICE > 0 and config.MIN_AVG_DOLLAR_VOL > 0
      and config.MIN_CASH_BUFFER > 0 and config.SCAN_MIN_TRADABLE > 0)
check("§1 rotation-vs-breaker policy knob is a bool (explicit + operator-tunable)",
      isinstance(getattr(config, "ROTATION_IGNORES_LOSS_BREAKER", True), bool))


# ══ §2  CALENDAR-ANCHORED rotation-due logic — FIXED synthetic clock (no wall-
#        clock fragility: _rotation_due takes `now` as a parameter) ════════════
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)          # fixed mid-month
PREV_MO = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)      # prior calendar month
SAME_MO = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)      # same month, 1d ago
check("§2 due when a NEW calendar month began (even if < rotation_days elapsed)",
      sleeves._rotation_due(PREV_MO, (NOW - PREV_MO).days, er["rotation_days"], NOW) is True)
check("§2 NOT due mid-month when < rotation_days have elapsed",
      sleeves._rotation_due(SAME_MO, 1, er["rotation_days"], NOW) is False)
check("§2 due as a staleness backstop once rotation_days elapse in-month",
      sleeves._rotation_due(NOW - timedelta(days=er["rotation_days"] + 1),
                            er["rotation_days"] + 1, er["rotation_days"], NOW) is True)
check("§2 due when there is no valid prior rotation timestamp",
      sleeves._rotation_due(None, 9999, er["rotation_days"], NOW) is True)


# ══ §3  THE BUG: a DUE rotation must fire despite a tripped PER-SLEEVE breaker ══
config.ROTATION_IGNORES_LOSS_BREAKER = True     # production default
s = seed_due_rotation_sleeve(breaker_tripped=True)
held_eq = broker.sleeve_equity(s, _px_map(list(s["positions"])))
_ok, _why = risk.entries_allowed("etf_rotation", s, held_eq)
check("§3 scenario is valid: the per-sleeve daily loss breaker IS tripped",
      _ok is False and "circuit breaker" in _why, _why[:80])

sleeves.run_etf_rotation("paper")
r = state.get_sleeve("etf_rotation")
check("§3 a DUE rotation FIRED despite the tripped breaker "
      "(last_rotation advanced into the current month)",
      cur_month(r.get("last_rotation")), f"last_rotation={r.get('last_rotation')}")
check("§3 the sleeve actually ROTATED into the new momentum leaders",
      set(r["positions"]) == {"GLD", "TLT", "XLU"}, str(sorted(r["positions"])))
check("§3 the stale June book (SMH/XLK/EEM) was fully rotated OUT",
      not ({"SMH", "XLK", "EEM"} & set(r["positions"])))
check("§3 the breaker OVERRIDE is journalled (auditable, not silent)",
      journal_has("rotation_breaker_override"))


# ══ §3b  A HALTED rotation blocks the BUY leg but the protective abs-momentum
#         exit STILL routes (the halt/exit asymmetry the sleeve is built on) ════
s = seed_due_rotation_sleeve(breaker_tripped=False, halted=True)
before_lr = s["last_rotation"]
sleeves.run_etf_rotation("paper")
r = state.get_sleeve("etf_rotation")
check("§3b a HALTED sleeve does NOT run the full rotation (buy leg blocked)",
      r["last_rotation"] == before_lr and not ({"GLD", "TLT", "XLU"} & set(r["positions"])),
      f"last_rotation={r.get('last_rotation')} pos={sorted(r['positions'])}")
check("§3b ...but the below-hurdle held name (EEM, mom -3 <= 1.0) STILL exits to cash",
      "EEM" not in r["positions"] and journal_has("rotation_abs_momentum_exit"),
      str(sorted(r["positions"])))


# ══ §4  LEGACY path still selectable: flag OFF -> the whole rotation defers ═════
config.ROTATION_IGNORES_LOSS_BREAKER = False
s = seed_due_rotation_sleeve(breaker_tripped=True)
before_lr = s["last_rotation"]
sleeves.run_etf_rotation("paper")
r = state.get_sleeve("etf_rotation")
check("§4 with the knob OFF, a tripped breaker DEFERS the rotation (legacy)",
      r["last_rotation"] == before_lr and not ({"GLD", "TLT", "XLU"} & set(r["positions"])),
      f"last_rotation={r.get('last_rotation')} pos={sorted(r['positions'])}")
config.ROTATION_IGNORES_LOSS_BREAKER = True     # restore production default


# ══ §4b  A true FIRM-WIDE crash brake is STILL honored — the override bypasses
#         ONLY the per-sleeve breaker, never the firm-wide all-stop ════════════
fw_dir = Path(tempfile.mkdtemp(prefix="lochraven_e2e_firmbrk_"))
_prev = config.STATE_DIR
config.STATE_DIR = fw_dir
state.save("macro_calendar", {"ts": iso(datetime.now(timezone.utc)),
                              "tier": "rule", "overlay": {}})
state.set_settings(mode="paper")
seed_due_rotation_sleeve(breaker_tripped=False)          # rotation sleeve is DUE
today = datetime.now(timezone.utc).astimezone(risk._ET).strftime("%Y-%m-%d")
for k in config.SLEEVES:                                  # drive every sleeve deep red
    sv = state.get_sleeve(k)
    eq = broker.sleeve_equity(sv, _px_map(list(sv.get("positions", {}))))
    sv["day_pnl_anchor"] = {"date": today, "equity": eq + 3_000.0}
    sv["peak_equity"] = max(sv.get("peak_equity", eq), eq + 3_000.0)
    state.save_sleeve(k, sv)
firm_ok, firm_why = risk.firm_daily_breaker()
check("§4b scenario is valid: the FIRM-WIDE daily breaker is tripped",
      firm_ok is False and "FIRM daily breaker" in firm_why, firm_why[:70])
before_lr = state.get_sleeve("etf_rotation")["last_rotation"]
sleeves.run_etf_rotation("paper")
r = state.get_sleeve("etf_rotation")
check("§4b a DUE rotation DEFERS on a firm-wide crash (override honors the all-stop)",
      r["last_rotation"] == before_lr and not ({"GLD", "TLT", "XLU"} & set(r["positions"])),
      f"last_rotation={r.get('last_rotation')} pos={sorted(r['positions'])}")
config.STATE_DIR = _prev
state.set_settings(mode="paper")


# ══ §5  LIVE vs DEMO: same DECISION, and LIVE SELF-CORRECTS across settle ══════
class MockPublicClient:
    """The slice of the Public.com SDK the live path touches. Orders are placed
    NEW; the scenario flips them to FILLED before calling settle_pending()."""

    def __init__(self):
        self.orders = {}

    def get_portfolio(self):
        bp = SimpleNamespace(cash_only_buying_power=1_000_000.0,
                             options_buying_power=500_000.0)
        return SimpleNamespace(buying_power=bp, positions=[])

    def place_order(self, req):
        oid = str(req.order_id)
        self.orders[oid] = SimpleNamespace(order_id=oid, status="NEW",
                                           filled_quantity=0.0, average_price=0.0)
        return self.orders[oid]

    def get_order(self, oid):
        return self.orders[oid]

    def cancel_order(self, oid):
        self.orders[oid].status = "CANCELLED"

    def get_quotes(self, instruments=None, **kw):
        out = []
        for ins in instruments or []:
            sym = getattr(ins, "symbol", "")
            px = PRICES.get(sym, 0.0)
            out.append(SimpleNamespace(symbol=sym, bid=px or None, ask=px or None,
                                       last=px, instrument=ins))
        return out


def rotation_decision(mode: str) -> dict:
    """Run one DUE rotation in a fresh sandbox in the given mode; capture the set
    of symbols the sleeve DECIDES to buy vs sell (spying on broker.execute)."""
    prev_dir, prev_client = config.STATE_DIR, broker.get_client
    config.STATE_DIR = Path(tempfile.mkdtemp(prefix=f"lochraven_e2e_{mode}_"))
    state.save("macro_calendar", {"ts": iso(datetime.now(timezone.utc)),
                                  "tier": "rule", "overlay": {}})
    state.set_settings(mode=mode)
    if mode == "live":
        broker.get_client = lambda: MockPublicClient()
    seed_due_rotation_sleeve(breaker_tripped=True)
    orders = {"buy": set(), "sell": set()}
    real_execute = broker.execute

    def spy(m, sl, **kw):
        if kw.get("side") in ("buy", "sell") and kw.get("symbol"):
            orders[kw["side"]].add(kw["symbol"])
        return real_execute(m, sl, **kw)

    broker.execute = spy
    try:
        sleeves.run_etf_rotation(mode)
    finally:
        broker.execute = real_execute
        broker.get_client = prev_client
        state.set_settings(mode="paper")
        config.STATE_DIR = prev_dir
    return orders


paper_dec = rotation_decision("paper")
check("§5 DEMO (paper) rotation decides: buy the 3 leaders, sell the 3 laggards",
      paper_dec["buy"] == {"GLD", "TLT", "XLU"}
      and {"SMH", "XLK", "EEM"} <= paper_dec["sell"], str(paper_dec))

# LIVE: a persistent sandbox + one client, driven across the pending->settle cycle.
live_dir = Path(tempfile.mkdtemp(prefix="lochraven_e2e_live_"))
_prev_dir, _prev_client = config.STATE_DIR, broker.get_client
config.STATE_DIR = live_dir
state.save("macro_calendar", {"ts": iso(datetime.now(timezone.utc)),
                              "tier": "rule", "overlay": {}})
state.set_settings(mode="live")
client = MockPublicClient()
broker.get_client = lambda: client
seed_lr = seed_due_rotation_sleeve(breaker_tripped=True, opened_mode="live")["last_rotation"]

cash_ok = []


def _cash_sane(sl):
    reserved = sum(p.get("reserved", 0) for p in sl.get("pending_orders", []))
    cash_ok.append(sl["cash"] >= -1e-6 and sl["cash"] + reserved >= -1e-6)


def settle_all(sl):
    for p in list(sl.get("pending_orders", [])):
        o = client.orders[p["order_id"]]
        o.status = "FILLED"
        o.filled_quantity = p["quantity"]
        o.average_price = p["limit_price"]
    n = broker.settle_pending(sl)
    state.save_sleeve("etf_rotation", sl)
    return n


# Pass 1: sells go pending, buys are STARVED — the sleeve must NOT strand.
sleeves.run_etf_rotation("live")
r1 = state.get_sleeve("etf_rotation")
_cash_sane(r1)
pend_sells = {p["symbol"] for p in r1.get("pending_orders", []) if p["side"] == "sell"}
pend_buys = {p["symbol"] for p in r1.get("pending_orders", []) if p["side"] == "buy"}
check("§5 LIVE routes the IDENTICAL SELL decisions (sell-leg parity vs demo)",
      pend_sells == {"SMH", "XLK", "EEM"} == paper_dec["sell"], str(pend_sells))
check("§5 LIVE does NOT strand: buys starved this pass -> last_rotation HELD "
      "for retry (not advanced), never frozen for a month",
      r1["last_rotation"] == seed_lr and not pend_buys
      and journal_has("rotation_buy_leg_deferred", live_dir),
      f"last_rotation={r1['last_rotation']} pend_buys={pend_buys}")

# The sells settle on the next sweep -> cash is credited, old book removed.
settle_all(r1)
r1b = state.get_sleeve("etf_rotation")
_cash_sane(r1b)

# Pass 2 (retry, still due): now-funded buys route; settle books them.
sleeves.run_etf_rotation("live")
r2 = state.get_sleeve("etf_rotation")
_cash_sane(r2)
settle_all(r2)
r3 = state.get_sleeve("etf_rotation")
_cash_sane(r3)
check("§5 LIVE SELF-CORRECTS: after the pending->settle cycle the sleeve holds "
      "the SAME leaders demo bought, and last_rotation finally advances",
      set(r3["positions"]) == {"GLD", "TLT", "XLU"} and cur_month(r3["last_rotation"]),
      f"pos={sorted(r3['positions'])} last_rotation={r3['last_rotation']}")
check("§5 sleeve cash + reserved never went negative across the whole live cycle",
      all(cash_ok), f"snapshots_ok={cash_ok}")

broker.get_client = _prev_client
config.STATE_DIR = _prev_dir
state.set_settings(mode="paper")


# ══ §5c  Anti-livelock: a STRUCTURALLY unbuyable target (persistent no-price)
#         must NOT freeze the clock forever — after the retry cap it advances +
#         alerts ONCE instead of logging "deferred" every cycle. ════════════════
sc_dir = Path(tempfile.mkdtemp(prefix="lochraven_e2e_struct_"))
_pd = config.STATE_DIR
config.STATE_DIR = sc_dir
state.save("macro_calendar", {"ts": iso(datetime.now(timezone.utc)),
                              "tier": "rule", "overlay": {}})
state.set_settings(mode="paper")
_real_eqp = broker.equity_prices
# Price the HELD names (so they can be sold) but NOT the rotation targets ->
# the buy leg is structurally starved every cycle (no price, never settles).
broker.equity_prices = lambda syms: {k: v for k, v in _px_map(syms).items()
                                     if k in ("SMH", "XLK", "EEM")}
seed_due_rotation_sleeve(breaker_tripped=False)
struct_lr0 = state.get_sleeve("etf_rotation")["last_rotation"]
for _ in range(config.ROTATION_MAX_BUY_DEFERS + 1):
    sleeves.run_etf_rotation("paper")
r = state.get_sleeve("etf_rotation")
check("§5c a persistently-unbuyable target advances the clock after the retry cap "
      "(no livelock / no per-cycle alarm spam)",
      cur_month(r["last_rotation"]) and r["last_rotation"] != struct_lr0
      and journal_has("rotation_buy_leg_unfunded", sc_dir),
      f"last_rotation={r['last_rotation']}")
broker.equity_prices = _real_eqp
config.STATE_DIR = _pd
state.set_settings(mode="paper")


# ══ §6  END-TO-END firm pass: every sleeve decides + executes, books stay sane ═
config.STATE_DIR = Path(tempfile.mkdtemp(prefix="lochraven_e2e_firm_"))
state.save("macro_calendar", {"ts": iso(datetime.now(timezone.utc)),
                              "tier": "rule", "overlay": {}})
state.set_settings(mode="paper")
_earn = (datetime.now(timezone.utc) + timedelta(days=180))   # computed, not hardcoded


def pkt(ticker, sector="Technology", price=None):
    return {"ticker": ticker,
            "asof": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "quant_screen_score": 70.0,
            "technicals": {"price": price or PRICES.get(ticker, 100.0),
                           "vol_ann_pct": 30.0, "avg_dollar_vol": 5e8},
            "fundamentals": {"ticker": ticker, "sector": sector},
            "news_headlines": ["(scripted offline packet)"],
            "next_earnings": {"date": _earn.strftime("%Y-%m-%d"), "days_away": 180}}


def verdict(ticker, rating, conviction, weight=10.0, calls=False):
    return {"kind": "stock",
            "verdict": {"rating": rating, "conviction": conviction,
                        "target_weight_pct": weight, "thesis": f"{ticker} thesis",
                        "suitable_for_calls": calls},
            "packet": pkt(ticker)}


data_engine.build_packet = lambda t, tech=None, quant=60.0: pkt(t)
data_engine.fetch_history = lambda tickers, period="2y": None  # unused in this pass
SECTORS = {"NVDA": "Technology", "AMD": "Technology", "MSFT": "Communication Services"}
VERDICTS = {"NVDA": verdict("NVDA", "STRONG_BUY", 85, calls=True),
            "AMD": verdict("AMD", "BUY", 74),
            "MSFT": verdict("MSFT", "BUY", 66)}

firm_ok, firm_err = True, ""
try:
    sleeves.run_alpha_stocks(VERDICTS, SECTORS, "paper")
    sleeves.run_core_blend(VERDICTS, SECTORS, "paper")
    sleeves.run_calls_only(VERDICTS, "paper")
    seed_due_rotation_sleeve(breaker_tripped=False)   # clean-day rotation
    sleeves.run_etf_rotation("paper")
except Exception as e:               # a production cycle must never throw
    firm_ok = False
    firm_err = repr(e)
check("§6 a full four-sleeve firm pass runs end-to-end without raising",
      firm_ok, firm_err)

violations = []
for sl in config.SLEEVES:
    st = state.get_sleeve(sl)
    if st["cash"] < -1e-6:
        violations.append(f"{sl} cash {st['cash']:.2f} < 0")
    if st.get("halted"):
        violations.append(f"{sl} unexpectedly halted: {st.get('halt_reason')}")
    for sym, p in st["positions"].items():
        if not isinstance(p.get("entry_price"), (int, float)) or \
           not isinstance(p.get("quantity"), (int, float)) or p["quantity"] <= 0:
            violations.append(f"{sl}:{sym} malformed")
check("§6 no sleeve cash went negative, no spurious halt, no malformed position",
      not violations, "; ".join(violations)[:200])
check("§6 alpha + rotation both actually deployed capital this pass",
      state.get_sleeve("alpha_stocks")["positions"]
      and state.get_sleeve("etf_rotation")["positions"])


# ══ §7  Paper->Live carries the LEARNED state (mode-agnostic knowledge) ════════
state.save("lessons", {"lessons": [{"lesson": "keep stops honest", "n": 3}]})
state.save("tuning", {"caps": {"MAX_FIRM_NAME_PCT": 9.0}})
state.set_settings(mode="live")
lessons_after = state.load("lessons", {}).get("lessons")
tuning_after = state.load("tuning", {}).get("caps")
state.set_settings(mode="paper")
check("§7 learned lessons + tuning survive a paper->live flip (carry to live)",
      lessons_after and tuning_after == {"MAX_FIRM_NAME_PCT": 9.0},
      f"lessons={bool(lessons_after)} tuning={tuning_after}")


# ══ Report ════════════════════════════════════════════════════════════════════
print()
for status, name, detail in RESULTS:
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
fails = [x for x in RESULTS if x[0] == "FAIL"]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
sys.exit(1 if fails else 0)
