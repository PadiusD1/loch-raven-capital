"""Portfolio desks — how each of the four sleeves turns PM verdicts into orders.

  alpha_stocks  — buys STRONG_BUY/BUY single names, concentrated book
  core_blend    — maintains a fixed ETF core, adds conviction-stock satellites
  calls_only    — long calls on STRONG_BUY names the judge flags suitable_for_calls
  etf_rotation  — pure dual-momentum rotation (no Claude dependency)

All pricing goes through one batched download per sleeve to stay friendly
with data-provider rate limits.
"""
import hashlib
from datetime import datetime, timezone

import config
from firm import allocation, broker, data_engine, exits, risk, state

RATING_BUYS = ("STRONG_BUY", "BUY")
RATING_SELLS = ("SELL", "STRONG_SELL")


def _stop_mults(vol_ann=None) -> tuple[float, float]:
    """Sigma multipliers for the vol-aware stop/trail — LEARNED, not hardcoded,
    and PER VOLATILITY REGIME (2026-07-13). The stop distance is already each
    name's own daily sigma x multiplier; this lets the MULTIPLE differ by regime
    so a high-vol name that mean-reverts intraday (MU: 5-10% swings that bounce)
    can carry a wider sigma-multiple than a calm name (COST) without one global
    number whipsawing the volatile ones or leaving the quiet ones unprotected.
    The review (Job 2g) tunes each regime from its OWN bucketed post-exit-excess.
    Lookup order: per-bucket override -> global -> default 2.5/3.0. Clamped to
    config.STOP_BOUNDS; live-read from tuning.json 'stops'."""
    st = (state.load("tuning", {}).get("stops") or {})
    bucket = config.vol_bucket(vol_ann)
    over = st.get(bucket) if isinstance(st.get(bucket), dict) else {}
    def _clamp(key: str, default: float) -> float:
        lo, hi = config.STOP_BOUNDS[key]
        v = over.get(key, st.get(key))     # bucket override, else global, else default
        return min(hi, max(lo, float(v))) if isinstance(v, (int, float)) else default
    return _clamp("stop_sigma_mult", 2.5), _clamp("trail_sigma_mult", 3.0)


def _vol_stops(cfg: dict, vol_ann) -> tuple[float, float]:
    """Volatility-aware per-position stops: a flat -12% is six sigma for a
    quiet staple but everyday noise for a 100%-vol small cap. Stop = max of
    the sleeve base and ~2.5 daily sigmas (learned per regime), capped 25%/30%."""
    base_stop, base_trail = cfg["hard_stop_pct"], cfg["trail_pct"]
    if not isinstance(vol_ann, (int, float)) or vol_ann <= 0:
        return base_stop, base_trail
    daily = vol_ann / 15.9          # annualized -> daily sigma
    smult, tmult = _stop_mults(vol_ann)   # per-regime learned multipliers
    stop = min(25.0, max(base_stop, round(smult * daily, 1)))
    trail = min(30.0, max(base_trail, round(tmult * daily, 1)))
    return stop, trail


def _tailwind(sector: str) -> bool:
    """Was this stock's sector LEADING the market at entry time? Stamped on
    the trade so the learning review can measure whether tailwinds win."""
    from firm import riskbrain
    return bool(sector) and sector in (riskbrain.latest_regime().get("leading") or [])


def _verdict_ok(v: dict, min_conviction: int) -> bool:
    # A degraded fallback (judge timeout / CLI outage) is a quant composite
    # wearing a committee badge — it may bench/exit names, never BUY them
    return (v.get("rating") in RATING_BUYS
            and not v.get("degraded")
            and isinstance(v.get("conviction"), (int, float))
            and v["conviction"] >= min_conviction)


def _size_qty(budget: float, px: float, mode: str) -> float:
    """Share count for a dollar budget (2026-07-01 purpose review #3):
    int() truncation on $500-1,000 stocks cut intended weights 30-64% — MU
    filled 1 share ($908) against a ~$2,600 intent, so alpha's active share
    was ~17% and even perfect verdicts moved vs-SPY by basis points. PAPER
    fills go fractional whenever truncation would cut the intent >30% (the
    paper broker handles float quantities end-to-end); LIVE keeps whole
    shares until Public.com fractional support is verified. All risk caps
    check DOLLARS (cost = qty*px), so sizing precision loosens nothing."""
    if px <= 0 or budget <= 0:
        return 0
    whole = int(budget / px)
    if mode != "paper":
        return whole
    if whole >= 1 and (budget - whole * px) / budget <= 0.30:
        return whole
    frac = round(budget / px, 4)
    return frac if frac >= 0.05 else 0     # dust guard


def _entry_desk_scores(vfull: dict) -> dict:
    """Per-desk scores at ENTRY, stamped into buy meta so the closed trade
    carries the committee scores that actually opened it (2026-07-01 goal
    audit, fix 4b — the ledger's history-scan fallback mis-attributed
    re-researched names to a LATER cycle's scores). Same extraction as the
    research-history writer in cycle.py."""
    r = vfull.get("research") or {}
    return {d: (r.get(d) or {}).get("score")
            for d in r if isinstance(r.get(d), dict)}


def _ranked_buys(verdicts: dict, min_conviction: int,
                 kinds: tuple[str, ...] = ("stock",)) -> list[tuple[str, dict]]:
    out = []
    for sym, d in verdicts.items():
        if d.get("kind") not in kinds:
            # Routing an ETF to a stocks-only sleeve is normal. But a MISSING /
            # unknown kind on a REAL buy is a data-loss drop (MU, 2026-07-01) —
            # surface it so a valid buy can never again silently vanish. (cycle.py
            # backfills kind first; this catches anything that slips past.)
            _v = d.get("verdict") or {}
            if (d.get("kind") not in ("stock", "etf")
                    and _v.get("rating") in RATING_BUYS and not _v.get("degraded")):
                state.journal("buy_skipped_kind_unresolved", symbol=sym,
                              kind=d.get("kind"), wanted=list(kinds),
                              conviction=_v.get("conviction"))
            continue
        v = d.get("verdict") or {}
        if v.get("rating") in RATING_BUYS and v.get("degraded"):
            state.journal("degraded_buy_blocked", symbol=sym,
                          conviction=v.get("conviction"))
            continue
        if _verdict_ok(v, min_conviction):
            out.append((sym, v))
    return sorted(out, key=lambda kv: kv[1]["conviction"], reverse=True)


def _equitize_idle_cash(s: dict, prices: dict, mode: str) -> None:
    """Anti-cash-drag: park idle cash above the floor in the benchmark.
    A SPY-benchmarked book that sits in cash loses to SPY by default —
    cash is only a position when the RISK DESK says so (soft tier, breaker,
    halt all skip this). Runs after the real buys so conviction always gets
    the cash first."""
    cfg = config.CASH_EQUITIZATION.get(s["sleeve"])
    if not cfg or s.get("halted") or risk.risk_scale(s) < 1.0:
        return
    # DYNAMIC parking seat (operator 2026-07-16): the parking ETF is EARNED by
    # dual momentum (firm/allocation.py), not hardcoded — the self-heal below
    # already migrates any stale parking holding to the current seat.
    sym = allocation.parking_symbol(s["sleeve"]) or cfg["symbol"]
    # Self-heal a parking-SYMBOL change (calls BIL->SPY, operator 2026-07-02):
    # liquidate any cash_equitization holding that is NOT the current target
    # symbol back to cash so it can re-park in the new instrument — a stale
    # parking otherwise sits orphaned forever after the config flips.
    for _osym, _op in list(s["positions"].items()):
        if (_op.get("cash_equitization") and _osym != sym
                and _op.get("instrument") != "option"):
            _px = prices.get(_osym) or broker.equity_prices([_osym]).get(_osym, 0)
            if _px and _op.get("quantity"):
                broker.execute(mode, s, symbol=_osym, side="sell",
                               quantity=_op["quantity"], price=_px,
                               meta={"reason": "parking symbol changed -> "
                                     f"reclaim to re-park in {sym}"})
    equity = broker.sleeve_equity(s, prices)
    if equity <= 0:
        return
    invested = equity - s["cash"]
    # Top up whenever invested drifts a real step below TARGET, not only when
    # it breaches the hard floor (goal audit fix 7): gating on min(70) alone
    # left a 70-90% dead zone where sells stranded ~$2.6k idle for weeks —
    # in a SPY-benchmarked book, idle cash IS an underperformance position.
    # The -5 band keeps it from churning a share on every drift tick.
    if invested / equity * 100 >= cfg["target_invested_pct"] - 5.0:
        return
    px = prices.get(sym, 0)
    if px <= 0:
        px = broker.equity_prices([sym]).get(sym, 0)
    if px <= 0:
        state.journal("equitize_skipped_no_price", sleeve=s["sleeve"], symbol=sym)
        return
    # PARKING CEILING (fleet re-grade action 2): parking + core seats +
    # rotation share one momentum engine and can converge into ONE ticker —
    # up to ~50% of firm equity that no cap ever priced. Stop parking MORE
    # into a symbol already at the firm-wide look-through ceiling; the
    # reservoir sits in cash instead (an honest de-risk, not a drag — the
    # ceiling only binds when the firm is already saturated in the ticker).
    _ceiling = float(getattr(config, "PARKING_SYMBOL_MAX_FIRM_PCT", 50.0))
    _cur_pct = risk.firm_symbol_exposure_pct(sym)
    if _cur_pct >= _ceiling:
        state.journal("equitize_blocked_parking_ceiling", sleeve=s["sleeve"],
                      symbol=sym, firm_pct=round(_cur_pct, 1),
                      ceiling_pct=_ceiling)
        return
    budget = min(equity * cfg["target_invested_pct"] / 100 - invested,
                 s["cash"] - config.MIN_CASH_BUFFER)
    qty = int(budget / px)
    if qty < 1:
        return
    broker.execute(mode, s, symbol=sym, side="buy", quantity=qty, price=px,
                   meta={"cash_equitization": True,
                         "strategy": "cash_equitization",
                         "reason": "cash equitization (idle cash -> benchmark)"})


def _free_cash_from_equitization(s: dict, needed: float, prices: dict,
                                 mode: str) -> None:
    """Conviction outranks parking: sell just enough of the equitization
    position to fund a real buy the sleeve is about to make."""
    sym = allocation.parking_symbol(s["sleeve"])
    pos = s["positions"].get(sym) if sym else None
    if not pos or not pos.get("cash_equitization"):
        # Seat may have JUST switched — pull from any parking position still
        # migrating (the equitize self-heal liquidates it next pass anyway).
        sym, pos = next(((k, p) for k, p in s["positions"].items()
                         if p.get("cash_equitization")
                         and p.get("instrument") != "option"), (None, None))
    if not pos:
        return
    # RESERVOIR SALE IN FLIGHT (2026-07-25 audit, finding #1): a live parking
    # SELL already placed for this same reservoir leaves `exit_pending` on the
    # position until settle_pending books it. broker.execute refuses a second
    # sell on an exit_pending position — but only for THIS in-memory copy of
    # the sleeve, so a caller that discards `s` (the flush's early-exit paths,
    # pre-fix) re-placed the identical live order every sweep, forever, with no
    # record. Refuse it HERE, at the single choke point every sleeve uses, and
    # say so: the pending order is about to credit the cash anyway, so the
    # correct behaviour is to wait one sweep, never to sell the reservoir twice.
    if pos.get("exit_pending"):
        state.journal("equitization_pull_skipped_exit_pending",
                      sleeve=s.get("sleeve"), symbol=sym, needed=round(needed, 2),
                      order_id=str(pos.get("exit_pending"))[:40])
        return
    short = needed - (s["cash"] - config.MIN_CASH_BUFFER)
    px = prices.get(sym, 0)
    if short <= 0 or px <= 0:
        return
    qty = min(pos["quantity"], int(short / px) + 1)
    if qty >= 1:
        broker.execute(mode, s, symbol=sym, side="sell", quantity=qty, price=px,
                       meta={"reason": "equitization sold to fund conviction buy"})


def _earnings_ok(sym: str, pk: dict, sleeve: str) -> bool:
    """Never OPEN a position within a day of an earnings report — a momentum
    setup into a print is a coin flip, not a process. The committee saw the
    date (it's in the packet); this is the hard backstop."""
    days = (pk.get("next_earnings") or {}).get("days_away")
    if isinstance(days, int) and 0 <= days <= 1:
        state.journal("buy_skipped_earnings", sleeve=sleeve, symbol=sym,
                      days_to_earnings=days)
        return False
    return True


def _days_until(iso_date: str | None) -> int | None:
    """Whole days from today (UTC) until an ISO date (e.g. an option expiration).
    None if unparseable — callers fail open (2026-06-14 audit A5)."""
    try:
        d = datetime.fromisoformat(str(iso_date)[:10]).date()
        return (d - datetime.now(timezone.utc).date()).days
    except (ValueError, TypeError):
        return None


def _entry_drift_ok(sym: str, px: float, pk: dict, sleeve: str) -> bool:
    """The committee judged the prior close; execution happens 30-90+ min into
    the next session at a live quote. Past a vol-scaled band the thesis price
    is stale — momentum names gap on news, and buying the gap is chasing, not
    following the process. Skipped names get re-judged next cycle at the new
    level. Fails open when the packet carries no reference price."""
    tech = pk.get("technicals") or {}
    ref = tech.get("price")
    if not isinstance(ref, (int, float)) or ref <= 0:
        return True
    vol = tech.get("vol_ann_pct")
    daily_sigma = vol / 15.9 if isinstance(vol, (int, float)) and vol > 0 else 0.0
    band = max(risk.tuned_cap("MAX_ENTRY_DRIFT_PCT"), 0.75 * daily_sigma)
    drift = (px / ref - 1) * 100
    if abs(drift) > band:
        state.journal("buy_skipped_price_drift", sleeve=sleeve, symbol=sym,
                      drift_pct=round(drift, 2), band_pct=round(band, 2),
                      thesis_price=ref, exec_price=round(px, 4))
        return False
    return True


def _edge_weight_tilt(pk: dict) -> float:
    """AUDIT 2026-07-16 #9 — close the measured-edge -> sizing loop with ONE
    bounded lever: when the firm's OWN scorecard shows a real (n>=30) positive
    momentum edge (edge_health), a top-momentum name (factor >=80) earns up to
    +2pts of target weight and a weak-momentum name (<=40) gives up to -2pts.
    The committee's weight stays the anchor; the tilt is mechanical, hard-
    bounded at +/-2, and the call site journals the counterfactual (untilted)
    weight so the learning loop can grade whether the tilt ADDS anything
    before it is ever widened. Fail-neutral: any read error tilts 0."""
    try:
        eh = state.load("edge_health", {}) or {}
        edge = next((e for e in (eh.get("edges") or [])
                     if str(e.get("edge", "")) == "factor:momentum"), None)
        if not edge or (edge.get("n") or 0) < 30:
            return 0.0
        val = float(edge.get("value") or 0)
        if val <= 0:
            return 0.0
        mom = (pk.get("factor_scores") or {}).get("momentum")
        if not isinstance(mom, (int, float)):
            return 0.0
        mag = min(2.0, round(val / 9.0, 2))     # +18pts edge -> the full +/-2
        if mom >= 80:
            return mag
        if mom <= 40:
            return -mag
        return 0.0
    except Exception:
        return 0.0


def _apply_sells(s: dict, verdicts: dict, mode: str, prices: dict) -> None:
    """Exit any held name the judge turned SELL on. Core ETFs are exempt from
    THIS path only because allocation.apply_sell_verdicts handles a seated
    ETF's SELL properly — sell AND seat the next-best in one atomic change
    (operator 2026-07-16: "when an ETF becomes a sell, sell it"); selling
    here without replacement would leave the mechanical core underweight
    until the next rebalance rebuys the SAME name."""
    core = (set(allocation.core_weights()) if s["sleeve"] == "core_blend"
            else set(config.SLEEVES.get(s["sleeve"], {}).get("core_weights") or {}))
    for sym in list(s["positions"].keys()):
        pos = s["positions"][sym]
        if pos["instrument"] == "option":
            continue
        if pos.get("cash_equitization"):
            continue   # benchmark parking is not a thesis — nothing to SELL
        v = (verdicts.get(sym) or {}).get("verdict") or {}
        if v.get("rating") in RATING_SELLS and v.get("degraded"):
            # Degraded = a quant-only fallback (judge timeout / Opus outage), NOT
            # a committee decision — it must NEVER trade, on the SELL side too
            # (Feature A guarded degraded BUYs; the SELL path was unguarded and
            # would liquidate a real position on a non-committee signal — audit
            # 2026-07-02). Real price risk is still handled by the hard/trailing
            # stops in monitor.py; we simply don't exit on a degraded thesis.
            state.journal("degraded_sell_blocked", sleeve=s["sleeve"], symbol=sym,
                          rating=v.get("rating"))
            continue
        if v.get("rating") in RATING_SELLS and sym in core:
            state.journal("core_sell_suppressed", sleeve=s["sleeve"], symbol=sym,
                          rating=v.get("rating"),
                          note="strategic core held; verdict recorded as information")
            continue
        if v.get("rating") in RATING_SELLS:
            px = prices.get(sym, 0)
            if px > 0:
                broker.execute(mode, s, symbol=sym, side="sell",
                               quantity=pos["quantity"], price=px,
                               meta={"reason": f"PM verdict {v['rating']}"})
            else:
                # An unexecutable SELL verdict is a risk event, not a detail —
                # the exit monitor retries it every sweep, but say so loudly
                state.journal("sell_skipped_no_price", sleeve=s["sleeve"],
                              symbol=sym, rating=v.get("rating"))


# ── Sleeve 1: Alpha Stocks ────────────────────────────────────────────────────

def run_alpha_stocks(verdicts: dict, sectors: dict, mode: str) -> None:
    with state.SLEEVE_LOCK:
        _run_alpha_stocks(verdicts, sectors, mode)


