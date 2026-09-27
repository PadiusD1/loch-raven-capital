"""Dynamic ETF seats — the parking and core ETFs are EARNED, not granted.

Operator mandate (2026-07-16): "auto pick the best ETFs for alpha and core...
if a better ETF comes than the one already there, switch and remove it, and
when an ETF becomes a SELL, sell it — don't just stick with SPY as a core
main one or others."

Design: the SAME validated dual-momentum engine that runs etf_rotation
(data_engine.rotation_signals — blended 3m/6m return on completed bars) ranks
two eligible universes:

  PARKING seat (alpha_stocks + calls_only cash equitization) — broad,
      deep-liquid ETFs only (config.PARKING_ELIGIBLE). One seat.
  CORE seats (core_blend's mechanical 55%) — three slots keeping the original
      35/15/5 weight structure (config.CORE_SLOT_WEIGHTS), drawn from the
      diversified CORE_ELIGIBLE universe.

Switch discipline (anti-churn hysteresis): a challenger must out-score the
incumbent by ETF_SEAT_SWITCH_MARGIN on ETF_SEAT_SWITCH_CONSECUTIVE
consecutive looks (the 8:30 premarket pass) before the seat flips. A
NON-degraded committee SELL/STRONG_SELL verdict on a seated ETF bypasses
hysteresis entirely — sell now, seat the next-best (the operator's "when an
ETF becomes a sell, sell it").

State: state/etf_seats.json
  {"parking": {"symbol": "SPY", "since": ts, "challenger": {"symbol": .., "streak": n}},
   "core": {"slots": ["VOO","QQQ","GLD"], "since": ts,
            "challengers": {"<slot_idx>": {"symbol": .., "streak": n}}}}

Consumers read through the accessors here (parking_symbol / core_weights) —
config.CASH_EQUITIZATION and the legacy core_weights stay as DEFAULTS so a
missing/corrupt seats file degrades to the exact pre-2026-07-16 behavior.

Trades: seat changes SELL the old seat position and BUY the new one within
the owning sleeve, under SLEEVE_LOCK, via broker.execute — market-closed buy
legs queue for the open exactly like any entry (the sell books; parking/core
capital re-deploys at 9:30 via the pending-open flush). Every change is
journaled + alerted. All fail-soft: a data failure keeps the incumbent seat.
"""
import re as _re
from datetime import datetime, timezone

import config
from firm import broker, state

_SEATS_KEY = "etf_seats"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_seats() -> dict:
    return {
        "parking": {"symbol": (config.CASH_EQUITIZATION.get("alpha_stocks")
                               or {}).get("symbol", config.BENCHMARK),
                    "since": "", "challenger": {}},
        "core": {"slots": list((config.SLEEVES["core_blend"].get("core_weights")
                                or {"VOO": 35.0, "QQQ": 15.0, "GLD": 5.0}).keys()),
                 "since": "", "challengers": {}},
    }


def _load_seats() -> dict:
    seats = state.load(_SEATS_KEY, None)
    if not isinstance(seats, dict) or "parking" not in seats or "core" not in seats:
        seats = _default_seats()
    return seats


def parking_symbol(sleeve: str = "alpha_stocks") -> str:
    """The CURRENT parking ETF for a sleeve — dynamic seat when set, else the
    config default. Both parking sleeves share one seat (one reservoir
    instrument across the firm keeps reconciliation simple)."""
    if sleeve not in ("alpha_stocks", "calls_only"):
        return (config.CASH_EQUITIZATION.get(sleeve) or {}).get("symbol", "")
    try:
        return (_load_seats().get("parking") or {}).get("symbol") \
            or config.BENCHMARK
    except Exception:
        return config.BENCHMARK


def core_weights() -> dict[str, float]:
    """core_blend's CURRENT mechanical core {symbol: weight_pct} — dynamic
    slots at the fixed CORE_SLOT_WEIGHTS, else the config default."""
    try:
        slots = (_load_seats().get("core") or {}).get("slots") or []
        w = list(getattr(config, "CORE_SLOT_WEIGHTS", (35.0, 15.0, 5.0)))
        if len(slots) == len(w):
            return {s: w[i] for i, s in enumerate(slots)}
    except Exception:
        pass
    return dict(config.SLEEVES["core_blend"].get("core_weights")
                or {"VOO": 35.0, "QQQ": 15.0, "GLD": 5.0})


def _firm_pct(symbol: str) -> float:
    """Look-through firm share of one ticker (lazy import — risk imports
    allocation lazily; a top-level cross-import would be circular)."""
    from firm import risk as _rk
    return _rk.firm_symbol_exposure_pct(symbol)


