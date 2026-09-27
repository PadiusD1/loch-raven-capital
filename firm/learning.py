"""Firm memory — the firm journals every outcome, reviews itself with Opus,
and feeds the lessons back into every future research prompt.

  record_outcomes()  every closed trade gets matched to the verdict that
                     caused it -> performance ledger (state/ledger.json)
  run_review()       once >=5 new trades close, Opus acts as Head of
                     Performance Review: wins vs losses, what the desks got
                     right/wrong, distilled into <=12 durable lessons
  lessons_context()  those lessons are injected into every analyst, debate,
                     and judge prompt — the committee remembers
"""
import json
import re
from datetime import datetime, timezone

import config
from firm import brain, state

REVIEW_MIN_NEW_TRADES = 5
# Cold-start guard (2026-06-14 audit A7): the FIRST review writes the firm's
# founding lessons — distilling them from a handful of trades inside one week's
# single market regime is the most overfit-prone moment in the fund's life. The
# first review therefore needs a LARGER, calendar-SPREAD sample; after that the
# ongoing cadence (REVIEW_MIN_NEW_TRADES) applies.
COLD_START_MIN_TRADES = 20
COLD_START_MIN_SPAN_DAYS = 21
# Overlapping-window dedup: two verdicts on the SAME name a few days apart share
# ~95% of their 21-trading-day forward window — they are ONE outcome, not two.
# Counting both inflates effective n and biases stats toward frequently-rejudged
# (held/benched) names. Keep one verdict per name per ~window.
OVERLAP_WINDOW_DAYS = 30          # ~21 trading days in calendar days
MAX_LESSONS = 12

# The review may re-tune the Bench's thresholds, but ONLY inside these hard
# bounds — the learning loop can adapt the firm, never break it.
BENCH_BOUNDS = {
    "improve_pts": (3.0, 10.0),
    "decay_drop_pts": (3.0, 12.0),
    "missing_cycles_drop": (2, 6),
    "max_rechecks": (1, 4),
    "max_age_days": (10, 60),
    "stop_reclaim_pct": (1.0, 8.0),   # how much recovery proves a stop-out re-entry
}


def _underlying(symbol: str) -> str:
    """AAPL -> AAPL; AMD260618C00477500 -> AMD."""
    m = re.match(r"^([A-Z\-]+?)\d{6}[CP]\d{8}$", symbol)
    return m.group(1) if m else symbol


def _spy_window_ret(spy_ser, opened_iso: str, closed_iso: str) -> float | None:
    """SPY % return over a trade's holding window — the mandate is beating
    SPY, so every outcome is judged RELATIVE to what the index did while
    the capital was deployed. A +5% win during a +8% SPY run is a loss."""
    if spy_ser is None or not opened_iso or not closed_iso:
        return None
    try:
        seg = spy_ser[(spy_ser.index >= opened_iso[:10])
                      & (spy_ser.index <= closed_iso[:10])]
        if len(seg) >= 2 and float(seg.iloc[0]) > 0:
            return float(seg.iloc[-1] / seg.iloc[0] - 1) * 100
        if len(seg) == 1:
            return 0.0     # intraday round trip — index basically flat
    except Exception:
        pass
    return None


_SPY_UNSET = object()


def prefetch_spy_series():
    """1y SPY closes for trade-window attribution, fetched OUTSIDE any lock.
    Callers that hold (or are about to take) SLEEVE_LOCK must call this FIRST
    and pass the result to record_outcomes — the lazy in-function fetch is a
    network call with no timeout, and holding the sleeve RLock across it
    starves the 1s options loop into the 180s watchdog restart (batch review
    2026-07-01 R3/R4). Returns None on any failure (rows record spy=None)."""
    try:
        from firm import data_engine
        return data_engine.fetch_history(
            [config.BENCHMARK], period="1y")[config.BENCHMARK].dropna()
    except Exception:
        return None


_MECHANICAL_STRATEGIES = ("cash_equitization", "etf_seat")


def is_mechanical_trade(t: dict) -> bool:
    """PLUMBING, not an investment decision (operator 2026-07-20: 'these
    changes don't count'): cash-equitization parking round-trips and ETF
    seat-change migrations (including the 07-20 SPY->SOXX misfire and its
    corrective reverts) must never enter win-rate, the ledger, or the
    lessons loop — a 'loss' booked by a reservoir migration would teach the
    committee about a decision nobody made. ONE predicate shared by every
    grading surface (record_outcomes, the dashboard win-rate, the seat
    scorecard) so a surface can never drift back to counting them. Matches
    the strategy stamp, the seat_change flag, or the booked reason prefix —
    the prefix leg covers historical rows that predate the stamps."""
    if (t.get("strategy") or "") in _MECHANICAL_STRATEGIES or t.get("seat_change"):
        return True
    r = str(t.get("reason") or t.get("exit_reason") or "")
    # "satellite unwound" = the 07-02 de-dup policy migration force-selling
    # core satellites when the rule changed (the de-dup was itself removed the
    # same day) — a code change's trades, not the committee's (2026-07-20).
    return r.startswith(("ETF seat change", "equitization", "satellite unwound"))


def record_outcomes(spy_ser=_SPY_UNSET) -> int:
    """Match newly closed trades to the verdicts that drove them.

    `spy_ser`: pass `prefetch_spy_series()`'s result when calling under
    SLEEVE_LOCK (see above). Default keeps the legacy lazy fetch for
    lock-free callers."""
    ledger = state.load("ledger", {"trades": [], "seen": []})
    # A partial ledger on disk must degrade to empty lists, never KeyError —
    # this crash once marked an otherwise-complete cycle as failed
    ledger.setdefault("trades", [])
    ledger.setdefault("seen", [])
    seen = set(ledger.get("seen", []))
    history = state.load("research", {}).get("history", [])
    if spy_ser is _SPY_UNSET:
        spy_ser, spy_loaded = None, False   # fetched lazily, on first new trade
    else:
        spy_loaded = True                   # caller prefetched (possibly None)
    new = 0
    for sleeve in config.SLEEVES:
        s = state.get_sleeve(sleeve)
        for t in s.get("closed_trades", []):
            # trade_id is collision-proof (2026-06-10 week-sim: same-tick
            # closes shared a closed_at on Windows and one trade vanished).
            # Legacy rows without it keep the EXACT old key format so
            # already-recorded trades never re-record as duplicates.
            key = (t.get("trade_id")
                   or (f"{t['symbol']}|{t.get('closed_at')}" if t.get("closed_at")
                       else f"{t['symbol']}|{t.get('entry')}|{t.get('exit')}|{t.get('pnl')}"))
            if key in seen:
                continue
            seen.add(key)
            # Cash-equitization parking round-trips AND ETF seat migrations
            # are PLUMBING, not investment decisions — recording them would
            # open the 20-trade cold-start gate on non-decisions and dilute
            # win_rate/vs_spy with benchmark-tracking noise (2026-07-01 goal
            # audit fix 4c; extended 2026-07-20 after seat-change closes with
            # an empty strategy stamp slipped past the old string equality —
            # operator: "these changes don't count"). Marked seen (never
            # re-scanned) but never counted or attributed.
            if is_mechanical_trade(t):
                continue
            tick = _underlying(t["symbol"])
            # Verdict-at-ENTRY first (stamped on the position -> closed trade
            # since 2026-06-10): the verdict that actually opened the trade.
            # History scan is the fallback for positions that predate it —
            # and even then it can mis-attribute across re-research cycles, so
            # DEGRADED history rows are skipped (fix 4b): a quant-fallback row
            # carries fabricated conviction/scores (TXN's ledger row got a
            # later degraded cycle's all-100 desk scores). The newest REAL
            # verdict attributes; if none exists, unknown beats fabricated.
            verdict = None
            desk_scores = t.get("desk_scores") or None   # entry-time stamp first
            if t.get("rating"):
                verdict = {"rating": t.get("rating"),
                           "conviction": t.get("conviction"),
                           "degraded": bool(t.get("degraded")),
                           "thesis": (t.get("thesis") or "")[:200]}
            for h in reversed(history):
                v = (h.get("verdicts") or {}).get(tick)
                if v:
                    if v.get("degraded"):
                        continue           # fabricated fallback — keep scanning older
                    if verdict is None:
                        verdict = {"rating": v.get("rating"),
                                   "conviction": v.get("conviction"),
                                   "degraded": bool(v.get("degraded")),
                                   "thesis": (v.get("thesis") or "")[:200]}
                    if desk_scores is None:
                        desk_scores = (h.get("desk_scores") or {}).get(tick)
                    break
            if not spy_loaded:
                spy_loaded = True
                try:
                    from firm import data_engine
                    spy_ser = data_engine.fetch_history(
                        [config.BENCHMARK], period="1y")[config.BENCHMARK].dropna()
                except Exception:
                    spy_ser = None
            spy_w = _spy_window_ret(spy_ser, t.get("opened_at", ""),
                                    t.get("closed_at", ""))
            trade_ret = None
            try:
                if t.get("entry") and float(t["entry"]) > 0:
                    trade_ret = (float(t.get("exit", 0)) / float(t["entry"]) - 1) * 100
            except (TypeError, ValueError, ZeroDivisionError):
                pass
            # Duration/size attribution fields (goal audit 4a): the closed trade
            # already carries these; the ledger row is the only place that
            # SURVIVES (sleeve closed_trades cap at 300 and were wiped once) —
            # a field missed at record time can never be backfilled.
            _unit = 100 if t.get("instrument") == "option" else 1
            try:
                _cost = round(float(t.get("entry") or 0)
                              * float(t.get("quantity") or 0) * _unit, 2)
            except (TypeError, ValueError):
                _cost = None
            ledger["trades"].append({
                "sleeve": sleeve, "ticker": tick, "symbol": t["symbol"],
                "instrument": t.get("instrument", "equity"),
                "entry": t.get("entry"), "exit": t.get("exit"),
                "opened_at": t.get("opened_at", ""),
                "quantity": t.get("quantity"), "cost": _cost,
                "pnl": t.get("pnl"), "exit_reason": t.get("reason", ""),
                "closed_at": t.get("closed_at", ""), "verdict": verdict,
                "desk_scores": desk_scores,
                "depth": t.get("depth", ""),
                "factors": t.get("factors") or {},
                "spy_ret_pct": round(spy_w, 2) if spy_w is not None else None,
                "excess_ret_pct": (round(trade_ret - spy_w, 2)
                                   if spy_w is not None and trade_ret is not None
                                   else None),
                "reentry_after_stop": bool(t.get("reentry_after_stop")),
                # Annualized vol AT ENTRY — broker.py stamps it on every closed
                # trade (2026-07-13) but the row build never copied it through,
                # so config.vol_bucket(None) returned "mid" for 100% of rows and
                # stop_evidence_by_vol could not diverge by regime: the per-regime
                # sigma multiple in sleeves._stop_mults had literally no evidence
                # to tune on (2026-07-25 audit: `any('vol_ann' in t for t in
                # ledger['trades'])` was False across all 19 live rows). The
                # ledger row is the only copy that SURVIVES the 300-trade sleeve
                # cap, so a field missed here can never be backfilled.
                "vol_ann": t.get("vol_ann"),
                # THE SAME COPY-THROUGH BUG, AGAIN, ONE FIELD OVER (fix brief
                # 2026-07-28 #7). broker.py has stamped stop_pct/trail_stop_pct
                # on every closed trade since the vol-aware stops shipped, and
                # this row build dropped both at the ledger boundary for
                # exactly the reason the comment above describes for vol_ann.
                # Without them the DESIGNED stop distance is gone by the time
                # anything can grade the exit, so "the stop filled 17 points
                # past its design" was unmeasurable and the review had to be
                # told about it by a model eyeballing raw percentages — which
                # is how MU's correctly-designed -17% stop (2.5 sigma on a
                # 107-vol name IS ~17%) ended up filed as slippage.
                "stop_pct": t.get("stop_pct"),
                "trail_stop_pct": t.get("trail_stop_pct"),
                "strategy": t.get("strategy", ""),
                "momentum_score": t.get("momentum_score"),
                "sector": t.get("sector", ""),
                "sector_tailwind": bool(t.get("sector_tailwind")),
                "mode": t.get("mode", "paper"),
            })
            new += 1
    # Ordered eviction (oldest first) — truncating a set evicts randomly and
    # can drop keys for still-retained trades, duplicating ledger rows
    seen_ordered = [k for k in ledger.get("seen", []) if k in seen]
    seen_ordered += [k for k in seen if k not in set(seen_ordered)]
    ledger["seen"] = seen_ordered[-1500:]
    # Monotonic lifetime counter (2026-06-10 audit): the review gate compares
    # against this, NOT len(trades) — once the list caps at 500, len() pins
    # and the review would silently never fire again. Migration: never seed
    # below what the review has already covered.
    base = ledger.get("total_recorded")
    if base is None:
        base = max(len(ledger["trades"]) - new,
                   state.load("lessons", {}).get("reviewed_through", 0))
    ledger["total_recorded"] = base + new
    ledger["trades"] = ledger["trades"][-500:]
    state.save("ledger", ledger)
    if new:
        state.journal("outcomes_recorded", new_trades=new)
    return new


_STRATEGY_LEDGER_STRATEGIES = ("dual_momentum",)


def _is_strategy_trade(t: dict) -> bool:
    """A validated MECHANICAL strategy's exit (dual-momentum rotation): stays
    in the ledger for strategy-level review (test 5b), but is NOT a committee
    decision. Matched structurally too — sleeve or reason prefix — because
    the live 07-06 EEM row predates the strategy stamp and slipped a
    stamp-only filter straight back into the headline (2026-07-20)."""
    if (t.get("strategy") or "") in _STRATEGY_LEDGER_STRATEGIES:
        return True
    return (t.get("sleeve") == "etf_rotation"
            or str(t.get("exit_reason") or t.get("reason") or "")
            .startswith("rotation exit"))


def _is_committee_trade(t: dict) -> bool:
    """A ledger row that may be used to GRADE THE COMMITTEE (2026-07-25 audit).
    ONE predicate for every committee-facing aggregation in stats(), for the
    same reason is_mechanical_trade is one predicate: five hand-rolled filters
    is five chances for a surface to drift back to counting decisions nobody
    made. Excludes (a) validated mechanical-strategy exits — a dual-momentum
    rank rule fired them, and by_strategy is their review surface — and (b)
    DEGRADED quant-fallback verdicts, whose rating/conviction/desk scores are a
    synthetic composite, not a judgment (goal audit 4d). Surfaces that own a
    dedicated degraded bucket (by_rating, by_conviction) keep it and only skip
    the strategy leg; surfaces with nowhere to put a fabricated verdict use
    this.

    (c) MARK ARTIFACTS (operator 2026-07-28, PRSU). A row whose exit price was
    never a tradeable price is not evidence of anything the committee or the
    exit ladder did. Four equity exits filled on quotes outside the day's real
    range — PRSU 39.08 and 37.51 against true lows of 51.68 and 50.60, INNV
    10.20 vs 10.93, STRZ 22.49 vs 25.98 — and those four carry the WORST
    stop-slippage numbers in the book. Left in, Job 2g would widen every stop
    in the fund to accommodate losses that never happened. They stay in the
    ledger (the audit trail is the point) and out of every evidence table."""
    return (not _is_strategy_trade(t)
            and not (t.get("verdict") or {}).get("degraded")
            and not t.get("mark_artifact"))


def _exit_family(reason: str | None) -> str:
    """Bucket a booked exit reason into the family the exit-ladder designer
    reasons about. CASE-INSENSITIVE, and matched against the strings the code
    ACTUALLY emits (2026-07-25 audit) — the old one-liner tested lowercase
    substrings against raw reasons, so:
      * "hard SL -15.0%" (monitor.py option path) contains no lowercase "stop"
        and fell to `other`;
      * "TP1 +45% sell 50%" / "TP2 +60% sell all but runner" failed a lowercase
        "tp" test, so EVERY take-profit exit fell to `other`;
      * nothing in this repo ever books the word "target", so that family was
        unreachable — the entire options book graded as `other`.
    exits.py::_exit_evidence feeds these buckets straight to the Opus exit
    strategist, so the ladder was being tuned on mis-bucketed evidence.
    Emitted reasons covered: "hard stop -12.1%" / "trailing stop -15.1% from
    peak" (monitor equity), "hard SL -15.0%" / "TP1..TP3" / "trail X% from peak"
    / "expiry exit (N DTE)" (monitor options), "stop propagated from X (...)"
    (peer stops — bucketed by the ORIGIN's family, which is the mechanism being
    graded), "PM verdict SELL" / "underlying verdict SELL" / "rotation exit" /
    parking + seat-change reasons (all correctly `other`: no ladder decided
    them). Classified on the FULL reason — the old [:40] truncation was a
    display-only relic that could cut a family token off the end."""
    r = str(reason or "").lower()
    if "ratchet" in r:                     # "profit ratchet: +X% held of a +Y% peak"
        # Its own family (2026-07-26): the ratchet's give-back cap is a TUNABLE
        # (RATCHET_BOUNDS) and the review tunes it from THIS bucket's post-exit
        # excess — positive excess after ratchet exits means it cut winners
        # still running (raise the cap), deep negative means it banked tops
        # correctly (hold or lower). Folded into `trail` it would be invisible.
        return "ratchet"
    if "trail" in r:                       # incl. "trailing stop", "trail X% from peak"
        return "trail"
    if "tp1" in r or "tp2" in r or "tp3" in r or "target" in r or "take profit" in r:
        return "target"
    if "stop" in r or "hard sl" in r:      # "hard stop", "hard SL", "stop propagated"
        return "stop"
    if "expiry" in r:                      # time decay forced the exit, no ladder tier did
        return "expiry"
    return "other"