def _eligible_add(v: dict, pos: dict, px: float, pk: dict, sector_tailwind: bool) -> bool:
    """A held position qualifies for a MEASURED add on strength only when the
    committee affirmatively wants more AND the name is a CONFIRMED, WINNING
    momentum leader with a strengthening catalyst or sector tailwind. Pyramid
    into proven winners, NEVER average down (operator 2026-06-25: "if it's proven
    the ride keeps going up ... it needs to discern")."""
    if not getattr(config, "ADD_ON_STRENGTH", False):
        return False
    if pos.get("cash_equitization"):
        return False
    if v.get("degraded") or v.get("rating") not in ("BUY", "STRONG_BUY"):
        return False                       # the committee must want MORE — not HOLD
    entry = pos.get("entry_price") or 0
    if not (px > 0 and entry > 0 and px > entry):
        return False                       # WINNERS only — never average a loser down
    if pos.get("adds_count", 0) >= int(getattr(config, "ADD_MAX_COUNT", 2)):
        return False                       # bounded — pyramid in a few steps, then stop
    last = pos.get("last_add_ts")
    if last:
        try:
            if (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days \
                    < int(getattr(config, "ADD_COOLDOWN_DAYS", 3)):
                return False               # no daily topping-up
        except (ValueError, TypeError):
            return False                   # unparseable add timestamp -> fail SAFE, don't add
    # CONFIRMED momentum: a positive 3-month return AND price still ABOVE its
    # 50-day MA (never pyramid into a name that has rolled over), or a strong
    # momentum factor as the alt path. NOTE: compute_technicals emits the BOOLEAN
    # 'above_sma50' — there is no numeric 'sma50' in the packet, so the old
    # px>=sma50 read was dead code that let a topping name slip through with a
    # stale-positive ret_3m (adversarial review 2026-06-29).
    tech = pk.get("technicals") or {}
    fac = pk.get("factor_scores") or {}
    ret3 = tech.get("ret_3m")
    above50 = tech.get("above_sma50")
    mom = fac.get("momentum")
    trend_ok = (isinstance(ret3, (int, float)) and ret3 > 0 and above50 is True)
    mom_ok = isinstance(mom, (int, float)) and mom >= float(getattr(config, "ADD_MOM_MIN", 60))
    if not (trend_ok or mom_ok):
        return False
    # A STRENGTHENING catalyst (a real earnings beat) OR sector strength — the
    # transcript/8-K/sector read the operator asked the fund to weigh.
    sup = (pk.get("recent_earnings") or {}).get("surprise_pct")
    catalyst_ok = isinstance(sup, (int, float)) and sup >= float(
        getattr(config, "ADD_MIN_SURPRISE_PCT", 5.0))
    return catalyst_ok or bool(sector_tailwind)


def _maybe_add_on_strength(s: dict, sym: str, v: dict, verdicts: dict, prices: dict,
                           equity: float, sectors: dict, mode: str, cfg: dict) -> None:
    """Add a MEASURED step to a held confirmed-leader winner, toward the higher
    target weight the committee asked for, with EVERY risk cap applied to the
    resulting TOTAL position. Position-size cap sees existing+add; sector/firm/
    liquidity caps see the increment (they already sum the held position)."""
    pos = s["positions"].get(sym) or {}
    pk = verdicts.get(sym, {}).get("packet") or {}
    px = prices.get(sym, 0)
    sector = sectors.get(sym, "")
    if px <= 0 or equity <= 0 or not _eligible_add(v, pos, px, pk, _tailwind(sector)):
        return
    # An add is NEW capital into the name — honor the risk desk's sector
    # restriction exactly like the new-buy path (a sector-drawdown event sets
    # restricted_sectors WITHOUT halting the sleeve, so entries_allowed stays
    # True; adding here would re-inflate the very sector the desk amputated —
    # review 2026-06-29).
    if not risk.sector_allowed(s, sector):
        state.journal("add_blocked_sector_restricted", sleeve="alpha_stocks",
                      symbol=sym, sector=sector)
        return
    cur_val = pos.get("quantity", 0) * px
    cur_wt = cur_val / equity * 100
    target = min(float(v.get("target_weight_pct") or 0), cfg["max_position_pct"])
    gap = target - cur_wt
    if gap <= 0.5:                          # already at/above the asked weight
        return
    step = min(gap, float(getattr(config, "ADD_STEP_MAX_PCT", 4.0)))
    vol = (pk.get("technicals") or {}).get("vol_ann_pct")
    if isinstance(vol, (int, float)) and vol > 50:
        step *= max(0.4, 50.0 / vol)        # vol-scale the step like the main path
    add_dollars = equity * step / 100 * risk.risk_scale(s)
    # 2026-07-25 audit (finding #6): an add could not reach PARKED capital and
    # then returned SILENTLY at zero shares. Live proof: MU wanted ~$200 of add
    # at px 920.95 -> int(200/920.95) = 0 -> bare `return`, while $5,853 sat in
    # the RSP reservoir; lifetime add_on_strength=3 with add_not_booked=0, i.e.
    # the failure left no trace anywhere. Two fixes, both minimal:
    #   1. pull the reservoir first, exactly like the conviction-buy path does
    #      (same helper, same rule: conviction outranks parking). This is a
    #      no-op when raw cash already covers the step.
    #   2. NEVER return silently — journal the zero-share skip with the numbers
    #      that produced it, so "the add step is smaller than one share" is a
    #      visible, gradeable fact instead of an invisible one.
    _free_cash_from_equitization(s, add_dollars, prices, mode)
    budget = min(add_dollars, s["cash"] - config.MIN_CASH_BUFFER)
    add_qty = int(budget / px) if px else 0
    if add_qty < 1:
        state.journal(
            "add_skipped_size_zero", sleeve="alpha_stocks", symbol=sym,
            add_dollars=round(add_dollars, 2), budget=round(budget, 2),
            price=round(px, 4), cash=round(s["cash"], 2),
            step_pct=round(step, 2), from_weight=round(cur_wt, 1),
            toward_target=round(target, 1),
            reason=("add step is below one whole share at this price"
                    if add_dollars >= px else
                    "insufficient cash for one share even after freeing "
                    "parked capital"))
        return
    add_cost = add_qty * px
    total_val = cur_val + add_cost          # position cap covers the WHOLE position
    for ok, why, ev in (
        (*risk.check_position_size("alpha_stocks", equity, total_val,
                                   cfg["max_position_pct"]), "add_blocked_position_size"),
        (*risk.check_sector_cap(s["positions"], sectors, sym, add_cost, equity),
         "add_blocked_sector"),
        (*risk.check_firm_name_cap(sym, add_cost), "add_blocked_firm_cap"),
        (*risk.check_firm_sector_cap(sym, sector, add_cost), "add_blocked_firm_sector"),
    ):
        if not ok:
            state.journal(ev, sleeve="alpha_stocks", symbol=sym, reason=why)
            return
    adv = (pk.get("technicals") or {}).get("avg_dollar_vol")
    if isinstance(adv, (int, float)) and adv > 0 \
            and add_cost > adv * config.MAX_POSITION_OF_ADV_PCT / 100:
        state.journal("add_blocked_liquidity", sleeve="alpha_stocks", symbol=sym,
                      cost=add_cost, adv=adv)
        return
    res = broker.execute(mode, s, symbol=sym, side="buy", quantity=add_qty, price=px,
                         meta={"thesis": v.get("thesis", ""), "conviction": v.get("conviction"),
                               "rating": v.get("rating"), "add_on_strength": True,
                               "sector": sector})
    # Only COUNT the add (and feed the learning ledger) once it actually BOOKED.
    # A LIVE buy returns success+pending (settles async via settle_pending); a
    # paper buy can fail on cash/market-closed. Bumping adds_count on a phantom
    # add would burn the bounded add budget + the 3-day cooldown and feed the
    # judge an add that never happened (review 2026-06-29).
    if not (isinstance(res, dict) and res.get("success") and not res.get("pending")):
        state.journal("add_not_booked", sleeve="alpha_stocks", symbol=sym, add_qty=add_qty,
                      pending=bool(isinstance(res, dict) and res.get("pending")),
                      error=(res.get("error") if isinstance(res, dict) else "no result"))
        return
    p = s["positions"].get(sym)
    if p:
        p["adds_count"] = p.get("adds_count", 0) + 1
        p["last_add_ts"] = datetime.now(timezone.utc).isoformat()
    state.journal("add_on_strength", sleeve="alpha_stocks", symbol=sym, add_qty=add_qty,
                  add_cost=round(add_cost, 2), from_weight=round(cur_wt, 1),
                  toward_target=round(target, 1), conviction=v.get("conviction"),
                  surprise_pct=(pk.get("recent_earnings") or {}).get("surprise_pct"))


def _held_days(pos: dict) -> float:
    """Whole days a position has been open (from opened_at). Unknown/unparseable
    -> 0.0, so an age-unverifiable holding is treated as FRESH and thus PROTECTED
    from eviction (the churn-averse default)."""
    ts = pos.get("opened_at")
    if not ts:
        return 0.0
    try:
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(ts)).total_seconds() / 86400
    except (ValueError, TypeError):
        return 0.0


def _weakest_evictable(s: dict, verdicts: dict, prices: dict,
                       core_syms: set) -> tuple[str, float] | None:
    """The weakest holding a FULL sleeve may evict to admit a stronger name.
    ALL guardrails must hold: not the cash-equitization parking, not a core ETF,
    the committee re-rated it only HOLD this cycle (a name still rated BUY-family
    is never evicted — the judge still wants it; a SELL is already handled by
    _apply_sells), it is NOT green (never sell a winner to chase a newcomer), NOT
    on add-on-strength, and NOT freshly entered. Returns (symbol, conviction)."""
    best: tuple[float, str] | None = None
    for hsym, pos in s["positions"].items():
        if pos.get("cash_equitization") or hsym in core_syms:
            continue
        hv = (verdicts.get(hsym) or {}).get("verdict") or {}
        if hv.get("rating") != "HOLD" or hv.get("degraded"):
            continue                       # SELL is handled by _apply_sells; BUY = the
                                           # judge still wants it; DEGRADED = a quant-only
                                           # fallback that must never drive capital action
        conv = hv.get("conviction")
        if not isinstance(conv, (int, float)):
            continue                       # unparsed conviction -> fail SAFE, don't evict
        entry = pos.get("entry_price") or 0
        px = prices.get(hsym, 0)
        if entry > 0 and px > 0 and px > entry:
            continue                       # green winner — protect
        if pos.get("adds_count", 0) > 0:
            continue                       # pyramided winner — protect
        if _held_days(pos) < float(getattr(config, "REPLACE_MIN_HOLD_DAYS", 3)):
            continue                       # too fresh — let it work
        if best is None or float(conv) < best[0]:
            best = (float(conv), hsym)
    return (best[1], best[0]) if best else None


def _swap_cooldown_active(s: dict, sym: str) -> bool:
    """True while `sym` is inside its post-eviction anti-round-trip window — a name
    just swapped OUT of the book may not be bought back (by ANY path) for
    REPLACE_ROUNDTRIP_DAYS, mirroring the stop-out re-entry cool-off so a fresh
    conviction flip can't whipsaw a name straight back in."""
    rec = (s.get("swapped_out") or {}).get(sym)
    if not rec:
        return False
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(rec["ts"])).total_seconds() / 86400
    except (ValueError, TypeError, KeyError):
        return False
    return age < float(getattr(config, "REPLACE_ROUNDTRIP_DAYS", 3))


def _replace_margin() -> float:
    """Fix brief 2026-07-21 #9 (evidence-over-constants): the static +12 swap
    margin assumed healthy conviction dispersion. Under the compressed regime
    (BUY-family stdev ~2.7, p90-p10 ~5-6) +12 was structurally UNREACHABLE —
    the book could never rotate a satellite no matter how much stronger the
    challenger was in the signal's own live resolution (07-21: DVA 66 vs
    weakest 65, need 77). The margin now follows the MEASURED trailing
    BUY-family p90-p10: healthy dispersion (>8, the health check's own bar)
    keeps the static REPLACE_MIN_MARGIN; a compressed regime clamps to
    max(observed spread, 4) so "materially stronger" stays meaningful
    RELATIVE to what the judge actually emits. This is the immediate
    mechanical hard-gate item #9 asked for — the consumer degrades WITH the
    signal instead of silently pricing off a constant. Fail-static to the
    configured margin on any read error."""
    static = float(getattr(config, "REPLACE_MIN_MARGIN", 12.0))
    try:
        hist = (state.load("research", {}) or {}).get("history", [])[-30:]
        convs = sorted(
            float(v.get("conviction")) for h in hist
            for v in (h.get("verdicts") or {}).values()
            if not (v or {}).get("degraded")
            and (v or {}).get("rating") in ("BUY", "STRONG_BUY")
            and isinstance(v.get("conviction"), (int, float)))
        if len(convs) >= 15:
            spread = (convs[int(0.9 * (len(convs) - 1))]
                      - convs[int(0.1 * (len(convs) - 1))])
            if spread <= 8.0:
                return max(4.0, min(static, float(spread)))
    except Exception:
        pass
    return static


def _plan_swap(s, sym, v, verdicts, prices, cfg, core_syms, swaps):
    """DECIDE whether a book-full sleeve should evict a holding to admit `sym`.
    Returns (weak_sym, weak_conv) to evict, or None. Does NOT sell — the caller
    commits the eviction ONLY after the replacement buy has cleared every gate, so
    a thesis-backed holding is never sold for a buy that then fails (2026-07-01
    review). Rare + guarded: master switch, per-cycle bound, anti-round-trip,
    margin, and hysteresis (keyed on the BUY candidate so near-tied HOLD reshuffles
    can't perpetually reset a persistent intent)."""
    if not getattr(config, "REPLACE_ON_CONVICTION", False):
        return None
    if swaps[0] >= int(getattr(config, "REPLACE_MAX_PER_CYCLE", 1)):
        return None
    wctx = ((verdicts.get(sym) or {}).get("packet") or {}).get("watchlist_context") or {}
    if wctx.get("stop_out"):
        return None
    new_conv = v.get("conviction")
    new_conv = float(new_conv) if isinstance(new_conv, (int, float)) else 0.0
    weak = _weakest_evictable(s, verdicts, prices, core_syms)
    if not weak:
        return None
    weak_sym, weak_conv = weak
    _margin = _replace_margin()
    if new_conv < weak_conv + _margin:
        # Journal the margin miss (operator 2026-07-20): this was a SILENT
        # return, so a book-full skip never said how close the challenger
        # came — and under compressed convictions (stdev 2.8) the margin was
        # structurally unreachable with nothing recording it. One line per
        # candidate per full-book cycle, bounded by the ranked list.
        state.journal("swap_rejected_margin", sleeve=s["sleeve"], buy=sym,
                      weakest=weak_sym, new_conv=new_conv, weak_conv=weak_conv,
                      need=weak_conv + _margin,
                      margin=_margin,
                      margin_basis=("dispersion_clamped"
                                    if _margin < float(getattr(
                                        config, "REPLACE_MIN_MARGIN", 12.0))
                                    else "static"))
        return None                        # not materially stronger — noise, skip
    intents = s.setdefault("swap_intents", {})
    count = (intents.get(sym) or {}).get("count", 0) + 1
    intents[sym] = {"weak": weak_sym, "count": count}
    need = int(getattr(config, "REPLACE_HYSTERESIS_CYCLES", 2))
    if count < need:
        state.journal("swap_deferred_hysteresis", sleeve=s["sleeve"], buy=sym,
                      sell=weak_sym, count=count, need=need,
                      new_conv=new_conv, weak_conv=weak_conv)
        return None
    return (weak_sym, weak_conv)


def recent_stop_block(sym: str, px: float) -> tuple[bool, str]:
    """Is `sym` inside its post-stop cool-off, and has it failed to reclaim?

    Returns (blocked, why). The firm's stop-out recovery contract is explicit:
    a stopped name waits STOP_OUT_COOLOFF_DAYS **and** must reclaim the stop
    price + STOP_OUT_RECLAIM_PCT before the book re-enters it ("no falling-knife
    re-entry", CLAUDE.md).

    2026-07-26 (operator-found, live): that contract was enforced in exactly ONE
    place — watchlist.py's bench-PROMOTION gate. Every other way a name can reach
    an entry bypassed it, because the bench entry itself disappears the moment the
    committee returns BUY ("BUY graduates off the bench"). Observed: CON was
    stopped -16.7% on 07-25, re-entered the quant SCAN as a mid-cap candidate the
    same night, was judged BUY at conviction 68, and queued for the open at 31.63
    against a stop of 31.61 — ~16h after the stop and 3% below its own reclaim
    bar. OSCR took the third path: still held in core_blend, so it was
    re-underwritten as a HELD name and never consulted the bench at all.

    So the gate now lives at ENTRY, where every path converges, and reads the
    LEDGER (durable, mode-agnostic, survives the bench entry being consumed)
    rather than the bench. A stop recorded by any sleeve blocks re-entry in any
    sleeve — the risk decision was about the NAME, not about one book's copy.
    """
    if not getattr(config, "ENFORCE_STOP_COOLOFF_AT_ENTRY", True):
        return False, ""
    try:
        from firm import watchlist as _wl
        cooloff_d = float(_wl._param("stop_cooloff_days", _wl.STOP_OUT_COOLOFF_DAYS))
        reclaim_p = float(_wl._param("stop_reclaim_pct", _wl.STOP_OUT_RECLAIM_PCT))
        rows = (state.load("ledger", {}) or {}).get("trades") or []
        now = datetime.now(timezone.utc)
        newest = None
        for t in rows:
            if (t.get("ticker") or t.get("symbol")) != sym:
                continue
            reason = str(t.get("exit_reason") or "").lower()
            # Only RISK exits carry a cool-off. A committee SELL, a rotation exit
            # or a seat change is a thesis/mechanical decision, not a knife.
            if "stop" not in reason:
                continue
            closed = t.get("closed_at") or ""
            try:
                age_d = (now - datetime.fromisoformat(closed)).total_seconds() / 86400
            except Exception:
                continue
            if age_d >= cooloff_d:
                continue
            if newest is None or closed > newest[0]:
                newest = (closed, float(t.get("exit") or 0), age_d, reason)
        if newest is None:
            return False, ""
        _closed, exit_px, age_d, reason = newest
        if exit_px <= 0:                       # unknown stop price -> cool-off alone
            return True, (f"stopped {age_d:.1f}d ago ({reason}); cool-off "
                          f"{cooloff_d:.0f}d, stop price unknown")
        bar = exit_px * (1 + reclaim_p / 100)
        if px >= bar:                          # reclaimed -> the thesis earned a retry
            return False, ""
        return True, (f"stopped {age_d:.1f}d ago at {exit_px:.2f} ({reason}); "
                      f"needs {bar:.2f} (+{reclaim_p:.0f}%) or {cooloff_d:.0f}d "
                      f"cool-off, price {px:.2f}")
    except Exception:
        return False, ""                       # never block a buy on a bookkeeping bug


def _planned_swap_proceeds(s: dict, weak_sym: str, prices: dict) -> float:
    """Cash a PLANNED eviction will free, priced exactly the way
    _commit_swap_sell will actually sell it (LIVE quote only — no stale
    entry_price fallback, because a swap with no live quote defers instead of
    executing, so counting a stale mark would fund a buy from money that never
    arrives). 2026-07-25 audit finding #7: both swap paths sized the
    replacement from PRE-eviction cash. core_blend was the live casualty —
    cash $135.10 against equity $24,692, so _desired_sat computed $2,222 but
    `budget = min(2222, 35.10)` collapsed to $35.10: in PAPER that bought ~1
    share (~$32) right after liquidating a $1,957 holding, and in LIVE
    _size_qty returns 0 for any price above $35.10, so the swap NEVER committed
    and core_blend's whole rotation engine was inert (`position_swapped`
    appears 0 times in 5,139 journal rows). alpha_stocks has the same ordering
    and survived only because its parking reservoir happened to be funded.
    The eviction is a KNOWN, imminent credit — include it in the funding basis,
    then re-clamp against real cash once the sell has actually booked."""
    pos = (s.get("positions") or {}).get(weak_sym) or {}
    px = prices.get(weak_sym, 0)
    qty = pos.get("quantity") or 0
    return qty * px if px > 0 and qty > 0 else 0.0


def _commit_swap_sell(s, weak_sym, weak_conv, sym, new_conv, prices, mode, swaps) -> bool:
    """EXECUTE a planned eviction, called immediately BEFORE the validated buy.
    Returns True only if the slot is genuinely freed — a paper fill, or a LIVE sell
    that is NOT still pending. (An accepted-but-unfilled live sell leaves the
    position + cash untouched, so treating it as freed would breach max_positions
    and mis-size the buy against un-freed cash — the CRITICAL 2026-07-01 finding.)
    Stamps an anti-round-trip cooldown on the evicted name + journals the swap."""
    # AUDIT 2026-07-16 #10: when the replacement BUY would only QUEUE for the
    # open (market closed + the bell gate active — the 8:30 held cycle), the
    # eviction SELL would book NOW while the buy can still die on the drift
    # band at 9:30. That is the half-swap the 2026-07-01 fix exists to
    # prevent. Defer the whole swap to an open-market cycle; the swap intent +
    # hysteresis counters survive for the next look. Keyed to the EXACT
    # would-queue condition so a no-gate harness/paper config is unaffected.
    from firm import market_hours as _mh_swap
    if not _mh_swap.market_is_open() and (
            mode == "live"
            or (mode == "paper" and config.PAPER_ENFORCE_MARKET_HOURS)):
        state.journal("swap_deferred_market_closed", sleeve=s["sleeve"],
                      sell=weak_sym, buy=sym)
        return False
    pos = s["positions"].get(weak_sym) or {}
    px = prices.get(weak_sym, 0)           # NO stale entry_price fallback — a missing
    if px <= 0 or pos.get("quantity", 0) <= 0:   # live quote defers the swap this cycle
        state.journal("swap_sell_skipped_no_price", sleeve=s["sleeve"], sell=weak_sym)
        return False
    res = broker.execute(mode, s, symbol=weak_sym, side="sell",
                         quantity=pos.get("quantity", 0), price=px,
                         meta={"reason": "conviction swap — evicted for stronger buy",
                               "swapped_for": sym})
    if not (isinstance(res, dict) and res.get("success") and not res.get("pending")):
        state.journal("swap_sell_pending", sleeve=s["sleeve"], sell=weak_sym, buy=sym,
                      pending=bool(isinstance(res, dict) and res.get("pending")),
                      error=(res.get("error") if isinstance(res, dict) else "no result"))
        return False
    (s.get("swap_intents") or {}).pop(sym, None)          # intent consumed
    s.setdefault("swapped_out", {})[weak_sym] = {
        "ts": datetime.now(timezone.utc).isoformat(), "for": sym}
    swaps[0] += 1
    state.journal("position_swapped", sleeve=s["sleeve"], sold=weak_sym,
                  sold_conviction=weak_conv, bought=sym, bought_conviction=new_conv,
                  margin=round(new_conv - weak_conv, 1))
    return True