def _seat_trade(sleeve: str, old_sym: str, new_sym: str, prices: dict,
                mode: str, reason: str, is_parking: bool) -> bool:
    """Swap one seat inside its sleeve: sell the old position (if held), buy
    the new with the freed cash. Runs under SLEEVE_LOCK. The sell books even
    off-hours (exits always route); a closed-market buy leg QUEUES for the
    open via the standard pending-open contract — capital is out of the old
    seat immediately and re-deploys at 9:30.

    CHOKE POINT (IG 2026-07-27 #5): every seat placement in this module funnels
    through here, so the eligibility gate is re-asserted on `new_sym` one last
    time. The per-path gates above pick the best ELIGIBLE candidate; this stops
    any future path — or a caller that forgets — from ever booking real trades
    into a leveraged/unclassifiable seat. Returns False, so the caller keeps
    its incumbent exactly as it does for any other deferred flip."""
    _ok_new, _why_new = _seat_eligible(new_sym)
    if not _ok_new:
        state.journal("etf_seat_selection_rejected", sleeve=sleeve,
                      seat_from=old_sym, candidate=new_sym, why=_why_new,
                      path="_seat_trade choke point", reason=reason[:80])
        return False
    with state.SLEEVE_LOCK:
        s = state.get_sleeve(sleeve)
        pos = (s.get("positions") or {}).get(old_sym)
        freed = 0.0
        if pos and pos.get("quantity", 0) > 0:
            px_old = prices.get(old_sym) or pos.get("last_price") \
                or pos.get("entry_price") or 0
            if px_old <= 0:
                state.journal("etf_seat_switch_deferred", sleeve=sleeve,
                              old=old_sym, new=new_sym, why="no price for old seat")
                return False
            # Capture BEFORE the sell — _book_sell zeroes the position dict in
            # place, so reading pos["quantity"] afterwards yields 0 and the
            # buy leg silently starves (caught by §78, 2026-07-16).
            _qty_held = pos["quantity"]
            res = broker.execute(mode, s, symbol=old_sym, side="sell",
                                 quantity=_qty_held, price=px_old,
                                 meta={"reason": f"ETF seat change: {reason}",
                                       "seat_change": True})
            if not (isinstance(res, dict) and res.get("success")):
                state.journal("etf_seat_switch_deferred", sleeve=sleeve,
                              old=old_sym, new=new_sym,
                              why=str((res or {}).get("error", "sell failed"))[:80])
                return False
            freed = _qty_held * px_old
        px_new = prices.get(new_sym) or 0
        budget = max(0.0, (freed if freed > 0 else 0.0))
        if budget <= 0:
            # nothing held (fresh seat) — the owning machinery (equitize /
            # core rebalance) deploys into the new seat on its own pass
            state.save_sleeve(sleeve, s)
            return True
        if px_new <= 0:
            # capital sits in cash until the equitize/rebalance pass redeploys
            state.journal("etf_seat_buy_deferred", sleeve=sleeve, new=new_sym,
                          why="no price for new seat — equitize pass redeploys")
            state.save_sleeve(sleeve, s)
            return True
        qty = round(budget / px_new, 4) if mode == "paper" else int(budget / px_new)
        if qty > 0:
            # strategy stamp -> the position -> its future close row, so the
            # mechanical-trade filter (learning.is_mechanical_trade) catches
            # seat plumbing by stamp, not just by reason prefix (2026-07-20:
            # seat closes with strategy='' were about to enter the win rate)
            meta = {"reason": f"ETF seat change: {reason}", "seat_change": True,
                    "strategy": "cash_equitization" if is_parking else "etf_seat"}
            if is_parking:
                meta["cash_equitization"] = True
            if mode != "paper":
                # LIVE FUNDING (fleet re-grade action 3): the sell above is
                # PENDING — cash credits only at settle_pending, so a buy
                # executed now sizes against money that isn't there and
                # starves silently (the failure was not even journaled).
                # Queue the buy leg as a mechanical open intent instead; the
                # monitor's flush fills it once the sell settles, clamped to
                # the cash actually available.
                broker._queue_open_entry(s, new_sym, qty, px_new, meta,
                                         "equity", mode)
                state.journal("etf_seat_buy_queued_live", sleeve=sleeve,
                              new=new_sym, qty=qty)
            else:
                res = broker.execute(mode, s, symbol=new_sym, side="buy",
                                     quantity=qty, price=px_new, meta=meta)
                if not (isinstance(res, dict)
                        and (res.get("success") or res.get("queued"))):
                    # The buy-leg failure was previously SILENT — a
                    # half-completed seat flip left capital in cash with no
                    # operator-visible record (fleet re-grade action 3).
                    state.journal("etf_seat_buy_failed", sleeve=sleeve,
                                  new=new_sym, qty=qty,
                                  error=str((res or {}).get("error",
                                                            ""))[:80])
                elif is_parking and res.get("success") \
                        and new_sym in (s.get("positions") or {}):
                    s["positions"][new_sym]["cash_equitization"] = True
        state.save_sleeve(sleeve, s)
        return True


def _flip_outcome(seat: str, old_sym: str, new_sym: str, legs: dict) -> bool:
    """Collapse the per-sleeve legs of a two-sleeve PARKING flip into one
    verdict, and NAME the divergence when the legs disagree.

    Audit 2026-07-26: the parking seat is shared by alpha_stocks and
    calls_only, but each leg trades independently. When one leg booked and the
    other did not, the combined `ok` went False, the seats file kept the OLD
    symbol — and one sleeve was already holding the NEW one. The books and the
    seat of record disagreed with NOTHING naming it: the only journal line was
    etf_seat_switch_deferred (per-leg, and it fires on the healthy 'nothing
    happened' path too), so the divergence read as a routine skip. Downstream,
    parking_symbol() returns the old ticker for BOTH sleeves, so the diverged
    sleeve's holding is invisible to equitization until someone notices.

    No auto-rollback (operator rule): the succeeding leg has already booked a
    real sell and buy — unwinding it would churn real capital to chase
    symmetry. Report it, let the operator decide; the next pass re-attempts
    the flip from the seat of record either way.
    """
    ok = bool(legs) and all(legs.values())
    if len(set(legs.values())) > 1:
        _flipped = sorted(sl for sl, v in legs.items() if v)
        _stuck = sorted(sl for sl, v in legs.items() if not v)
        state.journal("etf_seat_partial_flip", seat=seat, old=old_sym,
                      new=new_sym, flipped=_flipped, failed=_stuck,
                      legs={sl: bool(v) for sl, v in legs.items()},
                      seat_of_record=old_sym)
        _alert("PARKING SEAT PARTIALLY FLIPPED",
               f"{old_sym} -> {new_sym}: {', '.join(_flipped)} moved but "
               f"{', '.join(_stuck)} did not. The seat of record stays "
               f"{old_sym}, so the books and the seats file DISAGREE. Not "
               "rolled back (the moved leg already booked real trades) — "
               "check the failed sleeve and re-run the seat pass.",
               severity="warning")
    return ok