def stats() -> dict:
    _all_trades = state.load("ledger", {}).get("trades", [])
    # ONE exclusion, at the source (operator 2026-07-28). stats() builds eight
    # separate tables by looping over `trades`, and a row whose FILL PRICE never
    # traded is invalid evidence in every one of them — by_sleeve, by_strategy,
    # by_conviction, by_tailwind, by_mode included. Filtering per-table is eight
    # chances to miss one, which is exactly how the strategy-trade filter drifted
    # in 2026-07-25. The rows stay in the ledger; they just never reach a table.
    _artifacts = [t for t in _all_trades if t.get("mark_artifact")]
    trades = [t for t in _all_trades if not t.get("mark_artifact")]
    if not trades:
        return {"trades": 0}
    # HEADLINE numbers grade committee DECISIONS only (operator 2026-07-20:
    # "these numbers still need permanent fixing"): mechanical-strategy exits
    # (dual_momentum rotation) stay IN the ledger by design — test 5b,
    # 'rotation strategy feeds the learning loop', strategy-level review via
    # by_strategy — but a rank-rule exit the committee never decided must not
    # inflate the committee's win rate or P&L. The +190 EEM rotation exit and
    # +105 of de-dup unwinds were flattering the headline to 58.3%/+83 when
    # the honest decision record was 50%/-213.
    # `mark_artifact` is excluded here too (operator 2026-07-28). The degraded
    # bucket exists so a fabricated VERDICT still has somewhere honest to sit;
    # a fabricated FILL has nowhere — the decision was real, the price was not,
    # so counting it as a loss grades the committee for a bug in the quote
    # path. The four PRSU/STRZ/INNV rows moved the headline win rate and P&L by
    # $1,516 on trades whose exits never happened.
    decisions = [t for t in trades
                 if not _is_strategy_trade(t) and not t.get("mark_artifact")]
    wins = [t for t in decisions if (t.get("pnl") or 0) > 0]
    by_sleeve: dict[str, dict] = {}
    for t in trades:
        b = by_sleeve.setdefault(t["sleeve"], {"n": 0, "wins": 0, "pnl": 0.0})
        b["n"] += 1
        b["wins"] += 1 if (t.get("pnl") or 0) > 0 else 0
        b["pnl"] = round(b["pnl"] + (t.get("pnl") or 0), 2)
    # by_rating grades the COMMITTEE's own ratings, so it gets the SAME filter as
    # the headline (2026-07-25 audit): it iterated the RAW ledger, so a mechanical
    # rotation exit and a degraded quant fallback both landed in a real rating
    # tier — live, HOLD n=6 carried EEM's +190.42 dual-momentum rotation exit and
    # BUY n=13 carried TXN's degraded row (synthetic conviction 90, all-100 desk
    # scores). The headline comment nine lines above already forbids exactly this;
    # the filter had simply never been carried down to the per-rating table.
    # Degraded rows keep their OWN bucket (never dropped, never mixed), matching
    # verdict_report_card's degraded_fallback tier and by_strategy's naming.
    by_rating: dict[str, dict] = {}
    for t in trades:
        if _is_strategy_trade(t) or t.get("mark_artifact"):
            continue          # a fill that never traded cannot grade a rating
        v = t.get("verdict") or {}
        r = ("degraded_fallback" if v.get("degraded")
             else v.get("rating") or "no_verdict")
        b = by_rating.setdefault(r, {"n": 0, "wins": 0, "pnl": 0.0})
        b["n"] += 1
        b["wins"] += 1 if (t.get("pnl") or 0) > 0 else 0
        b["pnl"] = round(b["pnl"] + (t.get("pnl") or 0), 2)
    reentries = [t for t in trades if t.get("reentry_after_stop")]
    re_wins = [t for t in reentries if (t.get("pnl") or 0) > 0]
    by_strategy: dict[str, dict] = {}
    for t in trades:
        v = t.get("verdict") or {}
        strat = t.get("strategy") or (
            "degraded_fallback" if v.get("degraded")
            else "committee" if t.get("verdict") else "other")
        b = by_strategy.setdefault(strat, {"n": 0, "wins": 0, "pnl": 0.0})
        b["n"] += 1
        b["wins"] += 1 if (t.get("pnl") or 0) > 0 else 0
        b["pnl"] = round(b["pnl"] + (t.get("pnl") or 0), 2)
    # Conviction calibration (2026-06-10 audit): the sleeves gate capital on
    # 55/60/70 thresholds — the review must SEE whether conviction predicts
    # outcomes or the thresholds are decoration. DEGRADED verdicts are bucketed
    # separately (goal audit 4d): their conviction is a synthetic quant
    # composite, not a committee judgment — mixing them in would mis-calibrate
    # the very thresholds this table exists to tune (regret_audit already
    # filters degraded; calibration must match).
    by_conviction: dict[str, dict] = {}
    for t in trades:
        # A mechanical rotation exit carries whatever advisory verdict was last
        # journaled on the name, and the committee never gated capital on it —
        # letting it into a conviction band mis-calibrates the 55/60/70
        # thresholds this table exists to tune (2026-07-25 audit; degraded rows
        # already had their own band below and keep it).
        if _is_strategy_trade(t):
            continue
        v = t.get("verdict") or {}
        c = v.get("conviction")
        if not isinstance(c, (int, float)):
            continue
        band = ("degraded" if v.get("degraded")
                else "<60" if c < 60 else "60-69" if c < 70
                else "70-79" if c < 80 else "80+")
        b = by_conviction.setdefault(band, {"n": 0, "wins": 0, "pnl": 0.0})
        b["n"] += 1
        b["wins"] += 1 if (t.get("pnl") or 0) > 0 else 0
        b["pnl"] = round(b["pnl"] + (t.get("pnl") or 0), 2)
    by_tailwind: dict[str, dict] = {}
    for t in trades:
        key = "leading_sector_entry" if t.get("sector_tailwind") else "other_entry"
        b = by_tailwind.setdefault(key, {"n": 0, "wins": 0, "pnl": 0.0})
        b["n"] += 1
        b["wins"] += 1 if (t.get("pnl") or 0) > 0 else 0
        b["pnl"] = round(b["pnl"] + (t.get("pnl") or 0), 2)
    by_mode: dict[str, dict] = {}
    for t in trades:
        m = t.get("mode") or "paper"
        b = by_mode.setdefault(m, {"n": 0, "wins": 0, "pnl": 0.0})
        b["n"] += 1
        b["wins"] += 1 if (t.get("pnl") or 0) > 0 else 0
        b["pnl"] = round(b["pnl"] + (t.get("pnl") or 0), 2)
    # Exit-quality by reason (purpose review #7): avg POST-exit forward excess
    # per exit path — persistently POSITIVE post-exit excess on stop-family
    # exits is the evidence that the trail cuts winners too early (the review
    # may then tune it inside hard bounds; never by intuition)
    by_exit: dict[str, dict] = {}
    # Stop-quality split by VOLATILITY REGIME (2026-07-13, operator): a global
    # avg hides that high-vol names (MU) whipsaw out and bounce while calm names
    # (COST) don't — the review must see each regime's post-exit excess to tune
    # the per-regime sigma multiple, not one number for all.
    stop_vol: dict[str, dict] = {}
    for t in trades:
        # Same committee filter as the headline (2026-07-25 audit): a rotation
        # exit is a rank rule, not an exit-ladder decision, and a degraded row's
        # entry was never underwritten — neither belongs in the evidence that
        # tunes stops and the option ladder.
        if not _is_committee_trade(t):
            continue
        fam = _exit_family(t.get("exit_reason"))
        b = by_exit.setdefault(fam, {"n": 0, "post_n": 0, "post_sum": 0.0, "pnl": 0.0,
                                     "horizons": {}})
        b["n"] += 1
        b["pnl"] = round(b["pnl"] + (t.get("pnl") or 0), 2)
        px = t.get("post_exit_excess_pct")
        if isinstance(px, (int, float)):
            b["post_n"] += 1
            b["post_sum"] = round(b["post_sum"] + px, 2)
            # Per-horizon split (fix brief 2026-07-27 #5): a whipsaw shows a
            # positive 5-session excess that decays by 21; a correctly-banked
            # top stays negative at every horizon. The blended number alone
            # cannot separate them, and Job 2g tunes on that distinction.
            for _h in POST_EXIT_HORIZONS:
                _o = (t.get("post_exit") or {}).get(str(_h))
                if isinstance(_o, dict) and isinstance(_o.get("excess_pct"), (int, float)):
                    _hb = b["horizons"].setdefault(str(_h), {"n": 0, "sum": 0.0})
                    _hb["n"] += 1
                    _hb["sum"] = round(_hb["sum"] + _o["excess_pct"], 2)
            # `ratchet` joins trail/stop here (fix brief 2026-07-27 #5): the
            # ratchet is a give-back cap on the SAME vol-scaled exit mechanism,
            # so its whipsaw evidence belongs in the per-regime sigma tuning
            # table. Split out as its own FAMILY (2026-07-26) it would otherwise
            # have silently left stop_evidence_by_vol as the ladder widened.
            if fam in ("trail", "stop", "ratchet"):
                # AN UNKNOWN VOL IS NOT A MID VOL (fix brief 2026-07-28 #6).
                # config.vol_bucket(None) -> "mid" is the right fail-safe at
                # TRADE time (never size or stop a name as if it were extreme),
                # but in an EVIDENCE table it launders unknowns into a real
                # regime: all 7 rows of the only populated bucket were
                # vol_ann=None rows, one of them MU at 107 annualized vol. The
                # review then tunes the mid-vol sigma multiple on high-vol
                # whipsaws. Unknowns get their own labelled bucket so the
                # sample size of each REAL regime stays honest; _vol_ann_at
                # drains this bucket at the postmarket wrap.
                _va = t.get("vol_ann")
                _vb = (config.vol_bucket(_va)
                       if isinstance(_va, (int, float)) and _va > 0
                       else "unknown")
                sv = stop_vol.setdefault(_vb, {"n": 0, "post_sum": 0.0})
                sv["n"] += 1
                sv["post_sum"] = round(sv["post_sum"] + px, 2)
    # Mandate-relative outcomes: a trade that made money but LAGGED SPY over
    # its own holding window is a mandate loss — the review must judge in
    # excess terms or bull markets teach false lessons
    # ... and it must judge the COMMITTEE (2026-07-25 audit): this iterated the
    # raw ledger, so a dual-momentum rotation exit's index-relative return and a
    # degraded fallback's counted toward "did our judgment beat SPY".
    excess = [t["excess_ret_pct"] for t in trades
              if _is_committee_trade(t)
              and isinstance(t.get("excess_ret_pct"), (int, float))]
    vs_spy = {
        "trades_measured": len(excess),
        "beat_spy_rate_pct": round(
            sum(1 for x in excess if x > 0) / len(excess) * 100, 1)
        if excess else None,
        "avg_excess_ret_pct": round(sum(excess) / len(excess), 2)
        if excess else None,
    }
    # REALIZED-money factor attribution: split CLOSED trades at each factor's
    # median, compare the high half's avg SPY-relative excess vs the low half.
    # This is the verdict report card's twin on actual P&L — together they
    # tell the review whether momentum/value/quality earned real money, so a
    # factor tilt is driven by evidence, never by theory (operator 2026-06-13).
    # Committee-only (2026-07-25 audit): a factor tilt decided off a sample that
    # includes rank-rule rotation exits and degraded fallbacks would be tilting
    # on outcomes the research floor never chose — the opposite of the
    # evidence-driven intent stated above.
    by_factor: dict[str, dict] = {}
    for lens in ("momentum", "value", "quality", "revisions"):
        obs = [((t.get("factors") or {}).get(lens), t.get("excess_ret_pct"))
               for t in trades if _is_committee_trade(t)]
        obs = [(s, x) for s, x in obs
               if isinstance(s, (int, float)) and isinstance(x, (int, float))]
        if len(obs) < 6:
            continue
        obs.sort()
        mid = len(obs) // 2
        lo = [x for _, x in obs[:mid]]
        hi = [x for _, x in obs[-mid:]]
        by_factor[lens] = {
            "n": len(obs),
            "high_half_excess_pct": round(sum(hi) / len(hi), 2),
            "low_half_excess_pct": round(sum(lo) / len(lo), 2),
            "edge_pts": round(sum(hi) / len(hi) - sum(lo) / len(lo), 2)}
    return {
        "vs_spy": vs_spy,
        "by_strategy": by_strategy,
        "by_conviction": by_conviction,
        "by_factor": by_factor,
        "by_sector_tailwind": by_tailwind,
        "by_mode": by_mode,
        "by_exit_reason": {k: {**{kk: vv for kk, vv in v.items() if kk != "horizons"},
                               "avg_post_exit_excess_pct":
                               round(v["post_sum"] / v["post_n"], 2) if v["post_n"] else None,
                               # sessions -> avg SPY-relative excess after the exit
                               "avg_post_exit_excess_by_horizon":
                               {h: round(hb["sum"] / hb["n"], 2)
                                for h, hb in v["horizons"].items() if hb["n"]}}
                           for k, v in by_exit.items()},
        # INTENDED-vs-REALIZED stop depth (fix brief 2026-07-28 #7). Its twin
        # below asks "was the exit EARLY?"; this asks "did it fill WHERE it was
        # designed to?" — a stop can be correctly timed and still cost 17 extra
        # points, and only this number sees that.
        "stop_slippage": stop_slippage(trades),
        # stop/trail post-exit excess split by vol regime (low/mid/high): POSITIVE
        # in a regime = its stop is whipsawing (widen that regime's multiple)
        "stop_evidence_by_vol": {k: {"n": v["n"],
                                     "avg_post_exit_excess_pct":
                                     round(v["post_sum"] / v["n"], 2) if v["n"] else None}
                                 for k, v in stop_vol.items()},
        # Named so a shrinking sample can never look like a shrinking problem
        # — an excluded row is reported, never silently dropped.
        "excluded_mark_artifacts": {
            "n": len(_artifacts),
            "pnl": round(sum(t.get("pnl") or 0 for t in _artifacts), 2),
            "symbols": sorted({str(t.get("symbol")) for t in _artifacts}),
            "why": ("exit price outside the session's real high/low range — "
                    "see scripts/flag_mark_artifacts.py"),
        } if _artifacts else None,
        "trades": len(decisions),
        "strategy_trades": len(trades) - len(decisions),
        "win_rate_pct": (round(len(wins) / len(decisions) * 100, 1)
                         if decisions else None),
        "total_pnl": round(sum(t.get("pnl") or 0 for t in decisions), 2),
        "by_sleeve": by_sleeve,
        "by_rating": by_rating,
        "stop_out_reentries": {
            "n": len(reentries),
            "win_rate_pct": round(len(re_wins) / len(reentries) * 100, 1)
            if reentries else None,
            "pnl": round(sum(t.get("pnl") or 0 for t in reentries), 2),
        },
    }


LESSON_MAX_AGE_DAYS = 120
MAX_STALE_LESSONS = 5
CALIB_MIN_N = 8          # min verdicts per side before rating-calibration is decisive
# Price-fetch BATCH size for the report card (2026-07-25 audit) — a per-request
# vendor limit, NOT a population cap. The card fetches every graded ticker in
# chunks of this size and concatenates; it must never be used to slice the
# ticker list (that silently ungraded 66 of 180 aged verdicts, alphabetically).
FETCH_BATCH = 80


def _eod_marks_defective() -> bool:
    """True while the 'eod mark-to-close' health check is failing/poisoned or
    promoted into the standing-defects registry (2026-08-13 IG audit).

    Read-only reuse of the persisted health report + standing_defects state —
    the same pure-state-read pattern health._escalate_conviction_spread uses;
    never recomputes a sweep. Consumers (the edge-weight tilt in sleeves.py,
    the report card's provisional stamp below) defer learning-driven mutations
    while the close feed that grades them is untrustworthy. Fail-open (False)
    on any read error — a broken probe must not freeze learning forever."""
    try:
        if "eod mark-to-close" in (state.load("standing_defects", {}) or {}):
            return True
        _chk = next((c for c in (state.load("health", {}) or {}).get("checks", [])
                     if c.get("name") == "eod mark-to-close"), None)
        return bool(_chk is not None and not _chk.get("ok")
                    and not _chk.get("undetermined"))
    except Exception:
        return False