def _run_alpha_stocks(verdicts: dict, sectors: dict, mode: str) -> None:
    cfg = config.SLEEVES["alpha_stocks"]
    s = state.get_sleeve("alpha_stocks")
    ranked = _ranked_buys(verdicts, cfg["min_conviction"])
    prices = broker.equity_prices(
        list(s["positions"].keys()) + [sym for sym, _ in ranked])
    equity = broker.sleeve_equity(s, prices)
    _apply_sells(s, verdicts, mode, prices)

    ok, why = risk.entries_allowed("alpha_stocks", s, equity)
    if ok:
        book_full_skips: list[str] = []
        _swaps = [0]                       # per-cycle swap budget (REPLACE_MAX_PER_CYCLE)
        _rsyms = {sy for sy, _ in ranked}  # drop stale swap-intents for names no longer ranked
        s["swap_intents"] = {k: vv for k, vv in (s.get("swap_intents") or {}).items()
                             if k in _rsyms}
        s["swapped_out"] = {k: r for k, r in (s.get("swapped_out") or {}).items()
                            if _swap_cooldown_active(s, k)}   # expire old cooldowns
        for sym, v in ranked:
            _swap = None                   # a planned (weak_sym, weak_conv) eviction, if any
            # The equitization parking spot never consumes a conviction slot
            real_count = sum(1 for p in s["positions"].values()
                             if not p.get("cash_equitization"))
            if sym in s["positions"]:
                # Already held: not a NEW slot, but a confirmed-leader winner the
                # committee wants more of can take a measured ADD on strength.
                _maybe_add_on_strength(s, sym, v, verdicts, prices, equity,
                                       sectors, mode, cfg)
                continue
            if _swap_cooldown_active(s, sym):   # just swapped OUT — no immediate re-buy
                state.journal("buy_skipped_swap_cooldown", sleeve="alpha_stocks", symbol=sym)
                continue
            if real_count >= cfg["max_positions"]:
                # Book full: PLAN a swap now (guards + hysteresis) but do NOT sell
                # yet — the eviction is committed only after `sym` clears every buy
                # gate below, so a holding is never sold for a buy that then fails.
                _swap = _plan_swap(s, sym, v, verdicts, prices, cfg, set(), _swaps)
                if not _swap:
                    # NAME-full but CAPITAL-empty (operator 2026-07-16: "$16K in
                    # SPY parked while BUYs were turned away — parking is there
                    # UNTIL other stocks get bought"). With the committee sizing
                    # small (3-4% per name), 8 slots deployed only 27.5% of the
                    # book while 64.9% sat parked — the cap was counting NAMES,
                    # not capital. When real deployment is under the configured
                    # floor and the parking reservoir is live, the slot cap
                    # yields (hard ceiling max_positions+EXTRA keeps focus
                    # binding); the buy is funded by the existing
                    # _free_cash_from_equitization pull at sizing below.
                    from firm import routing as _routing_cap
                    if not _routing_cap.parked_capacity("alpha_stocks", s, prices):
                        book_full_skips.append(sym)
                        continue
                    state.journal("book_full_admitted_parked_cash",
                                  sleeve="alpha_stocks", symbol=sym,
                                  real_positions=real_count,
                                  conviction=v.get("conviction"))
            if not risk.sector_allowed(s, sectors.get(sym, "")):
                state.journal("buy_blocked_sector", sleeve="alpha_stocks",
                              symbol=sym, sector=sectors.get(sym))
                continue
            px = prices.get(sym, 0)
            if px <= 0:
                # A no-trade cycle must explain itself — silent price-skips
                # hid a full data outage on 2026-06-10
                state.journal("buy_skipped_no_price", sleeve="alpha_stocks",
                              symbol=sym)
                continue
            pk = verdicts.get(sym, {}).get("packet") or {}
            if not _entry_drift_ok(sym, px, pk, "alpha_stocks"):
                continue
            if not _earnings_ok(sym, pk, "alpha_stocks"):
                continue
            try:        # a hallucinated verdict field must never kill the cycle
                weight = min(float(v.get("target_weight_pct") or 10.0),
                             cfg["max_position_pct"])
            except (ValueError, TypeError):
                weight = 10.0
            # (2026-08-13 operator decision) ALPHA DE-RISK: halve every
            # per-name target until the screen re-validates (restore condition
            # and rationale live on config.ALPHA_SIZING_DERISK). Applied to
            # the judge's weight BEFORE the deployment scaler / tilt / vol
            # scaling so every downstream cap and journal sees the de-risked
            # base; freed capital reaches SPY parking via the existing
            # equitization pass, which journals its own trades.
            weight *= float(getattr(config, "ALPHA_SIZING_DERISK", 1.0))
            # DEPLOYMENT SCALER (re-grade audit 2026-07-16, A+ push): a book
            # under the deployment floor converts parked beta into researched
            # alpha by scaling entry weights up — bounded at SCALE_MAX, capped
            # by max_position_pct, journaled with the counterfactual. Self-
            # neutralizes once the judge's own sizing deploys the book.
            _real_inv = sum((p.get("quantity") or 0)
                            * (prices.get(k) or p.get("last_price")
                               or p.get("entry_price") or 0)
                            for k, p in s["positions"].items()
                            if not p.get("cash_equitization"))
            _dep_pct = _real_inv / equity * 100 if equity > 0 else 100.0
            _floor = float(getattr(config, "ALPHA_DEPLOYMENT_FLOOR_PCT", 60.0))
            if _dep_pct < _floor:
                _smax = float(getattr(config, "ALPHA_DEPLOYMENT_SCALE_MAX", 1.5))
                # linear: at 0% deployed -> full SCALE_MAX; at the floor -> 1.0
                _scale = 1.0 + (_smax - 1.0) * (1.0 - _dep_pct / _floor)
                _w_before = weight
                weight = min(weight * _scale, cfg["max_position_pct"])
                state.journal("deployment_scale_applied", sleeve="alpha_stocks",
                              symbol=sym, deployed_pct=round(_dep_pct, 1),
                              scale=round(_scale, 3),
                              counterfactual_weight=round(_w_before, 2),
                              scaled_weight=round(weight, 2))
            # AUDIT 2026-07-16 #9: measured-edge tilt — the firm's own graded
            # momentum edge nudges the committee's weight by at most +/-2pts.
            # Journaled WITH the counterfactual so the loop can grade the tilt
            # itself; a dead/negative/thin edge tilts exactly 0.
            _tilt = _edge_weight_tilt(pk)
            # Learning-integrity guard (2026-08-13 IG audit): while the 'eod
            # mark-to-close' health check is failing/poisoned (or a declared
            # standing defect), the close-price convention behind the graded
            # momentum edge is not trustworthy — DEFER the weight mutation
            # (journaled skip) rather than size capital on poisoned evidence.
            # Read-only reuse of the persisted health/standing-defect state
            # via learning._eod_marks_defective; fail-open on any read error.
            if _tilt:
                try:
                    from firm import learning as _lrn_tilt
                    if _lrn_tilt._eod_marks_defective():
                        state.journal("edge_tilt_deferred_poisoned",
                                      sleeve="alpha_stocks", symbol=sym,
                                      tilt_skipped=_tilt)
                        _tilt = 0.0
                except Exception:
                    pass
            if _tilt:
                state.journal("edge_weight_tilt", sleeve="alpha_stocks",
                              symbol=sym, counterfactual_weight=weight,
                              tilt=_tilt,
                              tilted_weight=round(max(1.0, min(
                                  weight + _tilt, cfg["max_position_pct"])), 2))
                weight = max(1.0, min(weight + _tilt, cfg["max_position_pct"]))
            # Vol-aware sizing (2026-06-10 audit): the LLM's target weight is
            # un-scaled — equal weights mean the 100%-vol name carries 3x the
            # dollar risk of the 30%-vol name. Scale down past 50% ann. vol.
            vol = (pk.get("technicals") or {}).get("vol_ann_pct")
            if isinstance(vol, (int, float)) and vol > 50:
                weight *= max(0.4, 50.0 / vol)
            desired = equity * weight / 100 * risk.risk_scale(s)
            # Conviction outranks parking: pull cash back from the
            # equitization position before sizing against the buffer
            _free_cash_from_equitization(s, desired, prices, mode)
            # PLANNED-EVICTION PROCEEDS (2026-07-25 audit finding #7): when a
            # swap is planned, the weak holding's value is cash this book is
            # about to have — sizing the replacement from PRE-eviction cash
            # alone shrinks it to whatever happened to be idle. Alpha survived
            # this only because the parking pull above usually funded it; with
            # an empty reservoir it fails exactly like core_blend did. The
            # commit below re-clamps to the cash the sell REALLY freed, so an
            # over-optimistic basis can never route an unfunded order.
            _proceeds = (_planned_swap_proceeds(s, _swap[0], prices)
                         if _swap else 0.0)
            budget = min(desired,
                         s["cash"] + _proceeds - config.MIN_CASH_BUFFER)
            qty = _size_qty(budget, px, mode)
            if qty < (1 if mode != "paper" else 0.05):
                state.journal("buy_skipped_insufficient_cash", sleeve="alpha_stocks",
                              symbol=sym, budget=round(budget, 2), price=round(px, 2),
                              cash=round(s["cash"], 2))
                continue
            cost = qty * px
            # Post-stop cool-off (2026-07-26): a name this firm stopped out for
            # RISK does not come straight back just because the committee likes
            # it again — it waits out the cool-off or reclaims the stop +3%.
            _sb, _sbwhy = recent_stop_block(sym, px)
            if _sb:
                state.journal("buy_blocked_stop_cooloff", sleeve="alpha_stocks",
                              symbol=sym, reason=_sbwhy)
                continue
            ok1, why1 = risk.check_position_size("alpha_stocks", equity, cost, cfg["max_position_pct"])
            if not ok1:
                state.journal("buy_blocked_position_size", sleeve="alpha_stocks",
                              symbol=sym, reason=why1)
                continue
            ok2, why2 = risk.check_sector_cap(s["positions"], sectors, sym, cost, equity)
            if not ok2:   # was a SILENT drop — a STRONG_BUY in an already-heavy sector
                state.journal("buy_blocked_sector", sleeve="alpha_stocks",
                              symbol=sym, reason=why2)
                continue
            ok3, why3 = risk.check_correlation(s, sym)
            if not ok3:
                state.journal("buy_blocked_correlation", sleeve="alpha_stocks",
                              symbol=sym, reason=why3)
            ok4, why4 = risk.check_firm_name_cap(sym, cost)
            if not ok4:
                state.journal("buy_blocked_firm_cap", sleeve="alpha_stocks",
                              symbol=sym, reason=why4)
                continue
            ok5, why5 = risk.check_firm_sector_cap(sym, sectors.get(sym, ""), cost)
            if not ok5:
                state.journal("buy_blocked_firm_sector", sleeve="alpha_stocks",
                              symbol=sym, reason=why5)
                continue
            # Liquidity guard: never be a whale in the name — entry capped vs
            # avg daily dollar volume so fills stay clean as the fund scales
            adv = ((verdicts.get(sym, {}).get("packet") or {})
                   .get("technicals") or {}).get("avg_dollar_vol")
            if isinstance(adv, (int, float)) and adv > 0 \
                    and cost > adv * config.MAX_POSITION_OF_ADV_PCT / 100:
                state.journal("buy_blocked_liquidity", sleeve="alpha_stocks",
                              symbol=sym, cost=cost, adv=adv)
                continue
            if ok3:   # ok1/ok2 already journaled+continued on failure above
                # `sym` has now cleared EVERY gate — only now commit a planned
                # eviction. If the sell can't actually free the slot (live pending
                # / no live price) skip the buy rather than half-completing a swap.
                if _swap:
                    if not _commit_swap_sell(s, _swap[0], _swap[1], sym,
                                             v.get("conviction"), prices, mode,
                                             _swaps):
                        continue
                    # RE-SIZE FROM POST-SELL CASH (finding #7): `qty` was sized
                    # on the planned-proceeds basis; the eviction has now
                    # actually booked, so clamp DOWN to the money that really
                    # arrived. Clamping down only shrinks the notional, so every
                    # cap already cleared above still holds.
                    _post_q = _size_qty(
                        max(0.0, s["cash"] - config.MIN_CASH_BUFFER), px, mode)
                    if _post_q < qty:
                        qty = _post_q
                    if qty < (1 if mode != "paper" else 0.05):
                        state.journal("swap_buy_undersized_after_sell",
                                      sleeve="alpha_stocks", symbol=sym,
                                      sold=_swap[0], qty=qty,
                                      price=round(px, 4),
                                      cash=round(s["cash"], 2))
                        continue
                wctx = pk.get("watchlist_context") or {}
                stop, trail = _vol_stops(cfg, (pk.get("technicals") or {}).get("vol_ann_pct"))
                broker.execute(mode, s, symbol=sym, side="buy", quantity=qty, price=px,
                               meta={"thesis": v.get("thesis", ""),
                                     "conviction": v.get("conviction"),
                                     "rating": v.get("rating"),
                                     "degraded": bool(v.get("degraded")),
                                     "depth": state.get_settings().get("depth", ""),
                                     "sector": sectors.get(sym, ""),
                                     "sector_tailwind": _tailwind(sectors.get(sym, "")),
                                     # factor lenses at entry -> realized P&L
                                     # becomes factor-attributed in the ledger
                                     "factors": pk.get("factor_scores") or {},
                                     # per-desk scores at entry (goal audit 4b)
                                     "desk_scores": _entry_desk_scores(verdicts.get(sym) or {}),
                                     "stop_pct": stop, "trail_stop_pct": trail,
                                     "vol_ann": (pk.get("technicals") or {}).get("vol_ann_pct"),
                                     # flush re-checks (fleet action 9): a
                                     # queued-for-open fill re-runs the
                                     # earnings-window + ADV-footprint gates
                                     # from these stamps
                                     "next_earnings_date": (pk.get("next_earnings") or {}).get("date"),
                                     "avg_dollar_vol": (pk.get("technicals") or {}).get("avg_dollar_vol"),
                                     "reentry_after_stop": bool(wctx.get("stop_out"))})
        if book_full_skips:
            # A BUY the book had no room for must say so — "the firm had a
            # BUY but nothing happened" should never be a mystery (2026-06-12)
            state.journal("buy_skipped_book_full", sleeve="alpha_stocks",
                          symbols=book_full_skips,
                          max_positions=cfg["max_positions"])
        # After conviction got every dollar it asked for, park the residue
        _equitize_idle_cash(s, prices, mode)
    else:
        state.journal("entries_blocked", sleeve="alpha_stocks", reason=why)

    equity = broker.sleeve_equity(s, prices)
    risk.mark_equity("alpha_stocks", s, equity, broker.sleeve_risk_equity(s, equity))
    state.save_sleeve("alpha_stocks", s)


# ── Sleeve 2: Core Blend ──────────────────────────────────────────────────────

def run_core_blend(verdicts: dict, sectors: dict, mode: str) -> None:
    with state.SLEEVE_LOCK:
        _run_core_blend(verdicts, sectors, mode)


def _run_core_blend(verdicts: dict, sectors: dict, mode: str) -> None:
    cfg = config.SLEEVES["core_blend"]
    s = state.get_sleeve("core_blend")
    # DYNAMIC core seats (operator 2026-07-16): the core ETFs are EARNED by
    # dual momentum (firm/allocation.py) at the fixed 35/15/5 slot weights —
    # the config trio is only the seed/fallback.
    _cw = allocation.core_weights()
    # Migration hygiene: an ETF that LOST its seat but is still held (its
    # seat-change sell deferred on a missing quote) is sold here, so an
    # unseated ETF can never linger as a phantom core holding.
    # 2026-07-25 audit (finding #5): this gated on `core_seat` ALONE — a flag
    # written only by the core-rebalance buy a few lines below, and NEVER by
    # allocation.py, which stamps `seat_change: True` on every seat it trades
    # (allocation.py:129,155) and is the engine that actually EARNS the seats.
    # Live core_blend held SOXX/USO/SMH, all three carrying seat_change and
    # none carrying core_seat, so an ETF that LOST its seat was never swept.
    # Worse than a stranded holding: an unseated ETF is `not in _cw`, so the
    # satellite scan below counts it as a SATELLITE, spending satellite budget
    # on dead core plumbing and freezing the satellite book. Gate on EITHER
    # seat stamp — two writers, ONE predicate. (Parking is still excluded by
    # the cash_equitization test: allocation stamps both flags on a parking
    # seat trade, so the order of these tests matters.)
    for _old in list(s["positions"].keys()):
        _op = s["positions"][_old]
        if (_op.get("instrument") == "equity" and not _op.get("cash_equitization")
                and (_op.get("core_seat") or _op.get("seat_change"))
                and _old not in _cw):
            _opx = broker.equity_prices([_old]).get(_old, 0)
            if _opx > 0:
                broker.execute(mode, s, symbol=_old, side="sell",
                               quantity=_op["quantity"], price=_opx,
                               meta={"reason": "unseated core ETF — migration sweep"})
    # Core Blend's mandate is stocks + ETFs: researched ETF verdicts are buyable
    ranked = _ranked_buys(verdicts, cfg["min_conviction"], kinds=("stock", "etf"))
    prices = broker.equity_prices(
        list(s["positions"].keys()) + list(_cw)
        + [sym for sym, _ in ranked])
    equity = broker.sleeve_equity(s, prices)
    _apply_sells(s, verdicts, mode, prices)

    ok, why = risk.entries_allowed("core_blend", s, equity)
    if ok:
        # Maintain the ETF core at target weights (tolerance 4%)
        for etf, target_pct in _cw.items():
            px = prices.get(etf, 0)
            if px <= 0:
                state.journal("buy_skipped_no_price", sleeve="core_blend",
                              symbol=etf, note="core rebalance skipped")
                continue
            held_val = s["positions"].get(etf, {}).get("market_value", 0)
            gap = equity * target_pct / 100 - held_val
            if gap > equity * 0.04 and s["cash"] > gap:
                qty = int(gap / px)
                if qty >= 1:
                    broker.execute(mode, s, symbol=etf, side="buy", quantity=qty,
                                   price=px, meta={"reason": "core rebalance",
                                                   "core_seat": True})
                    if etf in s["positions"]:
                        s["positions"][etf]["core_seat"] = True

        # Satellite conviction stocks (the parking ETF is neither core nor a
        # satellite — it never consumes satellite budget)
        sat_syms = [sym for sym, pos in s["positions"].items()
                    if sym not in _cw and pos["instrument"] == "equity"
                    and not pos.get("cash_equitization")]
        # Capacity is BUDGET-bound, not count-bound (operator 2026-07-17): the
        # mechanical core owns its earned weights; satellites may fill the
        # rest (100 - core = 45%). No count cap — the book adds names while
        # budget room remains and swap-rotates the weakest out once it's spent.
        _sat_budget_pct = max(0.0, 100.0 - sum(_cw.values()))
        _min_viable = float(cfg.get("satellite_min_viable_pct", 3.0))

        def _sat_used_pct() -> float:
            val = sum((s["positions"][x].get("quantity") or 0)
                      * (prices.get(x) or s["positions"][x].get("last_price")
                         or s["positions"][x].get("entry_price") or 0)
                      for x in sat_syms if x in s["positions"])
            return val / equity * 100 if equity > 0 else 100.0

        sat_full_skips: list[str] = []
        _swaps = [0]                       # per-cycle swap budget (REPLACE_MAX_PER_CYCLE)
        _rsyms = {sy for sy, _ in ranked}  # drop stale swap-intents for names no longer ranked
        s["swap_intents"] = {k: vv for k, vv in (s.get("swap_intents") or {}).items()
                             if k in _rsyms}
        s["swapped_out"] = {k: r for k, r in (s.get("swapped_out") or {}).items()
                            if _swap_cooldown_active(s, k)}   # expire old cooldowns
        # Core selects its satellites purely by conviction rank (operator
        # 2026-07-02: a real multi-strategy fund HOLDS the same name across
        # books — the earlier "core must avoid alpha's names / tilt away" de-dup
        # was removed; sleeves are independent and may overlap).
        for sym, v in ranked:
            _swap = None                   # a planned (weak_sym, weak_conv) eviction, if any
            if sym in s["positions"]:
                continue
            if _swap_cooldown_active(s, sym):   # just swapped OUT — no immediate re-buy
                state.journal("buy_skipped_swap_cooldown", sleeve="core_blend", symbol=sym)
                continue
            _room_pct = _sat_budget_pct - _sat_used_pct()
            if _room_pct < _min_viable:
                # Satellite BUDGET spent: PLAN a swap now (guards + hysteresis);
                # the eviction is committed only after `sym` clears every buy
                # gate below, so a holding is never sold for a buy that then fails.
                _swap = _plan_swap(s, sym, v, verdicts, prices, cfg,
                                   set(_cw), _swaps)
                if not _swap:
                    sat_full_skips.append(sym)
                    continue
            if not risk.sector_allowed(s, sectors.get(sym, "")):
                state.journal("buy_blocked_sector", sleeve="core_blend",
                              symbol=sym, sector=sectors.get(sym))
                continue
            px = prices.get(sym, 0)
            # Post-stop cool-off (2026-07-26) — same contract as alpha. Core is
            # where OSCR survived as a held duplicate after alpha stopped it, so
            # this sleeve must honour the NAME-level stop too, not just its own.
            _sb, _sbwhy = recent_stop_block(sym, px)
            if _sb:
                state.journal("buy_blocked_stop_cooloff", sleeve="core_blend",
                              symbol=sym, reason=_sbwhy)
                continue
            if px <= 0:
                state.journal("buy_skipped_no_price", sleeve="core_blend",
                              symbol=sym)
                continue
            pk = verdicts.get(sym, {}).get("packet") or {}
            if not _entry_drift_ok(sym, px, pk, "core_blend"):
                continue
            if not _earnings_ok(sym, pk, "core_blend"):
                continue
            # Size to the standard satellite weight, shrunk to the remaining
            # budget room when no eviction will free space (a swap frees
            # roughly one position's worth, so it sizes at the full standard).
            _size_pct = (cfg["satellite_position_pct"] if _swap
                         else min(cfg["satellite_position_pct"],
                                  max(_room_pct, 0.0)))
            _desired_sat = equity * _size_pct / 100 * risk.risk_scale(s)
            # Conviction outranks parking (same rule as alpha/calls): reclaim
            # cash from the SPY parking before sizing — without this, cash
            # parked after a de-dup cycle could never fund the NEXT cycle's
            # satellites (week-sim caught it, 2026-07-01).
            _free_cash_from_equitization(s, _desired_sat, prices, mode)
            # PLANNED-EVICTION PROCEEDS (2026-07-25 audit finding #7): the
            # replacement used to be sized from PRE-eviction cash, which on the
            # live book meant min($2,222, $135.10 - buffer) = $35.10 — a swap
            # that liquidated a $1,957 holding to buy ~1 share in paper and,
            # in LIVE, never committed at all because _size_qty returns 0 for
            # any price above the residual cash. `position_swapped` never once
            # appears in 5,139 journal rows: core_blend's rotation engine was
            # inert. The eviction's proceeds are a known imminent credit, so
            # they belong in the funding basis; the commit below re-clamps to
            # the cash that actually arrived.
            _proceeds = (_planned_swap_proceeds(s, _swap[0], prices)
                         if _swap else 0.0)
            budget = min(_desired_sat,
                         s["cash"] + _proceeds - config.MIN_CASH_BUFFER)
            qty = _size_qty(budget, px, mode)
            if qty < (1 if mode != "paper" else 0.05):
                state.journal("buy_skipped_insufficient_cash", sleeve="core_blend",
                              symbol=sym, budget=round(budget, 2), price=round(px, 2),
                              cash=round(s["cash"], 2))
                continue
            # MINIMUM-NOTIONAL FLOOR (finding #7): a satellite consumes a NAME
            # slot, research attention and the exit monitor's budget, so no
            # path may open a token position — the $35 buy above would have
            # been a permanent dust holding wearing a satellite's costs. The
            # floor is the sleeve's OWN definition of viable
            # (satellite_min_viable_pct, the same 3% the budget-room gate uses
            # to decide whether a new name fits at all), not a new constant.
            # Scaled by risk_scale for the same reason _desired_sat is: in a
            # soft-risk drawdown the desk deliberately sizes DOWN, and a fixed
            # floor would turn that into a blanket refusal to open anything —
            # a de-risk must shrink positions, not silently close the book.
            _min_notional = equity * _min_viable / 100 * risk.risk_scale(s)
            if qty * px < _min_notional:
                state.journal("buy_skipped_below_min_notional", sleeve="core_blend",
                              symbol=sym, notional=round(qty * px, 2),
                              min_notional=round(_min_notional, 2),
                              budget=round(budget, 2), cash=round(s["cash"], 2),
                              swap_planned=bool(_swap))
                continue
            ok2, why2 = risk.check_sector_cap(s["positions"], sectors, sym, qty * px, equity)
            if not ok2:   # was a SILENT drop
                state.journal("buy_blocked_sector", sleeve="core_blend",
                              symbol=sym, reason=why2)
                continue
            ok3, why3 = risk.check_correlation(s, sym)
            if not ok3:
                state.journal("buy_blocked_correlation", sleeve="core_blend",
                              symbol=sym, reason=why3)
            ok4, why4 = risk.check_firm_name_cap(sym, qty * px)
            if not ok4:
                state.journal("buy_blocked_firm_cap", sleeve="core_blend",
                              symbol=sym, reason=why4)
                continue
            ok5, why5 = risk.check_firm_sector_cap(sym, sectors.get(sym, ""), qty * px)
            if not ok5:
                state.journal("buy_blocked_firm_sector", sleeve="core_blend",
                              symbol=sym, reason=why5)
                continue
            if ok3:   # ok2 already journaled+continued on failure above
                # `sym` cleared every gate — only now commit a planned eviction;
                # if the sell can't free the slot, skip the buy (no half-swap).
                if _swap:
                    if not _commit_swap_sell(s, _swap[0], _swap[1], sym,
                                             v.get("conviction"), prices, mode, _swaps):
                        continue
                    sat_syms = [x for x in sat_syms if x != _swap[0]]   # evicted -> slot freed
                    # RE-SIZE FROM POST-SELL CASH (finding #7): `qty` was sized
                    # on the planned-proceeds basis; the eviction has booked, so
                    # clamp DOWN to the money that really arrived. Shrinking the
                    # notional keeps every cap cleared above valid. A result
                    # under the viability floor is a half-swap the operator must
                    # SEE (the freed cash is re-parked by the equitizer below),
                    # so it is journaled, never silently converted to dust.
                    _post_q = _size_qty(
                        max(0.0, s["cash"] - config.MIN_CASH_BUFFER), px, mode)
                    if _post_q < qty:
                        qty = _post_q
                    if (qty < (1 if mode != "paper" else 0.05)
                            or qty * px < _min_notional):
                        state.journal("swap_buy_undersized_after_sell",
                                      sleeve="core_blend", symbol=sym,
                                      sold=_swap[0], qty=qty, price=round(px, 4),
                                      notional=round(qty * px, 2),
                                      min_notional=round(_min_notional, 2),
                                      cash=round(s["cash"], 2))
                        continue
                wctx = pk.get("watchlist_context") or {}
                stop, trail = _vol_stops(cfg, (pk.get("technicals") or {}).get("vol_ann_pct"))
                broker.execute(mode, s, symbol=sym, side="buy", quantity=qty, price=px,
                               meta={"thesis": v.get("thesis", ""),
                                     "conviction": v.get("conviction"),
                                     "rating": v.get("rating"),
                                     "degraded": bool(v.get("degraded")),
                                     "depth": state.get_settings().get("depth", ""),
                                     "sector": sectors.get(sym, ""),
                                     "sector_tailwind": _tailwind(sectors.get(sym, "")),
                                     "factors": pk.get("factor_scores") or {},
                                     # per-desk scores at entry (goal audit 4b)
                                     "desk_scores": _entry_desk_scores(verdicts.get(sym) or {}),
                                     "stop_pct": stop, "trail_stop_pct": trail,
                                     "vol_ann": (pk.get("technicals") or {}).get("vol_ann_pct"),
                                     "next_earnings_date": (pk.get("next_earnings") or {}).get("date"),
                                     "avg_dollar_vol": (pk.get("technicals") or {}).get("avg_dollar_vol"),
                                     "reentry_after_stop": bool(wctx.get("stop_out"))})
                sat_syms.append(sym)
        if sat_full_skips:
            state.journal("buy_skipped_book_full", sleeve="core_blend",
                          symbols=sat_full_skips,
                          satellite_budget_pct=round(_sat_budget_pct, 1),
                          satellite_used_pct=round(_sat_used_pct(), 1))
        # Companion to the satellite de-dup (#5b, adversarial verify): cash
        # freed by skipped-overlap satellites parks in SPY instead of idling —
        # a SPY-benchmarked sleeve holding cash loses to its benchmark by
        # default. Distinct flagged position; never merged into the core ETFs.
        _equitize_idle_cash(s, prices, mode)
    else:
        state.journal("entries_blocked", sleeve="core_blend", reason=why)

    equity = broker.sleeve_equity(s, prices)
    risk.mark_equity("core_blend", s, equity, broker.sleeve_risk_equity(s, equity))
    state.save_sleeve("core_blend", s)