def _switch_brake() -> tuple[float, int, str]:
    """(margin_mult, extra_streak, why) — the seat machinery learning from its
    OWN record (operator 2026-07-21: 'swaps need to make sure they learn from
    themselves so these swaps aren't continuously losing money'). The
    scorecard (learning.seat_switch_scorecard, cached in state on every daily
    grade) scores each rank-won flip against its counterfactual — what the
    EVICTED seat would have returned over the same window. While that trailing
    record is LOSING (>= SEAT_BRAKE_MIN_GRADED flips graded AND win rate <
    SEAT_BRAKE_WIN_RATE OR mean edge < 0), every challenger needs a
    SEAT_BRAKE_MARGIN_MULT-times lead held SEAT_BRAKE_EXTRA_STREAK extra
    looks — flips get harder, not banned (a genuine regime break can still
    clear a doubled bar). Self-reversing the moment the record recovers.
    Absent/stale/error scorecard applies NO brake: fail-open to the validated
    momentum engine, never to an unmeasured guess."""
    try:
        sc = state.load("seat_switch_scorecard", {}) or {}
        n = int(sc.get("switches_graded") or 0)
        if n < int(getattr(config, "SEAT_BRAKE_MIN_GRADED", 4)):
            return 1.0, 0, ""
        wins = int(sc.get("switch_wins") or 0)
        mean_edge = sc.get("mean_edge_pts")
        losing = (wins / n < float(getattr(config, "SEAT_BRAKE_WIN_RATE", 0.40))
                  or (isinstance(mean_edge, (int, float)) and mean_edge < 0))
        if not losing:
            return 1.0, 0, ""
        return (float(getattr(config, "SEAT_BRAKE_MARGIN_MULT", 2.0)),
                int(getattr(config, "SEAT_BRAKE_EXTRA_STREAK", 1)),
                f"trailing switch record losing: {wins}/{n} wins, "
                f"mean edge {mean_edge}pts")
    except Exception:
        return 1.0, 0, ""


def _challenge(incumbent: str, ranked: list, margin: float,
               prior: dict, extra_need: int = 0) -> tuple[str | None, dict]:
    """Hysteresis: returns (winner_or_None, new_challenger_state). A
    challenger must LEAD the incumbent by `margin` for CONSECUTIVE looks
    (+extra_need looks while the switch-record brake is on)."""
    scores = dict(ranked)
    inc_score = scores.get(incumbent)
    if not ranked:
        return None, {}
    top_sym, top_score = ranked[0]
    if top_sym == incumbent or inc_score is None:
        return None, {}
    if top_score < (inc_score + margin):
        return None, {}                      # lead too thin — no streak
    streak = (prior.get("streak", 0) + 1
              if prior.get("symbol") == top_sym else 1)
    need = int(getattr(config, "ETF_SEAT_SWITCH_CONSECUTIVE", 2)) + extra_need
    if streak >= need:
        return top_sym, {}
    return None, {"symbol": top_sym, "streak": streak,
                  "lead": round(top_score - inc_score, 2)}


def refresh_etf_pool(force: bool = False) -> dict:
    """Weekly liquidity screen over EVERY Public-buyable ETF (operator
    2026-07-16: the full landscape competes, not a hand-list). Survivors of
    the price + median-$-volume floors, ranked by liquidity, become the pool
    that feeds _core_eligible/_parking_eligible and the scan's ETF candidates.
    Chunked downloads, self-gating TTL, fail-soft (a failed build keeps the
    prior pool; no pool at all falls back to the config hand-lists)."""
    pool = state.load("etf_pool", {}) or {}
    ttl_d = float(getattr(config, "ETF_POOL_TTL_DAYS", 7.0))
    try:
        age_ok = (datetime.now(timezone.utc)
                  - datetime.fromisoformat(pool.get("asof", ""))
                  ).total_seconds() / 86400 < ttl_d
    except (ValueError, TypeError):
        age_ok = False
    if not force and pool.get("etfs") and age_ok:
        return pool
    try:
        from firm import universe
        dir_etfs = set(universe.fetch_all_us_etfs())
        snap = state.load("tradable_universe", {}) or {}
        buyable = set(snap.get("buyable") or [])
        cands = sorted(dir_etfs & buyable) if buyable else sorted(dir_etfs)
        if not cands:
            state.journal("etf_pool_no_candidates")
            return pool
        min_px = float(getattr(config, "ETF_POOL_MIN_PRICE", 5.0))
        min_dv = float(getattr(config, "ETF_POOL_MIN_DOLLAR_VOL", 5e6))
        chunk = int(getattr(config, "SCAN_CHUNK", 300))
        from firm import data_engine as _de_pool   # leak-safe yf_download
        rows: list[tuple[str, float, float]] = []   # (sym, dollar_vol, px)
        for i in range(0, len(cands), chunk):
            batch = cands[i:i + chunk]
            try:
                data = _de_pool.yf_download(batch, period="3mo", interval="1d",
                                            auto_adjust=True, progress=False,
                                            threads=True, group_by="column")
                closes = data["Close"] if "Close" in data else None
                vols = data["Volume"] if "Volume" in data else None
                if closes is None:
                    continue
                for t in getattr(closes, "columns", []):
                    s = closes[t].dropna()
                    if len(s) < 40:
                        continue
                    px = float(s.iloc[-1])
                    if px < min_px:
                        continue
                    v = vols[t].dropna() if vols is not None and t in vols.columns \
                        else None
                    if v is None or len(v) < 20:
                        continue
                    dv = float((v.tail(60) * s.tail(60)).median())
                    if dv >= min_dv:
                        rows.append((t, dv, px))
            except Exception:
                state.journal("etf_pool_chunk_failed", start=i, size=len(batch))
        if len(rows) < 20:
            state.journal("etf_pool_build_thin", survivors=len(rows),
                          note="kept prior pool")
            return pool
        rows.sort(key=lambda r: r[1], reverse=True)
        cap = int(getattr(config, "ETF_POOL_MAX", 150))
        try:
            from firm import universe as _uni_names
            _names = _uni_names.etf_names() or {}
        except Exception:
            _names = {}
        new_pool = {
            "asof": _now(), "screened": len(cands), "survivors": len(rows),
            "etfs": [{"symbol": t, "dollar_vol": round(dv), "price": round(px, 2),
                      "leveraged": _is_leveraged(t, _names.get(t))}
                     for t, dv, px in rows[:cap]],
        }
        state.save("etf_pool", new_pool)
        state.journal("etf_pool_refreshed", screened=len(cands),
                      survivors=len(rows), kept=len(new_pool["etfs"]),
                      top5=[e["symbol"] for e in new_pool["etfs"][:5]])
        return new_pool
    except Exception as e:
        state.journal("etf_pool_error", error=str(e)[:150])
        return pool