def verdict_report_card(max_cycles: int = 50, min_age_days: int = 3) -> dict:
    # max_cycles 12->50 (goal audit 4d): at ~2 cycles/day, 12 covered under a
    # week — the first review would have graded only days of judgments while
    # weeks of aged, gradable history sat unread. 50 = the full retained window.
    """Grade EVERY committee verdict — including the HOLDs, SELLs, and
    no-calls that never became trades — against what the name actually did
    next, SPY-relative. The ledger only grades what the firm OWNED; without
    this card, a committee that rates every future winner HOLD looks
    flawless because nothing it says is ever checked (2026-06-12 operator
    design request). 'Correct' means: BUY-family verdicts beat SPY forward,
    HOLD/SELL-family verdicts don't."""
    history = state.load("research", {}).get("history", [])
    now = datetime.now(timezone.utc)
    # Dedupe to ONE verdict per (ticker, calendar-day): two cycles on the same day
    # (a 9:45 run + a holdings re-review, say) judge the same name and would
    # otherwise double-count its IDENTICAL forward outcome into the rating buckets,
    # corrupting the calibration ladder (IG 2026-06-14: QH counted as both
    # STRONG_SELL and HOLD on 06-10, inflating both tiers off one +123% outlier).
    # history is oldest->newest, so the latest same-day verdict wins.
    deduped: dict = {}
    for h in history[-max_cycles:]:
        try:
            age_d = (now - datetime.fromisoformat(h["asof"])).days
        except (KeyError, ValueError, TypeError):
            continue
        if age_d < min_age_days:
            continue
        d0 = h["asof"][:10]
        ctx = h.get("context") or {}
        for t, v in (h.get("verdicts") or {}).items():
            v = v or {}                       # a None verdict value must not crash (matches regret_audit)
            c = ctx.get(t) or {}
            deduped[(t, d0)] = (d0, t, v.get("rating"), v.get("conviction"),
                                bool(c.get("had_news")), c.get("factors") or {},
                                # trust tier; old rows lack it -> infer from had_news
                                c.get("news_tier") or ("aggregator" if c.get("had_news") else "blind"),
                                bool(c.get("macro_news")),   # judged during a live geopolitical-news period?
                                # OWNED at verdict time (old back-data lacks it -> False, so a
                                # later purchase can't retro-flip a pass-HOLD into a KEEP)
                                bool(c.get("owned")),
                                # DEGRADED = quant-only fallback, not a committee judgment —
                                # calibrated separately (goal audit 4d, matches regret_audit)
                                bool(v.get("degraded")),
                                # [10] DECISION-TIME SCREEN SCORE (2026-07-28) — grades the
                                # SCREEN, not the judge. Appended last so every existing
                                # r[0..9] consumer is untouched. None on rows written before
                                # cycle.py started carrying it.
                                # READ FROM CONTEXT (operator 2026-08-19). This
                                # read the raw verdict dict, which has never
                                # carried the key — 0 of 1,467 retained rows —
                                # so screen_predictiveness was {} on every card
                                # ever produced while learning.py's own review
                                # prompt instructs the reviewer to tune
                                # QUANT_COMPRESS_* FROM that empty table. The
                                # writer now stamps it on the context row
                                # (cycle.py); the verdict fallback stays so a
                                # future writer that puts it back on the verdict
                                # cannot silently re-empty the surface.
                                c.get("quant_screen_score")
                                if c.get("quant_screen_score") is not None
                                else v.get("quant_screen_score"))
    # Overlapping-window dedup (A7): same name re-judged within ~one forward window
    # shares ~95% of its 21-day outcome — keep ONE per name per window so a
    # frequently re-judged (held/benched) name isn't counted many times as if each
    # were an independent observation. Keep the OLDEST per window (not the latest):
    # _fwd_excess measures up to 21 forward sessions, so the oldest row in a bucket
    # has the MOST-MATURE forward window — keeping the youngest would feed the
    # calibration ladder a noisy 2-3 session stub (change-audit verify, 2026-06-14).
    # Partitioned by (ticker, degraded) — a degraded row anchoring the window
    # would EVICT the later real verdict from the calibration ladder while
    # itself landing only in the degraded bucket (batch review R9: 58 live
    # real-verdict evictions in the retained history). Degraded and real
    # populations dedupe independently; they never share a ladder anyway.
    _by_tkr: dict = {}
    for _row in deduped.values():
        _by_tkr.setdefault((_row[1], _row[9]), []).append(_row)
    rows = []
    for _tkr, _rws in _by_tkr.items():
        _rws.sort(key=lambda r: r[0])                 # EARLIEST date first = most mature window
        _last = None
        for _r in _rws:
            try:
                _d = datetime.fromisoformat(_r[0])
            except (ValueError, TypeError):
                rows.append(_r)
                continue
            if _last is None or (_d - _last).days >= OVERLAP_WINDOW_DAYS:
                rows.append(_r)
                _last = _d
    if not rows:
        return {"note": f"no verdicts >= {min_age_days} days old yet — "
                        "the card fills in as judgments age",
                "rows_considered": 0, "graded": 0, "rows_dropped": 0}
    # BATCH the price fetch, never TRUNCATE it (2026-07-25 audit): this was
    # `sorted({...})[:80]`, a hard alphabetical cut masquerading as a batch size.
    # With 125 distinct names in the retained history, every ticker past the 80th
    # A-to-Z was absent from `closes`, _fwd_excess returned None for it, and the
    # verdict was skipped with no journal line — state/report_card_stats.json read
    # rows_considered=180, graded=114, and the 66-row gap WAS the cut (it swallowed
    # real losers OSCR/PRSU and every one of QQQ/SPY/SMH/SOXX/SOXL). Every
    # downstream surface — by_rating, rating_calibration, factor_predictiveness,
    # news_value, overlooked_winners, the judge digest, the health desk's
    # degraded/edge checks — was therefore fed an A-to-O biased sample, and because
    # the cutoff LETTER moves as the universe changes, the graded population
    # silently re-composed cycle to cycle. Chunked fetch = same per-call vendor
    # load, whole population graded.
    tickers = sorted({r[1] for r in rows} - {config.BENCHMARK})
    try:
        import pandas as pd

        from firm import data_engine
        _syms = [config.BENCHMARK] + tickers
        _batches = [_syms[i:i + FETCH_BATCH]
                    for i in range(0, len(_syms), FETCH_BATCH)]
        # A trailing 1-symbol batch comes back from yfinance single-indexed (a
        # "Close" column, not a ticker column) — pad it with the benchmark so
        # every batch is multi-symbol; the duplicate column is dropped below.
        if _batches and len(_batches[-1]) == 1:
            _batches[-1] = _batches[-1] + [config.BENCHMARK]
        _frames = [data_engine.fetch_history(b, period="6mo") for b in _batches]
        closes = _frames[0] if len(_frames) == 1 else pd.concat(_frames, axis=1)
        closes = closes.loc[:, ~closes.columns.duplicated()]
        spy = closes[config.BENCHMARK].dropna()
    except Exception:
        # rows_considered/graded/rows_dropped distinguish "dead grading pipeline"
        # (aged rows, zero graded) from "young sample" for the health desk (fix
        # brief 2026-07-17 #2: an empty card on 174 emitted verdicts looked
        # identical to a healthy young fund)
        return {"note": "price data unavailable",
                "rows_considered": len(rows), "graded": 0,
                "rows_dropped": len(rows)}

    def _fwd_excess(t: str, d0: str) -> float | None:
        if t not in closes.columns:
            return None
        s = closes[t].dropna()
        seg = s[s.index >= d0][:22]
        if len(seg) < 2 or float(seg.iloc[0]) <= 0:
            return None
        r = (float(seg.iloc[-1]) / float(seg.iloc[0]) - 1) * 100
        sp = spy[spy.index >= d0][:22]
        if len(sp) >= 2:
            r -= (float(sp.iloc[-1]) / float(sp.iloc[0]) - 1) * 100
        return r

    buys = ("BUY", "STRONG_BUY")
    by_rating: dict[str, dict] = {}
    by_screen: dict[str, dict] = {}   # screen-band predictiveness (2026-07-28)
    # Is news actually HELPING? Split verdict accuracy by whether the
    # committee saw headlines — if news-backed verdicts are no more accurate
    # than blind ones, the news input is noise, not signal (answers the
    # operator's "is that even beneficial?" with measured data, 2026-06-13)
    # 3-way by TRUST TIER (not just had-news): does reliable-wire news predict
    # while aggregator noise doesn't? correct% = reliability, avg excess = does
    # it MOVE things. trusted~=blind means even good news isn't being used well;
    # aggregator~=blind means aggregator news is noise to discount.
    by_news = {"trusted": {"n": 0, "correct": 0, "sum_excess": 0.0},
               "aggregator": {"n": 0, "correct": 0, "sum_excess": 0.0},
               "blind": {"n": 0, "correct": 0, "sum_excess": 0.0}}
    # Which FACTOR predicts winners? For each lens, collect (score, forward
    # excess) so we can compare high-scoring vs low-scoring names. This is how
    # the fund LEARNS whether value/quality add alpha before any capital moves
    # toward them (operator's "measure first" 2026-06-13).
    factor_obs = {"momentum": [], "value": [], "quality": [], "revisions": []}
    # Did judging DURING a live geopolitical/macro-news period help or hurt? (operator
    # 2026-06-15 — geopolitical news added to macro_brief + the learning curve.) Split
    # forward excess by whether geopolitical-news topics were active when judged.
    by_macro_news = {"geopolitical_period": {"n": 0, "correct": 0, "sum_excess": 0.0},
                     "quiet_period": {"n": 0, "correct": 0, "sum_excess": 0.0}}
    overlooked: list[dict] = []
    # An OWNED-name HOLD is "KEEP the winner", not "pass" — grade it correct when
    # the kept name OUTperforms (operator 2026-06-25: don't penalize holding a
    # winner that ran). Held winners that kept running are logged as MISSED ADDS so
    # the loop learns the too-strict-add behavior. CRITICAL: ownership is keyed to
    # VERDICT TIME (the `owned` flag persisted per row), NOT current holdings — a
    # name bought/sold AFTER an old HOLD must not retro-flip its grade or fabricate
    # a missed_add (adversarial review 2026-06-29).
    missed_adds: list[dict] = []
    _miss_thr = float(getattr(config, "MISSED_ADD_EXCESS_PCT", 8.0))
    graded = 0
    # Ungradable aged rows were an INVISIBLE loss until the 2026-07-25 audit: an
    # unpriceable ticker just `continue`d, so a truncated/failed fetch and a
    # genuinely young sample produced identical cards. Counted and returned so a
    # health check can assert COVERAGE (graded + rows_dropped == rows_considered),
    # not merely that grading produced something.
    dropped = 0
    for (d0, t, rating, conv, had_news, factors, news_tier, macro_news,
         owned_at, degraded, screen_q) in rows:
        x = _fwd_excess(t, d0)
        if x is None:
            dropped += 1
            continue
        graded += 1
        # SCREEN PREDICTIVENESS (2026-07-28). Grades the QUANT SCREEN itself, in
        # bands of its decision-time score. The screen chooses which names the
        # judge ever sees, so if its top band does not out-earn its lower bands
        # the ranking is decoration — and until now nothing measured it (the
        # score was never carried onto the graded row). This is also the
        # calibration evidence for QUANT_COMPRESS_BOUNDS: the log rescale made
        # "how hard to lean into extreme momentum tails" a bounded lever, and a
        # lever needs a signal. Degraded rows are excluded for the same reason
        # they are excluded everywhere else — a quant-only fallback grading the
        # quant screen is circular.
        if isinstance(screen_q, (int, float)) and not degraded:
            _band = ("top" if screen_q >= 88 else "high" if screen_q >= 84
                     else "mid" if screen_q >= 80 else "low")
            _sb = by_screen.setdefault(_band, {"n": 0, "sum_excess": 0.0,
                                               "wins": 0, "min": screen_q,
                                               "max": screen_q})
            _sb["n"] += 1
            _sb["sum_excess"] += x
            _sb["wins"] += 1 if x > 0 else 0
            _sb["min"] = min(_sb["min"], screen_q)
            _sb["max"] = max(_sb["max"], screen_q)
        # A DEGRADED verdict's rating/conviction are a synthetic quant composite
        # (goal audit 4d): observable in its OWN bucket, but never mixed into the
        # committee's rating/news/factor calibration — the 06-30 all-degraded
        # cycles would otherwise dominate the ladder with fabricated judgments.
        if degraded:
            b = by_rating.setdefault("degraded_fallback",
                                     {"n": 0, "correct": 0, "sum_excess": 0.0, "excess": []})
            b["n"] += 1
            b["sum_excess"] += x
            b["excess"].append(x)
            b["correct"] += 1 if (x > 0) == (rating in buys) else 0
            continue
        if owned_at and rating == "HOLD":
            correct = x > 0                  # KEEP: right when the held name beats SPY
            if x > _miss_thr:
                missed_adds.append({"ticker": t, "asof": d0, "conviction": conv,
                                    "fwd_excess_pct": round(x, 1)})
        else:
            correct = (x > 0) == (rating in buys)
        b = by_rating.setdefault(rating or "?",
                                 {"n": 0, "correct": 0, "sum_excess": 0.0, "excess": []})
        b["n"] += 1
        b["sum_excess"] += x
        b["excess"].append(x)
        b["correct"] += 1 if correct else 0
        nb = by_news.get(news_tier) or by_news["blind"]
        nb["n"] += 1
        nb["correct"] += 1 if correct else 0
        nb["sum_excess"] += x
        mb = by_macro_news["geopolitical_period" if macro_news else "quiet_period"]
        mb["n"] += 1
        mb["correct"] += 1 if correct else 0
        mb["sum_excess"] += x
        for lens, obs in factor_obs.items():
            sc = (factors or {}).get(lens)
            if isinstance(sc, (int, float)):
                obs.append((sc, x))
        # overlooked_winners = genuine PASS decisions the committee got wrong (should
        # have BOUGHT). An owned-HOLD KEEP that ran is NOT a pass — it belongs only in
        # missed_adds; double-listing it fed the judge contradictory signals (review
        # 2026-06-29).
        if rating not in buys and x > 5 and not (owned_at and rating == "HOLD"):
            overlooked.append({"ticker": t, "rated": rating,
                               "conviction": conv, "asof": d0,
                               "fwd_excess_pct": round(x, 1)})
    overlooked.sort(key=lambda m: -m["fwd_excess_pct"])

    def _factor_edge(obs: list) -> dict | None:
        """Split names at the median factor score; report avg forward excess
        of the high half vs the low half. A positive (high - low) gap means
        the factor is predictive FOR THIS FUND.

        THE LABEL MUST NEVER ORDER THE FEATURE (operator 2026-08-19).
        This line read `obs = sorted(obs)` over (score, excess) tuples. Python
        sorts tuples LEXICOGRAPHICALLY, so every group of names TIED on the
        factor score was ordered by its own forward excess — the outcome being
        measured — and the median split then handed the best-performing tied
        names to the "high half" and the worst to the "low half" BY
        CONSTRUCTION. The leak's size scales with the tie fraction, and the
        firm's screen saturates momentum: 75% of the 194 momentum observations
        share a score. Measured this session — buggy edge +14.3; with ties
        broken randomly instead, -2.7 with a 95% band of [-6.4, +1.3] over 2000
        resamples and a permutation p of 1.00. The strongest "edge" the firm
        had ever measured was an artifact of a sort.

        It was not inert. factor:momentum is the ONLY measured edge in the firm
        with a MONEY consumer: refresh_report_card_digest publishes it to
        edge_health, and sleeves._edge_weight_tilt gates on `val > 0` and sized
        min(2.0, 12.7/9) = +1.41 pts of EXTRA target weight onto essentially
        every alpha entry (22 of the 24 factor-retaining closed trades entered
        at momentum exactly 100) — in a book running -10.73 vs SPY. The true
        value is negative, so the correct tilt was 0.0 the whole time.

        Two changes, both conservative (they remove a fabricated signal; they
        loosen no risk control):
        (1) Sort by SCORE ONLY. Python's sort is stable, so ties keep their
            chronological insertion order, which is independent of the outcome.
        (2) Under heavy ties the median boundary falls INSIDE a tie block, so
            both halves carry the SAME score and the comparison is meaningless
            however the ties are broken. Split STRICTLY around the median score
            and, when either strict side is too thin, publish NO edge_pts at
            all. refresh_report_card_digest only emits an edge carrying a
            numeric edge_pts, so an undecidable factor drops out of edge_health
            and tilts nothing — no consumer change needed. A statistic that
            cannot discriminate must say so, not emit a number."""
        if len(obs) < 6:
            return None
        obs = sorted(obs, key=lambda t: t[0])       # score ONLY — never the label
        scores = [s for s, _ in obs]
        tie_frac = round(1.0 - len(set(scores)) / len(scores), 2)
        _min_side = int(getattr(config, "FACTOR_EDGE_MIN_SIDE", 3))
        # A CONSTANT factor cannot discriminate at all — say so, never score it.
        if len(set(scores)) < 2:
            return {"n": len(obs), "tie_fraction": tie_frac,
                    "undetermined": True, "high_half_n": 0, "low_half_n": 0,
                    "note": ("every observation carries the SAME factor score, "
                             "so the two halves are identical and no edge of "
                             "any size can be attributed to this factor")}
        # The tie block must sit entirely on ONE side of the cut, so the split
        # never depends on how ties are ordered. `>= med` keeps the whole
        # saturated block together and still compares it against the names
        # BELOW it — a real, outcome-independent contrast (live momentum: 59
        # names under 100 vs the 135 pinned at 100). A strict `> med` would
        # instead throw away the entire block and call a perfectly informative
        # two-level factor undecidable, which is how the first version of this
        # fix broke the section-26b fixture.
        med = scores[len(scores) // 2]
        lo = [x for s, x in obs if s < med]
        hi = [x for s, x in obs if s >= med]
        if len(lo) < _min_side or len(hi) < _min_side:
            # The median sits at the minimum (or near it) — cut on the middle
            # DISTINCT score instead so both sides carry real population.
            uniq = sorted(set(scores))
            cut = uniq[len(uniq) // 2]
            lo = [x for s, x in obs if s < cut]
            hi = [x for s, x in obs if s >= cut]
        if len(lo) < _min_side or len(hi) < _min_side:
            return {"n": len(obs), "tie_fraction": tie_frac,
                    "undetermined": True,
                    "high_half_n": len(hi), "low_half_n": len(lo),
                    "note": (f"{int(tie_frac * 100)}% of observations share a "
                             "factor score and the median split lands inside "
                             f"that tie block (strictly-below {len(lo)}, "
                             f"strictly-above {len(hi)}, floor {_min_side}) — "
                             "the two halves cannot be told apart, so no edge "
                             "is published and nothing is sized on it")}
        hi_avg, lo_avg = sum(hi) / len(hi), sum(lo) / len(lo)
        return {"n": len(obs),
                "tie_fraction": tie_frac,
                "high_half_n": len(hi), "low_half_n": len(lo),
                "high_half_excess_pct": round(hi_avg, 1),
                "low_half_excess_pct": round(lo_avg, 1),
                "edge_pts": round(hi_avg - lo_avg, 1)}

    factor_predictiveness = {lens: _factor_edge(obs)
                             for lens, obs in factor_obs.items()
                             if _factor_edge(obs)}

    # Rating CALIBRATION (IG 2026-06-14): correct_pct hides direction-magnitude
    # decoupling — a BUY can be "correct" (positive) yet realize LESS forward
    # excess than a "correct" SELL, i.e. the committee's directional signal is
    # inverted (its strongest sells outperform its buys). Assert the ladder is
    # monotone: buy-side forward excess must exceed sell-side. Uses the MEDIAN
    # (not the mean) so a single fat-tailed name can't fabricate an inversion
    # (the QH +123% outlier drove a false 'inverted' on the mean), AND guards on
    # min-n + single-name dominance so an early, tiny sample never cries wolf.
    def _median(xs: list) -> float | None:
        if not xs:
            return None
        s = sorted(xs)
        m = len(s) // 2
        return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2

    def _side(*ratings):
        xs = [x for r in ratings for x in by_rating.get(r, {}).get("excess", [])]
        return len(xs), _median(xs)
    n_buy, buy_med = _side("BUY", "STRONG_BUY")
    n_sell, sell_med = _side("SELL", "STRONG_SELL")
    rating_calibration = {"n_buy_side": n_buy, "n_sell_side": n_sell,
                          "buy_side_median_excess_pct": round(buy_med, 1) if buy_med is not None else None,
                          "sell_side_median_excess_pct": round(sell_med, 1) if sell_med is not None else None}
    if n_buy >= CALIB_MIN_N and n_sell >= CALIB_MIN_N and buy_med is not None and sell_med is not None:
        rating_calibration["inverted"] = sell_med > buy_med
        rating_calibration["note"] = (
            "INVERTED (by median, outlier-robust) — sell-rated names are "
            "outperforming buy-rated ones; the committee's directional read is "
            "broken, investigate before trusting convictions" if sell_med > buy_med
            else "monotone — buy-side median leads sell-side")
    else:
        rating_calibration["inverted"] = None
        rating_calibration["note"] = (f"insufficient sample (buy n={n_buy}, sell n={n_sell}; "
                                      f"need {CALIB_MIN_N} each) — not yet decisive")
    # PROVISIONAL STAMP (2026-08-13 IG audit): rows graded while the 'eod
    # mark-to-close' health check is failing/poisoned carry a close-feed
    # convention nobody trusts right now — stamp the card so consumers (edge
    # tilts, digests) can defer rather than learn from a poisoned period.
    _provisional = _eod_marks_defective()
    return {
        "fwd_window": "up to 21 trading days, SPY-relative",
        "provisional": _provisional,
        **({"provisional_reason": "'eod mark-to-close' health check is "
                                  "failing/poisoned — grades this period are "
                                  "suspect, defer weight/edge mutations"}
           if _provisional else {}),
        # Grading-pipeline liveness counters (fix brief 2026-07-17 #2): aged
        # rows with graded==0 = the pipeline is DEAD (price history empty /
        # vendor down), not a young sample — health asserts on these.
        # rows_dropped closes the accounting: graded + rows_dropped ==
        # rows_considered, so a coverage hole can never hide as "young sample".
        "rows_considered": len(rows), "graded": graded, "rows_dropped": dropped,
        # "correct" (raw count) + "excess" (raw list) are part of the public
        # shape: refresh_report_card_digest builds the base-rate and
        # losers-avoided lines from them (2026-07-02: the digest read keys the
        # card never returned — KeyError'd on first real cycle, judge blind)
        "by_rating": {k: {"n": b["n"], "correct": b["correct"],
                          "correct_pct": round(b["correct"] / b["n"] * 100, 1),
                          "avg_fwd_excess_pct": round(b["sum_excess"] / b["n"], 1),
                          "excess": [round(x, 1) for x in b.get("excess", [])]}
                      for k, b in by_rating.items() if b["n"]},
        # SCREEN bands, newest-first by strength. `spread_pts` is the top-vs-low
        # excess gap: positive means a higher screen score really did earn a
        # better forward return, so the ranking carries information; near zero or
        # negative means the screen is not discriminating and the compression
        # scale should be retuned rather than trusted.
        "screen_predictiveness": {
            k: {"n": v["n"], "score_range": [round(v["min"], 1), round(v["max"], 1)],
                "avg_fwd_excess_pct": round(v["sum_excess"] / v["n"], 1),
                "win_rate_pct": round(v["wins"] / v["n"] * 100, 1)}
            for k, v in by_screen.items() if v["n"]},
        "rating_calibration": rating_calibration,
        "news_value": {k: {"n": v["n"], "correct": v["correct"],
                           "correct_pct": round(v["correct"] / v["n"] * 100, 1),
                           "avg_fwd_excess_pct": round(v["sum_excess"] / v["n"], 1)}
                       for k, v in by_news.items() if v["n"]},
        "geopolitical_news_value": {k: {"n": v["n"], "correct": v["correct"],
                                        "correct_pct": round(v["correct"] / v["n"] * 100, 1),
                                        "avg_fwd_excess_pct": round(v["sum_excess"] / v["n"], 1)}
                                    for k, v in by_macro_news.items() if v["n"]},
        "factor_predictiveness": factor_predictiveness,
        "overlooked_winners": overlooked[:8],
        # Held winners that kept running without an add — the too-strict-add signal
        "missed_adds": sorted(missed_adds, key=lambda m: -m["fwd_excess_pct"])[:8],
    }


# Every journal event that MOVES a seat, mapped to what kind of decision it
# was. IG 2026-07-27 #6: the scorecard read only `etf_seat_switched`, so a
# seat's later history was invisible to it — a graded row stayed `still_open`
# forever once the seat had moved on by any OTHER mechanism, and the four
# corrective moves of 07-27 were never graded at all.
_SEAT_MOVE_EVENTS = {
    "etf_seat_switched": "rank",            # rank-won flip — the brake's input
    "etf_seat_incumbent_evicted": "eviction",   # rule enforcement
    "etf_seat_sold_on_verdict": "sold",         # committee SELL on a seat
}


def _seat_timeline(limit: int = 2500) -> list[dict]:
    """Every seat-MOVING event, oldest-first, with seat labels canonicalized by
    OCCUPANCY (IG 2026-07-27 #6).

    A switch closes when the SEAT moves on, so the scorecard needs each seat's
    full history, not just its rank flips. Two things made that history unusable
    before: the sell-on-verdict path emitted a different field pair
    (symbol/replacement instead of old/new), and it labeled core seats 0-based
    while review_seats labels them 1-based — so the 07-27 `core_slot_2`
    sell-on-verdict (really display slot 3, SMH -> SOXL) collided with the
    07-21 `core_slot_2` rank flip (QQQ -> USO).

    allocation.py now emits one label per seat, but the journal still holds the
    old rows, so the label is treated as a HINT and repaired against the chain:
    an event's outgoing symbol must be the seat's current occupant, and when it
    is not, the seat that DOES hold that symbol owns the event. Self-healing for
    legacy rows and for any future emitter drift; the reconstructed occupancy
    must land on the live seats file (that reconciliation is the test)."""
    raw = []
    for e in state.read_journal(limit):
        kind = _SEAT_MOVE_EVENTS.get(e.get("event") or "")
        if not kind:
            continue
        old = e.get("old") or e.get("symbol")
        new = e.get("new") or e.get("replacement")
        if not (old and new and e.get("ts")):
            continue
        raw.append({"ts": e["ts"], "seat": str(e.get("seat") or "?"),
                    "old": old, "new": new, "kind": kind})
    raw.sort(key=lambda r: r["ts"])           # read_journal returns newest-first
    occupant: dict[str, str] = {}
    for r in raw:
        seat = r["seat"]
        if occupant.get(seat) != r["old"]:
            owners = [s for s, occ in occupant.items() if occ == r["old"]]
            if len(owners) == 1:              # unambiguous -> repair the label
                seat = owners[0]
            # no owner: first event ever seen for this seat — the label stands
        r["seat"] = seat
        occupant[seat] = r["new"]
    return raw


def _live_seat_occupants() -> dict[str, str]:
    """The seats file as {canonical_seat_label: symbol} — the ground truth a
    `still_open` row must agree with. Core labels are 1-based to match
    allocation.review_seats."""
    try:
        from firm import allocation
        seats = allocation._load_seats()
    except Exception:
        return {}
    live = {}
    park = (seats.get("parking") or {}).get("symbol")
    if park:
        live["parking"] = park
    for i, s in enumerate((seats.get("core") or {}).get("slots") or []):
        if s:
            live[f"core_slot_{i + 1}"] = s
    return live


def _reconcile_open_seat_grades(graded: list) -> list:
    """Close any stored row that claims `still_open` while its seat has moved on
    (IG 2026-07-27 #6). The timeline above closes rows whose terminating event
    is still in the journal window; this is the durable backstop for rows whose
    events have ROTATED OUT — without it a row inherits `still_open: True`
    forever through the cumulative merge (exactly how `slot_1 VOO->SOXX` and
    `slot_3 GLD->SMH` still read open on 07-27 while slot_1 held EWT and slot_3
    held SOXX). A row may only stay open while its seat STILL holds the symbol
    that switch seated. The stored returns are left untouched and labeled — they
    were measured over an unbounded window and cannot be re-dated without the
    event that closed them."""
    live = _live_seat_occupants()
    if not live:
        return graded                          # no ground truth -> change nothing
    out = []
    for r in graded:
        if isinstance(r, dict) and r.get("still_open") and r.get("seat") in live:
            seated = str(r.get("switch") or "").split("->")[-1].strip()
            if seated and live[r["seat"]] != seated:
                r = dict(r)
                r["still_open"] = False
                r.setdefault("closed_by", "seat moved on")
                r["window"] = ("unbounded — graded while open, closed by "
                               "reconciliation against the live seats file")
        out.append(r)
    return out


def seat_switch_scorecard(max_switches: int = 12) -> dict:
    """Grade the ETF seat machinery's OWN decisions (operator 2026-07-20:
    'does this go into the learning loop too?'). Rotation/parking CLOSES
    already book P&L into the ledger, and routing feeds acceptance_report —
    but nothing graded a seat FLIP itself. For every etf_seat_switched /
    etf_seat_incumbent_evicted journal event, measure what the NEW seat
    returned vs what the EVICTED seat would have returned over the same
    window (to the seat's next switch, or to today while still open) — the
    exact counterfactual the momentum ranker bet on. Read-only, fail-soft;
    consumed by the IG snapshot so a rank engine that keeps losing its
    switches becomes findable data, not vibes."""
    try:
        from firm import data_engine
        # The FULL seat timeline, already sorted ascending and label-repaired
        # (see _seat_timeline). SORT ASCENDING BY TIMESTAMP, never trust list
        # order (2026-07-25 audit): state.read_journal returns NEWEST-FIRST
        # (state.py:249 reverses the tail), so an unsorted list resolved a
        # seat's window end to a timestamp BEFORE its own d0 — an empty price
        # segment, and every REPEAT flip of a seat silently failed to grade.
        moves = _seat_timeline()
        # A switch closes when ITS SEAT next moves — by rank flip, eviction, OR
        # sell-on-verdict (IG 2026-07-27 #6). Keying `until` off the rank-flip
        # list alone left a row open forever whenever the seat moved on by
        # another mechanism, which is every seat move that happened on 07-27.
        # Computed over the FULL timeline BEFORE any filtering or windowing: a
        # dropped event must still be able to end a kept row's window.
        for i, mv in enumerate(moves):
            nxt = next((m2 for m2 in moves[i + 1:] if m2["seat"] == mv["seat"]),
                       None)
            mv["until"] = nxt["ts"] if nxt else None
            mv["closed_by"] = nxt["kind"] if nxt else None
        # A rank flip whose winner was LATER evicted from the same seat as
        # unfit (the 07-20 SPY->SOXX misfire) was invalidated by the fund's own
        # rules; grading it would score a decision the machinery has already
        # disowned. Corrective moves ARE graded now — an eviction is an outcome,
        # arguably the most informative one — but they stay OUT of the
        # switches_graded/switch_wins aggregate the brake reads (operator
        # 2026-07-20: "these changes don't count" as ranking decisions).
        _invalidated = {(m["seat"], m["old"]) for m in moves
                        if m["kind"] == "eviction"}
        events = [m for m in moves
                  if not (m["kind"] == "rank"
                          and (m["seat"], m["new"]) in _invalidated)]
        # Window each family separately so a burst of corrective moves can never
        # starve the rank sample the brake depends on.
        _rank = [m for m in events if m["kind"] == "rank"][-max_switches:]
        _corr = [m for m in events if m["kind"] != "rank"][-max_switches:]
        events = sorted(_rank + _corr, key=lambda m: m["ts"])
        if not events:
            # NOT a zero card (audit 2026-07-26): returning switches_graded=0
            # here would DISARM the seat brake the moment the 07-20 flips age
            # out of the 2,500-line journal tail — permanently and silently,
            # since nothing re-derives them. Fall back to the CUMULATIVE
            # ledger: no new grades this pass, but history stands.
            return _seat_card(_merge_seat_grades([]),
                              "no rank-won seat switches in the journal window "
                              "(invalidated flips are excluded from grading); "
                              "counts are the cumulative graded ledger")
        syms = sorted({e["old"] for e in events} | {e["new"] for e in events})
        closes = data_engine.fetch_history(syms, period="6mo")
        rows, pending = [], []
        for ev in events:
            try:
                d0, d1 = ev["ts"][:10], (ev["until"] or "")[:10] or None

                def _ret(sym: str, _d0=None, _d1=None):
                    s_ = closes[sym].dropna()
                    seg = s_[s_.index >= (_d0 or d0)]
                    if _d1 or d1:
                        seg = seg[seg.index <= (_d1 or d1)]
                    if len(seg) < 2 or float(seg.iloc[0]) <= 0:
                        return None
                    return (float(seg.iloc[-1]) / float(seg.iloc[0]) - 1) * 100

                rn, ro = _ret(ev["new"]), _ret(ev["old"])
                if rn is None or ro is None:
                    # A move needs >=2 COMPLETED daily bars inside its window;
                    # a same-day flip (or one made today) has none yet. That is
                    # a DEFERRAL, not a decision to skip — surfaced on the card
                    # (IG 2026-07-27 #6 read four such moves as "ungraded" with
                    # nothing saying why). It grades on the next pass.
                    pending.append({"seat": ev["seat"],
                                    "switch": f"{ev['old']}->{ev['new']}",
                                    "on": d0, "kind": ev["kind"],
                                    "why": "fewer than 2 completed bars in the "
                                           "window (moved on/after the last "
                                           "completed bar)"})
                    continue
                edge = round(rn - ro, 2)
                rows.append({"seat": ev["seat"],
                             "switch": f"{ev['old']}->{ev['new']}", "on": d0,
                             # full switch ts -> the merge key (audit
                             # 2026-07-26); `on` alone collides when a seat
                             # flips twice in one day.
                             "ts": ev["ts"],
                             # `kind` splits the brake's rank sample from
                             # corrective moves (IG 2026-07-27 #6); legacy rows
                             # carry no kind and default to "rank", which is
                             # what they were.
                             "kind": ev["kind"],
                             "still_open": ev["until"] is None,
                             "closed_on": d1,
                             "closed_by": ev["closed_by"],
                             "new_ret_pct": round(rn, 2),
                             "old_ret_pct": round(ro, 2), "edge_pts": edge})
            except Exception:
                continue
        # Grade counts come from the CUMULATIVE ledger, not this pass's tail
        # slice — see _merge_seat_grades.
        return _seat_card(_merge_seat_grades(rows),
                          "edge_pts = new seat return minus what the evicted "
                          "seat would have returned over the same window; a row "
                          "closes when its SEAT next moves (rank flip, eviction "
                          "or sell-on-verdict), not when the symbol leaves the "
                          "book", pending)
    except Exception as e:
        return {"error": str(e)[:80]}


# Keep enough graded flips to outlive many journal rotations without letting
# the state file grow unbounded; the brake only ever needs the trailing record.
_SEAT_GRADES_MAX = 50


def _seat_grade_key(row: dict) -> str:
    """Stable identity of ONE graded flip: seat + the swap + the exact switch
    timestamp. A still-open flip is re-graded with a longer window every day,
    so the same key must OVERWRITE (fresher numbers), never duplicate."""
    return (f"{row.get('seat', '?')}|{row.get('switch', '?')}"
            f"|{row.get('ts') or row.get('on', '')}")


def _merge_seat_grades(new_rows: list) -> list:
    """Cumulative graded-flip ledger (audit 2026-07-26 — the seat brake could
    silently disarm itself). The grades were re-derived every pass from the
    newest 2,500 journal lines: once the 07-20 seat events scrolled out of
    that tail, switches_graded fell 3 -> 0, dropping permanently below
    SEAT_BRAKE_MIN_GRADED so the brake could never arm again — with no error
    and no journal line to notice. Journal rotation is a LOG concern; a graded
    decision is a LEARNING record and must outlive it. Merge by
    _seat_grade_key so re-grades update in place, keep the most recent
    _SEAT_GRADES_MAX, and never let a bad pass shrink history: an empty
    new_rows returns the stored ledger untouched."""
    merged = {}
    try:
        prior = state.load("seat_switch_scorecard", {}) or {}
        for r in (prior.get("graded") or []):
            if isinstance(r, dict):
                merged[_seat_grade_key(r)] = r
    except Exception:
        pass                                  # fail-soft: this pass's rows only
    for r in (new_rows or []):
        merged[_seat_grade_key(r)] = r
    out = sorted(merged.values(), key=lambda r: str(r.get("ts") or r.get("on") or ""))
    # Every read of the ledger passes the open rows against the LIVE seats file
    # (IG 2026-07-27 #6) — including the no-new-events path, which is precisely
    # when a stale `still_open` would otherwise survive another day.
    return _reconcile_open_seat_grades(out[-_SEAT_GRADES_MAX:])


def _seat_card(graded: list, note: str, pending: list | None = None) -> dict:
    """Build + persist the scorecard from the cumulative ledger. `graded` is
    persisted but kept OUT of the returned card — the IG snapshot embeds this
    return value, and it only needs the aggregates plus the last few rows.

    switches_graded / switch_wins / mean_edge_pts count RANK-WON flips only —
    they are the seat brake's input (allocation._switch_brake) and the operator
    ruled corrective moves out of the ranking record. Evictions and
    sell-on-verdict flips are graded and reported alongside (IG 2026-07-27 #6)
    so the machinery's corrective decisions stop being invisible, without
    letting a forced move brake the ranker."""
    rank = [r for r in graded if (r.get("kind") or "rank") == "rank"]
    corr = [r for r in graded if (r.get("kind") or "rank") != "rank"]
    n = len(rank)
    card = {"switches_graded": n,
            "switch_wins": sum(1 for r in rank if (r.get("edge_pts") or 0) > 0),
            "mean_edge_pts": (round(sum(r.get("edge_pts") or 0
                                        for r in rank) / n, 2) if n else None),
            "corrective_graded": len(corr),
            "corrective_wins": sum(1 for r in corr
                                   if (r.get("edge_pts") or 0) > 0),
            "corrective_mean_edge_pts": (
                round(sum(r.get("edge_pts") or 0 for r in corr) / len(corr), 2)
                if corr else None),
            # The open set is the claim that must reconcile with the seats file
            # — surfaced so a contradiction is readable, not buried in rows[].
            "open_switches": [f"{r.get('seat')} {r.get('switch')}"
                              for r in graded if r.get("still_open")],
            # Moves that REACHED the grader but had no completed bars yet — the
            # difference between "not graded" and "not gradable yet".
            "pending_grade": list(pending or []),
            "rows": graded[-8:], "note": note}
    _persist_seat_scorecard(card, graded)
    return card


def _persist_seat_scorecard(card: dict, graded: list | None = None) -> None:
    """Cache the seat-switch scorecard to state (operator 2026-07-21: 'swaps
    need to make sure they learn from themselves — these swaps must not keep
    losing money'). The 8:30 seat governor reads THIS cache to decide whether
    to tighten its own switch bar (allocation._switch_brake) — a pure state
    read, so the premarket seat path never fetches price history. Refreshed
    by every scorecard computation (daily IG snapshot). Errors never cached:
    a failed grade keeps the last good record rather than releasing the brake
    on a fetch hiccup.

    `graded` is the cumulative flip ledger (_merge_seat_grades). Omitting it
    CARRIES FORWARD whatever is already stored (audit 2026-07-26) — a caller
    that only refreshes the aggregate headline must never erase the history
    the brake depends on."""
    try:
        rec = {"asof": datetime.now(timezone.utc).isoformat(), **card}
        if graded is None:
            _prior = state.load("seat_switch_scorecard", {}) or {}
            graded = _prior.get("graded") or []
        rec["graded"] = graded
        state.save("seat_switch_scorecard", rec)
    except Exception:
        pass


def lessons_context() -> str:
    """Prompt block injected into the JUDGE call (2026-06-10 audit: no longer
    into the analyst desks — same prior applied 7x = echo chamber). SOFT
    expiry: every successful review re-stamps surviving lessons with
    added=now, so a lesson only ages past LESSON_MAX_AGE_DAYS when fewer
    than 5 trades closed in 4 months — a near-idle fund. Hard-dropping then
    would destroy knowledge exactly when there is no new evidence to replace
    it. Instead, stale lessons are demoted to a clearly labeled caution block
    (capped to the MAX_STALE_LESSONS most recently added) and the next review
    is instructed to re-confirm, re-caveat, or drop them — a rule learned in
    one regime must not steer the firm forever unexamined, but it must not
    vanish unexamined either."""
    lessons = state.load("lessons", {}).get("lessons", [])
    if not lessons:
        return ""
    cutoff = datetime.now(timezone.utc).timestamp() - LESSON_MAX_AGE_DAYS * 86400
    fresh, stale = [], []
    for x in lessons[:MAX_LESSONS]:
        added_ts = None
        try:
            added_ts = datetime.fromisoformat(x.get("added")).timestamp()
        except (ValueError, TypeError):
            pass  # unstamped/unparseable (pre-stamp lessons) -> treat as fresh
        if added_ts is not None and added_ts < cutoff:
            stale.append((added_ts, x))
        else:
            fresh.append(x)
    parts = ["FIRM MEMORY — lessons this firm learned from its OWN past trades. "
             "Weigh them in your judgment:"]
    parts += [f"- {x.get('lesson', '')}"
              + (" [provisional — verdict-evidence only, no closed trades yet]"
                 if x.get("provisional") else "")
              for x in fresh]
    if stale:
        stale.sort(key=lambda p: p[0], reverse=True)
        parts.append(
            f"STALE (unconfirmed in >{LESSON_MAX_AGE_DAYS} days of trading — "
            "weigh with caution, prefer fresh evidence):")
        parts += [f"- {x.get('lesson', '')}" for _, x in stale[:MAX_STALE_LESSONS]]
    return "\n".join(parts) + "\n\n"


def audit_bench(max_drops: int = 25) -> dict:
    """What happened AFTER the bench acted? Forward returns of dropped names
    (did we cut a future winner?) and how often promotions earn a BUY."""
    events = state.load("bench_ledger", {}).get("events", [])
    drops = [e for e in events if e.get("type") == "drop"][-max_drops:]
    promos = [e for e in events if e.get("type") == "promotion"]
    grads = [e for e in events if e.get("type") == "graduated"]
    stop_benched = [e for e in events if e.get("type") == "stop_out_benched"][-max_drops:]
    stop_recoveries = [e for e in promos if "stop-out recovery" in (e.get("why") or "")]

    # Price only the most recent 10 of each — keeps the audit cheap forever
    drops = drops[-10:]
    stop_benched = stop_benched[-10:]
    # Fixed 21-trading-day window, SPY-RELATIVE (2026-06-10 audit): variable
    # horizons + absolute returns made every drop look like a cut winner in
    # any up market — the honest question is "did it beat the index AFTER we
    # dropped it, over a comparable window?"
    fwd: dict[str, list[float]] = {}
    tickers = sorted({d["ticker"] for d in drops})

    def _fwd_rel(s, spy, start_date: str, window: int = 22) -> float | None:
        after = s[s.index >= start_date][:window]
        if len(after) < 2:
            return None
        ret = float(after.iloc[-1] / after.iloc[0] - 1) * 100
        if spy is not None:
            sp = spy[spy.index >= start_date][:window]
            if len(sp) >= 2:
                ret -= float(sp.iloc[-1] / sp.iloc[0] - 1) * 100
        return ret

    if tickers:
        try:
            from firm import data_engine
            closes = data_engine.fetch_history(
                sorted(set(tickers + [config.BENCHMARK])), period="6mo")
            spy = (closes[config.BENCHMARK].dropna()
                   if config.BENCHMARK in closes.columns else None)
            for d in drops:
                t = d["ticker"]
                if t not in closes.columns:
                    continue
                ret = _fwd_rel(closes[t].dropna(), spy, (d.get("ts") or "")[:10])
                if ret is not None:
                    reason_key = (d.get("reason", "other").split(":")[0])
                    fwd.setdefault(reason_key, []).append(round(ret, 1))
        except Exception:
            pass

    fwd_summary = {k: {"n": len(v), "avg_fwd_ret_pct": round(sum(v) / len(v), 1),
                       "max_missed_pct": round(max(v), 1)}
                   for k, v in fwd.items() if v}
    promo_grads = [g for g in grads if g.get("from_promotion")]
    buys = [g for g in promo_grads if g.get("rating") in ("BUY", "STRONG_BUY")]

    # Stop-out audit: were the stops right (name kept falling) or premature
    # (it recovered without us)? Re-entry P&L lives in the trade stats above.
    stop_fwd: list[float] = []
    stop_tickers = sorted({e["ticker"] for e in stop_benched})
    if stop_tickers:
        try:
            from firm import data_engine
            closes = data_engine.fetch_history(
                sorted(set(stop_tickers + [config.BENCHMARK])), period="6mo")
            spy = (closes[config.BENCHMARK].dropna()
                   if config.BENCHMARK in closes.columns else None)
            for e in stop_benched:
                t = e["ticker"]
                if t not in closes.columns:
                    continue
                ret = _fwd_rel(closes[t].dropna(), spy, (e.get("ts") or "")[:10])
                if ret is not None:
                    stop_fwd.append(round(ret, 1))
        except Exception:
            pass

    return {
        "fwd_window": "21 trading days, SPY-relative",
        "drops_analyzed": len(drops),
        "fwd_return_after_drop_by_reason": fwd_summary,
        "promotions": len(promos),
        "promotion_to_buy_rate_pct": round(len(buys) / len(promo_grads) * 100, 1)
        if promo_grads else None,
        "stop_outs": {
            "benched": len(stop_benched),
            "recovered_and_repromoted": len(stop_recoveries),
            "avg_fwd_return_after_stop_pct": round(sum(stop_fwd) / len(stop_fwd), 1)
            if stop_fwd else None,
        },
        "current_tuning": state.load("tuning", {}).get("bench", {}),
    }


def _apply_bench_tuning(raw: dict) -> dict | None:
    """Clamp Opus-proposed thresholds into BENCH_BOUNDS and persist them."""
    if not isinstance(raw, dict):
        return None
    out = {}
    for key, (lo, hi) in BENCH_BOUNDS.items():
        v = raw.get(key)
        if isinstance(v, (int, float)):
            out[key] = round(min(hi, max(lo, float(v))), 1)
    if not out:
        return None
    tuning = state.load("tuning", {})
    before = dict(tuning.get("bench", {}))
    tuning["bench"] = {**before, **out}
    tuning["bench_updated"] = datetime.now(timezone.utc).isoformat()
    state.save("tuning", tuning)
    state.journal("bench_tuned", before=before, after=tuning["bench"])
    return tuning["bench"]


def _file_fix_requests(requests: list) -> None:
    """The review found something only a CODE change can fix: file it as an
    engineering ticket for the operator, alert him, and write a paste-ready
    brief to state/FIX_BRIEFS.md — open Claude in ai-fund and say
    'read state/FIX_BRIEFS.md and fix item 1'."""
    items = [r for r in requests if isinstance(r, dict) and r.get("problem")][:5]
    if not items:
        return
    from firm import alerts
    with state.SLEEVE_LOCK:
        fb = state.load("fix_briefs", {"items": []})
        for r in items:
            r["filed_at"] = datetime.now(timezone.utc).isoformat()
        # RESOLUTION-AWARE EVICTION (operator 2026-07-28). The cap was a blunt
        # `[-10:]`, so filing 5 new briefs silently deleted the 5 oldest — and
        # on 07-28 those 5 were RESOLVED items carrying the entire written
        # record of why each was fixed or judged not-a-defect. The queue threw
        # away exactly the part that took the longest to produce. Rules now:
        #   1. an OPEN brief is never evicted — it is the work queue;
        #   2. resolved briefs are retired to `archive`, never dropped, so the
        #      reasoning survives even once the item leaves the active list;
        #   3. the cap applies to the ACTIVE list only.
        _all = (fb.get("items", []) + items)
        _cap = int(getattr(config, "FIX_BRIEF_MAX_ITEMS", 60))
        if len(_all) > _cap:
            _open = [r for r in _all if not r.get("resolved")]
            _done = [r for r in _all if r.get("resolved")]
            _keep_done = _done[-max(0, _cap - len(_open)):] if _cap > len(_open) else []
            _retired = [r for r in _done if r not in _keep_done]
            if _retired:
                fb["archive"] = (fb.get("archive", []) + _retired)[-500:]
                state.journal("fix_briefs_archived", n=len(_retired),
                              note="resolved briefs retired from the active "
                                   "list; reasoning preserved in fix_briefs.archive")
            _all = _keep_done + _open          # open items always survive
        fb["items"] = _all
        state.save("fix_briefs", fb)
        lines = ["# Fix Briefs — filed by the firm's own performance review",
                 "", "Open Claude Code in this folder and say: "
                 '"read state/FIX_BRIEFS.md and fix item N".', ""]
        for i, r in enumerate(fb["items"], 1):
            lines += [f"## Item {i} ({r.get('filed_at', '')[:10]})",
                      f"**Problem:** {r.get('problem', '')}",
                      f"**Evidence:** {r.get('evidence', '')}",
                      f"**Suspected area:** {r.get('suspected_area', '')}",
                      f"**Ask Claude:** {r.get('ask_claude', '')}"]
            # Triage outcome survives regeneration (2026-07-20): a resolved
            # brief that re-renders as OPEN reads as un-actioned forever.
            if r.get("resolved"):
                lines += [f"**Resolved:** {r['resolved']}"]
            lines += [""]
        (config.STATE_DIR / "FIX_BRIEFS.md").write_text("\n".join(lines),
                                                        encoding="utf-8")
    state.journal("fix_requests_filed", n=len(items),
                  problems=[r["problem"][:80] for r in items])
    alerts.alert("The firm found something to fix",
                 f"{len(items)} fix brief(s) filed: {items[0]['problem'][:120]} "
                 "— open Claude in ai-fund: 'read state/FIX_BRIEFS.md'")


BLOCK_EVENTS = ("buy_blocked_firm_cap", "buy_blocked_sector",
                "buy_blocked_correlation", "buy_blocked_liquidity",
                "buy_skipped_price_drift", "buy_skipped_earnings",
                "degraded_buy_blocked", "momentum_lane_skip",
                # goal audit fix 6: these caps are TUNABLE (CAP_BOUNDS) but their
                # blocks were invisible to the scorecard — retuning on zero evidence
                "buy_blocked_firm_sector", "buy_blocked_position_size",
                "buy_blocked_firm_cap_requote",
                # fix brief 2026-07-27 #9 — the caps audit attributed only 24 of
                # 35 rated BUYs because THIS tuple was a subset of what the entry
                # paths actually journal: the 11 names above cover ~40% of the
                # emitted block vocabulary. Everything below is a real, emitted
                # entry-side refusal (grepped from the journal writers across
                # firm/): a BUY that dies on any of them is explained, and a BUY
                # that matches NONE of them is the leak the review must see.
                # Book/slot pressure and its two resolutions (bench vs discard):
                "buy_skipped_book_full", "calls_skipped_book_full",
                "book_full_to_bench", "book_full_discarded",
                "book_full_admitted_parked_cash",
                # Cash / sizing / tradability:
                "buy_skipped_insufficient_cash", "buy_skipped_below_min_notional",
                "buy_skipped_no_price", "buy_skipped_kind_unresolved",
                "buy_skipped_swap_cooldown", "buy_skipped_premium_drift",
                "swap_rejected_margin", "swap_buy_undersized_after_sell",
                "add_skipped_size_zero", "rotation_buy_skipped_size_zero",
                "rotation_buy_leg_deferred", "rotation_buy_leg_unfunded",
                "add_blocked_liquidity", "add_blocked_sector_restricted",
                # Risk gates that are NOT caps (the post-stop cool-off, the
                # firm-level breaker, mode guards):
                "buy_blocked_stop_cooloff", "entries_blocked",
                "live_buy_blocked", "cross_mode_blocked",
                "satellite_skipped_alpha_overlap",
                "etf_seat_switch_blocked_convergence",
                # Options desk per-name refusals (calls AND puts):
                "calls_skipped_no_contract", "calls_skipped_already_held",
                "calls_skipped_size_zero", "calls_skipped_earnings_before_expiry",
                "calls_skipped_overnight_premium_cap",
                "puts_skipped_no_contract", "puts_skipped_size_zero",
                "puts_skipped_earnings_before_expiry",
                "puts_skipped_overnight_premium_cap",
                "options_skipped_not_tradable",
                # The at-the-open QUEUE: a BUY that never fills because the
                # market was shut, the queue expired, or a gate re-fired at the
                # flush is exactly as unowned as one a cap refused. NOTE
                # open_queue_filled / open_queue_partial_fill are deliberately
                # ABSENT — a fill is not a block, and counting it here would
                # credit the gate table with entries that actually opened.
                "order_blocked_market_closed", "entry_queued_for_open",
                "etf_seat_buy_queued_live", "open_queue_blocked",
                "open_queue_gate_blocked", "open_queue_add_gate_blocked",
                "open_queue_insufficient_cash", "open_queue_not_tradable",
                "open_queue_expired", "open_queue_failed", "open_queue_error",
                "open_queue_dropped_mode_flip", "open_queue_mechanical_deferred",
                "open_queue_requeued_research", "open_queue_drift_requeued",
                "open_queue_waiting_settle", "open_queue_skipped_option",
                "open_queue_already_held", "open_queue_add_base_gone",
                "open_queue_add_already_applied", "open_queue_requeue_failed",
                "open_queue_routing_patch_failed")


def _all_block_events() -> frozenset:
    """BLOCK_EVENTS unioned with routing's own block vocabulary (fix brief
    2026-07-27 #9). Two hand-maintained lists of the same thing is two chances
    to drift; routing._BLOCK_EVENTS is already kept current because a name
    missing from IT surfaces as a phantom SILENT DROP. Reading it here means a
    block newly taught to routing can never again be invisible to the caps
    audit. Lazy import + fail-soft: routing imports sleeves."""
    try:
        from firm import routing
        return frozenset(BLOCK_EVENTS) | frozenset(routing._BLOCK_EVENTS)
    except Exception:
        return frozenset(BLOCK_EVENTS)


def _block_symbols(e: dict) -> list[str]:
    """Every ticker one journaled block refers to. The writers are not uniform
    (fix brief 2026-07-27 #9): `symbol` is the common case, watchlist writes
    `ticker` (book_full_to_bench), and the sleeve book-full row writes a LIST
    under `symbols` — 27 of those were dropped by a reader that only looked at
    `symbol`, which is part of why blocked BUYs looked unattributed."""
    out = []
    for k in ("symbol", "ticker"):
        v = e.get(k)
        if isinstance(v, str) and v:
            out.append(v)
    v = e.get("symbols")
    if isinstance(v, (list, tuple)):
        out += [s for s in v if isinstance(s, str) and s]
    return list(dict.fromkeys(_underlying(s) for s in out))


def _fwd_excess(closes, spy, sym: str, start_iso: str,
                window: int = 22) -> float | None:
    """Forward SPY-relative return of one symbol from a date (<=21 sessions)."""
    try:
        if sym not in closes.columns:
            return None
        s = closes[sym].dropna()
        seg = s[s.index >= start_iso[:10]][:window]
        if len(seg) < 2 or float(seg.iloc[0]) <= 0:
            return None
        ret = float(seg.iloc[-1] / seg.iloc[0] - 1) * 100
        if spy is not None:
            sp = spy[spy.index >= start_iso[:10]][:window]
            if len(sp) >= 2:
                ret -= float(sp.iloc[-1] / sp.iloc[0] - 1) * 100
        return round(ret, 1)
    except Exception:
        return None


def blocked_audit(max_events: int = 150) -> dict:
    """What happened AFTER a risk gate said no? (2026-06-10 operator goal:
    'the learning loop should include how the caps result'.) Forward
    SPY-relative returns of every blocked/skipped entry tell the review
    whether each cap is avoiding losses or taxing alpha — the evidence
    cap_tuning proposals must cite.

    Also RECONCILES the committee's BUYs against those blocks (fix brief
    2026-07-27 #9): 14 of 35 rated BUYs never became positions while this
    table attributed only 24 blocks, so the leak was invisible. Every rated
    BUY that has no position and no ledger row now maps to a journaled gate
    or lands in the explicit `unattributed` bucket, graded on forward excess
    exactly like a gate. An unattributed leak is the FINDING — never silence.
    max_events 40 -> 150 for the same reason: the newest 40 rows are one busy
    cycle's worth of a ~70-name block vocabulary, so whole gates fell off the
    bottom of the window and read as zero blocks."""
    events = [e for e in state.read_journal(4000)
              if e.get("event") in _all_block_events() and _block_symbols(e)][:max_events]
    by_gate: dict[str, list[float]] = {}
    blocked_syms: set[str] = set()
    syms: set[str] = set()
    for e in events:
        for s in _block_symbols(e):
            syms.add(s)
            blocked_syms.add(s)
    # The unowned-BUY leak: rated BUY, no position, no ledger row (reuses the
    # SAME predicate regret_audit reports on, so the two surfaces can never
    # disagree about which names leaked).
    missed = _unowned_rated_buys()
    unattributed = {t: ts_ for t, ts_ in missed.items() if t not in blocked_syms}
    syms |= set(unattributed)
    if syms:
        try:
            from firm import data_engine
            closes = data_engine.fetch_history(
                sorted(syms | {config.BENCHMARK}), period="3mo")
            spy = (closes[config.BENCHMARK].dropna()
                   if config.BENCHMARK in closes.columns else None)
            for e in events:
                for s in _block_symbols(e):
                    r = _fwd_excess(closes, spy, s, e.get("ts") or "")
                    if r is not None:
                        by_gate.setdefault(e["event"], []).append(r)
            for t, ts_ in unattributed.items():
                r = _fwd_excess(closes, spy, t, ts_)
                if r is not None:
                    by_gate.setdefault("unattributed", []).append(r)
        except Exception:
            pass
    from firm import risk
    return {
        "fwd_window": "up to 21 trading days, SPY-relative",
        "by_gate": {k: {"n": len(v),
                        "avg_fwd_excess_pct": round(sum(v) / len(v), 1),
                        "missed_winners": sum(1 for x in v if x > 3),
                        "avoided_losers": sum(1 for x in v if x < -3)}
                    for k, v in by_gate.items() if v},
        # Loud, explicit reconciliation — the review must be able to say
        # "N BUYs died for a reason nobody recorded" instead of inferring it
        # from a gate table that quietly sums to less than the leak.
        "buy_attribution": {
            "buys_not_owned": len(missed),
            "attributed_to_a_gate": len(missed) - len(unattributed),
            "unattributed": len(unattributed),
            "unattributed_tickers": sorted(unattributed),
            "note": ("every rated BUY with no position and no ledger row is "
                     "either explained by a journaled gate above or listed "
                     "here; a non-empty unattributed list is an instrumentation "
                     "GAP in the verdict->fill path, not a risk decision — say "
                     "so in a lesson rather than tuning a cap for it"),
        },
        "current_caps": {k: risk.tuned_cap(k)
                         for k in {**config.CAP_BOUNDS,
                                   **getattr(config, "MOMENTUM_LANE_BOUNDS", {}),
                                   **getattr(config, "RATCHET_BOUNDS", {})}},
    }


def _rated_buys(max_cycles: int = 10) -> dict[str, str]:
    """ticker -> asof of the first non-degraded BUY/STRONG_BUY verdict in the
    last `max_cycles` research cycles."""
    history = state.load("research", {}).get("history", [])[-max_cycles:]
    rated: dict[str, str] = {}
    for h in history:
        for tick, v in (h.get("verdicts") or {}).items():
            if (v or {}).get("rating") in ("BUY", "STRONG_BUY") \
                    and not (v or {}).get("degraded") and tick not in rated:
                rated[tick] = h.get("asof", "")
    return rated


def _unowned_rated_buys(max_cycles: int = 10) -> dict[str, str]:
    """Rated BUYs the firm neither holds nor ever closed — the leak cohort.
    ONE predicate shared by regret_audit (which SIZES the leak) and
    blocked_audit (which ATTRIBUTES it), so the two surfaces can never
    disagree about which names leaked (fix brief 2026-07-27 #9)."""
    owned = {t.get("ticker") for t in state.load("ledger", {}).get("trades", [])}
    # Options rows book the CONTRACT as symbol but the underlying as ticker;
    # both are "we owned it" for regret purposes.
    owned |= {_underlying(t.get("symbol") or "")
              for t in state.load("ledger", {}).get("trades", [])}
    for sl in config.SLEEVES:
        owned |= {_underlying(p) for p in state.get_sleeve(sl).get("positions", {})}
    return {t: ts_ for t, ts_ in _rated_buys(max_cycles).items() if t not in owned}


def regret_audit() -> dict:
    """Selection regret: of the names the committee rated BUY/STRONG_BUY,
    how did the ones the firm did NOT end up owning perform? Beating SPY is
    the floor — this measures the distance between the fund and the best
    version of itself (2026-06-10 operator goal). blocked_audit.buy_attribution
    says WHY each of these never opened (fix brief 2026-07-27 #9)."""
    rated = _rated_buys()
    missed = _unowned_rated_buys()
    fwd: list[tuple[str, float]] = []
    if missed:
        try:
            from firm import data_engine
            closes = data_engine.fetch_history(
                sorted(set(missed) | {config.BENCHMARK}), period="3mo")
            spy = (closes[config.BENCHMARK].dropna()
                   if config.BENCHMARK in closes.columns else None)
            for t, ts_ in missed.items():
                r = _fwd_excess(closes, spy, t, ts_)
                if r is not None:
                    fwd.append((t, r))
        except Exception:
            pass
    return {
        "buys_rated": len(rated), "buys_not_owned": len(missed),
        "not_owned_avg_fwd_excess_pct": round(
            sum(r for _, r in fwd) / len(fwd), 1) if fwd else None,
        "biggest_miss": (max(fwd, key=lambda x: x[1]) if fwd else None),
    }


def _binding_trips() -> dict:
    """Breaker-trip telemetry for the review prompt, BINDING rows only (fix
    brief 2026-07-16 #5): etf_rotation runs through the loss breaker by design
    (ROTATION_IGNORES_LOSS_BREAKER) so its trips gate nothing — 5 of 7 rows in
    the audited window were advisory and read to the review as if real halts
    were churning. Advisory rows stay in state/breaker_trips for forensics;
    the review sees them only as an excluded count."""
    trips = state.load("breaker_trips", {}) or {}
    out: dict = {}
    n_adv = 0
    for day, rows in trips.items():
        keep = {sl: r for sl, r in (rows or {}).items()
                if not (r or {}).get("advisory")}
        n_adv += len(rows or {}) - len(keep)
        if keep:
            out[day] = keep
    if n_adv:
        out["_advisory_rows_excluded"] = n_adv
    return out


def _apply_cap_tuning(raw: dict) -> dict | None:
    """Clamp review-proposed risk caps into CAP_BOUNDS and persist them."""
    if not isinstance(raw, dict):
        return None
    out = {}
    # MOMENTUM_LANE_BOUNDS joined the allowed keys (AUDIT 2026-07-16 #9: the
    # largest blocked cohort — 228 momentum_lane_skip samples — fed a lever
    # _apply_cap_tuning could never move because only CAP_BOUNDS keys passed).
    _bounds = {**config.CAP_BOUNDS,
               **getattr(config, "MOMENTUM_LANE_BOUNDS", {}),
               **getattr(config, "RATCHET_BOUNDS", {}),
               **getattr(config, "ABS_STOP_BOUNDS", {}),
               **getattr(config, "QUANT_COMPRESS_BOUNDS", {})}
    for key, (lo, hi) in _bounds.items():
        v = raw.get(key)
        if isinstance(v, (int, float)):
            out[key] = round(min(hi, max(lo, float(v))), 2)
    if not out:
        return None
    tuning = state.load("tuning", {})
    before = dict(tuning.get("caps", {}))
    tuning["caps"] = {**before, **out}
    tuning["caps_updated"] = datetime.now(timezone.utc).isoformat()
    state.save("tuning", tuning)
    state.journal("caps_tuned", before=before, after=tuning["caps"])
    return tuning["caps"]


def _apply_stop_tuning(raw: dict) -> dict | None:
    """Clamp review-proposed equity stop/trail sigma multipliers into STOP_BOUNDS
    and persist (2026-07-13 learning audit): closes the open exit loop where the
    fund measured premature stops (by_exit_reason.avg_post_exit_excess_pct) but
    the 2.5/3.0-sigma stop had no mechanical tuning lever. sleeves._vol_stops
    live-reads tuning.json 'stops'. Same bounded, reversible contract as caps."""
    if not isinstance(raw, dict):
        return None
    bounds = getattr(config, "STOP_BOUNDS", {})
    buckets = {b for b, _, _ in getattr(config, "VOL_BUCKETS", ())}

    def _clamp_pair(d: dict) -> dict:
        o = {}
        for key, (lo, hi) in bounds.items():
            v = d.get(key)
            if isinstance(v, (int, float)):
                o[key] = round(min(hi, max(lo, float(v))), 2)
        return o

    out = _clamp_pair(raw)                       # global multipliers
    for b in buckets:                            # per-regime overrides
        if isinstance(raw.get(b), dict):
            bo = _clamp_pair(raw[b])
            if bo:
                out[b] = bo
    if not out:
        return None
    tuning = state.load("tuning", {})
    before = dict(tuning.get("stops", {}))
    merged = dict(before)
    for k, v in out.items():                     # merge per-regime dicts in place
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    tuning["stops"] = merged
    tuning["stops_updated"] = datetime.now(timezone.utc).isoformat()
    state.save("tuning", tuning)
    state.journal("stops_tuned", before=before, after=merged)
    return merged


def _benchmark_scoreboard() -> dict:
    """Per-sleeve since-inception return vs SPY over the same window — the
    one number the mandate is judged on, fed to every review."""
    out: dict = {}
    try:
        from firm import broker, data_engine
        for k in config.SLEEVES:
            s = state.get_sleeve(k)
            inception = s.get("inception") or ""
            if not inception:
                continue
            eq = broker.sleeve_equity(s)
            ret = (eq / s["starting_capital"] - 1) * 100
            spy_ret = data_engine.spy_return_since(
                inception, s.get("bench_inception_px"))
            # Capital PARKED in the benchmark (cash equitization) is waiting,
            # not working — the review must judge whether a persistently high
            # parked share is discipline (no qualifying setups in this tape)
            # or a conviction bar set too high to ever clear
            parked = sum((p.get("market_value") or p.get("cost") or 0)
                         for p in s.get("positions", {}).values()
                         if p.get("cash_equitization"))
            out[k] = {
                "return_pct": round(ret, 2),
                "parked_in_benchmark_pct": round(parked / eq * 100, 1) if eq > 0 else 0,
                "spy_pct": round(spy_ret, 2) if spy_ret is not None else None,
                "vs_spy_pct": round(ret - spy_ret, 2)
                if spy_ret is not None else None,
            }
    except Exception:
        pass
    return out


def _trades_span_days(trades: list) -> int:
    """Calendar span between the earliest and latest closed trade — so the first
    review can require the sample to spread across time (not one week), not just
    reach a count (2026-06-14 audit A7)."""
    ds = []
    for t in trades:
        for k in ("closed_at", "opened_at", "ts"):
            try:
                dt = datetime.fromisoformat(t[k])
                if dt.tzinfo is None:           # normalize so mixed naive/aware can't
                    dt = dt.replace(tzinfo=timezone.utc)  # crash the max/min comparison
                ds.append(dt)
                break
            except (KeyError, ValueError, TypeError):
                continue
    return (max(ds) - min(ds)).days if len(ds) >= 2 else 0


# Post-exit forward horizons in TRADING SESSIONS (fix brief 2026-07-27 #5).
# Three, not one: a stop that whipsaws shows a POSITIVE 5-session excess that
# has decayed by 21 (bounce), while a stop that sold a genuine top stays
# NEGATIVE at every horizon. One 21-day number cannot tell those apart, and
# Job 2g tunes the sigma multiple on exactly that distinction.
POST_EXIT_HORIZONS = (5, 10, 21)


def _post_exit_at(closes, spy, sym: str, d0: str, sessions: int) -> dict | None:
    """Post-exit return + SPY-relative excess over EXACTLY `sessions` trading
    days after `d0` (the exit date), or None if that many sessions have not
    printed yet. STRICT maturity is the point (fix brief 2026-07-27 #5): the
    general-purpose `_fwd_excess` silently returns whatever bars exist inside
    its window, so a trade closed three days ago would have been stamped as if
    it were a matured 21-session observation."""
    try:
        if sym not in closes.columns:
            return None
        s = closes[sym].dropna()
        seg = s[s.index >= d0[:10]]
        if len(seg) < sessions + 1 or float(seg.iloc[0]) <= 0:
            return None
        ret = float(seg.iloc[sessions] / seg.iloc[0] - 1) * 100
        exc = ret
        if spy is not None:
            sp = spy[spy.index >= d0[:10]]
            if len(sp) < sessions + 1 or float(sp.iloc[0]) <= 0:
                return None                  # no benchmark -> no EXCESS to record
            exc = ret - (float(sp.iloc[sessions] / sp.iloc[0] - 1) * 100)
        return {"ret_pct": round(ret, 2), "excess_pct": round(exc, 2)}
    except Exception:
        return None


def _exit_session_context(iso_ts: str) -> str:
    """WHERE in the trading day an exit filled — the variable that decides which
    fix a slippage number is even evidence for (fix brief 2026-07-28 #7).

      gap_at_open — filled in the first minutes of the regular session, i.e. the
                    position gapped THROUGH the stop overnight. No monitor
                    cadence can catch this; the lever is entry sizing/vol.
      extended    — filled in the 8:30-9:30 pre or post-close window, on thin
                    wide quotes. The lever is whether a stop may fire on an
                    extended-hours print at all.
      intraday    — filled during continuous regular trading. THIS is the only
                    bucket where evaluation cadence and fill latency are the
                    explanation.
    """
    try:
        from firm import market_hours as _mh
        ts = datetime.fromisoformat(str(iso_ts))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        et = ts.astimezone(_mh.ET)
        if _mh.market_is_open(ts):
            open_dt = et.replace(hour=_mh.OPEN_T.hour, minute=_mh.OPEN_T.minute,
                                 second=0, microsecond=0)
            secs = (et - open_dt).total_seconds()
            return "gap_at_open" if 0 <= secs <= 120 else "intraday"
        if _mh.extended_session_open(ts):
            return "extended"
        return "market_closed"
    except Exception:
        return "unknown"


def stop_slippage(trades: list[dict] | None = None) -> dict:
    """How far past its DESIGNED distance each stop/trail actually filled.

    fix brief 2026-07-28 #7 asked for `avg_stop_slippage_pct` in AGGREGATE
    STATS plus an answer to "are stops checked intraday or only at bar close?".
    The brief's own evidence list conflated three different things, so this
    separates them:

      * A WIDE stop is not slippage. MU filled at -17.6% and was listed as
        evidence of a broken stop engine — but MU ran 107% annualized vol, and
        2.5 sigma on 107 vol IS ~17%. That stop worked exactly as designed.
        Grading against the DESIGNED distance instead of a flat expectation is
        the whole point; `stop_pct` (stamped at entry) is the reference, and
        only where it is missing is a reconstruction used, labelled as such.
      * A trailing exit is designed as a give-back from the PEAK, so it is
        graded from the peak, never from entry.
      * WHERE it filled decides what the number means — see
        _exit_session_context. An average over all three buckets is a number
        with no fix attached to it.

    Negative slippage = filled DEEPER than designed. Read-only.
    """
    from firm import sleeves as _slv
    if trades is None:
        trades = (state.load("ledger", {}) or {}).get("trades") or []
    rows: list[dict] = []
    for t in trades:
        if not t.get("closed_at"):
            continue
        if t.get("mark_artifact"):
            # A fill that never traded cannot measure how far a stop slipped
            # (operator 2026-07-28) — and these rows are precisely the extreme
            # tail this table exists to explain. See _is_committee_trade.
            continue
        fam = _exit_family(t.get("exit_reason"))
        if fam not in ("stop", "trail", "ratchet"):
            continue
        entry, exit_px = t.get("entry"), t.get("exit")
        if not (isinstance(entry, (int, float)) and entry > 0
                and isinstance(exit_px, (int, float)) and exit_px > 0):
            continue
        is_trail = fam in ("trail", "ratchet")
        key = "trail_stop_pct" if is_trail else "stop_pct"
        intended, src = t.get(key), "stamped"
        if not isinstance(intended, (int, float)):
            # Reconstruct from the row's own vol via the SAME function the desk
            # sized it with, so design and grade cannot drift apart.
            cfg = config.SLEEVES.get(t.get("sleeve") or "alpha_stocks") or {}
            try:
                _s, _tr = _slv._vol_stops(cfg, t.get("vol_ann"))
                intended, src = (_tr if is_trail else _s), "reconstructed"
            except Exception:
                continue
        if not isinstance(intended, (int, float)) or intended <= 0:
            continue
        # A trailing exit's realized depth is measured from the PEAK. The peak
        # does not survive to the ledger, so a trail row is graded only when the
        # reason line carries the realized give-back the monitor computed.
        realized = None
        if is_trail:
            m = re.search(r"-(\d+(?:\.\d+)?)%\s+from peak", str(t.get("exit_reason") or ""))
            if m:
                realized = -float(m.group(1))
        else:
            realized = (exit_px / entry - 1) * 100
        if realized is None:
            continue
        rows.append({
            "symbol": t.get("symbol"), "family": fam, "source": src,
            "intended_pct": round(float(intended), 2),
            "realized_pct": round(realized, 2),
            # negative = deeper than designed
            "slippage_pct": round(realized + float(intended), 2),
            "vol_ann": t.get("vol_ann"),
            "context": _exit_session_context(str(t.get("closed_at"))),
            "closed_at": t.get("closed_at"),
        })
    if not rows:
        return {"n": 0, "avg_stop_slippage_pct": None,
                "note": "no graded stop/trail exits yet"}
    slips = [r["slippage_pct"] for r in rows]
    by_ctx: dict[str, dict] = {}
    for r in rows:
        b = by_ctx.setdefault(r["context"], {"n": 0, "sum": 0.0, "worst": 0.0})
        b["n"] += 1
        b["sum"] = round(b["sum"] + r["slippage_pct"], 2)
        b["worst"] = min(b["worst"], r["slippage_pct"])
    worst = sorted(rows, key=lambda r: r["slippage_pct"])[:4]
    return {
        "n": len(rows),
        "avg_stop_slippage_pct": round(sum(slips) / len(slips), 2),
        "median_stop_slippage_pct": round(sorted(slips)[len(slips) // 2], 2),
        "deeper_than_designed_n": sum(1 for s in slips if s < -0.5),
        "reconstructed_n": sum(1 for r in rows if r["source"] == "reconstructed"),
        "by_exit_context": {k: {"n": v["n"],
                                "avg_slippage_pct": round(v["sum"] / v["n"], 2),
                                "worst_pct": round(v["worst"], 2)}
                            for k, v in sorted(by_ctx.items())},
        "worst": [{k: r[k] for k in
                   ("symbol", "family", "intended_pct", "realized_pct",
                    "slippage_pct", "context", "source")} for r in worst],
        "note": ("slippage is realized MINUS designed; negative = filled deeper "
                 "than the design allowed. Read by_exit_context BEFORE the "
                 "headline: gap_at_open is overnight gap risk (no monitor "
                 "cadence catches it — the lever is entry sizing), extended is "
                 "a thin pre/post-market print, and only `intraday` is evidence "
                 "about evaluation cadence or fill latency."),
    }


def _vol_ann_at(closes, sym: str, d0: str, lookback: int = 30) -> float | None:
    """Annualized vol% from the `lookback` sessions of daily returns ENDING at
    the exit date — the vol regime the stop actually operated in.

    fix brief 2026-07-28 #6: `vol_ann` is stamped at ENTRY by broker.py and was
    only added on 2026-07-13, so 23 of 28 closed rows carry None. That is not a
    missing datum, it is a MISLABELLED one: config.vol_bucket(None) returns
    "mid" by design (a fail-safe that keeps an unknown out of an extreme bucket
    at TRADE time), so every unknown-vol exit was filed as mid-vol evidence.
    stop_evidence_by_vol — the table the review tunes the per-regime sigma
    multiple from — showed a single 'mid' bucket of n=7 whose members included
    MU at 107 vol. Widening the mid-vol stop on that is tuning one regime with
    another regime's whipsaws. Recomputing from the price series is a genuinely
    independent source, so it also survives the 300-row sleeve-record cap."""
    try:
        if sym not in closes.columns:
            return None
        s = closes[sym].dropna()
        seg = s[s.index <= d0[:10]]
        if len(seg) < lookback + 1:
            return None
        rets = seg.iloc[-(lookback + 1):].pct_change().dropna()
        if len(rets) < lookback // 2:
            return None
        sd = float(rets.std())
        if not (sd > 0):
            return None                      # flat/synthetic series — no signal
        return round(sd * (252 ** 0.5) * 100, 1)
    except Exception:
        return None


def backfill_post_exit_excess() -> int:
    """Exit-quality telemetry (2026-07-01 purpose review, rec #7): 100% of
    realized outcomes so far were decided by the trailing stop, and nothing
    measured what the exited names did NEXT — the one dataset that can ever
    justify (or refute) widening/ratcheting the trail. For each ledger row,
    record the symbol's post-exit 5/10/21-session return and SPY-relative
    excess: positive = the exit left money on the table; negative = the stop
    saved money. Runs at the postmarket wrap; idempotent (recomputes only the
    horizons a row is still missing, and saves only when something changed).

    fix brief 2026-07-27 #5 — WHY it never populated once in 21 closed trades:
    the gate was `age_d >= 30` CALENDAR days on an all-or-nothing 21-session
    stamp, and the oldest closed row in the book was 28 days old. Every row was
    rejected, so `post_exit_excess_pct` was None on 21/21 rows, which made
    by_exit_reason.post_n 0 for every family and left stop_evidence_by_vol
    (which only fills from rows that carry the stamp) an empty dict — Job 2g
    was unfalsifiable, exactly as the brief said. The fix: PER-HORIZON maturity
    measured in real printed sessions, so a 9-day-old exit contributes its
    5-session observation NOW and its 21-session one when it matures."""
    ledger = state.load("ledger", {}) or {}
    trades = ledger.get("trades") or []
    now = datetime.now(timezone.utc)
    todo = []
    for t in trades:
        if not t.get("closed_at"):
            continue
        try:
            age_d = (now - datetime.fromisoformat(t["closed_at"])).days
        except (ValueError, TypeError):
            continue
        # Cheap prefilter only: the SHORTEST horizon needs >= 5 sessions, which
        # cannot print in under 5 calendar days. The real gate is _post_exit_at.
        if age_d < min(POST_EXIT_HORIZONS):
            continue
        have = t.get("post_exit") or {}
        if any(str(h) not in have for h in POST_EXIT_HORIZONS):
            todo.append(t)
    # Vol backfill runs on its OWN maturity rule (fix brief 2026-07-28 #6): the
    # vol regime a stop fired in is knowable the instant the trade closes — it
    # must not wait on the 5-session post-exit window, or the row is already in
    # the evidence table (mislabelled 'mid') before its true bucket is known.
    # 120d ceiling: the 6mo fetch below cannot price a lookback older than that,
    # so retrying forever would just re-fetch for rows that can never resolve.
    def _closed_age_d(t: dict) -> int | None:
        try:
            return (now - datetime.fromisoformat(t["closed_at"])).days
        except (ValueError, TypeError, KeyError):
            return None

    todo_vol = [t for t in trades
                if t.get("closed_at")
                and not isinstance(t.get("vol_ann"), (int, float))
                and (_closed_age_d(t) is not None
                     and 0 <= _closed_age_d(t) <= 120)]
    if not todo and not todo_vol:
        return 0
    try:
        from firm import data_engine
        syms = sorted({_underlying(t["symbol"])
                       for t in (todo + todo_vol) if t.get("symbol")}
                      | {config.BENCHMARK})
        closes = data_engine.fetch_history(syms, period="6mo")
        spy = (closes[config.BENCHMARK].dropna()
               if config.BENCHMARK in closes.columns else None)
    except Exception:
        return 0
    done = 0
    for t in todo:
        # Option rows grade on the UNDERLYING: the contract itself expires and
        # has no post-exit series (HPE260731C00050000 -> HPE).
        sym = _underlying(t.get("symbol") or "")
        have = dict(t.get("post_exit") or {})
        changed = False
        for h in POST_EXIT_HORIZONS:
            if str(h) in have:
                continue
            obs = _post_exit_at(closes, spy, sym, t["closed_at"], h)
            if obs is not None:
                have[str(h)] = obs
                changed = True
        if not changed:
            continue
        t["post_exit"] = have
        # Flat mirror of the LONGEST matured horizon: by_exit_reason,
        # stop_evidence_by_vol and exits.py all read `post_exit_excess_pct`,
        # and a partially-matured trade must still be evidence rather than
        # silence. `post_exit_horizon_sessions` keeps the number honest about
        # which window produced it.
        best = max((h for h in POST_EXIT_HORIZONS if str(h) in have), default=None)
        if best is not None:
            t["post_exit_excess_pct"] = have[str(best)]["excess_pct"]
            t["post_exit_ret_pct"] = have[str(best)]["ret_pct"]
            t["post_exit_horizon_sessions"] = best
        done += 1
    vol_done = 0
    for t in todo_vol:
        v = _vol_ann_at(closes, _underlying(t.get("symbol") or ""),
                        t["closed_at"])
        if v is None:
            continue
        t["vol_ann"] = v
        # Provenance is part of the datum: a recomputed vol is a different
        # measurement from broker.py's entry stamp (30 sessions ENDING at the
        # exit vs the value observed at entry), and the review must be able to
        # tell them apart before it tunes a sigma multiple on either.
        t["vol_ann_source"] = "backfilled_30d_to_exit"
        vol_done += 1
    if done or vol_done:
        state.save("ledger", ledger)
    if done:
        state.journal("post_exit_excess_backfilled", n=done,
                      horizons=list(POST_EXIT_HORIZONS))
    if vol_done:
        state.journal("trade_vol_ann_backfilled", n=vol_done, lookback_sessions=30)
    return done


def acceptance_report(min_age_days: int = 3, max_events: int = 60) -> dict:
    """Grade the CROSS-SLEEVE ROUTING decisions on forward 21-session SPY-relative
    return (operator 2026-07-02: "route this into learning so the way stocks get
    accepted into each book matches what's best"). For every routed BUY name
    matured >= min_age_days: bucket the forward excess by the SLEEVE that PLACED
    it (and conviction band), and grade the COUNTERFACTUAL — names that QUALIFIED
    for a sleeve but were NOT placed. placed >> passed => the book's acceptance is
    picking the right names; passed >> placed => its bar is leaving alpha on the
    table. Reads the durable routing_history that routing.record_routing writes;
    feeds the judge digest and /api/routing. Pure measurement, no live change."""
    hist = (state.load("routing_history", {}) or {}).get("events", [])[-max_events:]
    if not hist:
        return {"note": "no routing history yet"}
    now = datetime.now(timezone.utc)
    obs, syms = [], set()
    for ev in hist:
        try:
            age = (now - datetime.fromisoformat(ev["asof"])).days
        except (KeyError, ValueError, TypeError):
            continue
        if age < min_age_days:
            continue
        d0 = ev["asof"][:10]
        for r in ev.get("rows", []):
            if r.get("conviction") is None:
                continue
            obs.append((d0, r["symbol"], r["conviction"],
                        r.get("placed") or [], r.get("qualified") or []))
            syms.add(r["symbol"])
    if not obs:
        return {"note": f"no routing decisions >= {min_age_days}d old yet"}
    try:
        from firm import data_engine
        closes = data_engine.fetch_history(
            sorted(syms | {config.BENCHMARK}), period="6mo")
        spy = closes[config.BENCHMARK].dropna()
    except Exception:
        return {"note": "price data unavailable"}

    def _fwd(t: str, d0: str):
        if t not in closes.columns:
            return None
        s = closes[t].dropna()
        seg = s[s.index >= d0][:22]
        if len(seg) < 2 or float(seg.iloc[0]) <= 0:
            return None
        r = (float(seg.iloc[-1]) / float(seg.iloc[0]) - 1) * 100
        sp = spy[spy.index >= d0][:22]
        if len(sp) >= 2:
            r -= (float(sp.iloc[-1]) / float(sp.iloc[0]) - 1) * 100
        return r

    def _band(c):
        c = c or 0
        return "<55" if c < 55 else "55-64" if c < 65 else "65-74" if c < 75 else "75+"

    def _avg(xs):
        return round(sum(xs) / len(xs), 1) if xs else None

    sleeves_all = ("alpha_stocks", "core_blend", "calls_only")
    placed_x = {sl: [] for sl in sleeves_all}
    passed_x = {sl: [] for sl in sleeves_all}
    band_x = {sl: {} for sl in sleeves_all}
    for d0, sym, conv, placed, qualified in obs:
        x = _fwd(sym, d0)
        if x is None:
            continue
        for sl in sleeves_all:
            if sl in placed:
                placed_x[sl].append(x)
                band_x[sl].setdefault(_band(conv), []).append(x)
            elif sl in qualified:
                passed_x[sl].append(x)
    by_sleeve = {}
    for sl in sleeves_all:
        if not placed_x[sl] and not passed_x[sl]:
            continue
        row = {"placed_n": len(placed_x[sl]),
               "placed_avg_fwd_excess_pct": _avg(placed_x[sl]),
               "passed_n": len(passed_x[sl]),
               "passed_avg_fwd_excess_pct": _avg(passed_x[sl]),
               "by_conviction": {b: {"n": len(xs), "avg_fwd_excess_pct": _avg(xs)}
                                 for b, xs in sorted(band_x[sl].items())}}
        p, q = _avg(placed_x[sl]), _avg(passed_x[sl])
        if (p is not None and q is not None
                and len(placed_x[sl]) >= 3 and len(passed_x[sl]) >= 3):
            row["edge_vs_passed_pts"] = round(p - q, 1)   # +ve = accepting well
        by_sleeve[sl] = row
    return {"fwd_window": "up to 21 trading days, SPY-relative",
            "n_decisions": len(obs), "by_sleeve": by_sleeve}


def refresh_report_card_digest() -> None:
    """Compute + cache a <=15-line digest of the verdict report card, once per
    cycle (2026-07-01 purpose review, rec #1a). The card grades every verdict
    on 21-session SPY-relative forward windows — corrective evidence the judge
    NEVER SAW (its only consumer was the trade-gated review, ~months out at
    2 closed trades). Cached in state so the judge-prompt injection is a pure
    read (no network in a worker thread); degrades to absent on data failure."""
    try:
        card = verdict_report_card()
        # Persist the grading heartbeat EVERY refresh — success or empty (fix
        # brief 2026-07-17 #2): the empty-card journal line below existed and
        # nobody read it; the health desk now asserts on THESE numbers (aged
        # verdicts with graded==0 = dead pipeline), which requires them to be
        # written even when — especially when — grading produced nothing.
        # Per-rating aggregates + calibration decidability persisted so the
        # health desk's sell-path-utilization check (fix brief 2026-07-21 #8)
        # is a pure state read — deep_neg_n counts graded rows whose forward
        # excess ran below -15% (the "should plausibly have been a SELL" tail).
        _calib = card.get("rating_calibration") or {}
        state.save("report_card_stats", {
            "asof": datetime.now(timezone.utc).isoformat(),
            "rows_considered": int(card.get("rows_considered") or 0),
            "graded": int(card.get("graded") or 0),
            # Coverage gap (2026-07-25 audit): aged rows the card could not price.
            # Persisted so the health desk reads it without recomputing the card —
            # a non-zero rows_dropped with a healthy `graded` is exactly the
            # silent-population-bias signature the alphabetical truncation had.
            "rows_dropped": int(card.get("rows_dropped") or 0),
            "note": str(card.get("note") or "")[:160],
            "by_rating_summary": {
                k: {"n": int(b.get("n") or 0),
                    "avg_fwd_excess_pct": b.get("avg_fwd_excess_pct"),
                    "deep_neg_n": sum(1 for x in (b.get("excess") or [])
                                      if isinstance(x, (int, float))
                                      and x < -15.0)}
                for k, b in (card.get("by_rating") or {}).items()},
            "calibration_undecidable": _calib.get("inverted") is None,
            "calibration_note": str(_calib.get("note") or "")[:160]})
        by_r = card.get("by_rating") or {}
        if not by_r:
            # LOUD empty (cognition audit #1b): an open feedback loop that says
            # nothing looks identical to a healthy one — the fund ran 22 days
            # with zero bytes of scorecard reaching the judge and no line said so
            state.journal("report_card_digest_empty",
                          note=str(card.get("note") or "no gradable rows"))
            return
        lines = ["YOUR OWN SCORECARD (forward 21-session returns vs SPY of your "
                 "past verdicts — judge yourself against it):"]
        _all_x: list[float] = []
        for r in ("STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"):
            b = by_r.get(r)
            if b and b.get("n"):
                lines.append(
                    f"- {r}: n={b['n']}, correct {b['correct']}/{b['n']}, "
                    f"avg fwd excess {b['avg_fwd_excess_pct']:+.1f}%")
                _all_x += [x for x in (b.get("excess") or [])
                           if isinstance(x, (int, float))]
        # Base rate (cognition audit #1d): without it, the winners-you-missed
        # list is an availability-bias engine — the judge can't tell its own
        # miscalibration from a bull tape where everything went up.
        if _all_x:
            _all_x.sort()
            _med = _all_x[len(_all_x) // 2]
            lines.append(f"- BASE RATE: median fwd excess of ALL graded names: "
                         f"{_med:+.1f}% (n={len(_all_x)})")
        # DECIDABILITY GATE (2026-07-26 forensics, CONFIRMED-MAJOR): the two
        # action-inducing blocks below — "winners you passed on" and the
        # HOLD-bar-too-conservative push — were shipping while the SAME card
        # stamped calibration_undecidable (buy n<8 / sell n<8). Effect, measured:
        # the judge converted the 60-64-conviction band from HOLD to BUY between
        # cycles #48 and #50 with the code-computed evidence components UNCHANGED
        # — cycle #51 hit 26/27 BUY, and ATEX (named in the passed-winners list)
        # was the first flip. Cherry-picked exemplars on an undecidable sample are
        # an availability-bias engine, not calibration. Neutral per-rating stats,
        # the base rate, and the symmetric losers-avoided line still ship.
        _decidable = _calib.get("inverted") is not None
        ov = card.get("overlooked_winners") or []
        if ov and _decidable:
            tops = sorted(ov, key=lambda x: -x.get("fwd_excess_pct", 0))[:3]
            _hold_n = int((by_r.get("HOLD") or {}).get("n") or 0)
            lines.append("- Winners you PASSED on (rated HOLD/SELL, then beat SPY): "
                         + ", ".join(f"{o['ticker']} {o['fwd_excess_pct']:+.0f}%"
                                     for o in tops)
                         + f" — {len(ov)} of {_hold_n} HOLDs; weigh against the "
                           "base rate above, not in isolation")
        # Quantified HOLD-calibration signal (2026-07-13 "earn-up"): when a large,
        # MEASURED share of HOLD calls went on to be real winners, the HOLD bar is
        # too high and the book hugs the benchmark. Surface the magnitude so the
        # committee widens conviction on genuinely strong setups. Strictly
        # evidence-gated (n + rate thresholds) and self-reversing — it goes quiet
        # the instant the HOLD cohort stops leaving winners on the table, and it
        # explicitly does NOT license lowering the bar on thin/conflicting evidence.
        _hold_all = [x for x in ((by_r.get("HOLD") or {}).get("excess") or [])
                     if isinstance(x, (int, float))]
        if _decidable and len(_hold_all) >= int(getattr(config, "HOLD_MISS_MIN_N", 15)):
            _win = float(getattr(config, "HOLD_MISS_WIN_PTS", 5.0))
            _big = sorted(x for x in _hold_all if x >= _win)
            _rate = len(_big) / len(_hold_all) * 100
            if _rate >= float(getattr(config, "HOLD_MISS_RATE_PCT", 25.0)):
                _med_big = _big[len(_big) // 2]
                lines.append(
                    f"- CALIBRATION (act on this): {_rate:.0f}% of your HOLD calls "
                    f"(n={len(_hold_all)}) went on to beat SPY by >= {_win:.0f}% "
                    f"(median winner {_med_big:+.0f}%). Your HOLD bar is measurably "
                    "too conservative and the book is hugging SPY. When the "
                    "fundamental + technical + factor evidence genuinely lines up, "
                    "COMMIT — issue BUY with a real target weight rather than "
                    "defaulting to HOLD. This is NOT license to lower the bar on "
                    "thin, conflicting, or momentum-only evidence; discipline on "
                    "weak setups still stands.")
        # Symmetric exemplars (#1d): passes that LAGGED — caution that was right
        _hold_x = [x for x in ((by_r.get("HOLD") or {}).get("excess") or [])
                   if isinstance(x, (int, float)) and x < 0]
        if _hold_x:
            lines.append(f"- Losers you CORRECTLY avoided: {len(_hold_x)} passes "
                         f"went on to lag SPY (avg {sum(_hold_x) / len(_hold_x):+.1f}%)")
        ma = card.get("missed_adds") or []
        if ma:
            lines.append("- Held winners you did not add to: "
                         + ", ".join(f"{o['ticker']} {o['fwd_excess_pct']:+.0f}%"
                                     for o in ma[:3]))
        nv = card.get("news_value") or {}
        if nv:
            _nl = [f"{k} {v['correct']}/{v['n']}" for k, v in nv.items() if v.get("n")]
            if _nl:
                lines.append("- Accuracy by news tier: " + ", ".join(_nl))
        # ACCEPTANCE QUALITY (operator 2026-07-02): show the committee how the
        # names it routes into each book actually perform forward — this is how
        # "the way stocks get accepted matches what's best" closes the loop, since
        # acceptance is driven by the CONVICTION the judge sets. edge_vs_passed>0
        # means the book accepted the right names; <0 means its bar left winners
        # on the table. Own try — an acceptance-data gap never sinks the digest.
        try:
            _acc = acceptance_report()
            _abs = (_acc or {}).get("by_sleeve") or {}
            _al = []
            for _sl, _r in _abs.items():
                _seg = f"{_sl.split('_')[0]} placed n={_r['placed_n']} " \
                       f"{_r['placed_avg_fwd_excess_pct']:+.1f}%" \
                    if _r.get("placed_avg_fwd_excess_pct") is not None else ""
                if _r.get("edge_vs_passed_pts") is not None:
                    _seg += f" (vs passed {_r['edge_vs_passed_pts']:+.1f}pts)"
                if _seg:
                    _al.append(_seg)
            if _al:
                lines.append("- ACCEPTANCE (fwd excess of names each book took, "
                             "vs ones it qualified but passed): " + "; ".join(_al))
        except Exception:
            state.journal("acceptance_digest_error")
        # Edge-integrity: an edge (factor OR news tier) can go anti-predictive.
        # Compute it BEFORE saving the digest so an inverted edge injects an
        # explicit DOWN-WEIGHT DIRECTIVE the judge reads and acts on — surfacing
        # it is the compensation (the judge IS the weighting function), which
        # turns the anti-predictive signal from an unhandled alarm into a
        # MANAGED risk (2026-07-02). Own try so an edge failure never takes the
        # judge's digest down with it.
        _edges, _inverted, _neutralized, _collinear = [], [], [], []
        try:
            for _lens, _fe in (card.get("factor_predictiveness") or {}).items():
                if (_fe and _fe.get("n", 0) >= 10
                        and isinstance(_fe.get("edge_pts"), (int, float))):
                    _edges.append({"edge": f"factor:{_lens}", "n": _fe["n"],
                                   "value": _fe["edge_pts"]})
            _blind = (nv.get("blind") or {})
            for _tier, _v in nv.items():
                if _tier == "blind" or _v.get("n", 0) < 8 or _blind.get("n", 0) < 8:
                    continue
                _edges.append({"edge": f"news:{_tier}-vs-blind", "n": _v["n"],
                               "value": round(_v["avg_fwd_excess_pct"]
                                              - _blind["avg_fwd_excess_pct"], 1)})
            # Collinearity de-dup (IG 2026-07-06 high, ratified): two edges whose
            # value AND n match within tolerance are the SAME variable measured
            # twice (e.g. 'geopolitical regime' == 'had a trusted/aggregator source',
            # n=25 both) and must not be double-counted in conviction. Keep the first
            # of each collinear group; record the rest so the weight is applied ONCE.
            _tol = float(getattr(config, "EDGE_COLLINEAR_TOL", 1.0))
            _ntol = int(getattr(config, "EDGE_COLLINEAR_N_TOL", 2))
            _kept: list = []
            for _e in _edges:
                _dup = next((k for k in _kept
                             if abs(k["value"] - _e["value"]) <= _tol
                             and abs(k["n"] - _e["n"]) <= _ntol), None)
                if _dup:
                    _collinear.append({"edge": _e["edge"], "duplicate_of": _dup["edge"]})
                else:
                    _kept.append(_e)
            # Auto-NEUTRALIZE (not merely down-weight) a NEWS tier measured NET-
            # HARMFUL over a real sample (IG 2026-07-06 high): value <= the
            # neutralize floor with n >= the min sample means it is not noise but
            # measurably value-destroying — the judge is told to IGNORE it entirely.
            _neu_pts = float(getattr(config, "NEWS_EDGE_NEUTRALIZE_PTS", -5.0))
            _neu_n = int(getattr(config, "NEWS_EDGE_NEUTRALIZE_MIN_N", 20))
            _neutralized = [e for e in _kept if e["edge"].startswith("news:")
                            and e["value"] <= _neu_pts and e["n"] >= _neu_n]
            _neu_edges = {e["edge"] for e in _neutralized}
            _inverted = [e for e in _kept if e["value"] <= -2.0]
            if _inverted:
                _news_inv = [e for e in _inverted if e["edge"].startswith("news:")]
                _fac_inv = [e for e in _inverted if e["edge"].startswith("factor:")]
                _hard = [e for e in _news_inv if e["edge"] in _neu_edges]
                _soft = [e for e in _news_inv if e["edge"] not in _neu_edges]
                _d = "SIGNAL DOWN-WEIGHT (measured this period, ACT on it): "
                if _hard:
                    _d += ("your NEWS read is measured NET-HARMFUL over a real sample ("
                           + ", ".join(f"{e['edge'].split(':')[1]} {e['value']:+.1f}pts "
                                       f"n={e['n']}" for e in _hard)
                           + ") — IGNORE the news tape for CONVICTION; decide on "
                           "fundamentals, technicals and factor evidence ALONE. ")
                if _soft:
                    _d += ("these news tiers are anti-predictive this period ("
                           + ", ".join(f"{e['edge'].split(':')[1]} {e['value']:+.1f}pts"
                                       for e in _soft)
                           + ") — weight headlines LOW. ")
                if _fac_inv:
                    _d += ("these FACTORS have been anti-predictive ("
                           + ", ".join(f"{e['edge'].split(':')[1]} {e['value']:+.1f}pts"
                                       for e in _fac_inv)
                           + ") — do not lean on them this period.")
                lines.append("- " + _d.strip())
        except Exception:
            state.journal("edge_health_failed")
        state.save("report_card_digest", {
            "asof": datetime.now(timezone.utc).isoformat(),
            "text": "\n".join(lines[:16]) + "\n\n"})
        # health.py reads this snapshot (pure state read every 30 min). The
        # digest carried the down-weight directive above, so an inverted edge
        # is a MANAGED risk, not an unhandled one.
        try:
            state.save("edge_health", {
                "asof": datetime.now(timezone.utc).isoformat(),
                "edges": _edges, "inverted": _inverted,
                "neutralized": _neutralized, "collinear": _collinear,
                "compensated": bool(_inverted)})
        except Exception:
            state.journal("edge_health_failed")
        # Degraded-cohort snapshot for the health desk (IG 2026-07-03 high,
        # ratified): the card already quarantines degraded verdicts into their
        # own bucket (buys blocked in sleeves._ranked_buys, rows excluded from
        # every evidence gate), but nothing ALARMED when the cohort ran far
        # below real verdicts (07-03: n=34, 32% correct, −12.2% excess).
        # health.py must never recompute the price-fetching card on its 30-min
        # sweep — persist the tiny numeric slice it needs, like edge_health.
        try:
            _br_dg = card.get("by_rating") or {}
            _dg = _br_dg.get("degraded_fallback") or {}
            _rl_n = sum(b.get("n", 0) for r, b in _br_dg.items()
                        if r != "degraded_fallback" and isinstance(b, dict))
            _rl_c = sum(b.get("correct", 0) for r, b in _br_dg.items()
                        if r != "degraded_fallback" and isinstance(b, dict))
            # Read the bucket's ALREADY-COMPUTED percentages (2026-07-16 fix of
            # IG items 3+8): the public by_rating shape carries correct_pct /
            # avg_fwd_excess_pct, NOT raw sum_excess — the old `_dg["sum_excess"]`
            # read KeyError'd on every populated bucket, journaled
            # degraded_health_failed (9x in the IG evidence), and the snapshot
            # NEVER persisted — leaving health's trailing-accuracy gate blind
            # while the degraded cohort ran 23-29% correct at -14 to -18% excess.
            state.save("degraded_health", {
                "asof": datetime.now(timezone.utc).isoformat(),
                "n": int(_dg.get("n") or 0),
                "correct_pct": (_dg.get("correct_pct")
                                if _dg.get("n") else None),
                "avg_fwd_excess_pct": (_dg.get("avg_fwd_excess_pct")
                                       if _dg.get("n") else None),
                "real_n": _rl_n,
                "real_correct_pct": (round(_rl_c / _rl_n * 100, 1)
                                     if _rl_n else None)})
        except Exception:
            state.journal("degraded_health_failed")
    except Exception:
        state.journal("report_card_digest_failed")


HELD_DET_MIN_CYCLES = 3       # consecutive BUY-family verdicts before it's a pattern
HELD_DET_MIN_DRAWDOWN = 5.0   # ...while the position is at least this far underwater


def held_deterioration(min_cycles: int = HELD_DET_MIN_CYCLES,
                       min_dd_pct: float = HELD_DET_MIN_DRAWDOWN) -> dict:
    """Names the committee keeps re-affirming while the position bleeds.

    Operator 2026-07-26: "grading with cycles should highlight — hey, still hold?
    or no sell by now!". Built because three separate hypotheses for the fund's
    universal BUY bias were tested and REFUTED (guidance asymmetry — fixed, no
    effect; missing fundamentals — false, packets carry 22-26 populated fields;
    ownership bias — false, held names actually score LOWER). Rather than guess a
    fourth cause, instrument the symptom: measure per name, per cycle, how long
    the committee has said BUY while the position went the other way.

    That makes an invisible bias a graded, falsifiable number — and the streaks
    feed straight back into the judge prompt, which is the part generic guidance
    could not do. "You have rated ATEX BUY for 4 consecutive cycles while it fell
    9.8%" is a specific, checkable fact about its OWN record; "use the full
    ladder" is an exhortation it can agree with and then ignore.

    Pure read (research history + live sleeves). Returns newest-first streaks.
    """
    hist = (state.load("research", {}) or {}).get("history") or []
    held: dict[str, dict] = {}
    for sl in ("alpha_stocks", "core_blend"):
        for sym, p in (state.get_sleeve(sl).get("positions") or {}).items():
            if p.get("cash_equitization") or p.get("instrument") != "equity":
                continue
            entry = p.get("entry_price") or 0
            last = p.get("last_price") or entry
            if entry <= 0:
                continue
            held.setdefault(sym, {"sleeves": [], "pnl_pct": (last / entry - 1) * 100,
                                  "entry": entry, "last": last})
            held[sym]["sleeves"].append(sl)

    BUYS = ("BUY", "STRONG_BUY")
    rows = []
    for sym, info in held.items():
        streak, convs, first_asof = 0, [], ""
        # newest-first: walk back until the verdict stops being BUY-family
        for h in reversed(hist):
            v = (h.get("verdicts") or {}).get(sym)
            if not isinstance(v, dict) or not v.get("rating"):
                continue
            if v.get("degraded") or v.get("rating") not in BUYS:
                break
            streak += 1
            first_asof = h.get("asof") or first_asof
            if isinstance(v.get("conviction"), (int, float)):
                convs.append(v["conviction"])
        if streak >= min_cycles and info["pnl_pct"] <= -min_dd_pct:
            rows.append({
                "ticker": sym, "sleeves": sorted(set(info["sleeves"])),
                "buy_streak": streak, "pnl_pct": round(info["pnl_pct"], 1),
                "avg_conviction": round(sum(convs) / len(convs), 1) if convs else None,
                "conviction_trend": (round(convs[0] - convs[-1], 1)
                                     if len(convs) > 1 else 0.0),
                "since": first_asof[:19],
            })
    rows.sort(key=lambda r: (r["pnl_pct"], -r["buy_streak"]))
    return {"asof": datetime.now(timezone.utc).isoformat(),
            "min_cycles": min_cycles, "min_drawdown_pct": min_dd_pct,
            "n_flagged": len(rows), "held_checked": len(held), "rows": rows}


def held_deterioration_context() -> str:
    """The tracker's streaks as a judge-prompt block — the fund's own record on
    the exact names it is about to re-underwrite. Empty string when nothing is
    flagged, so a healthy book adds no prompt noise."""
    try:
        d = state.load("held_deterioration", {}) or {}
        rows = d.get("rows") or []
        if not rows:
            return ""
        lines = "; ".join(
            f"{r['ticker']} — {r['buy_streak']} straight BUY verdicts while the "
            f"position went {r['pnl_pct']:+.1f}%" for r in rows[:6])
        return ("YOUR OWN RECORD ON NAMES YOU ALREADY HOLD — treat this as "
                "evidence about your judgement, not as pressure to sell: " + lines
                + ". A thesis re-affirmed while the position moves against you is "
                "either (a) still right and the market is wrong — then say so and "
                "name the specific evidence that survived, or (b) a thesis you "
                "have not re-tested since entry. If it is (b), the honest rating "
                "is HOLD or SELL. Do NOT re-affirm a BUY on momentum that has "
                "already reversed.\n\n")
    except Exception:
        return ""


def digest_context() -> str:
    """The cached scorecard digest for judge prompts — pure state read, fresh
    within 30h. The cycle refreshes it each morning (~24h cadence), so a healthy
    day always serves a <24h digest; the extra 6h is slack so a SINGLE ungradable
    cycle (a transient data_engine.fetch_history gap leaves by_rating empty →
    refresh_report_card_digest skips the save) doesn't blind the judge when a
    good digest from the prior day still exists. On 2026-07-08 the prior good
    07-07 digest aged ~7 min past a hard 24h during the outage and the judge went
    open-loop (judge_flying_blind); a 30h window covers a one-cycle hiccup + the
    daily cadence while still refusing a genuinely stale (>30h) scorecard."""
    d = state.load("report_card_digest", {}) or {}
    try:
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(d.get("asof", ""))).total_seconds() / 3600
        if age_h <= 30 and d.get("text"):
            return str(d["text"])
    except (ValueError, TypeError):
        pass
    return ""


def _card_for_prompt(card: dict) -> dict:
    """The card for PROMPT embedding: drop the raw per-name `excess` float
    lists (they exist for the digest's base-rate/losers-avoided math and would
    only bloat an Opus prompt with unlabeled numbers — 2026-07-02). Also drop
    the `degraded_fallback` bucket: it is an ADVISORY alarm cohort (outage
    fallback verdicts the firm never acted on — degraded_buy_blocked) consumed
    by degraded_health, NOT a real rating; its poor forward returns (~-10%
    excess) sitting labeled in a REVIEW prompt could skew the Opus reviewer's
    read of the real ratings. The raw verdict_report_card() output is untouched,
    so degraded_health still reads by_rating['degraded_fallback'] (IG 2026-07-09
    #4: it never reached calibration or the per-cycle judge digest — this closes
    the one residual leak into the periodic review prompts)."""
    out = dict(card)
    br = out.get("by_rating")
    if isinstance(br, dict):
        out["by_rating"] = {k: {kk: vv for kk, vv in b.items() if kk != "excess"}
                            if isinstance(b, dict) else b
                            for k, b in br.items() if k != "degraded_fallback"}
    return out


def _cross_model_refute(lessons: list) -> list:
    """Independent fresh review of newly distilled lessons.

    Office rule 11 now permits a separate process on the same model when the
    other provider is unavailable. The shared text router starts a fresh call
    with only this review packet and the fund doctrine, never the proposer's
    conversation or tools, and selects the strongest eligible subscription.

    A lesson dies only on refuted=true WITH a concrete contradicting reason,
    and at most half the batch can be killed. Failures preserve all proposed
    lessons. REVIEW_REFUTE_MODEL remains the enable switch and the exact-model
    standalone fallback when this host has no Office installation.
    """
    _model = str(getattr(config, "REVIEW_REFUTE_MODEL", "") or "")
    if not _model or not lessons:
        return lessons
    try:
        from firm import brain
        _numbered = [{"i": i, "lesson": (x.get("lesson") or x.get("text") or str(x))
                      if isinstance(x, dict) else str(x)}
                     for i, x in enumerate(lessons)]
        _prompt = (
            "You are an independent risk auditor in a fresh separate review run. "
            "Below are trading lessons a previous model run just "
            "distilled from a small ledger (n<30 trades, so overfitting to "
            "noise is the base-rate failure). For each, decide if it should be "
            "REFUTED: kill it only if it is (a) fit to too few samples to mean "
            "anything, (b) contradicted by evidence stated inside the batch "
            "itself, (c) regime-dependent but phrased as universal, or (d) a "
            "restatement of survivorship/selection rather than an edge. A "
            "plausible, modest, evidence-bounded lesson SURVIVES.\n\n"
            f"LESSONS:\n{json.dumps(_numbered, indent=1)}\n\n"
            'Reply with ONLY JSON: {"verdicts": [{"i": <int>, "refuted": '
            '<true|false>, "reason": "<one line naming the evidence, required '
            'when refuted>"}]}')
        shared = brain._office_module("model_text")
        if shared is None:
            _res = brain.ask_json(_prompt, model=_model, retries=1)
        else:
            _res = None
            review_prompt = _prompt
            for attempt in range(2):
                result = shared.generate(review_prompt, system=brain._full_system_prompt(),
                                         timeout=config.CLAUDE_TIMEOUT_S)
                if result.status != "ok" or not result.text:
                    brain._note_failure(str(result.detail or ""))
                    state.journal("lesson_refute_unavailable", status=result.status,
                                  model=result.model, detail=str(result.detail or "")[:500])
                    return lessons
                _model = result.model
                _res = brain.extract_json(result.text)
                if isinstance(_res, dict) and isinstance(_res.get("verdicts"), list):
                    break
                _res = None
                review_prompt = _prompt + "\n\nIMPORTANT: respond with ONLY the specified JSON object."
            if _res is None:
                state.journal("lesson_refute_unavailable", status="invalid_json", model=_model)
                return lessons
        _v = {d.get("i"): d for d in (_res or {}).get("verdicts", [])
              if isinstance(d, dict)}
        _killed = [i for i, x in enumerate(lessons)
                   if _v.get(i, {}).get("refuted") is True
                   and str(_v.get(i, {}).get("reason") or "").strip()]
        if not _killed or len(_killed) > len(lessons) / 2:
            if len(_killed) > len(lessons) / 2:
                state.journal("lesson_refute_overkill_ignored",
                              killed=len(_killed), total=len(lessons),
                              model=_model)
            return lessons
        kept = [x for i, x in enumerate(lessons) if i not in _killed]
        state.journal("lessons_cross_model_refuted", model=_model,
                      killed=[{"i": i,
                               "reason": str(_v[i].get("reason"))[:120]}
                              for i in _killed],
                      kept=len(kept), proposed=len(lessons))
        return kept
    except Exception as exc:
        try:
            state.journal("lesson_refute_unavailable", status="error", detail=str(exc)[:500])
        except Exception:
            pass
        return lessons          # the refuter must never cost the fund its review


def verdict_review() -> dict | None:
    """VERDICT-ONLY review track (2026-07-01 purpose review, rec #1b): the
    fund's fastest-accumulating evidence is graded VERDICTS (~10-15/day), not
    closed trades (~2/3wk) — waiting for the 20-trade money-gate leaves the
    judge's systematic HOLD-bias uncorrected for months while the proof sits
    computed. This track writes ADVISORY LESSONS ONLY (max 3, flagged
    provisional): it never touches cap/bench tuning, and it uses its OWN
    watermark — writing `reviewed_through` here would flip the money-review's
    first_review flag and silently collapse the 20-trade anti-overfit gate to
    5 (adversarial verify 2026-07-01, hazard 1)."""
    if not getattr(config, "VERDICT_REVIEW", True):
        return None
    st = state.load("lessons", {"lessons": [], "reviewed_through": 0})
    try:
        last = st.get("verdict_reviewed_asof", "")
        age_d = ((datetime.now(timezone.utc) - datetime.fromisoformat(last))
                 .total_seconds() / 86400) if last else 1e9
    except (ValueError, TypeError):
        age_d = 1e9
    if age_d < float(getattr(config, "VERDICT_REVIEW_EVERY_DAYS", 7)):
        return None
    card = verdict_report_card()
    by_r = card.get("by_rating") or {}
    # Gate on NON-degraded graded rows only (verify correction 1: degraded rows
    # are quarantined evidence and must not open an evidence gate)
    _real_rows = sum(b.get("n", 0) for r, b in by_r.items()
                     if r != "degraded_fallback")
    _buyside = sum(b.get("n", 0) for r, b in by_r.items()
                   if r in ("BUY", "STRONG_BUY"))
    if (_real_rows < int(getattr(config, "VERDICT_REVIEW_MIN_ROWS", 60))
            or _buyside < int(getattr(config, "VERDICT_REVIEW_MIN_BUYSIDE", 10))):
        return None
    if not brain.available():
        return None
    prompt = (
        "You are the HEAD OF PERFORMANCE REVIEW at an institutional asset "
        "manager. No trades have closed yet in volume, but the committee's "
        "VERDICTS have been graded against 21-session forward SPY-relative "
        "returns — judgment quality is measurable before P&L is. Study the "
        "report card and distill AT MOST 3 short, durable, actionable lessons "
        "about the committee's JUDGMENT (rating calibration, conviction usage, "
        "what it systematically over/under-rates). Write each lesson as an "
        "OPERATING RULE the judge can apply at the moment of rating, in the "
        "form: 'WHEN <the concrete, recognizable setup this card proves the "
        "committee misjudges> THEN <what to do differently in that verdict> "
        "(evidence: <the specific numbers>)'. A diagnosis without a WHEN/THEN "
        "correction is not a lesson — the judge cannot act on 'the committee "
        "under-rates winners', but it CAN act on 'WHEN momentum is top-half "
        "and the draft verdict is HOLD with conviction >=60, THEN write the "
        "explicit bull case before settling'. These are ADVISORY lessons "
        "for the judge prompt only — do not propose parameter changes, cap "
        "changes, or sizing rules (those wait for closed-trade evidence). "
        "If the evidence is genuinely mixed, return fewer lessons or none.\n\n"
        f"VERDICT REPORT CARD:\n{json.dumps(_card_for_prompt(card), indent=1, default=str)}\n\n"
        'Respond with ONLY this JSON:\n{"lessons": ["<lesson 1>", ...]}')
    # ask_json, NOT ask (cognition audit 2026-07-01): ask() takes `timeout`
    # (not timeout_s) and returns STR — the original call raised a swallowed
    # TypeError, so the track was dead on arrival. The §43 test hid it by
    # mocking brain.ask with an assumed contract (mock-vs-real, again).
    res = brain.ask_json(prompt, timeout=config.CLAUDE_TIMEOUT_S)
    if not isinstance(res, dict) or not isinstance(res.get("lessons"), list):
        state.journal("verdict_review_failed")
        return None
    now = datetime.now(timezone.utc).isoformat()
    fresh = [{"lesson": str(x)[:400], "added": now, "provisional": True,
              "source": "verdict_review"}
             for x in res["lessons"][:3] if str(x).strip()]
    # Replace prior provisional verdict-lessons; never displace trade-earned ones
    kept = [x for x in st.get("lessons", [])
            if not (x.get("provisional") and x.get("source") == "verdict_review")]
    st["lessons"] = (kept + fresh)[:MAX_LESSONS]
    st["verdict_reviewed_asof"] = now          # OWN watermark — reviewed_through untouched
    state.save("lessons", st)
    state.journal("verdict_review_done", lessons=len(fresh),
                  rows=_real_rows, buyside=_buyside)
    return {"lessons": len(fresh)}


def run_review(force: bool = False) -> dict | None:
    """Opus performance review: distill durable lessons from the ledger."""
    ledger = state.load("ledger", {"trades": []})
    trades = ledger.get("trades") or []
    total = ledger.get("total_recorded", len(trades))
    lessons_st = state.load("lessons", {"lessons": [], "reviewed_through": 0})
    n_new = total - lessons_st.get("reviewed_through", 0)
    if not trades and not getattr(config, "VERDICT_REVIEW", True):
        return None
    if not force:
        first_review = lessons_st.get("reviewed_through", 0) == 0
        gate_blocked = (
            (first_review and (n_new < COLD_START_MIN_TRADES
                               or _trades_span_days(trades) < COLD_START_MIN_SPAN_DAYS))
            or (not first_review and n_new < REVIEW_MIN_NEW_TRADES))
        if gate_blocked or not trades:
            # The trade-gated money review can't fire yet — run the verdict-only
            # advisory track instead (its own gates + throttle inside)
            try:
                verdict_review()
            except Exception:
                state.journal("verdict_review_error")
            return None
    if not brain.available():
        return None

    recent = trades[-40:]
    bench = audit_bench()
    bounds_txt = json.dumps({k: list(v) for k, v in BENCH_BOUNDS.items()})
    cap_bounds_txt = json.dumps(
        {k: list(v) for k, v in {**config.CAP_BOUNDS,
                                 **getattr(config, "MOMENTUM_LANE_BOUNDS",
                                           {})}.items()})
    stop_bounds_txt = json.dumps({k: list(v) for k, v in
                                  getattr(config, "STOP_BOUNDS", {}).items()})
    prompt = (
        "You are the HEAD OF PERFORMANCE REVIEW at an institutional asset "
        "manager. Below are this firm's own closed trades (with the committee "
        "verdict that drove each one), aggregate stats, a WATCHLIST BENCH "
        "AUDIT (what happened to names after we dropped or promoted them), "
        "and the current lesson list.\n\n"
        "AMBITION MANDATE: beating SPY is the FLOOR, not the target. Use the "
        "CAPS AUDIT (forward outcome of every entry a risk gate refused) and "
        "the REGRET AUDIT (committee BUYs the firm never owned) to close the "
        "gap between realized and ACHIEVABLE performance — push on timid "
        "sizing, over-tight gates, and conviction thresholds that filter out "
        "winners, while never excusing uncompensated risk. The best version "
        "of this firm is the standard; adequacy is failure. Watch "
        "parked_in_benchmark_pct in the scoreboard: capital parked in SPY is "
        "waiting, not working — a persistently high parked share means the "
        "committee finds nothing, and you must judge from the verdict history "
        "whether that is DISCIPLINE (this tape offers no qualifying setups) "
        "or a conviction bar set too high to ever clear. If candidate after "
        "candidate is rated HOLD with conviction in the 50s-60s while the "
        "names keep performing, the bar is the problem.\n\n"
        "Job 1 — LESSONS: distill the most durable, actionable lessons: desk "
        "biases, which verdict patterns make money, which exit reasons recur "
        "on losers, what conviction levels are trustworthy. Merge, rewrite, or "
        "drop stale/contradicted lessons. Specific and falsifiable only. "
        "Write each lesson as an OPERATING RULE applicable at the moment of "
        "decision: 'WHEN <concrete setup> THEN <what to do differently> "
        "(evidence: <numbers>)' — a diagnosis without a WHEN/THEN correction "
        "is not actionable. "
        "Any CURRENT LESSON whose 'added' stamp is older than "
        f"{LESSON_MAX_AGE_DAYS} days is shown to the judge as STALE — "
        "explicitly re-confirm, re-caveat, or drop each one; do not leave it "
        "unexamined. "
        "ADVERSARIAL VALIDATION (mandatory): before keeping ANY lesson, "
        "actively try to REFUTE it — check whether it only held in one market "
        "regime (use the sector-regime history and trade dates). Drop lessons "
        "that are regime artifacts, or caveat them explicitly ('in up-trending "
        "tape only'). A lesson learned in a bubble and applied in a bear "
        "market is how firms die — do not let FIRM MEMORY become an echo "
        "chamber of one regime.\n\n"
        "Job 2 — BENCH TUNING (optional): if the audit shows the bench is "
        "cutting names too early (big forward returns after drops) or holding "
        "them too long (drops going nowhere, low promotion-to-BUY rate), "
        "propose new thresholds. Only propose a change when the evidence "
        f"supports it. Allowed keys and hard bounds: {bounds_txt}\n\n"
        "Job 2b — CAP TUNING (optional): the firm's risk caps are LEARNABLE "
        "within hard bounds. The CAPS AUDIT shows the forward SPY-relative "
        "outcome of every entry a gate refused: a gate whose blocks keep "
        "OUTPERFORMING is taxing alpha (loosen it); one whose blocks keep "
        "underperforming is earning its keep (hold or tighten). Only propose "
        "changes the audit evidence supports — n>=5 per gate before judging. "
        f"Allowed keys and hard bounds: {cap_bounds_txt}\n\n"
        "Job 2c — FACTOR EVIDENCE (momentum + value + quality): this is a "
        "momentum-screened fund now MEASURING whether value and quality also "
        "predict winners. Read both `factor_predictiveness` (verdict report "
        "card) and `by_factor` (realized closed-trade P&L): each splits names "
        "at a factor's median and shows the high-half vs low-half SPY-relative "
        "edge. If a factor shows a durable POSITIVE edge across BOTH (n>=6, "
        "consistent), write a LESSON telling the committee to weight it (e.g. "
        "'cheap + high-quality names have beaten SPY by X pts — favor value "
        "when momentum is comparable'); if a factor shows NO edge or a "
        "negative one, say so and tell the committee NOT to pay up for it. "
        "Your lever is the JUDGE's reasoning via lessons — there is no "
        "auto-reweight of the screen, by design, until the evidence is durable "
        "(do NOT over-fit to a handful of trades or one regime).\n\n"
        "Job 2d — NEWS RELIABILITY (operator: 'is this source reliable / does "
        "this news move things / is it a miss to act on?'): the report card's "
        "`news_value` now splits verdict accuracy AND avg forward SPY-excess by "
        "news TIER — trusted (a wire/desk source), aggregator (only low-trust "
        "sources), blind (no news). Compare them (n>=6 each): if TRUSTED-backed "
        "verdicts beat blind but AGGREGATOR-backed do not, write a LESSON to "
        "weight trusted-wire headlines and DISCOUNT aggregator noise; if even "
        "trusted news adds no edge, tell the committee to treat news as "
        "risk/catalyst CONTEXT only, never a standalone buy signal. This is how "
        "the fund LEARNS which news is signal vs noise — via the judge's "
        "reasoning (lessons), not a hard filter.\n\n"
        "Job 2e — RATING CALIBRATION (the most important directional check): the "
        "report card's `rating_calibration` compares buy-side vs sell-side forward "
        "SPY-excess. If `inverted` is true (sell-rated names are OUTPERFORMING "
        "buy-rated ones with sufficient sample), the committee's directional "
        "signal is broken — its convictions are anti-predictive. Write a CRITICAL "
        "lesson naming the likely cause (over-bearish judge, momentum chasing tops, "
        "a desk lens inverted) and tell the committee to re-examine. If `inverted` "
        "is null (insufficient sample), do NOT act — the fund is too young to "
        "conclude; note it as a watch item only. Never over-fit to a handful of "
        "verdicts.\n\n"
        "Job 2f — GEOPOLITICAL-NEWS CONTEXT: the report card's "
        "`geopolitical_news_value` splits forward SPY-excess by whether the "
        "committee judged DURING a live geopolitical/macro-news period (war, Fed, "
        "tariffs, OPEC, etc.) vs a quiet one. With enough sample (n>=6 each), if "
        "verdicts made during geopolitical-news periods do markedly WORSE, write a "
        "lesson telling the committee to size down / demand higher conviction when "
        "macro headlines are hot (event risk dominates stock-specific theses); if "
        "they do as well or better, note the committee handles event risk fine. "
        "Insufficient sample -> watch only.\n\n"
        "Job 2f2 — STOP SLIPPAGE (read BEFORE Job 2g): `stop_slippage` grades "
        "each stop/trail against the distance it was DESIGNED with, split by "
        "WHERE it filled. A wide stop is not slippage — 2.5 sigma on a 107-vol "
        "name IS ~17%, so judge depth against `intended_pct`, never against a "
        "flat expectation. Only the `intraday` bucket is evidence about the "
        "monitor: `gap_at_open` is overnight gap risk (no cadence catches it — "
        "the lever is entry sizing on high-gap names) and `extended` is a thin "
        "pre/post-market print (the lever is whether a stop may fire on one at "
        "all). If `intraday` slippage is near zero, the stop ENGINE is sound "
        "and any lesson about 'stops filling too deep' must name the real "
        "bucket or it sends the next session down a false trail.\n\n"
        "Job 2g — STOP/TRAIL TUNING (optional): the equity stop and trailing "
        "stop are vol-scaled sigma multipliers (default stop 2.5, trail 3.0 "
        "sigma) applied to each name's OWN daily sigma, LEARNABLE within hard "
        "bounds — GLOBALLY and PER VOLATILITY REGIME. AGGREGATE STATS carries "
        "two views: by_exit_reason.avg_post_exit_excess_pct (all names) and "
        "stop_evidence_by_vol (the SAME post-exit excess split into low/mid/high "
        "vol regimes, plus an `unknown` bucket for rows whose vol could not be "
        "measured — that bucket is NOT a regime and must NEVER be tuned on, in "
        "either direction; report it and move on). A high-vol name (MU) can "
        "drop 5-10% intraday and bounce, "
        "whipsawing a stop that fits a calm name (COST): if a REGIME shows "
        "persistently POSITIVE post-exit excess (names kept beating SPY after we "
        "exited, n>=5 IN THAT regime), its stop is too tight — RAISE that "
        "regime's sigma multiple. If a regime shows NEGATIVE post-exit excess, "
        "the stop earned its keep — hold or lower it. PREFER a per-regime change "
        "when the regimes diverge; only move the global when they agree or a "
        "regime lacks its own n>=5. Over-tight AND over-loose both cost money. "
        f"Bounds (apply to each regime): {stop_bounds_txt}\n\n"
        "BENCHMARK SCOREBOARD (the MANDATE is beating SPY risk-adjusted — "
        "judge every win and loss RELATIVE to it. A profitable trade that "
        "lagged SPY over its own holding window is a mandate loss; each "
        "trade below carries spy_ret_pct/excess_ret_pct for exactly this):\n"
        f"{json.dumps(_benchmark_scoreboard(), indent=1)}\n\n"
        f"AGGREGATE STATS:\n{json.dumps(stats(), indent=1)}\n\n"
        "CAPS AUDIT (forward SPY-relative outcome of blocked entries, by "
        "gate). Read `buy_attribution` FIRST: it reconciles every committee BUY "
        "that never became a position against the gate that refused it. A "
        "non-zero `unattributed` count means those BUYs died with NO recorded "
        "reason — that is an instrumentation hole in the verdict-to-fill path "
        "and it must be called out in a lesson naming the tickers; never "
        "attribute it to a cap, and never pass over it in silence (fix brief "
        f"2026-07-27 #9):\n{json.dumps(blocked_audit(), indent=1, default=str)}\n\n"
        "REGRET AUDIT (committee BUYs the firm never owned — the gap to the "
        f"best version of itself):\n{json.dumps(regret_audit(), indent=1, default=str)}\n\n"
        "VERDICT REPORT CARD (EVERY rating graded against what the name did "
        "next, SPY-relative — including the HOLDs, SELLs, and calls-desk "
        "no-calls that never became trades. 'overlooked_winners' are names "
        "rated HOLD/SELL that then beat SPY by >5pts: each is the committee "
        "being wrong in the expensive direction. 'missed_adds' are names the "
        "firm OWNED, rated HOLD, that then kept beating SPY — we held a winner "
        "but never ADDED to it; if these recur, the add-on-strength bar is too "
        "strict for confirmed leaders and you must say so in a lesson (the "
        "committee can add to a proven winner, not only on a deep pullback). If "
        "HOLD correct_pct is poor while HOLD convictions run high, the committee "
        "is CONFIDENTLY wrong — say so in a lesson and name the cause (conviction "
        "bar, desk evidence, or judge calibration):\n"
        f"{json.dumps(_card_for_prompt(verdict_report_card()), indent=1, default=str)}\n\n"
        f"BENCH AUDIT:\n{json.dumps(bench, indent=1, default=str)}\n\n"
        "ROTATION DESK HISTORY (dual-momentum signals — judge whether the "
        "rotation strategy is selecting well, rotating at the right cadence, "
        "and using its cash filter correctly):\n"
        f"{json.dumps(state.load('rotation_history', {}).get('events', [])[-10:], indent=1, default=str)}\n\n"
        "SECTOR REGIME HISTORY (which sectors were leading/lagging the market "
        "over time — judge whether entering stocks in LEADING sectors "
        "outperforms, using the by_sector_tailwind stats above):\n"
        f"{json.dumps(state.load('sector_regime', {}).get('history', [])[-10:], indent=1, default=str)}\n\n"
        "RISK EVENTS (ALL halts — kill switches with diagnoses, exit failures, "
        "reconcile drift, state corruption — and their outcomes. Judge whether "
        "each halt was warranted, whether sector restrictions vs full halts "
        "were the correct response, and whether recovery timing cost or saved "
        "money):\n"
        f"{json.dumps(__import__('firm.riskbrain', fromlist=['x']).events_for_review(), indent=1, default=str)}\n\n"
        "DAILY BREAKER TRIPS (last 14 days, BINDING trips only — frequent "
        "trips mean thresholds are mis-tuned or a strategy is running too "
        "hot; the rotation sleeve's advisory rows gate nothing and are "
        "excluded, shown only as a count):\n"
        f"{json.dumps(_binding_trips(), indent=1, default=str)}\n\n"
        f"RECENT CLOSED TRADES:\n{json.dumps(recent, indent=1, default=str)}\n\n"
        f"CURRENT LESSONS:\n{json.dumps(lessons_st.get('lessons', []), indent=1)}\n\n"
        "Job 3 — FIX REQUESTS (optional): if the evidence shows a problem you "
        "CANNOT fix with lessons or bench tuning — a rule in the system's "
        "code/config that is mis-designed, a threshold outside your tuning "
        "bounds, contradictory behaviors, a strategy that is structurally "
        "losing — file a fix request FOR THE HUMAN OPERATOR. He will hand it "
        "to Claude Code to implement, so write it as a precise engineering "
        "ticket: the problem, the evidence, where you suspect it lives "
        "(sleeve/module/parameter), and the exact instruction to give Claude.\n\n"
        'Respond with ONLY this JSON: {"lessons": [{"lesson": "<one sentence, '
        'specific>", "evidence": "<one sentence citing the trades/stats>"}], '
        '"bench_tuning": {<only keys you want changed, or omit entirely>}, '
        '"cap_tuning": {<only CAP_BOUNDS, MOMENTUM_LANE_BOUNDS, RATCHET_BOUNDS, '
        'ABS_STOP_BOUNDS or QUANT_COMPRESS_BOUNDS keys you want changed, or omit '
        '— MOMENTUM_LANE_MOM_MIN governs the momentum-lane skip gate whose '
        'blocked cohort you see in the evidence. The QUANT_FULL_SCALE_* keys set '
        'how far a momentum input must run before the quant SCREEN gives it full '
        'credit, i.e. how hard the screen leans into extreme tails. Tune them '
        'from `screen_predictiveness`: if the `top` band does NOT out-earn `high` '
        'and `mid`, the screen is over-rewarding extreme momentum — LOWER the '
        'full-scales so strong and extreme count more alike. If the top band '
        'earns clearly more, RAISE them. If the bands are indistinguishable the '
        'screen is not discriminating at all and that is the finding to report, '
        'not a number to nudge. Sample sizes are small — move one key at a time '
        'and only when a band has real n. '
        'PROFIT_RATCHET_GIVEBACK_PCT caps how much of a peak GAIN a winner may '
        'hand back before being booked, the single most return-sensitive knob '
        'in the fund — tune it from the `ratchet` bucket of by_exit_reason: '
        'positive post-exit excess there means it cut winners still running '
        '(RAISE it), deep negative means it banked tops correctly (hold or '
        'LOWER). Propose a bounded move ONLY when the cohort data justifies it>, '
        '"stop_tuning": {<STOP_BOUNDS keys for a GLOBAL change and/or per-regime '
        'overrides, e.g. {"trail_sigma_mult": 3.2, "high": {"trail_sigma_mult": '
        '3.6}}; omit if none>}, '
        '"fix_requests": [{"problem": "<one sentence>", "evidence": "<cite the '
        'data>", "suspected_area": "<sleeve/module/parameter>", "ask_claude": '
        '"<the exact request to paste into Claude Code>"}] (omit if none)} '
        f"(maximum {MAX_LESSONS} lessons, ordered by importance)"
    )
    res = brain.ask_json(prompt, retries=1)
    if not res or not isinstance(res.get("lessons"), list):
        return None
    res["lessons"] = _cross_model_refute(res["lessons"])
    tuned = _apply_bench_tuning(res.get("bench_tuning") or {})
    caps_tuned = _apply_cap_tuning(res.get("cap_tuning") or {})
    stops_tuned = _apply_stop_tuning(res.get("stop_tuning") or {})
    _file_fix_requests(res.get("fix_requests") or [])
    # Stamp each lesson's birth (or re-confirmation — surviving a review IS
    # re-confirmation) so lessons_context can age out stale rules
    now_iso = datetime.now(timezone.utc).isoformat()
    for x in res["lessons"]:
        if isinstance(x, dict):
            x["added"] = now_iso
    out = {
        "lessons": res["lessons"][:MAX_LESSONS],
        "reviewed_through": total,
        "asof": datetime.now(timezone.utc).isoformat(),
        "bench_tuning_applied": tuned,
        "cap_tuning_applied": caps_tuned,
        "stop_tuning_applied": stops_tuned,
    }
    # AUDIT 2026-07-16: the fresh dict was WIPING the verdict review's own
    # watermark (verdict_reviewed_asof, set at ~1534) — every money review
    # reset the verdict-review cadence to "never ran". Carry it forward.
    _prev_lessons = state.load("lessons", {}) or {}
    if _prev_lessons.get("verdict_reviewed_asof"):
        out["verdict_reviewed_asof"] = _prev_lessons["verdict_reviewed_asof"]
    state.save("lessons", out)
    state.journal("learning_review", n_lessons=len(out["lessons"]),
                  trades_reviewed=len(trades),
                  bench_tuned=bool(tuned), caps_tuned=bool(caps_tuned),
                  stops_tuned=bool(stops_tuned))
    return out