# ── Sleeve 3: Calls Desk ──────────────────────────────────────────────────────

def _est_option_fill_cost(mid: float, spread_pct) -> float:
    """Estimated PAPER cost of ONE contract at entry (2026-08-13 IG audit, the
    PEB defect): desk sizing divided the budget by the raw mid*100, but the
    paper broker fills a buy at mid*(1 + spread_pct/200 * option_spread_frac)
    plus a per-contract fee (broker._paper_fill_price + the fee line in
    execute()), so a max-size order could clear every desk gate and then die in
    execute()'s cash check as a silent drop. Mirrors the fill model exactly;
    sizing against it yields FEWER contracts = strictly safer. Fail-static to
    the raw mid cost on any bad input — never a crash, never more risk."""
    try:
        est = float(mid)
        sl = getattr(config, "PAPER_SLIPPAGE", {}) or {}
        frac = float(sl.get("option_spread_frac", 0.0) or 0.0)
        if isinstance(spread_pct, (int, float)) and spread_pct > 0 and frac > 0:
            est = est * (1 + float(spread_pct) / 200.0 * frac)
        return est * 100 + float(sl.get("option_fee_per_contract", 0.0) or 0.0)
    except Exception:
        return float(mid) * 100 if mid else 0.0


def _judge_options_veto(v: dict, side: str) -> bool:
    """Fleet re-grade action 1 (2026-07-16): the judge's suitability flag ran
    0-true in 575 verdicts — a reflexive False that, as a hard gate, kept the
    options desk (25% of firm capital) inert for the fund's WHOLE LIFE. The
    flag is DEMOTED to a veto. Fix brief 2026-07-21 #6: the 07-16 demotion
    keyed the veto on reason-PRESENCE, but the judge schema REQUIRES the
    reason field on every verdict — so every bare False arrived "explained"
    (95/235 verdicts in the trailing 10 cycles carried one) and the veto
    re-bound universally; the mechanical screen never decided and the desk
    stayed inert one link further down (STRONG_BUY-never-emitted -> flag-
    never-true -> reason-always-present). The veto now binds ONLY on the
    judge's EXPLICIT options_veto_<side> opt-in (schema-defined override,
    absent on legacy verdicts) that still carries a stated reason — an
    unexplained veto never binds, exactly as the 07-16 contract intended."""
    if not v.get(f"options_veto_{side}"):
        return False
    reason = str(v.get(f"suitable_for_{side}_reason") or "").strip()
    return len(reason) >= int(getattr(config,
                                      "OPTIONS_JUDGE_VETO_MIN_REASON", 15))


def _mech_options_why(sym: str | None, pk: dict | None) -> str | None:
    """WHICH leg of the mechanical screen refused the name, or None when it
    passes. Added 2026-07-28 so `puts_no_candidates` / `calls_no_candidates`
    can say what actually blocked a name instead of the label
    "mechanical_screen". _mech_options_ok is DEFINED as this returning None,
    so the gate and its own explanation can never drift apart — the failure
    mode this codebase has hit repeatedly (a silence-explainer that reports a
    different gate than the one that ran sends every auditor down a false
    trail; see the 07-21 census fix)."""
    if sym:
        from firm import tradable as _trd
        if not _trd.options_enabled(sym):
            return "no listed options on Public for this name"
    days = ((pk or {}).get("next_earnings") or {}).get("days_away")
    max_dte = int(config.SLEEVES["calls_only"].get("max_dte", 28))
    if isinstance(days, (int, float)) and 0 <= days <= max_dte:
        return (f"earnings in {int(days)}d sits inside the {max_dte}d max "
                "option life (binary-event trade in disguise)")
    return None


def _mech_options_ok(sym: str | None, pk: dict | None) -> bool:
    """Desk-side MECHANICAL suitability — the objective legs of the judge's
    own stated criteria, checked from data instead of asked as a vibe:
    options must trade on Public for the name, and no earnings print may sit
    inside the option's possible life (the sleeve's max_dte — a print inside
    the window makes the trade a binary-event straddle in disguise). Chain
    quality (OI, spread) is enforced downstream by pick_call/pick_put, so a
    name that passes here with a junk chain still never opens. Fails open on
    missing packet data — the downstream contract picker is the backstop."""
    return _mech_options_why(sym, pk) is None


def _calls_entry_ok(v: dict, sym: str | None = None,
                    pk: dict | None = None) -> bool:
    """The calls sleeve entry gate — the firm's HIGHEST bar, on the leveraged
    book. Broadened from STRONG_BUY-only (operator 2026-07-02): a gate keyed
    SOLELY on a rating the judge structurally never emitted (0 STRONG_BUYs in
    357 verdicts) was unreachable — the sleeve sat inert for weeks. It now also
    takes a BUY the committee marked options-suitable at NEAR-TOP conviction
    (>= CALLS_BUY_CONVICTION_MIN, a HIGHER bar than the STRONG_BUY path's
    min_conviction), so the leveraged sleeve can finally act on the firm's best
    ideas WITHOUT lowering the bar to noise. Degraded/quant-only verdicts never
    qualify. Fleet re-grade action 1: the judge's flag endorses or VETOES
    (with a stated reason); a reflexive bare False no longer binds — the
    desk's mechanical screen decides."""
    if not v or v.get("degraded"):
        return False
    conv = v.get("conviction") or 0
    if v.get("rating") == "STRONG_BUY":
        if conv < config.SLEEVES["calls_only"]["min_conviction"]:
            return False
    elif v.get("rating") == "BUY":
        if conv < _options_conviction_min("calls"):
            return False
    else:
        return False
    if v.get("suitable_for_calls"):
        return True
    if _judge_options_veto(v, "calls"):
        return False
    return _mech_options_ok(sym, pk)


_CONV_BAR_MEMO: dict = {"stamp": None, "bars": {}}
_CONV_FAMILIES = {
    "calls": ("BUY", "STRONG_BUY"),
    "puts": ("SELL", "STRONG_SELL"),
    "pooled": ("BUY", "STRONG_BUY", "SELL", "STRONG_SELL"),
}


def _research_stamp():
    """Cheap change-detector for the research snapshot (file mtime + size) —
    the memo key, so the gate never re-parses an unchanged 8 MB file."""
    try:
        st = (config.STATE_DIR / "research.json").stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def _options_conviction_min(side: str = "pooled") -> int:
    """AUDIT 2026-07-16 F1/#3: the fixed BUY>=72 bar was empirically
    unreachable against the judge's OWN output (575 non-degraded verdicts:
    BUY mean 62.7, max 72 with n=4 — the desk never traded once in 5+ weeks
    and 25% of firm capital ran as a SPY tracker). The bar is now the rolling
    p90 of the trailing conviction distribution — the firm's literal top
    decile — floored at CALLS_BUY_CONVICTION_FLOOR (never noise) and capped at
    the old 72 (never stricter than the bar that was already judged safe).
    Adaptive: if the committee's conviction regime shifts up, the bar follows
    it up. Fail-static: any read error returns the old 72.

    PER SIDE (2026-09-01, fix brief 90 — 'puts gate: 40 real SELL-family
    verdicts, 0 ever qualified'). One POOLED p90 sat at 68-69 because the
    committee's BUYs run hotter than its SELLs (measured: BUY p90 70, SELL p90
    64, pooled 68), so no SELL could ever clear a bar the BUYs set — the exact
    07-16 calls-gate bug class, mirrored. `calls` reads the BUY family, `puts`
    the SELL family; `pooled` is kept for any legacy reader. MEMOIZED on the
    research file's stamp: every gate evaluation re-parsed the ~8 MB research
    file (0.18 s) and _run_calls_only does that per verdict INSIDE SLEEVE_LOCK,
    starving the 1 s options monitor — the R3 hazard on the leveraged book."""
    fam = _CONV_FAMILIES.get(side, _CONV_FAMILIES["pooled"])
    try:
        stamp = _research_stamp()
        if (stamp is not None and _CONV_BAR_MEMO["stamp"] == stamp
                and side in _CONV_BAR_MEMO["bars"]):
            return int(_CONV_BAR_MEMO["bars"][side])
        hist = (state.load("research", {}) or {}).get("history", [])[-30:]
        convs = sorted(
            v.get("conviction") for h in hist
            for v in (h.get("verdicts") or {}).values()
            if not (v or {}).get("degraded")
            and v.get("rating") in fam
            and isinstance(v.get("conviction"), (int, float)))
        bar = int(config.CALLS_BUY_CONVICTION_MIN)
        if len(convs) >= 10:
            p90 = convs[int(0.9 * (len(convs) - 1))]
            # fallback literal follows config's 65 -> 60 (2026-08-13 operator
            # decision) so a config-read failure can't resurrect the old floor
            bar = int(max(getattr(config, "CALLS_BUY_CONVICTION_FLOOR", 60),
                          min(config.CALLS_BUY_CONVICTION_MIN, p90)))
        if _CONV_BAR_MEMO["stamp"] != stamp:
            _CONV_BAR_MEMO["stamp"] = stamp
            _CONV_BAR_MEMO["bars"] = {}
        _CONV_BAR_MEMO["bars"][side] = bar
        return bar
    except Exception:
        pass
    return int(config.CALLS_BUY_CONVICTION_MIN)


def _options_reentry_blocked(s: dict, underlying: str) -> str | None:
    """(2026-09-01, fix brief 16) HPE was stopped out of a call and re-bought
    the same session, twice (-$864 across the two lots). broker._book_sell has
    carried a `stop_cooldown` block on the closed-trade row since 07-27 with
    the note 'desk gate: sleeves.py' — and no gate ever consumed it, so the
    same underlying can be stopped and re-entered in LIVE too. Returns the
    reason when the most recent stop on this underlying is younger than its
    cooldown, else None. Reads the sleeve's own closed_trades (the desk's
    memory of its own stops)."""
    try:
        now = datetime.now(timezone.utc)
        und = str(underlying or "").upper()
        for t in reversed((s.get("closed_trades") or [])[-300:]):
            cd = t.get("stop_cooldown") or {}
            if not cd:
                continue
            if str(cd.get("underlying") or t.get("underlying") or "").upper() != und:
                continue
            stopped = datetime.fromisoformat(
                str(cd.get("stopped_at") or "").replace("Z", "+00:00"))
            if stopped.tzinfo is None:
                stopped = stopped.replace(tzinfo=timezone.utc)
            days = int(cd.get("cooldown_days")
                       or getattr(config, "CALLS_REENTRY_COOLDOWN_DAYS", 10))
            age_d = (now - stopped).total_seconds() / 86400
            if age_d < days:
                return (f"stopped out {age_d:.1f}d ago ({cd.get('reason') or 'stop'}); "
                        f"re-entry cooldown {days}d")
            return None            # the latest stop on this name is past its cooldown
    except Exception:
        return None
    return None


def _puts_entry_ok(v: dict, sym: str | None = None,
                   pk: dict | None = None) -> bool:
    """The PUTS entry gate (combined options desk, operator 2026-07-07) — the
    bearish MIRROR of _calls_entry_ok on the SAME leveraged book: long OTM puts on
    the firm's highest-conviction SELL-side. STRONG_SELL at the sleeve
    min_conviction, OR a SELL the committee marked options-suitable at NEAR-TOP
    conviction (>= CALLS_BUY_CONVICTION_MIN). Degraded/quant-only verdicts never
    qualify — a fabricated fallback must never open a leveraged put. Fleet
    re-grade action 1: same judge-veto demotion as the calls gate."""
    if not v or v.get("degraded"):
        return False
    conv = v.get("conviction") or 0
    if v.get("rating") == "STRONG_SELL":
        if conv < config.SLEEVES["calls_only"]["min_conviction"]:
            return False
    elif v.get("rating") == "SELL":
        if conv < _options_conviction_min("puts"):
            return False
    else:
        return False
    if v.get("suitable_for_puts"):
        return True
    if _judge_options_veto(v, "puts"):
        return False
    return _mech_options_ok(sym, pk)


def _preplan_calls_ladders(verdicts: dict) -> dict:
    """Warm the Opus exit-ladder call for each ranked candidate BEFORE the
    sleeve lock is taken (2026-07-02 review: the ladder call is a multi-minute
    Opus call and it ran INSIDE SLEEVE_LOCK, freezing the 1s calls monitor and
    every exit sweep for the whole book while a single new call was priced —
    the R3 hazard on the leveraged book). Pure read: a state snapshot only to
    decide WHICH ladders to warm; the authoritative open decisions stay in the
    locked pass, which falls back to an inline call on any cache miss. Returns
    {osi: ladder}."""
    cache: dict = {}
    try:
        cfg = config.SLEEVES["calls_only"]
        s = state.get_sleeve("calls_only")
        equity = broker.sleeve_equity(s, broker.fetch_marks(s), from_fetch_marks=True)
        ok, _ = risk.entries_allowed("calls_only", s, equity)
        if not ok:
            return cache
        ranked = sorted(
            ((sym, d) for sym, d in verdicts.items()
             if d.get("kind") == "stock"
             and _calls_entry_ok(d["verdict"], sym, d.get("packet"))),
            key=lambda kv: kv[1]["verdict"]["conviction"], reverse=True)
        held = {p.get("underlying") for p in s["positions"].values()}
        for sym, d in ranked[:cfg["max_positions"] + 2]:
            if sym in held:
                continue
            contract = broker.pick_call(sym, cfg["otm_pct"], cfg["min_dte"],
                                        cfg["max_dte"])
            if not contract:
                continue
            tech = (d.get("packet") or {}).get("technicals", {})
            cache[contract["osi"]] = exits.plan_option_exits(
                contract, d["verdict"], tech)
        # Combined desk (2026-07-07): warm PUT ladders for the top SELL-side too,
        # so the locked puts pass consumes the cache instead of an inline Opus call.
        ranked_p = sorted(
            ((sym, d) for sym, d in verdicts.items()
             if d.get("kind") == "stock"
             and _puts_entry_ok(d["verdict"], sym, d.get("packet"))),
            key=lambda kv: kv[1]["verdict"]["conviction"], reverse=True)
        for sym, d in ranked_p[:cfg["max_positions"] + 2]:
            if sym in held:
                continue
            contract = broker.pick_put(sym, cfg["otm_pct"], cfg["min_dte"],
                                       cfg["max_dte"])
            if not contract:
                continue
            tech = (d.get("packet") or {}).get("technicals", {})
            cache[contract["osi"]] = exits.plan_option_exits(
                contract, d["verdict"], tech)
    except Exception:
        return cache          # any hiccup -> locked pass plans inline as before
    return cache


def run_calls_only(verdicts: dict, mode: str) -> None:
    # Opus ladder calls happen HERE, outside the lock; the locked pass consumes
    # the warmed cache (and falls back to an inline call on a miss).
    ladder_cache = _preplan_calls_ladders(verdicts)
    with state.SLEEVE_LOCK:
        _run_calls_only(verdicts, mode, ladder_cache)