# Leveraged/inverse/daily-reset products (SOXL, TQQQ, SQQQ, ProShares Short...)
# — momentum ADORES a 3x fund in a rally, but a mechanical, unresearched SEAT
# must never hold a decaying daily-reset instrument: parking is the cash
# reservoir and core is the book's 55% ballast. They stay in the POOL (the
# committee may research and deliberately trade them as satellites); they are
# excluded from SEAT eligibility only. "Short-term/duration/maturity" bond
# funds are NOT shorts — the lookahead spares them.
_LEVERAGED_RX = _re.compile(
    r"\b\d(?:\.\d)?x\b|ultra|inverse|leveraged|\bbull\b|\bbear\b"
    r"|\bshort\b(?!\s*-?\s*(?:term|duration|maturity))",
    _re.IGNORECASE)


# Known daily-reset families by SYMBOL — the belt to the name-regex's braces.
# 2026-07-27 live incident: the name feed came back EMPTY on a pool refresh,
# `_is_leveraged` searched "" for every ticker, the whole pool reclassified as
# unleveraged, and the sell-on-verdict replacement seated SOXL (3x semis) on a
# core slot and MUU (2x single-stock Micron) on the 35% ballast slot. A gate
# that fails OPEN on missing data is not a gate.
_LEVERAGED_SYMS = frozenset((
    "SOXL SOXS TQQQ SQQQ UPRO SPXU SPXL SPXS TNA TZA LABU LABD TECL TECS "
    "FAS FAZ UDOW SDOW QLD SSO SDS QID TMF TMV UVXY SVXY BOIL KOLD NUGT DUST "
    "JNUG JDST ERX ERY DPST DRV DRN CURE YINN YANG WEBL WEBS BULZ BERZ "
    "MUU NVDL NVDU NVDD TSLL TSLQ TSLR AMDL AAPU AAPD MSFU MSFD METU METD "
    "GGLL GGLS AMZU AMZD CONL PLTU MSTU MSTX MSTZ SMCX SOXX3").split())


# Curated KNOWN-SAFE majors, verifiable by symbol alone — the mirror of the
# blocklist. Without it, a name-feed outage under fail-closed marks the WHOLE
# pool unknown and freezes every seat including SPY/RSP (observed 2026-07-27:
# 131 of 150 unknown). A broad index/sector/bond major is safe to classify by
# symbol; everything else stays unknown-ineligible until the name feed returns.
_KNOWN_UNLEVERED = frozenset((
    "SPY VOO IVV VTI RSP QQQ QQQM DIA IWM IWB IWD IWF MDY IJH IJR SCHD SCHG "
    "SCHB VIG VYM VTV VUG VB VO VXF GLD IAU SLV GDX TLT IEF SHY BIL SHV SGOV "
    "AGG BND LQD HYG TIP VTIP XLK XLF XLV XLE XLI XLP XLU XLY XLB XLRE XLC "
    "SMH SOXX XBI IBB ITB XHB KRE KBE XOP OIH USO UNG EEM EFA VEA VWO IEMG "
    "IEFA ACWI VT EWJ EWZ FXI MCHI INDA EWY EWT VNQ IYR ARKK JEPI JEPQ DVY "
    "COWZ MTUM QUAL USMV VLUE SPLV RWL PRF").split())


def _is_leveraged(sym: str, name: str | None = None) -> bool | None:
    """True = leveraged, False = verified unleveraged, None = UNKNOWN (name
    feed empty). Seat-eligibility consumers must treat None as INELIGIBLE —
    an unresearched mechanical seat never holds an instrument the firm could
    not even classify (fail closed, 2026-07-27 incident above)."""
    if sym and sym.upper() in _LEVERAGED_SYMS:
        return True
    if sym and sym.upper() in _KNOWN_UNLEVERED:
        return False
    if name is None:
        try:
            from firm import universe
            name = (universe.etf_names() or {}).get(sym)
        except Exception:
            name = None
    if not name:
        return None                      # unknown — caller must fail closed
    return bool(_LEVERAGED_RX.search(name))


def _pool_symbols(seats_only: bool = False) -> list[str]:
    pool = (state.load("etf_pool", {}) or {}).get("etfs") or []
    if seats_only:
        return [e["symbol"] for e in pool
                if e.get("symbol") and e.get("leveraged") is False]
                # `is False`, not falsy: None means the name feed was down and
                # the product could not be classified — ineligible (2026-07-27)
    return [e.get("symbol") for e in pool if e.get("symbol")]