def _puts_buy_pass(verdicts: dict, s: dict, cfg: dict, equity: float, mode: str,
                   ladder_cache: dict) -> None:
    """PUTS pass of the combined options desk (operator 2026-07-07): long OTM puts
    on the committee's highest-conviction SELL/STRONG_SELL names — the bearish
    mirror of the calls buy-pass on the SAME sleeve. Shares max_positions, the
    overnight-premium cap, firm caps, the exit ladder and the live path with
    calls. A put profits as the underlying FALLS; the premium-% SL/TP ladder is
    direction-agnostic and reused unchanged. Runs INSIDE SLEEVE_LOCK (caller)."""
    ranked = sorted(
        ((sym, d) for sym, d in verdicts.items()
         if d.get("kind") == "stock"
         and _puts_entry_ok(d["verdict"], sym, d.get("packet"))),
        key=lambda kv: kv[1]["verdict"]["conviction"], reverse=True)
    if not ranked:
        # SILENCE MUST EXPLAIN ITSELF ON THIS SIDE TOO (operator 2026-07-28:
        # "a lot of sell but i don't see any options"). The calls pass has
        # journaled `calls_no_candidates` with a per-leg census since 07-21;
        # the puts mirror returned here with NOTHING, so a day like 07-28 —
        # six committee SELLs, zero puts — left no record of which leg killed
        # each name. The operator could not tell a working-as-designed refusal
        # from a dead gate, and neither could the health desk. Unlike the calls
        # census this one names names: the SELL-family set is small enough to
        # print, and the per-name reason IS the answer to the question.
        _sell_bar = _options_conviction_min("puts")
        _killed = {"rating_or_degraded": 0, "below_conviction_bar": 0,
                   "judge_veto": 0, "mechanical_screen": 0}
        _why_by_sym: dict[str, str] = {}
        _sellf = 0
        for sym, d in verdicts.items():
            if d.get("kind") != "stock":
                continue
            v = d["verdict"] or {}
            if (v.get("degraded")
                    or v.get("rating") not in ("SELL", "STRONG_SELL")):
                _killed["rating_or_degraded"] += 1
                continue
            _sellf += 1
            conv = v.get("conviction") or 0
            bar = (cfg["min_conviction"] if v.get("rating") == "STRONG_SELL"
                   else _sell_bar)
            if conv < bar:
                _killed["below_conviction_bar"] += 1
                _why_by_sym[sym] = f"conviction {conv} < {bar}"
                continue
            if not v.get("suitable_for_puts") and _judge_options_veto(v, "puts"):
                _killed["judge_veto"] += 1
                _why_by_sym[sym] = "judge options_veto_puts (reason stated)"
            else:
                # ranked is empty, so the mechanical screen is what said no —
                # report WHICH leg so this is actionable, not just a label.
                _killed["mechanical_screen"] += 1
                _why_by_sym[sym] = (_mech_options_why(sym, d.get("packet"))
                                        or "GATE/CENSUS DRIFT: every census "
                                           "leg passed yet the gate refused")
        state.journal(
            "puts_no_candidates",
            sell_family_judged=_sellf,
            strong_sells=sum(1 for _, d in verdicts.items()
                             if d.get("kind") == "stock"
                             and (d["verdict"] or {}).get("rating")
                             == "STRONG_SELL"),
            killed_by=_killed,
            per_name=dict(sorted(_why_by_sym.items())[:12]),
            bar=f"STRONG_SELL (conv>={cfg['min_conviction']}) OR "
                f"SELL (conv>={_sell_bar}, SELL-family adaptive p90)")
        return
    # Decision-time gate record (fix brief 2026-07-17 #1): route_coverage's
    # "qualified" number re-evaluates TODAY'S gate against a 30-row history
    # window while "opened" counts journal orders over weeks — the two windows
    # drift apart and the IG read the mismatch as a gate BYPASS. Journaling the
    # qualified set at the moment the desk acts makes open-vs-qualified
    # reconcilable over ONE window: every option open must be preceded by a
    # same-cycle qualification for its underlying, or the reconcile hard-fails.
    state.journal("options_gate_qualified", side="puts",
                  symbols=[sym for sym, _ in ranked], n=len(ranked))
    spots = broker.equity_prices([sym for sym, _ in ranked])
    for sym, d in ranked:
        v = d["verdict"]
        _real_puts = sum(1 for p in s["positions"].values()
                         if not p.get("cash_equitization"))
        if _real_puts >= cfg["max_positions"]:
            # Journal the stop (operator 2026-07-28) — the calls pass got this
            # on 07-25 and the puts mirror, as usual, did not. An unjournaled
            # skip leaves a decision-time-QUALIFIED name with neither an open
            # nor a reason, which route_coverage must (correctly) report as a
            # silent evaporation: a real block misreported as an inert gate.
            state.journal("puts_skipped_book_full", symbol=sym,
                          held=_real_puts, cap=cfg["max_positions"])
            break
        if any(p.get("underlying") == sym for p in s["positions"].values()):
            state.journal("puts_skipped_already_held", symbol=sym)
            continue
        _cd = _options_reentry_blocked(s, sym)
        if not _cd:
            # (2026-09-02 full audit, bench-and-watchlist-1) The desk only knew
            # its OWN option stops. A name the equity sleeve stopped out this
            # morning could be re-entered this afternoon as a LEVERAGED put or
            # call -- the same mistake the equity book is already forbidden to
            # make, at 5-10x the delta. `spots` is already fetched above, so
            # this costs no extra request.
            _eb, _ewhy = recent_stop_block(sym, spots.get(sym, 0))
            if _eb:
                _cd = f"equity sleeve stop-out: {_ewhy}"
        if _cd:
            state.journal("puts_skipped_reentry_cooldown", symbol=sym, why=_cd)
            continue
        spot = spots.get(sym, 0)
        if spot > 0 and not _entry_drift_ok(sym, spot, d.get("packet") or {}, "calls_only"):
            continue
        if not _earnings_ok(sym, d.get("packet") or {}, "calls_only"):
            continue
        contract = broker.pick_put(sym, cfg["otm_pct"], cfg["min_dte"], cfg["max_dte"])
        if not contract:
            state.journal("puts_skipped_no_contract", symbol=sym,
                          reason="no liquid put in DTE window")
            continue
        # Never hold a long put THROUGH an earnings print (same overnight gap risk
        # as a call — the SL cannot route after hours).
        _ne = ((d.get("packet") or {}).get("next_earnings") or {}).get("days_away")
        _exp = _days_until(contract.get("expiration"))
        if isinstance(_ne, int) and 0 <= _ne <= (_exp if _exp is not None else 10**9):
            state.journal("puts_skipped_earnings_before_expiry", symbol=sym,
                          earnings_in=_ne, expiry_in=_exp)
            continue
        entry = contract["mid"]
        uvol = (d.get("packet", {}).get("technicals") or {}).get("vol_ann_pct")
        gap_scale = (max(0.4, min(1.0, 60.0 / uvol))
                     if isinstance(uvol, (int, float)) and uvol > 60 else 1.0)
        _desired = equity * cfg["max_position_pct"] / 100 * risk.risk_scale(s) * gap_scale
        # MIN-VIABLE CONTRACT + AFFORDABILITY STRETCH (2026-07-25 audit finding
        # #4). The calls pass got this on 2026-07-22/07-25; the puts pass — the
        # SAME sleeve, the same capital pool, the same parking reservoir — was
        # never given it, so the bearish side still deadlocked against its own
        # parking exactly as calls used to: calls_only carries ~$21,513 parked
        # in RSP against ~$3,248 raw cash, so a $1,200 put failed a $990 budget
        # even though the $2,476 hard cap fits it, the reservoir was never
        # freed (short<=0), and the name skipped size_zero every cycle forever.
        # Mirrored verbatim from the calls pass because the arithmetic is
        # identical: gap_scale is a MULTI-contract sizing-DOWN control and must
        # never zero out the firm's top SELL-side signal when ONE contract
        # still fits the gap-scale-free hard per-position cap; and when one
        # contract does NOT fit, try a cheaper strike a BOUNDED distance
        # further OTM (for a put, further OTM = a lower strike) rather than
        # excluding a whole underlying price class by silent arithmetic.
        # pick_put takes the same (ticker, otm_pct, min_dte, max_dte) signature
        # as pick_call, so the stretch ports unchanged. The bound
        # (CALLS_MAX_OTM_STRETCH_PCT — one knob for the one combined desk)
        # matters: an unbounded stretch would quietly turn the top signal into
        # a lottery ticket, so a still-unaffordable name is REFUSED with the
        # ceiling stated plainly. Raising max_position_pct is NOT the fix — it
        # was cut 25%->10% precisely so one worthless expiry cannot blow
        # through the sleeve's drawdown halt in a single trade.
        _one_contract = entry * 100
        _hard_cap = equity * cfg["max_position_pct"] / 100
        if _one_contract > _hard_cap:
            _otm = float(cfg["otm_pct"])
            _stretch_max = float(getattr(config, "CALLS_MAX_OTM_STRETCH_PCT", 6.0))
            while _otm < _stretch_max and _one_contract > _hard_cap:
                _otm = min(_stretch_max, _otm + 2.0)
                _alt = broker.pick_put(sym, _otm, cfg["min_dte"], cfg["max_dte"])
                if not _alt or _alt["mid"] * 100 >= _one_contract:
                    break                # no cheaper liquid strike out there
                contract, entry = _alt, _alt["mid"]
                _one_contract = entry * 100
            if _one_contract <= _hard_cap:
                state.journal("puts_otm_stretched", symbol=sym, otm_pct=_otm,
                              mid=entry, strike=contract.get("strike"),
                              one_contract=round(_one_contract, 2),
                              hard_cap=round(_hard_cap, 2))
        _fund_target = (max(_desired, _one_contract)
                        if _one_contract <= _hard_cap else _desired)
        # Freeing to _fund_target (not _desired) is what lets a valid
        # single-contract entry actually pull the parked capital it needs.
        _park = allocation.parking_symbol("calls_only")
        if _park and (s["positions"].get(_park) or {}).get("cash_equitization"):
            _free_cash_from_equitization(s, _fund_target,
                                         broker.equity_prices([_park]), mode)
        budget = min(_fund_target, s["cash"] - config.MIN_CASH_BUFFER)
        # Size against the ESTIMATED PAPER FILL, not the raw mid (2026-08-13
        # IG audit, PEB defect) — see _est_option_fill_cost. Fewer contracts,
        # never more.
        _fill_cost = _est_option_fill_cost(entry, contract.get("spread_pct"))
        contracts = int(budget / _fill_cost) if _fill_cost > 0 else 0
        if contracts < 1:
            state.journal(
                "puts_skipped_size_zero", symbol=sym,
                reason=(f"one contract (${_one_contract:,.0f}) exceeds the "
                        f"hard per-position cap ({cfg['max_position_pct']:.0f}%"
                        f" = ${_hard_cap:,.0f}) even after stretching to "
                        f"{getattr(config, 'CALLS_MAX_OTM_STRETCH_PCT', 6.0):.0f}% OTM — "
                        f"at this sleeve size the desk cannot hold a contract "
                        f"priced above ${_hard_cap / 100:,.2f}/share of premium"
                        if _one_contract > _hard_cap
                        else "insufficient capital for one contract even "
                             "after freeing parked capital"),
                one_contract=round(_one_contract, 2),
                hard_cap=round(_hard_cap, 2),
                premium_ceiling_per_share=round(_hard_cap / 100, 2))
            continue
        # Shared overnight option-premium cap (calls + puts): a long put also gaps
        # toward zero if the underlying gaps UP, so the whole book stays survivable.
        _open_prem = sum(p.get("market_value") or p.get("cost") or 0
                         for p in s["positions"].values() if p["instrument"] == "option")
        _prem_cap = equity * config.MAX_OVERNIGHT_CALL_PREMIUM_PCT / 100
        if _open_prem + contracts * entry * 100 > _prem_cap:
            _room = _prem_cap - _open_prem
            contracts = int(_room / (entry * 100)) if _room > 0 else 0
            if contracts < 1:
                state.journal("puts_skipped_overnight_premium_cap", symbol=sym,
                              open_premium=round(_open_prem, 0), cap=round(_prem_cap, 0))
                continue
        _csec = ((d.get("packet") or {}).get("fundamentals") or {}).get("sector", "")
        ok4, why4 = risk.check_firm_name_cap(sym, contracts * entry * 100)
        ok5, why5 = risk.check_firm_sector_cap(sym, _csec, contracts * entry * 100)
        if not (ok4 and ok5):
            state.journal("buy_blocked_firm_cap", sleeve="calls_only", symbol=sym,
                          reason=(why4 or why5))
            continue
        tech = (d.get("packet") or {}).get("technicals", {})
        ladder = (ladder_cache.get(contract["osi"])
                  or exits.plan_option_exits(contract, v, tech))
        spread = contract.get("spread_pct")
        if isinstance(spread, (int, float)) and spread > 0:
            ladder["sl_pct"] = round(min(15.0, max(ladder["sl_pct"], spread / 2 + 1.5)), 1)
        state.journal("exit_plan", sleeve="calls_only", symbol=contract["osi"],
                      source=ladder.get("source"), ladder=ladder, side="put")
        fresh = broker.option_quote_public(contract["osi"])
        if fresh > 0:
            if entry > 0 and (fresh / entry - 1) * 100 > 25:
                state.journal("buy_skipped_premium_drift", sleeve="calls_only",
                              symbol=contract["osi"], picked_mid=entry, fresh_mid=fresh)
                continue
            entry = fresh
            # requote re-size on the estimated fill too (2026-08-13 IG audit,
            # PEB defect); the premium-cap leg stays on raw premium notional —
            # that cap bounds overnight PREMIUM exposure, not cash.
            _fill_cost = _est_option_fill_cost(entry, contract.get("spread_pct"))
            _by_budget = int(budget / _fill_cost) if _fill_cost > 0 else 0
            _room2 = _prem_cap - _open_prem
            _by_room = int(_room2 / (entry * 100)) if _room2 > 0 else 0
            contracts = min(_by_budget, _by_room)
            if contracts < 1:
                # NAME THE BINDING CONSTRAINT (operator 2026-07-28). Both legs
                # land here, and this always blamed the overnight-premium cap —
                # so a MU put killed by a requote that moved the premium past
                # the CASH on hand was reported as a risk-cap block, sending
                # the operator to a knob that was not binding (room $6,163 vs a
                # $3,678 contract). Same bug on the calls side; same fix.
                if _by_budget < 1:
                    state.journal(
                        "puts_skipped_budget_after_requote", symbol=sym,
                        picked_mid=round(contract["mid"], 2), fresh_mid=round(entry, 2),
                        one_contract=round(entry * 100, 2), budget=round(budget, 2),
                        reason=("the re-quote moved the premium above the cash "
                                "this sleeve could free for one contract"))
                    continue
                state.journal("puts_skipped_overnight_premium_cap", symbol=sym,
                              open_premium=round(_open_prem, 0), cap=round(_prem_cap, 0))
                continue
            # POST-REQUOTE FIRM CAP RE-CHECK (2026-07-25 audit finding #4): the
            # calls pass re-runs the firm name/sector caps on the FRESH size
            # because a lower fresh mid recomputes a LARGER contract count than
            # the pre-Opus check cleared. The puts pass never did — so a put
            # whose premium fell between the pick and the requote could route a
            # bigger position than any firm cap ever approved, on the same
            # shared capital pool. Same check, same journal name, same window.
            _ok4b, _w4b = risk.check_firm_name_cap(sym, contracts * entry * 100)
            _ok5b, _w5b = risk.check_firm_sector_cap(sym, _csec,
                                                     contracts * entry * 100)
            if not (_ok4b and _ok5b):
                state.journal("buy_blocked_firm_cap_requote", sleeve="calls_only",
                              symbol=sym, reason=(_w4b or _w5b))
                continue
        meta = {
            "underlying": sym, "expiration": contract["expiration"],
            "strike": contract["strike"], "contract_type": "PUT",
            "open_close": "open", "conviction": v.get("conviction"),
            "rating": v.get("rating"), "sector": _csec,
            "depth": state.get_settings().get("depth", ""),
            "thesis": v.get("thesis", ""), "degraded": bool(v.get("degraded")),
            "factors": ((verdicts.get(sym) or {}).get("packet") or {}).get("factor_scores") or {},
            "sector_tailwind": _tailwind(_csec),
            "desk_scores": _entry_desk_scores(verdicts.get(sym) or {}),
            "spread_pct": contract.get("spread_pct"),
            "contracts_initial": contracts, "tp1_sold": False, "tp2_sold": False,
            "tp3_hit": False, "trail_pct": None, "ladder": ladder,
        }
        _res = broker.execute(mode, s, symbol=contract["osi"], side="buy",
                              quantity=contracts, price=entry,
                              instrument="option", meta=meta)
        # (2026-08-13 IG audit) the desk discarded execute()'s result — a
        # refused open (e.g. insufficient cash after slippage) vanished with
        # zero journal trace; the reconciler allowlists this event.
        if not (_res or {}).get("success"):
            state.journal("options_open_failed", sleeve="calls_only",
                          symbol=contract["osi"],
                          error=str((_res or {}).get("error"))[:150])