def _seat_eligible(sym: str) -> tuple[bool, str]:
    """(ok, why_not) — THE seat-eligibility gate, evaluated at the moment of
    SELECTION by every path that can seat a symbol.

    IG 2026-07-27 #5 (CRITICAL): eligibility was enforced only at EVICTION.
    SOXL (3x semis) and MUU (2x single-stock Micron) were SEATED by the
    sell-on-verdict replacement path on 07-27 while a name-feed outage had the
    whole pool cached `leveraged: False`; the eviction added the same day
    cleaned them up hours later. A rule that only runs after the money has
    moved is a cleanup crew, not a gate — so the same predicate now runs
    BEFORE every seat placement.

    Semantics = `_pool_symbols(seats_only=True)` membership (`leveraged is
    False`), hardened two ways because the pool's flag is a CACHE:
      1. the cached pool flag may never say True (belt);
      2. the classifier is re-run LIVE (braces) — `_LEVERAGED_SYMS` and
         `_KNOWN_UNLEVERED` are static frozensets, so a symbol on the blocklist
         is caught even while the name feed is down, which is exactly the
         condition that produced the incident. Trusting the cached flag alone
         would have re-seated SOXL from a pool refreshed during the outage.
    UNKNOWN (`None`) is INELIGIBLE — a mechanical, unresearched seat never
    holds an instrument the firm could not classify (fail closed).
    Symbols absent from the pool (the config hand-list fallbacks
    PARKING_ELIGIBLE / CORE_ELIGIBLE) are judged by the classifier alone, so a
    missing pool degrades to the curated majors rather than freezing.
    """
    if not sym:
        return False, "empty symbol"
    s = str(sym).upper()
    try:
        pool = (state.load("etf_pool", {}) or {}).get("etfs") or []
        flag = next((e.get("leveraged") for e in pool
                     if str(e.get("symbol") or "").upper() == s), "absent")
    except Exception:
        flag = "absent"                    # fail-soft: classifier still rules
    if flag is True:
        return False, "pool flags leveraged/daily-reset"
    live = _is_leveraged(sym)
    if live is True:
        return False, "leveraged/inverse/daily-reset product"
    if live is None:
        return False, "unclassifiable (name feed down) — fail closed"
    return True, ""


def _first_eligible(seat: str, ranked: list, path: str,
                    extra_ok=None) -> str | None:
    """First entry of a ranked list that clears `_seat_eligible` (plus any
    caller predicate) — the ranked-pick replacement for a blind `ranked[0]`.
    EVERY rejection is journaled (IG 2026-07-27 #5: "journal any rejection"),
    so a seat that skips its top-ranked name leaves a readable trail instead of
    a silent substitution. Accepts (sym, score) pairs or bare symbols."""
    for item in ranked or []:
        t = item[0] if isinstance(item, (tuple, list)) else item
        ok, why = _seat_eligible(t)
        if not ok:
            state.journal("etf_seat_selection_rejected", seat=seat,
                          candidate=t, why=why, path=path)
            continue
        if extra_ok is not None and not extra_ok(t):
            continue
        return t
    return None


def _seat_corr_ok(cand: str, seated: list[str], max_corr: float) -> bool:
    """Pairwise 6-mo daily-return correlation gate for CORE seats (2026-07-26
    audit: ticker-only dedup let SOXX + SMH — one semiconductor bet twice —
    occupy 40% of the ballast). True = allowed. Fails OPEN on any data gap so
    an outage can only admit, never freeze rotation."""
    try:
        from firm import data_engine as _de
        h = _de.fetch_history(sorted(set([cand] + seated)), period="6mo")
        closes = h["Close"] if "Close" in h else h
        rets = closes.pct_change().dropna(how="all")
        if cand not in getattr(rets, "columns", []):
            return True
        for s_ in seated:
            if s_ not in rets.columns:
                continue
            pair = rets[[cand, s_]].dropna()
            if len(pair) < 40:
                continue
            if float(pair[cand].corr(pair[s_])) > max_corr:
                return False
        return True
    except Exception:
        return True


def _parking_eligible() -> list[str]:
    """Parking = the fund's instant-cash reservoir: liquid ($1B+/day),
    unleveraged candidates from the WHOLE pool. Broadness is enforced
    BEHAVIORALLY, not by this list (operator 2026-07-20 v2: "like SPY but
    better... that type of calculation, not momentum"): the reservoir-fitness
    gates in data_engine (vol corridor around SPY's own vol + broad-market
    correlation + history) are what keep a SOXX-class sector fund out — a
    liquidity-only screen alone once handed the reservoir to SOXX, and a name
    list alone would blind the fund to a genuinely better broad ETF it hasn't
    heard of. Hand-list is the fail-safe when the pool is missing. Capped to
    bound the 1y history fetch."""
    pool = (state.load("etf_pool", {}) or {}).get("etfs") or []
    floor = float(getattr(config, "PARKING_MIN_DOLLAR_VOL", 1e9))
    dyn = [e["symbol"] for e in pool
           if (e.get("dollar_vol") or 0) >= floor and e.get("leveraged") is False]
    return dyn[:40] if len(dyn) >= 3 else list(
        getattr(config, "PARKING_ELIGIBLE", ["SPY"]))


def _core_eligible() -> list[str]:
    """Core seats draw from the top-N most liquid pool ETFs — the FULL
    landscape competes (sector, style, intl, bonds, gold, whatever earns it);
    config hand-list as fallback."""
    dyn = _pool_symbols(seats_only=True)[
        :int(getattr(config, "CORE_ELIGIBLE_POOL_N", 60))]
    return dyn if len(dyn) >= 10 else list(
        getattr(config, "CORE_ELIGIBLE", []))


def review_seats(mode: str | None = None) -> dict:
    """The 8:30 seat review: rank both eligible universes, advance/reset
    challenger streaks, execute any seat flips that cleared hysteresis.
    Fail-soft — any error keeps every incumbent."""
    from firm import data_engine
    mode = mode or state.get_settings().get("mode", "paper")
    out = {"parking_switched": None, "core_switched": []}
    try:
        seats = _load_seats()
        park_uni = _parking_eligible()
        core_uni = _core_eligible()
        # PARKING is ranked by RESERVOIR FITNESS, not momentum (operator
        # 2026-07-20: "like SPY but better — not too volatile, not too
        # stagnant, safe parking best to keep going forward"). Core seats
        # keep the validated momentum engine; only the cash reservoir gets
        # the safety calculation. Fitness scores are Sharpe-scale, so the
        # parking margin lives on its own config knob.
        sig_park = data_engine.reservoir_fitness(park_uni)
        sig_core = data_engine.rotation_signals(core_uni)
        margin = float(getattr(config, "ETF_SEAT_SWITCH_MARGIN", 4.0))
        park_margin = float(getattr(config, "PARKING_FITNESS_SWITCH_MARGIN", 0.35))
        # Switch-record brake (operator 2026-07-21): a losing trailing flip
        # record tightens BOTH switch bars until the record recovers.
        _bmult, _bextra, _bwhy = _switch_brake()
        if _bmult != 1.0 or _bextra:
            margin *= _bmult
            park_margin *= _bmult
            state.journal("seat_switch_brake_active", why=_bwhy,
                          margin_mult=_bmult, extra_streak=_bextra,
                          core_margin=round(margin, 2),
                          parking_margin=round(park_margin, 2))
        prices = broker.equity_prices(sorted(set(
            park_uni + core_uni
            + [seats["parking"]["symbol"]] + list(seats["core"]["slots"]))))

        # — parking seat —
        inc = seats["parking"]["symbol"]
        # UNFIT-INCUMBENT eviction (operator 2026-07-20): a liquidity-only
        # eligibility bug once seated SOXX — a sector fund — as the reservoir,
        # and nothing re-judged an incumbent. An incumbent that fails the
        # reservoir-fitness GATES (vol corridor / SPY correlation / history)
        # is a RULE BREACH, not a challenger contest: migrate straight to the
        # fittest ETF, no hysteresis (hysteresis protects rank flip-flop
        # between VALID seats; it must not protect an invalid one), alert.
        _park_scores = dict(sig_park["ranked"])
        _unfit_inc = inc not in _park_scores and bool(sig_park["ranked"])
        # SELECTION GATE (IG 2026-07-27 #5): the replacement is the best
        # SEAT-ELIGIBLE reservoir candidate, not a blind ranked[0]. Evicting an
        # unfit incumbent into a leveraged one would trade real capital to swap
        # one rule breach for a worse one.
        _best = _first_eligible("parking", sig_park["ranked"],
                                "unfit-incumbent eviction") if _unfit_inc else None
        if _unfit_inc and not _best:
            state.journal("etf_seat_eviction_no_replacement", seat="parking",
                          old=inc, why="no seat-eligible reservoir candidate")
        if _best:
            _why = (sig_park.get("gates") or {}).get(inc, "not in eligible pool")
            _legs = {sl: _seat_trade(sl, inc, _best, prices, mode,
                                     f"parking incumbent {inc} failed reservoir "
                                     f"gates ({_why}) — reverting to {_best}",
                                     is_parking=True)
                     for sl in ("alpha_stocks", "calls_only")}
            ok = _flip_outcome("parking", inc, _best, _legs)
            if ok:
                seats["parking"] = {"symbol": _best, "since": _now(),
                                    "challenger": {}}
                out["parking_switched"] = f"{inc}->{_best} (unfit incumbent)"
                state.journal("etf_seat_incumbent_evicted", seat="parking",
                              old=inc, new=_best, why=str(_why)[:120])
                _alert("PARKING ETF REVERTED",
                       f"{inc} -> {_best}: the incumbent failed the reservoir "
                       f"gates ({_why}) — the cash reservoir must behave like "
                       "SPY (vol corridor + broad-market correlation). "
                       "Migrated to the fittest reservoir ETF.")
                inc = _best
        winner, chal = _challenge(inc, sig_park["ranked"], park_margin,
                                  seats["parking"].get("challenger") or {},
                                  extra_need=_bextra)
        seats["parking"]["challenger"] = chal
        # SELECTION GATE (IG 2026-07-27 #5): hysteresis proves a challenger is
        # BETTER, never that it is ALLOWED. A 3x fund out-ranking SPY for two
        # looks is exactly how momentum hands the cash reservoir a daily-reset
        # product — the streak resets and the seat holds.
        if winner:
            _ok_w, _why_w = _seat_eligible(winner)
            if not _ok_w:
                state.journal("etf_seat_selection_rejected", seat="parking",
                              candidate=winner, why=_why_w,
                              path="hysteresis challenger")
                winner = None
        # CONVERGENCE CHECK (fleet re-grade action 2): the same momentum
        # engine ranks parking, core seats and the rotation sleeve — at a
        # momentum top they all pick the same ticker. Never hand the
        # ~40%-of-firm parking reservoir to a challenger the firm already
        # holds in size through other sleeves.
        if winner:
            _conv = _firm_pct(winner)
            if _conv > float(getattr(config, "ETF_SEAT_CONVERGENCE_MAX_PCT",
                                     12.0)):
                state.journal("etf_seat_switch_blocked_convergence",
                              seat="parking", challenger=winner,
                              firm_pct=round(_conv, 1))
                winner = None
        if winner:
            _legs = {sl: _seat_trade(sl, inc, winner, prices, mode,
                                     f"{winner} out-ranked {inc} past hysteresis",
                                     is_parking=True)
                     for sl in ("alpha_stocks", "calls_only")}
            ok = _flip_outcome("parking", inc, winner, _legs)
            if ok:
                seats["parking"] = {"symbol": winner, "since": _now(),
                                    "challenger": {}}
                out["parking_switched"] = f"{inc}->{winner}"
                state.journal("etf_seat_switched", seat="parking", old=inc,
                              new=winner, ranked=sig_park["ranked"][:4])
                _alert("PARKING ETF SWITCHED",
                       f"{inc} -> {winner}: the challenger out-ranked the "
                       "incumbent past hysteresis. Reservoir migrated.")

        # — core seats (best-N with slot weights, no duplicates) —
        slots = list(seats["core"]["slots"])
        chals = seats["core"].get("challengers") or {}
        for i, inc_c in enumerate(list(slots)):
            # the best eligible NOT already seated elsewhere in the core.
            # DIVERSIFICATION GATE (audit 2026-07-26 CRITICAL): dedup was by
            # TICKER identity only, so momentum flipped VOO->SOXX, QQQ->USO,
            # GLD->SMH in 3 days and the 55% "ballast" became SOXX 35% + SMH 5%
            # — the SAME semiconductor exposure twice — with broad-market weight
            # 0%. A core seat challenger must not be a near-duplicate of an
            # already-seated slot: pairwise 6-mo daily-return correlation with
            # every OTHER seated slot must stay under CORE_SEAT_MAX_CORR.
            # Fail-open per candidate (no data -> allowed) so a history outage
            # can never freeze seat rotation; the gate only ever REMOVES
            # near-duplicates from contention.
            # INELIGIBLE-INCUMBENT EVICTION (2026-07-27 live incident). The
            # sell-on-verdict path seated SOXL (3x semis) and MUU (2x Micron)
            # while a name-feed outage had the whole pool classified
            # unleveraged. Once the classifier was fixed the seats STILL could
            # not heal: the challenger path is rank+hysteresis only, and the
            # correlation gate below was computing "vs" the leveraged
            # incumbents — rejecting every sane replacement for correlating
            # with the very seat that had to go. Parking has an unfit-incumbent
            # eviction; core did not. Now it does: an incumbent that is not in
            # the seats-eligible pool (leveraged, unknown, or vanished) is
            # evicted IMMEDIATELY — no hysteresis, no margin — for the best
            # ranked eligible name not seated elsewhere, corr-gated against the
            # OTHER slots only (the departing incumbent must not veto its own
            # replacement).
            _eligible_now = set(_pool_symbols(seats_only=True))
            if inc_c and inc_c not in _eligible_now:
                _others_ev = [s_ for j, s_ in enumerate(slots) if j != i]
                _mx_ev = float(getattr(config, "CORE_SEAT_MAX_CORR", 0.85))
                # `_eligible_now` is the CACHED pool flag; `_first_eligible`
                # re-runs the classifier live on top of it (IG 2026-07-27 #5) —
                # the pool that fed this very incident was cached during a name
                # feed outage with every product marked unleveraged.
                _repl = _first_eligible(
                    f"core_slot_{i + 1}",
                    [(t, _sc) for t, _sc in sig_core["ranked"]
                     if t in _eligible_now and t not in slots],
                    "ineligible-incumbent eviction",
                    extra_ok=lambda t, _o=_others_ev, _m=_mx_ev: (
                        not _o or _seat_corr_ok(t, _o, _m)))
                if _repl and _seat_trade(
                        "core_blend", inc_c, _repl, prices, mode,
                        f"core slot {i + 1}: incumbent {inc_c} not seat-eligible "
                        "(leveraged/unclassifiable) — evicted", is_parking=False):
                    slots[i] = _repl
                    out["core_switched"].append(f"{inc_c}->{_repl} (ineligible)")
                    chals[str(i)] = {}
                    state.journal("etf_seat_incumbent_evicted",
                                  seat=f"core_slot_{i + 1}", old=inc_c,
                                  new=_repl, why="not seat-eligible")
                    _alert("CORE SEAT EVICTED — ineligible instrument",
                           f"Slot {i + 1}: {inc_c} is not seat-eligible "
                           f"(leveraged or unclassifiable) — replaced with "
                           f"{_repl}.", severity="warning")
                    inc_c = _repl
                elif not _repl:
                    state.journal("etf_seat_eviction_no_replacement",
                                  seat=f"core_slot_{i + 1}", old=inc_c)
            _others = [s_ for j, s_ in enumerate(slots) if j != i]
            avail = [(t, sc) for t, sc in sig_core["ranked"]
                     if t == inc_c or t not in slots]
            if _others:
                _mx = float(getattr(config, "CORE_SEAT_MAX_CORR", 0.85))
                _ok_corr, _cut = [], []
                for t, sc in avail:
                    if t == inc_c or _seat_corr_ok(t, _others, _mx):
                        _ok_corr.append((t, sc))
                    else:
                        _cut.append(t)
                if _cut:
                    state.journal("core_seat_corr_excluded", seat=f"core_slot_{i+1}",
                                  excluded=_cut[:6], vs=_others, max_corr=_mx)
                avail = _ok_corr
            winner, chal = _challenge(inc_c, avail, margin,
                                      chals.get(str(i)) or {},
                                      extra_need=_bextra)
            chals[str(i)] = chal
            # SELECTION GATE (IG 2026-07-27 #5). `avail` is filtered by the
            # CACHED pool flag via _core_eligible(); re-assert live so a stale
            # or outage-built pool can never rank a 3x fund onto the ballast.
            if winner:
                _ok_w, _why_w = _seat_eligible(winner)
                if not _ok_w:
                    state.journal("etf_seat_selection_rejected",
                                  seat=f"core_slot_{i + 1}", candidate=winner,
                                  why=_why_w, path="hysteresis challenger")
                    winner = None
            if winner and _firm_pct(winner) > float(getattr(
                    config, "ETF_SEAT_CONVERGENCE_MAX_PCT", 12.0)):
                state.journal("etf_seat_switch_blocked_convergence",
                              seat=f"core_slot_{i}", challenger=winner,
                              firm_pct=round(_firm_pct(winner), 1))
                winner = None
            if winner:
                if _seat_trade("core_blend", inc_c, winner, prices, mode,
                               f"core slot {i + 1}: {winner} out-ranked {inc_c}",
                               is_parking=False):
                    slots[i] = winner
                    out["core_switched"].append(f"{inc_c}->{winner}")
                    chals[str(i)] = {}
                    state.journal("etf_seat_switched", seat=f"core_slot_{i + 1}",
                                  old=inc_c, new=winner,
                                  ranked=sig_core["ranked"][:5])
                    _alert("CORE ETF SWITCHED",
                           f"Slot {i + 1}: {inc_c} -> {winner} "
                           "(out-ranked past hysteresis).")
        seats["core"]["slots"] = slots
        seats["core"]["challengers"] = chals
        if out["core_switched"]:
            seats["core"]["since"] = _now()
        state.save(_SEATS_KEY, seats)
        state.journal("etf_seats_reviewed",
                      parking=seats["parking"]["symbol"],
                      parking_challenger=seats["parking"].get("challenger") or None,
                      core=slots, core_challengers={k: v for k, v in chals.items() if v},
                      switched=out)
    except Exception as e:
        state.journal("etf_seat_review_error", error=str(e)[:150])
    return out