def _run_calls_only(verdicts: dict, mode: str, ladder_cache: dict | None = None) -> None:
    ladder_cache = ladder_cache or {}
    cfg = config.SLEEVES["calls_only"]
    s = state.get_sleeve("calls_only")
    marks = broker.fetch_marks(s)
    equity = broker.sleeve_equity(s, marks, from_fetch_marks=True)

    # Thesis-break exits (2026-06-10): held-call UNDERLYINGS are re-researched
    # every cycle — a call whose underlying the committee just turned SELL is
    # dead theta wearing a ladder. The ladder manages price, not narrative.
    for osi in list(s["positions"].keys()):
        pos = s["positions"][osi]
        if pos.get("instrument") != "option" or pos.get("exit_pending"):
            continue
        und = pos.get("underlying", "")
        v = (verdicts.get(und) or {}).get("verdict") or {}
        # Direction-aware thesis break (combined desk 2026-07-07): a CALL is dead
        # theta once the committee turns the underlying SELL; a PUT is dead once it
        # turns BUY. Close the option whose narrative just inverted against it.
        _brk = (RATING_BUYS if str(pos.get("contract_type", "CALL")).upper()
                .startswith("P") else RATING_SELLS)
        if v.get("rating") in _brk and v.get("degraded"):
            # A degraded quant-only fallback must not close a leveraged option on
            # a non-committee thesis (2026-07-02 audit; matches the equity guard).
            # The ladder's hard/trailing stops still manage price risk.
            state.journal("degraded_sell_blocked", sleeve="calls_only",
                          symbol=osi, underlying=und, rating=v.get("rating"),
                          contract_type=pos.get("contract_type", "CALL"))
            continue
        if v.get("rating") in _brk:
            px = marks.get(osi, 0) or float(pos.get("last_price") or 0)
            if px > 0:
                broker.execute(mode, s, symbol=osi, side="sell",
                               quantity=pos["quantity"], price=px,
                               instrument="option",
                               meta={"open_close": "close", "urgent": True,
                                     "reason": f"underlying verdict {v['rating']}"})
            else:
                state.journal("sell_skipped_no_price", sleeve="calls_only",
                              symbol=osi, rating=v.get("rating"))

    ok, why = risk.entries_allowed("calls_only", s, equity)
    if ok:
        ranked = sorted(
            ((sym, d) for sym, d in verdicts.items()
             if d.get("kind") == "stock"
             and _calls_entry_ok(d["verdict"], sym, d.get("packet"))),
            key=lambda kv: kv[1]["verdict"]["conviction"], reverse=True)
        if ranked:
            # Decision-time gate record — see _puts_buy_pass for the rationale
            # (fix brief 2026-07-17 #1: open-vs-qualified must reconcile over
            # ONE window, so qualification is journaled when the desk acts).
            state.journal("options_gate_qualified", side="calls",
                          symbols=[sym for sym, _ in ranked], n=len(ranked))
        # Silence must explain itself: an empty calls book on a bullish day
        # reads as a malfunction unless the desk states WHY nothing qualified
        # (the bar is deliberately the firm's highest: STRONG_BUY + options-
        # suitable + conviction >= min). Journaled once per cycle.
        if not ranked:
            # Per-leg census against the LIVE gate (fix brief 2026-07-21 #6:
            # the old line counted the raw judge flag against the STATIC 72
            # bar — on 07-08/07-14 it printed "qualifying_high_conv_buys: 4/21"
            # while ranked was empty, because those were degraded-fallback
            # conviction-90 verdicts the real gate rightly refuses. A silence
            # explainer that reports a DIFFERENT gate than the one that ran
            # sends every auditor — human, IG, and Claude — down a false trail.)
            pairs = [(sym, d) for sym, d in verdicts.items()
                     if d.get("kind") == "stock"]
            stocks = [d["verdict"] for _, d in pairs]
            _buy_bar = _options_conviction_min("calls")
            killed = {"rating_or_degraded": 0, "below_conviction_bar": 0,
                      "judge_veto": 0, "mechanical_screen": 0}
            hb = 0
            # per_name added 2026-07-28 alongside the puts census: the counts
            # say HOW MANY died at each leg, never WHICH name or why — and for
            # the last leg "mechanical_screen" was not even actionable.
            _why_by_sym: dict[str, str] = {}
            for sym, d in pairs:
                v = d["verdict"]
                if (v.get("degraded")
                        or v.get("rating") not in ("BUY", "STRONG_BUY")):
                    killed["rating_or_degraded"] += 1
                    continue
                conv = v.get("conviction") or 0
                bar = (cfg["min_conviction"] if v.get("rating") == "STRONG_BUY"
                       else _buy_bar)
                if conv < bar:
                    killed["below_conviction_bar"] += 1
                    _why_by_sym[sym] = f"conviction {conv} < {bar}"
                    continue
                hb += 1                      # cleared rating + LIVE conviction bar
                if (not v.get("suitable_for_calls")
                        and _judge_options_veto(v, "calls")):
                    killed["judge_veto"] += 1
                    _why_by_sym[sym] = "judge options_veto_calls (reason stated)"
                else:
                    killed["mechanical_screen"] += 1   # ranked empty => mech said no
                    _why_by_sym[sym] = (_mech_options_why(sym, d.get("packet"))
                                        or "GATE/CENSUS DRIFT: every census "
                                           "leg passed yet the gate refused")
            convs = [v.get("conviction") or 0 for v in stocks]
            state.journal(
                "calls_no_candidates",
                stocks_judged=len(stocks),
                strong_buys=sum(1 for v in stocks
                                if v.get("rating") == "STRONG_BUY"),
                qualifying_high_conv_buys=hb,
                killed_by=killed,
                per_name=dict(sorted(_why_by_sym.items())[:12]),
                max_conviction=max(convs) if convs else None,
                bar=f"STRONG_BUY (conv>={cfg['min_conviction']}) OR "
                    f"BUY (conv>={_buy_bar}, BUY-family adaptive p90)")
        spots = broker.equity_prices([sym for sym, _ in ranked]) if ranked else {}
        for sym, d in ranked:
            v = d["verdict"]
            # The SPY parking spot never consumes a contract slot (same rule as
            # alpha's SPY parking)
            _real_calls = sum(1 for p in s["positions"].values()
                              if not p.get("cash_equitization"))
            if _real_calls >= cfg["max_positions"]:
                # Journal the stop (operator 2026-07-25): an unjournaled skip has
                # no block record, so routing.record_routing reads the name as a
                # SILENT DROP — a real reason misreported as an unexplained one.
                state.journal("calls_skipped_book_full", symbol=sym,
                              held=_real_calls, cap=cfg["max_positions"])
                break
            if any(p.get("underlying") == sym for p in s["positions"].values()):
                state.journal("calls_skipped_already_held", symbol=sym)
                continue
            _cd = _options_reentry_blocked(s, sym)
            if not _cd:
                # see the puts pass: the desk must honour the EQUITY sleeve's
                # post-stop cool-off too (2026-09-02 full audit).
                _eb, _ewhy = recent_stop_block(sym, spots.get(sym, 0))
                if _eb:
                    _cd = f"equity sleeve stop-out: {_ewhy}"
            if _cd:
                state.journal("calls_skipped_reentry_cooldown", symbol=sym, why=_cd)
                continue
            # Leveraged book: a stale thesis price hurts 5-10x more through
            # the option delta — drift-guard the underlying before paying IV
            spot = spots.get(sym, 0)
            if spot > 0 and not _entry_drift_ok(sym, spot, d.get("packet") or {},
                                                "calls_only"):
                continue
            if not _earnings_ok(sym, d.get("packet") or {}, "calls_only"):
                continue
            contract = broker.pick_call(sym, cfg["otm_pct"], cfg["min_dte"], cfg["max_dte"])
            if not contract:
                # The firm's TOP signal (STRONG_BUY+suitable+conviction) found no
                # liquid contract in the DTE window — explain the silence (the desk's
                # own rule: "silence must explain itself"), don't vanish it.
                state.journal("calls_skipped_no_contract", symbol=sym,
                              reason="no liquid contract in DTE window")
                continue
            # A5 (2026-06-14 audit): NEVER hold a long call THROUGH an earnings
            # print. Earnings is the largest scheduled overnight gap for an option,
            # and the SL cannot route after hours — a bad print opens the next day
            # already through the stop, worthless. The 1-day _earnings_ok backstop
            # isn't enough for the leveraged book: skip if the underlying reports
            # before this contract expires (with a small buffer).
            _ne_days = ((d.get("packet") or {}).get("next_earnings") or {}).get("days_away")
            _exp_days = _days_until(contract.get("expiration"))
            # Skip when the print lands within the holding window (now .. expiry):
            # the call would be held THROUGH earnings. A call expiring BEFORE the
            # print is fine (no gap exposure) — so the bound is expiry, not +buffer.
            if (isinstance(_ne_days, int) and 0 <= _ne_days <= (_exp_days if _exp_days is not None else 10**9)):
                state.journal("calls_skipped_earnings_before_expiry", symbol=sym,
                              earnings_in=_ne_days, expiry_in=_exp_days)
                continue
            entry = contract["mid"]
            # Gap-risk-aware sizing: high-vol underlyings gap overnight past
            # any stop — size down so a worst-case gap is survivable
            uvol = (d.get("packet", {}).get("technicals") or {}).get("vol_ann_pct")
            gap_scale = max(0.4, min(1.0, 60.0 / uvol)) if isinstance(
                uvol, (int, float)) and uvol > 60 else 1.0
            _desired = (equity * cfg["max_position_pct"] / 100
                        * risk.risk_scale(s) * gap_scale)
            # MINIMUM-VIABLE CONTRACT (operator 2026-07-22: "there is capital,
            # it's just parked and can be sold for the call — make the call
            # work"). gap_scale is a MULTI-contract sizing-DOWN control for
            # volatile underlyings; it must NEVER zero out the firm's top signal
            # when ONE contract still fits the gap-scale-free hard per-position
            # cap. Before this the desk deadlocked: idle calls capital was
            # equitized into the parking ETF, then every high-vol top signal
            # gap-scaled to a _desired BELOW one contract AND below the tiny cash
            # residual — so `short<=0`, the parking was never freed, and the name
            # skipped size_zero. The sleeve sat at ~zero opens for the fund's
            # life while ~$23k sat parked in RSP. Floor the funding target at one
            # contract when one contract fits the hard cap; the overnight-premium
            # and firm-name caps below still bound the actual position.
            _one_contract = entry * 100
            _hard_cap = equity * cfg["max_position_pct"] / 100
            # AFFORDABILITY STRETCH (operator 2026-07-25: "MU was supposed to go
            # in as an option trade too like HPE but didn't"). MU qualified twice
            # and died on size_zero both times: one 2%-OTM contract cost $5,565
            # against a $2,502 hard cap. Because that cap is a DOLLAR figure and a
            # contract is 100x the premium, the desk silently excluded EVERY
            # underlying above roughly $420-625/share at any conviction — a whole
            # price class ruled out by arithmetic, declared nowhere, re-skipped
            # every cycle forever.
            # Raising max_position_pct was NOT the fix chosen here: config cut
            # it 25%->10% precisely because "one worthless expiry blew straight
            # through the drawdown halt in a single trade". Instead try a
            # cheaper strike a bounded distance further OTM. (The operator DID
            # subsequently raise the cap 10%->15% on 2026-07-28 to unblock the
            # excluded price class; the stretch below stays the FIRST resort,
            # because it fits a contract without enlarging premium at risk.
            # At 15% the cap now sits exactly AT kill_dd_pct — see config.)
            # The bound matters — stretching far
            # enough to fit any premium would quietly turn the firm's top signal
            # into a lottery ticket, so if the capped stretch still does not fit,
            # the name is REFUSED with the ceiling stated plainly.
            if _one_contract > _hard_cap:
                _otm = float(cfg["otm_pct"])
                _stretch_max = float(getattr(config, "CALLS_MAX_OTM_STRETCH_PCT", 6.0))
                while _otm < _stretch_max and _one_contract > _hard_cap:
                    _otm = min(_stretch_max, _otm + 2.0)
                    _alt = broker.pick_call(sym, _otm, cfg["min_dte"], cfg["max_dte"])
                    if not _alt or _alt["mid"] * 100 >= _one_contract:
                        break            # no cheaper liquid strike out there
                    contract, entry = _alt, _alt["mid"]
                    _one_contract = entry * 100
                if _one_contract <= _hard_cap:
                    state.journal("calls_otm_stretched", symbol=sym,
                                  otm_pct=_otm, mid=entry,
                                  strike=contract.get("strike"),
                                  one_contract=round(_one_contract, 2),
                                  hard_cap=round(_hard_cap, 2))
            _fund_target = (max(_desired, _one_contract)
                            if _one_contract <= _hard_cap else _desired)
            # Premium outranks parking (goal audit fix 3): reclaim cash from the
            # parking ETF before sizing, exactly like alpha's conviction buys —
            # the dry powder is IN the parking now, not in raw cash. Freeing to
            # _fund_target (not _desired) is what lets a valid single-contract
            # entry actually pull the parked capital it needs.
            _park = allocation.parking_symbol("calls_only")
            if _park and (s["positions"].get(_park) or {}).get("cash_equitization"):
                _free_cash_from_equitization(
                    s, _fund_target, broker.equity_prices([_park]), mode)
            budget = min(_fund_target, s["cash"] - config.MIN_CASH_BUFFER)
            # Size against the ESTIMATED PAPER FILL, not the raw mid
            # (2026-08-13 IG audit, PEB defect) — see _est_option_fill_cost.
            _fill_cost = _est_option_fill_cost(entry, contract.get("spread_pct"))
            contracts = int(budget / _fill_cost) if _fill_cost > 0 else 0
            if contracts < 1:
                state.journal(
                    "calls_skipped_size_zero", symbol=sym,
                    reason=(f"one contract (${_one_contract:,.0f}) exceeds the "
                            f"hard per-position cap ({cfg['max_position_pct']:.0f}%"
                            f" = ${_hard_cap:,.0f}) even after stretching to "
                            f"{getattr(config, 'CALLS_MAX_OTM_STRETCH_PCT', 6.0):.0f}% OTM — "
                            f"at this sleeve size the desk cannot hold a contract "
                            f"priced above ${_hard_cap / 100:,.2f}/share of premium"
                            if _one_contract > _hard_cap
                            else "insufficient capital for one contract even "
                                 "after freeing parked capital"),
                    one_contract=round(_one_contract, 2),
                    hard_cap=round(_hard_cap, 2),
                    premium_ceiling_per_share=round(_hard_cap / 100, 2))
                continue
            # A5: cap TOTAL open call premium held OVERNIGHT as a % of sleeve equity
            # — a long call can gap to ~zero overnight, so the whole leveraged book's
            # worst-case overnight loss must stay survivable. Trim to fit; skip if no
            # room. (Per-position max_position_pct doesn't bound the AGGREGATE.)
            _open_prem = sum(p.get("market_value") or p.get("cost") or 0
                             for p in s["positions"].values() if p["instrument"] == "option")
            _prem_cap = equity * config.MAX_OVERNIGHT_CALL_PREMIUM_PCT / 100
            if _open_prem + contracts * entry * 100 > _prem_cap:
                _room = _prem_cap - _open_prem
                contracts = int(_room / (entry * 100)) if _room > 0 else 0
                if contracts < 1:
                    state.journal("calls_skipped_overnight_premium_cap", symbol=sym,
                                  open_premium=round(_open_prem, 0), cap=round(_prem_cap, 0))
                    continue
            ok4, why4 = risk.check_firm_name_cap(sym, contracts * entry * 100)
            if not ok4:
                state.journal("buy_blocked_firm_cap", sleeve="calls_only",
                              symbol=sym, reason=why4)
                continue
            _csec = ((d.get("packet") or {}).get("fundamentals") or {}).get("sector", "")
            ok5, why5 = risk.check_firm_sector_cap(sym, _csec, contracts * entry * 100)
            if not ok5:
                state.journal("buy_blocked_firm_sector", sleeve="calls_only",
                              symbol=sym, reason=why5)
                continue
            # Opus exit strategist designs the TP/SL ladder for THIS trade.
            # Consume the ladder warmed OUTSIDE the lock (run_calls_only pre-pass);
            # only a cache miss (market moved -> different contract) falls back to
            # an inline Opus call, so the lock is essentially never held across it.
            tech = (d.get("packet") or {}).get("technicals", {})
            ladder = (ladder_cache.get(contract["osi"])
                      or exits.plan_option_exits(contract, v, tech))
            # SL must clear the bid/ask spread: a stop tighter than the spread
            # fires on quote noise before the trade can breathe
            spread = contract.get("spread_pct")
            if isinstance(spread, (int, float)) and spread > 0:
                ladder["sl_pct"] = round(min(15.0, max(ladder["sl_pct"],
                                                       spread / 2 + 1.5)), 1)
            state.journal("exit_plan", sleeve="calls_only", symbol=contract["osi"],
                          source=ladder.get("source"), ladder=ladder)
            # Re-quote after the blocking Opus ladder call (2026-06-10 audit:
            # the picked mid is minutes stale by now; through option delta a
            # small underlying move is a big premium move). Re-size on the
            # fresh mid; bail if the contract ran >25% past the analyzed price.
            fresh = broker.option_quote_public(contract["osi"])
            if fresh > 0:
                if entry > 0 and (fresh / entry - 1) * 100 > 25:
                    state.journal("buy_skipped_premium_drift", sleeve="calls_only",
                                  symbol=contract["osi"], picked_mid=entry,
                                  fresh_mid=fresh)
                    continue
                entry = fresh
                # requote re-size on the estimated fill (2026-08-13 IG audit,
                # PEB defect); premium-cap leg stays on raw premium notional.
                _fill_cost = _est_option_fill_cost(entry,
                                                   contract.get("spread_pct"))
                _by_budget = int(budget / _fill_cost) if _fill_cost > 0 else 0
                # Re-apply the A5 overnight-premium cap on the FRESH size: a lower
                # fresh mid recomputes a LARGER contract count that would blow past
                # the cap the pre-Opus trim enforced (change-audit verify 2026-06-14).
                _room2 = _prem_cap - _open_prem
                _by_room = int(_room2 / (entry * 100)) if _room2 > 0 else 0
                contracts = min(_by_budget, _by_room)
                if contracts < 1:
                    # Name the binding constraint — see the puts mirror
                    # (operator 2026-07-28): cash and the premium cap both
                    # land here and only one of them is ever the reason.
                    if _by_budget < 1:
                        state.journal(
                            "calls_skipped_budget_after_requote", symbol=sym,
                            picked_mid=round(contract["mid"], 2),
                            fresh_mid=round(entry, 2),
                            one_contract=round(entry * 100, 2),
                            budget=round(budget, 2),
                            reason=("the re-quote moved the premium above the "
                                    "cash this sleeve could free for one contract"))
                        continue
                    state.journal("calls_skipped_overnight_premium_cap", symbol=sym,
                                  open_premium=round(_open_prem, 0), cap=round(_prem_cap, 0))
                    continue
                # Firm name/sector caps too — contracts may have grown vs the earlier check.
                _ok4b, _w4b = risk.check_firm_name_cap(sym, contracts * entry * 100)
                _ok5b, _w5b = risk.check_firm_sector_cap(sym, _csec, contracts * entry * 100)
                if not (_ok4b and _ok5b):
                    state.journal("buy_blocked_firm_cap_requote", sleeve="calls_only",
                                  symbol=sym, reason=(_w4b or _w5b))
                    continue
            meta = {
                "underlying": sym, "expiration": contract["expiration"],
                "strike": contract["strike"], "contract_type": "CALL",
                "open_close": "open", "conviction": v.get("conviction"),
                "rating": v.get("rating"),
                # GICS sector so the firm-wide sector cap (and riskbrain sector
                # X-ray) actually count the calls book (change-audit verify 2026-06-14)
                "sector": _csec,
                "depth": state.get_settings().get("depth", ""),
                "thesis": v.get("thesis", ""),
                # entry-time attribution the equity sleeves already carry but
                # the calls meta lacked (2026-07-01 goal audit, fix 4b): without
                # these, a closed call can never be factor/desk-attributed and a
                # degraded-verdict call is indistinguishable from a real one
                "degraded": bool(v.get("degraded")),
                "factors": ((verdicts.get(sym) or {}).get("packet") or {}).get("factor_scores") or {},
                "sector_tailwind": _tailwind(_csec),
                "desk_scores": _entry_desk_scores(verdicts.get(sym) or {}),
                # spread at entry drives honest paper-fill haircuts on exits
                "spread_pct": contract.get("spread_pct"),
                # layered scaled-exit ladder state (per-trade, Opus-designed)
                "contracts_initial": contracts, "tp1_sold": False,
                "tp2_sold": False, "tp3_hit": False,
                "trail_pct": None, "ladder": ladder,
            }
            _res = broker.execute(mode, s, symbol=contract["osi"], side="buy",
                                  quantity=contracts, price=entry,
                                  instrument="option", meta=meta)
            # (2026-08-13 IG audit) log a refused open — see the puts mirror.
            if not (_res or {}).get("success"):
                state.journal("options_open_failed", sleeve="calls_only",
                              symbol=contract["osi"],
                              error=str((_res or {}).get("error"))[:150])
        # PUTS pass of the combined desk (2026-07-07): after calls take the dollars
        # they asked for, the bearish side buys puts on the top SELL/STRONG_SELL
        # names from the SAME remaining capital + shared caps.
        _puts_buy_pass(verdicts, s, cfg, equity, mode, ladder_cache)
        # After premium got every dollar it asked for, park the residue in
        # SPY (operator 2026-07-02: T-bill cash isn't readily freeable for the
        # next call on a live account) so idle dry powder stays in ONE liquid,
        # instantly-sellable instrument instead of $0. De-risking states skip
        # inside; the residue is reclaimed before the next premium sizing.
        _equitize_idle_cash(s, {}, mode)
    else:
        state.journal("entries_blocked", sleeve="calls_only", reason=why)

    equity = broker.sleeve_equity(s, broker.fetch_marks(s), from_fetch_marks=True)
    risk.mark_equity("calls_only", s, equity, broker.sleeve_risk_equity(s, equity))
    state.save_sleeve("calls_only", s)


# ── Sleeve 4: ETF Rotation ────────────────────────────────────────────────────

def _rotation_due(last_dt, days_since: int, rotation_days: int, now) -> bool:
    """Is the ETF sleeve due to rebalance? CALENDAR-MONTH anchored (operator
    2026-07-02: a rolling 28-day interval anchored to the last rotation DRIFTED
    off the calendar — a Jun 9 rotation set the next due date to Jul 7, so the
    NEW MONTH never triggered a re-test and the sleeve froze holding June's
    picks into a July rotation, giving back its return). Real dual-momentum
    funds rebalance at the month boundary. Due when: no valid last timestamp, OR
    the calendar month changed since the last rotation (a new month = a fresh
    ranking), OR rotation_days elapsed (max-staleness backstop). Fires once per
    month — after rotating, last_rotation moves into the current month so the
    month-change test is False until the next month rolls over."""
    if last_dt is None:
        return True
    if (now.year, now.month) != (last_dt.year, last_dt.month):
        return True
    return days_since >= rotation_days


def _rotation_corr_matrix(symbols: list[str]) -> dict[str, dict[str, float]]:
    """Pairwise 60-day DAILY-return correlations between rotation candidates.

    fix brief 2026-07-27 #8: MAX_BOOK_CORRELATION was enforced only on the
    discretionary book (risk.check_correlation), never inside this sleeve — so
    dual momentum, which by construction ranks whatever factor is running,
    handed the desk SMH + XLK + IWM (one tech-beta bet) and tripped the binding
    daily breaker on 5 separate days. Deliberately reuses the SAME mechanism
    risk.check_correlation uses (data_engine.fetch_history -> pct_change ->
    .corr()) so the sleeve gate and the book gate can never disagree about what
    "correlated" means. Returns {} on ANY data failure: the caller then FAILS
    OPEN, because a vendor outage must never stop a mechanical rotation (same
    posture as risk.check_correlation's journaled fail-open)."""
    try:
        closes = data_engine.fetch_history(sorted(set(symbols)), period="6mo")
        rets = closes.pct_change().dropna().tail(60)
        if len(rets) < 20:          # not enough bars to call anything correlated
            state.journal("rotation_corr_check_failed_open", sleeve="etf_rotation",
                          error=f"only {len(rets)} return rows for {len(symbols)} names")
            return {}
        out: dict[str, dict[str, float]] = {}
        cols = [t for t in symbols if t in rets.columns]
        for a in cols:
            row: dict[str, float] = {}
            for b in cols:
                if b == a:
                    continue
                c = float(rets[a].corr(rets[b]))
                if c == c:          # NaN guard (a constant/short series)
                    row[b] = c
            out[a] = row
        return out
    except Exception as e:
        # Journal loudly: a SILENT fail-open turns the gate into theater, which
        # is exactly the failure this brief was filed about.
        state.journal("rotation_corr_check_failed_open", sleeve="etf_rotation",
                      error=str(e)[:120])
        return {}


def _rotation_targets(sig: dict, top_n: int) -> list[str]:
    """Top-N dual-momentum winners, PAIRWISE-CORRELATION GATED (fix brief
    2026-07-27 #8). Walk the ranked list in order; reject a candidate whose
    60-day daily-return correlation to an ALREADY-SELECTED target exceeds
    MAX_BOOK_CORRELATION and take the NEXT-ranked name instead, so the sleeve
    still holds top_n positions but they are top_n distinct bets. Absolute
    momentum (score > cash hurdle) still gates eligibility first — the
    correlation cap can only re-order WITHIN the names momentum already
    approved, never smuggle a decaying ETF into the book."""
    hurdle = sig.get("cash_hurdle_3m", 0)
    eligible = [t for t, sc in sig.get("ranked", []) if sc > hurdle]
    if not eligible:
        return []
    cap = float(getattr(config, "MAX_BOOK_CORRELATION", 0.7))
    corr = _rotation_corr_matrix(eligible)
    picked: list[str] = []
    for cand in eligible:
        if len(picked) >= top_n:
            break
        # Worst (highest) correlation against anything already selected — the
        # gate is pairwise, not average: SMH's 0.93 to XLK must not be diluted
        # by a low reading against a third, uncorrelated holding.
        worst_sym, worst_c = "", 0.0
        for held in picked:
            c = (corr.get(cand) or {}).get(held)
            if c is not None and c > worst_c:
                worst_sym, worst_c = held, c
        if worst_sym and worst_c > cap:
            state.journal("rotation_corr_rejected", sleeve="etf_rotation",
                          symbol=cand, correlated_with=worst_sym,
                          corr=round(worst_c, 3), cap=cap,
                          note="pairwise 60d return correlation over the cap — "
                               "next-ranked ETF taken instead")
            continue
        picked.append(cand)
    return picked


def _rotation_signal_key(targets: list[str]) -> tuple[str, str]:
    """(hash of the ranked-target list, ET day) — the identity of one applied
    rotation signal (fix brief 2026-07-27 #8)."""
    try:
        from firm import market_hours as _mh
        et_day = datetime.now(_mh.ET).date().isoformat()
    except Exception:
        et_day = datetime.now(timezone.utc).date().isoformat()
    return hashlib.sha1("|".join(targets).encode("utf-8")).hexdigest()[:12], et_day


def _stamp_rotation_signal(s: dict, targets: list[str]) -> None:
    """Record an APPLIED signal. Called only where the rotation clock advances,
    never on the starved/deferred path — a deferred rotation MUST be free to
    retry its outstanding buy legs on the next cycle of the same ET day, so
    marking it 'already applied' would re-create the LIVE cash-settlement
    strand the defer path exists to fix."""
    h, et_day = _rotation_signal_key(targets)
    s["last_rotation_signal"] = {"hash": h, "et_day": et_day,
                                 "ts": datetime.now(timezone.utc).isoformat(),
                                 "targets": list(targets)}


def _rotation_signal_duplicate(s: dict, targets: list[str]) -> bool:
    """Idempotency for rotation SIGNAL APPLICATION (fix brief 2026-07-27 #8).

    The 2026-07-06 05:15 and 14:35 runs produced identical ranks and an
    identical target set 9 hours apart, and the sleeve applied both — a second
    application can only churn (sell/rebuy the same names, pay the spread
    twice, and reset the rotation clock on stale ranks). Dedupe by hashing the
    ranked-target list: an identical set inside the SAME ET DAY is a no-op.
    The ET day (not UTC) is the unit because the ranking keys off completed ET
    session bars, and it is a DAY (not "forever") so a genuine month-boundary
    rotation onto the same leaders still executes. The mark lives in SLEEVE
    STATE (written by _stamp_rotation_signal on clock advance), so the de-dupe
    survives a process restart rather than only an in-process session."""
    h, et_day = _rotation_signal_key(targets)
    last = s.get("last_rotation_signal") or {}
    if last.get("hash") == h and last.get("et_day") == et_day:
        state.journal("rotation_signal_duplicate", sleeve="etf_rotation",
                      targets=targets, signal_hash=h, et_day=et_day,
                      first_applied=last.get("ts"),
                      note="identical ranked-target set already applied today — "
                           "no-op (duplicate signal generation)")
        return True
    return False


def run_etf_rotation(mode: str) -> None:
    with state.SLEEVE_LOCK:
        _run_etf_rotation(mode)


def _run_etf_rotation(mode: str) -> None:
    cfg = config.SLEEVES["etf_rotation"]
    s = state.get_sleeve("etf_rotation")
    prices = broker.equity_prices(
        list(s["positions"].keys()) + config.ROTATION_UNIVERSE + [config.CASH_ETF])
    equity = broker.sleeve_equity(s, prices)

    last = s.get("last_rotation", "")
    days_since = 9999
    _last_dt = None
    if last:
        try:
            _last_dt = datetime.fromisoformat(last)
            days_since = (datetime.now(timezone.utc) - _last_dt).days
            if days_since < 0:        # clock anomaly -> treat as due
                days_since = 9999
                _last_dt = None
        except (ValueError, TypeError):
            days_since = 9999
    _due = _rotation_due(_last_dt, days_since, cfg["rotation_days"],
                         datetime.now(timezone.utc))
    # The breaker gate was dead config until 2026-06-10 (week-sim finding):
    # rotation never consulted entries_allowed. A tripped breaker DEFERS the
    # whole rotation to the next cycle — selling winners and then being
    # unable to buy targets would park the sleeve in cash for 28 days.
    rot_ok, rot_why = risk.entries_allowed("etf_rotation", s, equity)

    # A6 (2026-06-14 audit): intra-cycle ABSOLUTE-MOMENTUM break. Dual momentum's
    # cash filter only ran every rotation_days (28) — between rotations a held ETF
    # could break down and give back ~20% before the monthly clock or the -20% hard
    # stop reacted. Apply the SAME cash filter weekly: a held name whose absolute 3m
    # momentum has fallen to/below the cash hurdle is rotated to cash NOW. This is
    # NOT a trailing stop (which would churn: sell-on-dip then rebuy next rotation) —
    # it is dual momentum's own validated exit-to-cash rule, just applied each cycle.
    # Only BETWEEN full rotations (the rotation block below handles it when due).
    # This is a PROTECTIVE EXIT, so it is NOT gated on the halt flag — it routes
    # even when the sleeve is halted, exactly like the per-name hard/trailing stops
    # in monitor.py (a halt blocks NEW entries, never exits). Gating it on
    # not-halted contradicted that and left a halted rotation sleeve — the one with
    # NO committee fallback — unable to exit a breaking-down ETF until the -20%
    # hard stop, the slow backstop this check was added to front-run (change-audit
    # verify 2026-06-15).
    # The FULL rotation executes when DUE and not halted. It is a validated
    # mechanical REBALANCE, not a discretionary entry, so it is NOT gated on the
    # daily LOSS breaker (config.ROTATION_IGNORES_LOSS_BREAKER, default True): it
    # never raises gross exposure (fully-invested -> fully-invested, or to BIL
    # cash) and carries its OWN risk controls (the abs-momentum -> BIL cash
    # filter + the -20% hard stop). Gating it on the breaker stranded the sleeve
    # in the exact decayed ETFs it was due to rotate OUT of, and — now that "due"
    # is CALENDAR-anchored — a breaker tripping on each single daily cycle froze
    # it for the WHOLE month (operator 2026-07-02: June's SMH/XLK/EEM held into
    # July, last_rotation stuck at Jun 9, "rotation deferred" logged every
    # cycle). A hard sleeve HALT (corruption / kill switch) still blocks the buy
    # leg; the intra-cycle protective abs-momentum exit below is the safety net
    # for every OTHER cycle (between rotations, OR due-but-halted) and routes
    # even when halted, exactly like a stop. Set the flag False to restore the
    # legacy "defer the whole rotation when the breaker is tripped" behavior.
    # The override bypasses ONLY the PER-SLEEVE daily loss breaker (the frozen-
    # rotation bug). A true FIRM-WIDE crash brake is STILL honored: on a
    # correlated crash day that breaches FIRM_DAILY_LOSS_BREAKER_PCT the DUE
    # rotation defers — the protective abs-momentum exits above keep routing (a
    # decaying ETF is still sold to BIL), only the risk-on rebalance-INTO-new-
    # leaders waits for the firm-wide all-stop to clear. `rot_ok` folds both
    # breakers together, so the firm-wide check is subtracted back out explicitly.
    _breaker_override = getattr(config, "ROTATION_IGNORES_LOSS_BREAKER", True)
    _firm_ok, _ = risk.firm_daily_breaker()
    # (2026-09-02 full audit, risk-and-sizing-1) `rot_ok` is entries_allowed(),
    # which now also carries the exit-protection gate — and `or
    # _breaker_override` discarded the WHOLE verdict, not just the daily-loss
    # breaker the override was written for (the comment above says so in as
    # many words). So a due rotation could open brand-new positions while no
    # exit sweep had run for 10+ minutes. The override keeps bypassing the
    # per-sleeve breaker; it must never bypass a stop-routing outage.
    _exit_gap = risk.exit_protection_gap("etf_rotation")
    if _exit_gap and _due and not s.get("halted"):
        state.journal("entries_blocked_exit_protection_down",
                      sleeve="etf_rotation", reason=_exit_gap[:120])
    _rotation_runs = (_due and not s.get("halted") and _firm_ok
                      and not _exit_gap
                      and (rot_ok or _breaker_override))
    if not _rotation_runs and s["positions"]:
        try:
            _sig = data_engine.rotation_signals()
            _scores = dict(_sig["ranked"])
            _hurdle = _sig["cash_hurdle_3m"]
            for sym in list(s["positions"].keys()):
                if sym == config.CASH_ETF:
                    continue
                _sc = _scores.get(sym)
                if not isinstance(_sc, (int, float)):
                    # data-coverage loss on a HELD name -> it loses its only intra-
                    # cycle exit; surface it instead of silently passing over.
                    state.journal("rotation_abs_no_score", symbol=sym)
                    continue
                if _sc <= _hurdle:
                    _px = prices.get(sym, 0)
                    if _px > 0:
                        broker.execute(mode, s, symbol=sym, side="sell",
                                       quantity=s["positions"][sym]["quantity"], price=_px,
                                       meta={"reason": f"abs-momentum break to cash "
                                             f"(3m mom {_sc} <= hurdle {_hurdle})"})
                        state.journal("rotation_abs_momentum_exit", symbol=sym,
                                      mom=_sc, hurdle=_hurdle)
            equity = broker.sleeve_equity(s, prices)
        except Exception as e:
            state.journal("rotation_abs_check_error", error=str(e)[:120])

    if _rotation_runs and not rot_ok:
        # A DUE rebalance ran THROUGH a tripped loss breaker — journal the
        # override so the decision is auditable, never silent.
        state.journal("rotation_breaker_override", sleeve="etf_rotation",
                      reason=f"due rebalance ran despite: {rot_why}")
    elif _due and not s.get("halted") and not rot_ok:
        # Legacy path (flag off): the whole rotation defers to the next cycle.
        state.journal("entries_blocked", sleeve="etf_rotation",
                      reason=f"rotation deferred: {rot_why}")
    if _rotation_runs:
        sig = data_engine.rotation_signals()
        scores = dict(sig["ranked"])
        # fix brief 2026-07-27 #8: selection used to be a plain top-N slice, so
        # the sleeve could (and did) buy three names that are one bet. The gate
        # walks further down the ranked list to fill the same top_n seats with
        # UNCORRELATED leaders; on a data outage it returns the momentum order
        # unchanged (fail open, journaled).
        winners = _rotation_targets(sig, cfg["top_n"])
        targets = winners if winners else [config.CASH_ETF]
        # Duplicate-signal guard runs AFTER selection so the hash covers what
        # would actually be TRADED (post-correlation-gate), not the raw ranks.
        if _rotation_signal_duplicate(s, targets):
            _rotation_runs = False
    if _rotation_runs:
        state.journal("rotation_signal", sleeve="etf_rotation",
                      targets=targets, ranked=sig["ranked"][:6])

        # Rotation decisions feed the firm's memory: every signal snapshot is
        # kept so the Opus review can judge whether dual momentum is working
        hist = state.load("rotation_history", {"events": []})
        equity_now = broker.sleeve_equity(s, prices)
        hist["events"] = (hist.get("events", []) + [{
            "ts": datetime.now(timezone.utc).isoformat(),
            "targets": targets, "ranked_top6": sig["ranked"][:6],
            "cash_hurdle_3m": sig["cash_hurdle_3m"],
            "went_to_cash": targets == [config.CASH_ETF],
            "sleeve_equity": equity_now,
        }])[-50:]
        state.save("rotation_history", hist)

        for sym in list(s["positions"].keys()):
            if sym not in targets:
                px = prices.get(sym, 0)
                if px > 0:
                    broker.execute(mode, s, symbol=sym, side="sell",
                                   quantity=s["positions"][sym]["quantity"],
                                   price=px,
                                   meta={"reason": f"rotation exit (rank fell, "
                                                   f"mom {scores.get(sym, '?')})"})
                else:
                    state.journal("sell_skipped_no_price",
                                  sleeve="etf_rotation", symbol=sym)
        equity = broker.sleeve_equity(s, prices)
        # Deploy FULLY per the dual-momentum signal (audit #7, 2026-07-02): the
        # external risk_scale sold winners at full size but bought targets at
        # HALF, stranding ~50% in idle cash until the NEXT monthly rotation —
        # worse now that rebalancing is calendar-anchored. etf_rotation is a
        # MECHANICAL sleeve whose own cash filter (rotate to CASH_ETF when
        # absolute momentum turns negative) IS its risk management; it is "never
        # overridden" (CLAUDE.md). A firm-wide drawdown is expressed by the
        # signal rotating defensive/to-cash, not by a lopsided half-deploy.
        _targets_needed = [t for t in targets if t not in s["positions"]]
        _buys_routed = 0
        per_target = equity / len(targets)
        for i, sym in enumerate(targets):
            if sym in s["positions"]:
                continue
            px = prices.get(sym, 0)
            if px <= 0:
                state.journal("buy_skipped_no_price", sleeve="etf_rotation",
                              symbol=sym)
                continue
            qty = int(min(per_target, s["cash"] - config.MIN_CASH_BUFFER) / px)
            if qty >= 1:
                _r = broker.execute(mode, s, symbol=sym, side="buy", quantity=qty,
                                    price=px,
                                    meta={"reason": "rotation entry",
                                          "strategy": "dual_momentum",
                                          "momentum_score": scores.get(sym),
                                          "rank": i + 1})
                if _r.get("success"):
                    _buys_routed += 1
            else:
                # 2026-07-25 audit (finding #3): this branch journaled NOTHING.
                # A rotation target that rounds to zero shares is the sleeve's
                # capital silently failing to deploy — the same class of silent
                # skip every other desk was forced to explain. Say it: the
                # per-target slice, the price, and the cash that produced it.
                state.journal("rotation_buy_skipped_size_zero",
                              sleeve="etf_rotation", symbol=sym, price=round(px, 4),
                              per_target=round(per_target, 2),
                              cash=round(s["cash"], 2), rank=i + 1)
        # Advance the rotation clock ONLY if EVERY intended buy leg placed (or
        # there was nothing to buy). In LIVE mode the sells placed above go
        # PENDING (cash is credited only when settle_pending books the fill on a
        # LATER sweep), so a SAME-PASS buy can be starved to qty 0 by
        # broker.execute's cash gate. Advancing last_rotation then would freeze
        # the sleeve in cash until next month — the sells settle but no buy ever
        # re-fires (the LIVE-only strand, confirmed 2026-07-02). Three cases:
        #   * ALL legs placed / nothing to buy -> DONE, advance the clock.
        #   * starved -> HOLD last_rotation and retry next cycle (LIVE proceeds
        #     settle and fund the buy; a transient no-price blip recovers). Paper
        #     normally fills in one pass, so this path is the live/outage case.
        #   * starved for ROTATION_MAX_BUY_DEFERS cycles running -> the starvation
        #     is STRUCTURAL (persistent no-price / genuinely-unaffordable target).
        #     Holding then would LIVELOCK the clock and log every cycle — the exact
        #     frozen-clock + alarm-spam symptom the calendar/breaker fixes removed.
        #     Advance the clock and alert ONCE instead of freezing.
        # 2026-07-25 audit (finding #3): the gate was `_buys_routed or not
        # _targets_needed` — ANY ONE leg placing advanced the clock and marked
        # the rotation DONE, stranding every unplaced leg's capital in cash for
        # the rest of the calendar month (the month-anchored _rotation_due only
        # re-fires when the month rolls over). A partial rotation is not a
        # rotation: the sleeve's whole edge is holding the top-N, and running
        # 2-of-3 for four weeks is an un-validated strategy. The clock now
        # advances only when EVERY intended leg was placed — the remainder is
        # retried next cycle by the existing defer path, which still escapes to
        # the clock-advance + alert after ROTATION_MAX_BUY_DEFERS so a
        # structurally unfillable leg can never livelock the sleeve.
        _defers = s.get("rotation_defer_count", 0)
        _max_defers = getattr(config, "ROTATION_MAX_BUY_DEFERS", 3)
        if not _targets_needed or _buys_routed >= len(_targets_needed):
            s["last_rotation"] = datetime.now(timezone.utc).isoformat()
            s.pop("rotation_defer_count", None)
            _stamp_rotation_signal(s, targets)   # fix brief 2026-07-27 #8
        elif _defers < _max_defers:
            s["rotation_defer_count"] = _defers + 1
            state.journal("rotation_buy_leg_deferred", sleeve="etf_rotation",
                          targets=targets, attempt=_defers + 1,
                          # partial-completion telemetry (finding #3): which
                          # legs still owe, so a 2-of-3 rotation is visible as
                          # INCOMPLETE rather than reading as a clean rotation
                          routed=_buys_routed, needed=len(_targets_needed),
                          outstanding=[t for t in _targets_needed
                                       if t not in s["positions"]],
                          note="rotation incomplete (buy leg starved / awaiting "
                               "settled cash); last_rotation held so the "
                               "remaining legs retry next cycle")
        else:
            s["last_rotation"] = datetime.now(timezone.utc).isoformat()
            s.pop("rotation_defer_count", None)
            _stamp_rotation_signal(s, targets)   # fix brief 2026-07-27 #8
            state.journal("rotation_buy_leg_unfunded", sleeve="etf_rotation",
                          targets=_targets_needed, attempts=_defers,
                          note="buy leg could not fund after retries (structural: "
                               "no price / unaffordable); advancing clock to avoid "
                               "livelock + per-cycle alarm")
            try:
                from firm import alerts
                alerts.alert(
                    "ROTATION BUY LEG UNFUNDED",
                    f"etf_rotation could not fund {_targets_needed} after "
                    f"{_defers} retries — advancing the rotation clock so the "
                    "sleeve is not frozen. Check the data feed and available cash.")
            except Exception:
                pass

    equity = broker.sleeve_equity(s, prices)
    risk.mark_equity("etf_rotation", s, equity, broker.sleeve_risk_equity(s, equity))
    state.save_sleeve("etf_rotation", s)


# ── Queued-for-the-open entries ───────────────────────────────────────────────

def _requeue_research(sym: str, note: str) -> None:
    """A queued-open intent that can't fill HONESTLY (drift/blocked/unfunded)
    goes back to the committee via the operator-request rails — a decided BUY
    is re-judged fresh, never silently dropped (operator 2026-07-16)."""
    try:
        rq = state.load("research_requests", {"queue": []}) or {}
        qq = [r for r in (rq.get("queue") or []) if isinstance(r, dict)]
        if all(r.get("ticker") != sym for r in qq):
            qq.append({"ticker": sym, "kind": "stock", "note": note,
                       "requested_at": datetime.now(timezone.utc).isoformat()})
            state.save("research_requests", {"queue": qq})
            state.journal("open_queue_requeued_research", symbol=sym, note=note)
    except Exception:
        state.journal("open_queue_requeue_failed", symbol=sym)