def apply_sell_verdicts(verdicts: dict, mode: str) -> list[str]:
    """The operator's hard rule: a NON-degraded committee SELL/STRONG_SELL on
    a SEATED ETF executes — sell it, seat the next-best eligible immediately
    (hysteresis bypassed; conviction must clear the owning sleeve's bar).
    Rotation sleeve holdings stay advisory-only (validated mechanical
    strategy). Returns the demoted symbols."""
    from firm import data_engine
    demoted: list[str] = []
    try:
        seats = _load_seats()
        seated = {seats["parking"]["symbol"]: "parking"}
        # 1-BASED seat labels (IG 2026-07-27 #6). This path used to journal
        # `core_slot_{i}` (0-based) while review_seats journals
        # `core_slot_{i + 1}` — so this module emitted TWO different meanings
        # for the same label: the 07-27 `core_slot_2` sell-on-verdict was
        # really display slot 3 (SMH -> SOXL), and it collided head-on with the
        # 07-21 `core_slot_2` rank flip (QQQ -> USO) in the scorecard's
        # seat-keyed history. One label, one seat.
        for i, t in enumerate(seats["core"]["slots"]):
            seated.setdefault(t, f"core_slot_{i + 1}")
        min_conv = int(config.SLEEVES["core_blend"].get("min_conviction", 55))
        for sym, d in (verdicts or {}).items():
            v = (d or {}).get("verdict") or {}
            if sym not in seated or v.get("degraded"):
                continue
            if v.get("rating") not in ("SELL", "STRONG_SELL"):
                continue
            if (v.get("conviction") or 0) < min_conv:
                state.journal("etf_seat_sell_below_bar", symbol=sym,
                              conviction=v.get("conviction"))
                continue
            where = seated[sym]
            if where == "parking":
                uni = [t for t in _parking_eligible() if t != sym]
            else:
                uni = [t for t in _core_eligible()
                       if t != sym and t not in seats["core"]["slots"]]
            # PARKING replacements must pass the RESERVOIR gates, not raw
            # momentum (audit 2026-07-26 CRITICAL #2): rotation_signals ranks by
            # momentum alone, which is exactly the path that seated SOXX as
            # "parking" on 07-20 until the reservoir check evicted it 40 minutes
            # later. Parking is a cash reservoir — broad, low-vol, book-correlated
            # — so its replacement universe is reservoir_fitness's ranked list.
            # Core replacements keep the momentum ranking (that IS their mandate).
            if where == "parking":
                sig = data_engine.reservoir_fitness(uni)
            else:
                sig = data_engine.rotation_signals(uni)
            if not sig["ranked"]:
                state.journal("etf_seat_sell_no_replacement", symbol=sym)
                continue
            # SELECTION GATE (IG 2026-07-27 #5 — THIS is the path that seated
            # SOXL and MUU). A committee SELL forces the seat to MOVE; it never
            # licenses moving into a leveraged/unclassifiable product. Take the
            # best SEAT-ELIGIBLE replacement; if none clears the gate the seat
            # holds rather than trading into a rule breach — the incumbent is
            # already a SELL, and the next pass re-attempts with a fresh pool.
            new_sym = _first_eligible(where, sig["ranked"],
                                      "sell-on-verdict replacement")
            if not new_sym:
                state.journal("etf_seat_sell_no_replacement", symbol=sym,
                              seat=where, why="no seat-eligible replacement")
                continue
            prices = broker.equity_prices(sorted(set(uni + [sym, new_sym])))
            reason = (f"committee {v.get('rating')} (conv "
                      f"{v.get('conviction')}) — operator rule: a seated ETF "
                      "that becomes a SELL is SOLD")
            if where == "parking":
                # Same two-sleeve divergence surface as the rank/eviction
                # flips (audit 2026-07-26) — routed through _flip_outcome so a
                # half-completed sell-on-verdict is named, not swallowed. The
                # short-circuit is DELIBERATE and preserved: if the first leg
                # cannot trade, do not open a divergence by moving the second.
                _legs = {}
                for sl in ("alpha_stocks", "calls_only"):
                    _legs[sl] = _seat_trade(sl, sym, new_sym, prices, mode,
                                            reason, is_parking=True)
                    if not _legs[sl]:
                        break
                ok = _flip_outcome("parking", sym, new_sym, _legs)
                if ok:
                    seats["parking"] = {"symbol": new_sym, "since": _now(),
                                        "challenger": {}}
            else:
                idx = int(where.split("_")[-1]) - 1        # label is 1-based
                ok = _seat_trade("core_blend", sym, new_sym, prices, mode,
                                 reason, is_parking=False)
                if ok:
                    seats["core"]["slots"][idx] = new_sym
                    (seats["core"].setdefault("challengers", {}))[str(idx)] = {}
            if ok:
                demoted.append(sym)
                state.journal("etf_seat_sold_on_verdict", symbol=sym,
                              seat=where, replacement=new_sym,
                              conviction=v.get("conviction"))
                _alert("SEATED ETF SOLD ON COMMITTEE VERDICT",
                       f"{sym} ({where}) rated {v.get('rating')} conv "
                       f"{v.get('conviction')} — sold; {new_sym} seated.")
        if demoted:
            state.save(_SEATS_KEY, seats)
    except Exception as e:
        state.journal("etf_seat_verdict_error", error=str(e)[:150])
    return demoted


def _alert(title: str, msg: str, severity: str = "info") -> None:
    try:
        from firm import alerts
        alerts.alert(title, msg, severity)
    except Exception:
        pass