def flush_pending_open_entries(mode: str | None = None) -> int:
    """Execute queued-for-the-open entry intents (broker._queue_open_entry) at
    the next open — the operator's contract (2026-07-16, after four fresh BUY
    verdicts died when a cycle finished 90s past the bell): an entry the
    committee decided must never silently die on the clock. Same protections
    as any entry: the vol-scaled drift band vs the thesis price,
    risk.entries_allowed (breakers/halts), and the cash clamp with the parking
    pulled first. An intent that fails a gate is re-queued for a FRESH
    committee look. Called from the monitor sweep; serialized on SLEEVE_LOCK."""
    from firm import market_hours
    if not market_hours.market_is_open():
        return 0
    _peek = state.load("pending_open_entries", []) or []
    if not _peek:
        return 0
    mode = mode or state.get_settings().get("mode", "paper")
    # AUDIT 2026-07-16 F2/F7: quotes fetched OUTSIDE the lock (a network call
    # under SLEEVE_LOCK froze every monitor for its duration) and INCLUDE the
    # parking symbols — the reservoir pull no-ops on px=0, and without SPY in
    # the fetch every intent starved against raw cash while $16.5k sat parked.
    _syms = {e.get("symbol") for e in _peek if e.get("symbol")}
    _parks = {allocation.parking_symbol(e.get("sleeve"))
              for e in _peek if e.get("sleeve")}
    try:
        prices = broker.equity_prices(sorted(_syms | {p for p in _parks if p}))
    except Exception:
        prices = {}
    # Fleet re-grade action 8b: check_correlation's 6-month history download
    # ran UNDER the lock per intent at 9:30, stalling the 1s calls-exit loop
    # and the day's first gap-down stop sweep. Warm the data_engine history
    # cache OUTSIDE the lock with the EXACT per-intent key shapes the
    # under-lock gate will ask for (the cache is keyed by the sorted ticker
    # list), assuming earlier intents fill — the common path. A missed warm
    # costs only what every call used to cost.
    try:
        from firm import data_engine as _de
        _grow: dict[str, list] = {}
        for _e0 in _peek:
            _sl0, _sy0 = _e0.get("sleeve"), _e0.get("symbol")
            _m0 = _e0.get("meta") or {}
            if (not _sy0 or _sl0 not in config.SLEEVES
                    or _m0.get("cash_equitization") or _m0.get("seat_change")
                    or _sl0 == "etf_rotation"):     # mechanical: no correlation gate
                continue
            _sv0 = state.get_sleeve(_sl0)
            _core0 = (set(allocation.core_weights()) if _sl0 == "core_blend"
                      else set(config.SLEEVES.get(_sl0, {})
                               .get("core_weights") or {}))
            _held0 = [x for x, p in (_sv0.get("positions") or {}).items()
                      if p.get("instrument", "equity") == "equity"
                      and x not in _core0 and not p.get("cash_equitization")]
            _held0 += _grow.get(_sl0, [])
            if len(_held0) >= 2:
                _de.fetch_history(sorted(set(_held0 + [_sy0])), period="6mo")
            _grow.setdefault(_sl0, []).append(_sy0)
    except Exception:
        pass
    filled = 0
    keep: list = []
    with state.SLEEVE_LOCK:
        # Authoritative queue read INSIDE the lock (F7: pre-lock load + single
        # post-loop rewrite let concurrent sweeps double-fill).
        q = state.load("pending_open_entries", []) or []
        for i, e in enumerate(q):
            sym, sl = e.get("symbol"), e.get("sleeve")
            if not sym or sl not in config.SLEEVES:
                continue
            # STALE-INTENT TTL (fleet re-grade): a kept intent that has ridden
            # the queue for days is executing a dead thesis price — drop it
            # loudly; anything still interesting re-enters via the committee.
            try:
                _qage_d = (datetime.now(timezone.utc) - datetime.fromisoformat(
                    e.get("queued_at", ""))).total_seconds() / 86400.0
            except Exception:
                _qage_d = 0.0
            if _qage_d > float(getattr(config, "OPEN_QUEUE_TTL_DAYS", 5.0)):
                state.journal("open_queue_expired", sleeve=sl, symbol=sym,
                              age_days=round(_qage_d, 1))
                _requeue_research(sym, "queued-open intent expired (TTL)")
                continue
            # AUDIT (mode-blind): a paper-decided intent must never flush as a
            # LIVE order after a mode flip (or vice versa). Legacy intents
            # without a mode stamp are treated as current-mode.
            _imode = e.get("mode")
            if _imode and _imode != mode:
                state.journal("open_queue_dropped_mode_flip", sleeve=sl,
                              symbol=sym, intent_mode=_imode, current_mode=mode)
                continue
            # Fail-closed degraded refusal (2026-08-13 IG audit): every LIVE
            # entry path already hard-refuses a degraded_fallback verdict
            # (_ranked_buys/_verdict_ok/_eligible_add, _apply_sells, both
            # options gates, allocation.apply_sell_verdicts) — this flush was
            # the one verdict-derived order seam with NO degraded check, so a
            # stale/foreign intent stamped degraded=True could still route.
            # Advisory only, never an order.
            if (e.get("meta") or {}).get("degraded"):
                state.journal("degraded_verdict_blocked", sleeve=sl, symbol=sym,
                              path="pending_open_entry_flush")
                continue
            s = state.get_sleeve(sl)
            # SAVE-ON-MUTATION CONTRACT (2026-07-25 audit, CRITICAL
            # finding #1). Every early-exit path below used a bare
            # `continue`, so a `s` that had ALREADY placed a real broker
            # order was thrown away: the pending-order row, the
            # exit_pending lock and the cash math all vanished while the
            # order was LIVE at the broker, and the intent was kept, so
            # the next 60s sweep reloaded the unchanged file from disk and
            # placed the SAME parking sell again. Worked example: an intent
            # for 9 shares at $213.61 (~$1,922) against $300 of sleeve cash
            # issues a real 8-share RSP sell, then afford =
            # (300-100)/213.57 = 0.94 -> qty 0 -> `continue`, `s` discarded
            # -> ~390 unrecorded live orders/day up to the 5-day TTL, none
            # of which settle_pending can ever poll because no record of
            # them exists. Only the three success paths saved. PAPER hides
            # it completely (paper sells book instantly), so this could
            # only ever bite with real money.
            # The fix is structural, not per-path: `s` is persisted on
            # EVERY exit — return, continue, or exception — whenever it was
            # touched. _dirty is set immediately BEFORE each call that can
            # mutate `s` (never after: an exception mid-order must still
            # persist), and cleared by the success paths that already save.
            _dirty = False
            try:
                # AUDIT (idempotency): already held means the intent filled before
                # a crash/restart lost the queue write — never buy twice. EXCEPT
                # an add-on-strength intent (fleet re-grade action 5): an ADD is
                # BY DEFINITION already held — this guard silently voided every
                # premarket "adds size up" the committee ever decided, daily,
                # while the journal read like idempotency working.
                _pos = (s.get("positions") or {}).get(sym)
                _is_add = bool((e.get("meta") or {}).get("add_on_strength"))
                if _pos and not _pos.get("cash_equitization") and not _is_add:
                    state.journal("open_queue_already_held", sleeve=sl, symbol=sym)
                    continue
                if _is_add:
                    if not _pos or _pos.get("cash_equitization"):
                        # base position gone by the open (stopped out / sold) —
                        # there is nothing to top up; the committee re-decides.
                        state.journal("open_queue_add_base_gone", sleeve=sl,
                                      symbol=sym)
                        _requeue_research(sym, "queued add-on-strength: base "
                                               "position gone by the open")
                        continue
                    # crash-refill idempotency for adds: last_add_ts newer than
                    # the intent means this add already booked once.
                    if str(_pos.get("last_add_ts") or "") > str(e.get("queued_at")
                                                                or ""):
                        state.journal("open_queue_add_already_applied", sleeve=sl,
                                      symbol=sym)
                        continue
                # LIVE PARITY (2026-07-16): never fill a queued intent Public
                # cannot buy (fails open without a universe snapshot).
                from firm import tradable as _trd
                if not _trd.is_buyable(sym):
                    state.journal("open_queue_not_tradable", sleeve=sl, symbol=sym)
                    continue
                px = prices.get(sym) or 0
                if px <= 0:
                    keep.append(e)              # no quote yet — retry next sweep
                    continue
                thesis = e.get("thesis_price") or 0
                meta = e.get("meta") or {}
                # MECHANICAL ALLOCATION LEGS (re-grade audit 2026-07-16, found by
                # 3 independent reviewers): a queued PARKING or SEAT-CHANGE buy is
                # not a conviction entry — a 35% core slot can never pass an 18%
                # position cap, and requeue-to-research for SPY parking is
                # nonsense. These legs skip the conviction battery and the
                # research requeue entirely: fill at the open unless the price
                # moved implausibly (10% band -> retry next sweep), with only the
                # breaker/halt gate kept (entries_allowed below).
                # 2026-07-25 audit (finding #2): etf_rotation belongs in this
                # class too — EVERY leg it queues is a dual-momentum rebalance
                # entry, "never overridden by the committee" (CLAUDE.md).
                # Routed through the conviction battery it could not fill at
                # all: routing.open_slots reported book-full (fixed there too),
                # and a 1-of-top-3 leg is ~33% of the sleeve against the 18%
                # position-cap DEFAULT this sleeve falls back to (it defines
                # neither max_position_pct nor satellite_position_pct), so
                # position_size would have blocked it even with slots free —
                # and then requeued a MECHANICAL leg to the committee, which is
                # exactly the override the mandate forbids. Keyed on the SLEEVE,
                # not on the `strategy: dual_momentum` meta stamp, so a leg with
                # missing meta cannot silently fall back into the wrong class.
                _mechanical = bool(meta.get("cash_equitization")
                                   or meta.get("seat_change")
                                   or sl == "etf_rotation")
                vol = meta.get("vol_ann")
                sigma = vol / 15.9 if isinstance(vol, (int, float)) and vol > 0 else 0.0
                band = (10.0 if _mechanical
                        else max(risk.tuned_cap("MAX_ENTRY_DRIFT_PCT"), 0.75 * sigma))
                drift = abs(px / thesis - 1) * 100 if thesis else 0.0
                if _mechanical and drift > band:
                    keep.append(e)          # implausible gap — retry next sweep
                    state.journal("open_queue_mechanical_deferred", sleeve=sl,
                                  symbol=sym, drift_pct=round(drift, 2))
                    continue
                if not _mechanical and drift > band:
                    state.journal("open_queue_drift_requeued", sleeve=sl,
                                  symbol=sym, drift_pct=round(drift, 2),
                                  band_pct=round(band, 2))
                    _requeue_research(sym, f"queued-open entry drifted "
                                           f"{drift:.1f}% past the band — fresh look")
                    continue
                equity = broker.sleeve_equity(s, prices)
                ok, why = risk.entries_allowed(sl, s, equity)
                if not ok:
                    state.journal("open_queue_blocked", sleeve=sl, symbol=sym,
                                  reason=why, mechanical=_mechanical)
                    if _mechanical:
                        keep.append(e)      # allocation leg waits out the breaker
                    else:
                        _requeue_research(sym, f"queued-open entry blocked: {why}")
                    continue
                # AUDIT F4: flush is an ENTRY — re-run the gate battery the entry
                # loop would have run, against the book AS IT GROWS this flush
                # (s reloads per intent; fills save immediately below). Slots
                # (with the parked-capacity yield), sector mandate, position/
                # sector/firm caps, correlation. A gate-blocked intent goes back
                # to research — the committee re-decides, nothing silently dies.
                # Mechanical parking/seat legs skip the battery (see above): they
                # occupy no entry slot and answer to the allocation engine's own
                # rules, not the conviction caps.
                if _mechanical:
                    qty = e.get("quantity") or 0
                    afford = max(0.0, s["cash"] - config.MIN_CASH_BUFFER) / px
                    qty = min(qty, round(afford, 4) if mode == "paper" else int(afford))
                    if qty >= (0.05 if mode == "paper" else 1):
                        _dirty = True       # order about to route -> `s` is money
                        res = broker.execute(mode, s, symbol=sym, side="buy",
                                             quantity=qty, price=px, meta=meta)
                        if res.get("success"):
                            filled += 1
                            if meta.get("cash_equitization") \
                                    and sym in (s.get("positions") or {}):
                                s["positions"][sym]["cash_equitization"] = True
                            state.journal("open_queue_filled", sleeve=sl,
                                          symbol=sym, qty=qty, price=round(px, 4),
                                          mechanical=True)
                            state.save_sleeve(sl, s)
                            _dirty = False              # persisted right here
                            state.save("pending_open_entries", keep + q[i + 1:])
                        elif res.get("queued"):
                            keep.append(e)
                        else:
                            state.journal("open_queue_failed", sleeve=sl,
                                          symbol=sym, mechanical=True,
                                          error=str(res.get("error", ""))[:100])
                    else:
                        # A mechanical leg with no cash is usually a live seat
                        # flip whose SELL is still settling (fleet re-grade
                        # action 3) — dropping it left flips half-complete.
                        # Keep it; the TTL above bounds the retries.
                        keep.append(e)
                        state.journal("open_queue_insufficient_cash", sleeve=sl,
                                      symbol=sym, mechanical=True, kept=True)
                    continue
                if _is_add:
                    # ADD TOP-UP (fleet re-grade action 5): the 8:30 held cycle's
                    # "adds size up" contract executes HERE now. Adds skip the
                    # slots/sector-mandate gates (the name already occupies its
                    # slot) and run the add path's own battery: position cap on
                    # the WHOLE resulting position, sector/firm caps, ADV.
                    _add_qty = e.get("quantity") or 0
                    # The reservoir pull can place a REAL sell — from here on
                    # `s` must be persisted no matter which way this exits
                    # (the add path below `continue`s on cash and on 4 gates).
                    _dirty = True
                    _free_cash_from_equitization(s, _add_qty * px, prices, mode)
                    _aff = max(0.0, s["cash"] - config.MIN_CASH_BUFFER) / px
                    _add_qty = min(_add_qty,
                                   round(_aff, 4) if mode == "paper" else int(_aff))
                    if _add_qty < (0.05 if mode == "paper" else 1):
                        state.journal("open_queue_insufficient_cash", sleeve=sl,
                                      symbol=sym, add=True)
                        continue            # adds are best-effort, never requeued
                    _cur_val = (_pos.get("quantity") or 0) * px
                    _add_cost = _add_qty * px
                    _cfgm_a = (config.SLEEVES[sl].get("max_position_pct")
                               or config.SLEEVES[sl].get("satellite_position_pct")
                               or 18.0)
                    _sector_a = str(meta.get("sector") or "")
                    _ablk = None
                    for _gname, _gfn in (
                            ("position_size", lambda: risk.check_position_size(
                                sl, equity, _cur_val + _add_cost, _cfgm_a)),
                            ("sector_cap", lambda: risk.check_sector_cap(
                                s.get("positions") or {}, {sym: _sector_a}, sym,
                                _add_cost, equity)),
                            ("firm_name_cap", lambda: risk.check_firm_name_cap(
                                sym, _add_cost)),
                            ("firm_sector_cap", lambda: risk.check_firm_sector_cap(
                                sym, _sector_a, _add_cost))):
                        _okg, _wg = _gfn()
                        if not _okg:
                            _ablk = _gname
                            break
                    if _ablk:
                        state.journal("open_queue_add_gate_blocked", sleeve=sl,
                                      symbol=sym, gate=_ablk)
                        continue
                    res = broker.execute(mode, s, symbol=sym, side="buy",
                                         quantity=_add_qty, price=px, meta=meta)
                    if isinstance(res, dict) and res.get("success"):
                        filled += 1
                        if not res.get("pending"):
                            _pp = s["positions"].get(sym)
                            if _pp:
                                _pp["adds_count"] = _pp.get("adds_count", 0) + 1
                                _pp["last_add_ts"] = datetime.now(
                                    timezone.utc).isoformat()
                        state.journal("add_on_strength", sleeve=sl, symbol=sym,
                                      add_qty=_add_qty,
                                      add_cost=round(_add_cost, 2),
                                      conviction=meta.get("conviction"),
                                      via="open_queue_flush",
                                      pending=bool(res.get("pending")))
                        state.save_sleeve(sl, s)
                        _dirty = False                  # persisted right here
                        state.save("pending_open_entries", keep + q[i + 1:])
                    elif res.get("queued"):
                        keep.append(e)
                    else:
                        state.journal("open_queue_failed", sleeve=sl, symbol=sym,
                                      add=True,
                                      error=str(res.get("error", ""))[:100])
                    continue
                from firm import routing as _rt
                _free, _cap, _held_n = _rt.open_slots(sl)
                if _free <= 0 and not _rt.parked_capacity(sl, s, prices):
                    state.journal("open_queue_gate_blocked", sleeve=sl, symbol=sym,
                                  gate="book_full")
                    _requeue_research(sym, "queued-open entry: book full at flush")
                    continue
                _sector = str(meta.get("sector") or "")
                if not risk.sector_allowed(s, _sector):
                    state.journal("open_queue_gate_blocked", sleeve=sl, symbol=sym,
                                  gate="sector_mandate", sector=_sector)
                    _requeue_research(sym, "queued-open entry: sector not allowed")
                    continue
                # Post-stop cool-off (2026-07-26). The flush is the path that
                # actually re-bought CON/OSCR the night they were stopped, so the
                # gate matters MOST here: it re-checks against the OPEN's real
                # price, which is the first honest test of whether the name has
                # reclaimed. Requeued (not dropped) so the committee re-decides.
                _sb, _sbwhy = recent_stop_block(sym, px)
                if _sb:
                    state.journal("open_queue_gate_blocked", sleeve=sl, symbol=sym,
                                  gate="stop_cooloff", reason=_sbwhy)
                    _requeue_research(sym, f"queued-open entry: {_sbwhy}")
                    continue
                qty = e.get("quantity") or 0
                # THE ORIGINAL LEAK (finding #1): this reservoir pull places a
                # LIVE parking sell, and the five paths below it (unaffordable,
                # earnings window, ADV footprint, the 5-gate battery, a failed
                # execute) all `continue`d without saving. Mark dirty BEFORE
                # the call so even an exception inside it still persists.
                _dirty = True
                _free_cash_from_equitization(s, qty * px, prices, mode)
                afford = max(0.0, s["cash"] - config.MIN_CASH_BUFFER) / px
                qty = min(qty, int(afford) if mode != "paper" else round(afford, 4))
                if qty < (1 if mode != "paper" else 0.05):
                    # LIVE FUNDING (fleet re-grade action 3): in live mode the
                    # parking sell placed just above is PENDING — proceeds credit
                    # at settle_pending, one monitor sweep away. Starving now is
                    # a TIMING artifact, not a research question: keep the intent
                    # for the next sweep. Paper books sells instantly, so paper
                    # starvation is real -> back to the committee.
                    if mode != "paper" and any(
                            po.get("side") == "sell"
                            for po in (s.get("pending_orders") or [])):
                        keep.append(e)
                        state.journal("open_queue_waiting_settle", sleeve=sl,
                                      symbol=sym)
                    else:
                        state.journal("open_queue_insufficient_cash", sleeve=sl,
                                      symbol=sym)
                        _requeue_research(sym,
                                          "queued-open entry unaffordable at flush")
                    continue
                _cost = qty * px
                # core_blend conviction intents answer to the SATELLITE mandate,
                # not a sleeve-wide default (fleet re-grade action 9: 18.0 here
                # silently doubled core's 9% per-name risk at the flush).
                _cfgm = (config.SLEEVES[sl].get("max_position_pct")
                         or config.SLEEVES[sl].get("satellite_position_pct")
                         or 18.0)
                # Earnings + ADV re-checks (action 9): the normal entry path runs
                # both; the flush couldn't because queued meta carried neither —
                # entry sites now stamp next_earnings_date + avg_dollar_vol.
                _edays = _days_until(meta.get("next_earnings_date"))
                if isinstance(_edays, int) and 0 <= _edays <= 1:
                    state.journal("open_queue_gate_blocked", sleeve=sl, symbol=sym,
                                  gate="earnings_window", days_to_earnings=_edays)
                    _requeue_research(sym, "queued-open entry: earnings print "
                                           "inside a day")
                    continue
                _adv = meta.get("avg_dollar_vol")
                if isinstance(_adv, (int, float)) and _adv > 0 \
                        and _cost > _adv * config.MAX_POSITION_OF_ADV_PCT / 100:
                    state.journal("open_queue_gate_blocked", sleeve=sl, symbol=sym,
                                  gate="liquidity_adv", cost=round(_cost, 2),
                                  adv=_adv)
                    _requeue_research(sym, "queued-open entry: footprint too "
                                           "large vs ADV")
                    continue
                # LAZY battery (action 8b): correlation downloads 6 months of
                # history — evaluate it LAST and only when the cheap gates pass
                # (the pre-lock warm makes the usual case a cache hit).
                _gates = (
                    ("position_size", lambda: risk.check_position_size(
                        sl, equity, _cost, _cfgm)),
                    ("sector_cap", lambda: risk.check_sector_cap(
                        s.get("positions") or {}, {sym: _sector}, sym, _cost,
                        equity)),
                    ("firm_name_cap", lambda: risk.check_firm_name_cap(sym, _cost)),
                    ("firm_sector_cap", lambda: risk.check_firm_sector_cap(
                        sym, _sector, _cost)),
                    ("correlation", lambda: risk.check_correlation(s, sym)),
                )
                _blocked, _whyg = None, ""
                for _gname, _gfn in _gates:
                    _okg, _wg = _gfn()
                    if not _okg:
                        _blocked, _whyg = _gname, _wg
                        break
                if _blocked:
                    state.journal("open_queue_gate_blocked", sleeve=sl, symbol=sym,
                                  gate=_blocked, reason=str(_whyg)[:150])
                    _requeue_research(sym, f"queued-open entry gate-blocked: {_blocked}")
                    continue
                res = broker.execute(mode, s, symbol=sym, side="buy", quantity=qty,
                                     price=px, meta=meta)
                if res.get("success"):
                    filled += 1
                    state.journal("open_queue_filled", sleeve=sl, symbol=sym,
                                  qty=qty, price=round(px, 4))
                    # A quietly-shrunken fill is a decision the operator never made
                    # (AUDIT 2026-07-16): say when cash clamped >30% off the intent.
                    _intent_q = e.get("quantity") or 0
                    if _intent_q and qty < 0.7 * _intent_q:
                        state.journal("open_queue_partial_fill", sleeve=sl,
                                      symbol=sym, intended=_intent_q, filled_qty=qty,
                                      pct=round(qty / _intent_q * 100, 1))
                    state.save_sleeve(sl, s)
                    _dirty = False                      # persisted right here
                    # AUDIT (idempotency): persist the shrunken queue IMMEDIATELY —
                    # a crash between fill and end-of-loop must not re-buy.
                    state.save("pending_open_entries", keep + q[i + 1:])
                    # AUDIT F9 (learning): the acceptance channel graded queued
                    # buys as 'qualified-but-passed' counterfactuals — patch the
                    # newest routing_history row so the fund's own buy is graded
                    # as a PLACEMENT, not a pass.
                    try:
                        _hist = state.load("routing_history", {"events": []})
                        for _ev in reversed(_hist.get("events") or []):
                            _row = next((r for r in _ev.get("rows", [])
                                         if r.get("symbol") == sym), None)
                            if _row is not None:
                                if sl not in (_row.get("placed") or []):
                                    _row.setdefault("placed", []).append(sl)
                                state.save("routing_history", _hist)
                                break
                    except Exception:
                        state.journal("open_queue_routing_patch_failed", symbol=sym)
                elif res.get("queued"):
                    keep.append(e)              # market closed again — keep waiting
                else:
                    state.journal("open_queue_failed", sleeve=sl, symbol=sym,
                                  error=str(res.get("error", ""))[:100])
                    _requeue_research(sym, f"queued-open entry failed: "
                                           f"{str(res.get('error', ''))[:60]}")
            finally:
                # The single durability guarantee for this intent. A saved
                # sleeve keeps the pending row settle_pending polls and the
                # exit_pending lock broker.execute honours, so a repeat
                # sweep refuses the duplicate instead of re-placing it.
                if _dirty:
                    state.save_sleeve(sl, s)
        # Final rewrite INSIDE the lock, merged against concurrent appends
        # (fleet re-grade action 8a: the old save out past the lock was
        # last-writer-wins — it could erase a BUY a late cycle thread queued
        # mid-flush, the exact loss class this queue exists to prevent).
        _cur = state.load("pending_open_entries", []) or []
        _seen = {(x.get("sleeve"), x.get("symbol")) for x in q} \
            | {(x.get("sleeve"), x.get("symbol")) for x in keep}
        state.save("pending_open_entries",
                   keep + [x for x in _cur
                           if (x.get("sleeve"), x.get("symbol")) not in _seen])
    return filled
