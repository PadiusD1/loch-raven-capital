"""Cross-sleeve buy routing — the deliberate "which book(s) does this name
belong in?" decision, made VISIBLE and falsifiable.

Operator 2026-07-02: "today 3 buys, they're only in the alpha sleeve — is there
a method to say hey it qualifies for core too, let's put it in core too, or not?
that needs to be there."

The sleeves already trade INDEPENDENTLY off one shared verdict pool (alpha, core
and calls each apply their own mandate), so a qualifying name CAN land in more
than one book. What was missing is a record of the DECISION: for each BUY-rated
name, which sleeves it qualified for, which actually placed it, and — for any it
qualified for but didn't get into — WHY (slots full, a named block, or a genuine
silent drop the operator must see). This module computes that from the sleeves'
OWN eligibility gates (single source of truth — it can never disagree with what
really trades) and journals it as `sleeve_routing`, persisting the latest report
to state for the dashboard. It changes no trading behaviour; it makes the
existing routing transparent and catches a name that should have been placed but
wasn't.
"""
from datetime import datetime, timedelta, timezone

import config
from firm import state

RATING_BUYS = ("STRONG_BUY", "BUY")

# The per-name entry blocks the sleeves journal when they decline an eligible
# buy — used to explain a "qualified but not placed" name instead of crying
# "silent drop" when there was a real, recorded reason.
_BLOCK_EVENTS = (
    "buy_blocked_sector", "buy_skipped_price_drift", "buy_skipped_book_full",
    "buy_skipped_no_price", "buy_skipped_swap_cooldown", "buy_skipped_kind_unresolved",
    "buy_skipped_firm_cap", "buy_skipped_correlation", "buy_skipped_liquidity",
    "firm_sector_block", "firm_cap_requote", "position_size_block",
    # 2026-07-16 (four real blocks read as SILENT DROP): the journal names the
    # sleeves ACTUALLY emit that were missing from this map — an honest block
    # must never be reported as an unexplained drop.
    "order_blocked_market_closed", "entry_queued_for_open",
    "buy_skipped_insufficient_cash", "buy_blocked_correlation",
    "buy_blocked_position_size", "buy_blocked_firm_cap",
    "buy_blocked_firm_sector", "buy_blocked_liquidity",
    "buy_skipped_earnings", "book_full_admitted_parked_cash",
    # Options-desk per-name skips (2026-07-21): the desk journals WHY a
    # qualified underlying didn't open — those are honest blocks, never
    # silent drops.
    "calls_skipped_no_contract", "calls_skipped_earnings_before_expiry",
    "calls_skipped_size_zero", "calls_skipped_overnight_premium_cap",
    "puts_skipped_no_contract", "puts_skipped_earnings_before_expiry",
    "puts_skipped_size_zero", "puts_skipped_overnight_premium_cap",
    "buy_skipped_premium_drift", "buy_blocked_firm_cap_requote",
    # 2026-07-25: the two options-desk skips that journaled NOTHING (slot cap
    # reached, underlying already in the options book). Both are honest blocks
    # and both used to surface as unexplained SILENT DROPs.
    "calls_skipped_book_full", "calls_skipped_already_held",
    # 2026-07-26: the post-stop cool-off gate. A name the firm stopped out for
    # RISK is refused re-entry until it clears the cool-off or reclaims the stop
    # +3% — an honest, journaled block, but it was missing here and so surfaced
    # as an unexplained SILENT DROP the first cycle it fired (OSCR→alpha_stocks).
    # `open_queue_gate_blocked` covers the same refusal on the at-the-open flush.
    "buy_blocked_stop_cooloff", "open_queue_gate_blocked",
    # 2026-09-01: sleeve-level refusals (breaker / halt / exit-protection gate)
    # journal `entries_blocked` with the reason — an honest block, never a drop.
    "entries_blocked", "entries_blocked_exit_protection_down",
    "calls_skipped_reentry_cooldown", "puts_skipped_reentry_cooldown",
    "add_skipped_size_zero", "rotation_buy_skipped_size_zero",
    "buy_skipped_below_min_notional", "swap_buy_undersized_after_sell",
)


def eligibility(kind: str | None, v: dict, sym: str | None = None,
                packet: dict | None = None) -> dict:
    """Per-sleeve MANDATE eligibility for one verdict, delegating to the sleeves'
    OWN gates so this can never drift from what actually trades. etf_rotation is
    mechanical (verdict-blind) and is intentionally absent.

    Pass sym+packet when available (record_routing has them): the calls gate's
    mechanical screen keys on the PACKET's earnings window and the symbol's
    options tradability — a packet-less re-check fails open through those legs
    and disagrees with the desk (2026-07-21: the veto fix surfaced exactly this
    as three phantom calls_only SILENT DROPs the desk had in fact rejected on
    the earnings screen)."""
    from firm import sleeves
    return {
        "alpha_stocks": (kind == "stock"
                         and sleeves._verdict_ok(
                             v, config.SLEEVES["alpha_stocks"]["min_conviction"])),
        "core_blend": (kind in ("stock", "etf")
                       and sleeves._verdict_ok(
                           v, config.SLEEVES["core_blend"]["min_conviction"])),
        "calls_only": sleeves._calls_entry_ok(v, sym, packet),
    }


def open_slots(sleeve: str) -> tuple[int, int, int]:
    """(free, capacity, held) entry slots in a sleeve right now — the mechanical
    core (VOO/QQQ/GLD) and cash parking never count as occupied entry slots."""
    s = state.get_sleeve(sleeve)
    cfg = config.SLEEVES[sleeve]
    cap = int(cfg.get("max_positions") or 0)
    pos = s.get("positions") or {}
    if sleeve == "core_blend":
        # BUDGET-bound capacity (operator 2026-07-17, count cap removed):
        # satellites may fill everything the earned core seats don't claim.
        # free/cap are expressed in standard-satellite-size units so the
        # routing report keeps its "slots full (held/cap)" readability.
        from firm import allocation as _alloc_rt
        core_w = _alloc_rt.core_weights()
        sats = {k: p for k, p in pos.items()
                if k not in core_w and p.get("instrument") == "equity"
                and not p.get("cash_equitization")}
        held = len(sats)

        def _val(p: dict) -> float:
            return ((p.get("quantity") or 0)
                    * (p.get("last_price") or p.get("entry_price") or 0))

        eq = (s.get("cash") or 0) + sum(_val(p) for p in pos.values())
        budget_pct = max(0.0, 100.0 - sum(core_w.values()))
        used_pct = (sum(_val(p) for p in sats.values()) / eq * 100
                    if eq > 0 else 100.0)
        spp = float(cfg.get("satellite_position_pct") or 9.0)
        min_viable = float(cfg.get("satellite_min_viable_pct", 3.0))
        room = budget_pct - used_pct
        cap = max(held, int(budget_pct // spp))
        free = 0 if room < min_viable else max(1, int(room // spp))
        return free, cap, held
    elif sleeve == "etf_rotation":
        # 2026-07-25 audit (finding #2): etf_rotation carries NO max_positions
        # key, so `cap` fell to 0 above and this returned (0, 0, 3) — "book
        # full" on a sleeve holding a perfectly healthy mechanical top-3. The
        # flush's book-full gate then refused a rotation BUY leg and requeued
        # it to the COMMITTEE, the one thing this sleeve's mandate forbids
        # ("never overridden by the committee" — CLAUDE.md). Live consequence:
        # the 2026-07-06 half-rotation (EEM sell booked, IWM buy blocked).
        # The sleeve's real slot model is the dual-momentum top_n; the
        # defensive CASH_ETF seat is a target like any other, so it counts.
        # (Fixed HERE rather than by adding max_positions to config.SLEEVES so
        # the count cap can never be mistaken for a committee-style book cap:
        # etf_rotation's slot count IS top_n, by construction.)
        cap = int(cfg.get("top_n") or 0)
        held = sum(1 for p in pos.values()
                   if p.get("instrument", "equity") == "equity"
                   and not p.get("cash_equitization"))
    elif sleeve == "calls_only":
        held = sum(1 for p in pos.values() if p.get("instrument") == "option")
    else:                                   # alpha_stocks
        held = sum(1 for p in pos.values()
                   if p.get("instrument") == "equity"
                   and not p.get("cash_equitization"))
    return max(0, cap - held), cap, held


def _placed_in(sym: str) -> list[str]:
    """Which sleeves actually hold `sym` (an UNDERLYING ticker) right now.

    The options book keys positions by OSI (`HPE260731C00050000`), not by the
    underlying, so the plain `positions.get(sym)` lookup could never see a call
    or put and calls_only never entered `placed`. record_routing then found it
    qualified + unplaced + slot-free + no block record and stamped
    `silent_drop=True` on a trade that had in fact filled 2.5s earlier — which
    failed the 'sleeve routing' health check and sent the operator hunting a drop
    that never happened (operator 2026-07-25, HPE). Systematic, not a one-off:
    EVERY successful option open reported this way whenever a slot was free.
    The failure path journals the UNDERLYING (`calls_skipped_size_zero`), so only
    the success path was invisible — the reconciler saw drops and never fills.
    Match on the `underlying` field, with an OSI-suffix strip as the fallback for
    any legacy row that predates that field (same strip route_coverage uses)."""
    out = []
    for sl in ("alpha_stocks", "core_blend", "calls_only"):
        for key, p in (state.get_sleeve(sl).get("positions") or {}).items():
            if not p or p.get("cash_equitization"):
                continue
            if p.get("instrument") == "option":
                und = p.get("underlying") or (str(key)[:-15] if len(str(key)) >= 16 else "")
                hit = (und == sym)
            else:
                hit = (key == sym)
            if hit:
                out.append(sl)
                break
    return out


def _recent_block(sym: str, since_ts: str, journal: list) -> str | None:
    """The most recent per-name entry block for `sym` since `since_ts` (this
    cycle) — explains a qualified-but-unplaced name with a real reason."""
    for e in reversed(journal):
        if (e.get("symbol") == sym and e.get("ts", "") >= since_ts
                and e.get("event") in _BLOCK_EVENTS):
            return e.get("event")
    return None


def record_routing(verdicts: dict, since_ts: str | None = None) -> list:
    """Record the cross-sleeve routing decision for every BUY-family verdict.
    Call AFTER the sleeves have traded. Journals one `sleeve_routing` row per
    name and persists the report to state['routing_report'] for the dashboard.
    A name that qualified for a sleeve with a FREE slot, wasn't placed, and has
    NO recorded block is flagged `silent_drop=True` — the case the operator must
    never miss. Returns the report rows."""
    if not verdicts:
        state.save("routing_report", {"asof": datetime.now(timezone.utc).isoformat(),
                                      "rows": []})
        return []
    since = since_ts or (datetime.now(timezone.utc).isoformat()[:10])
    journal = state.read_journal(600)
    rows = []
    for sym, d in verdicts.items():
        v = (d or {}).get("verdict") or {}
        if v.get("rating") not in RATING_BUYS or v.get("degraded"):
            continue
        elig = eligibility(d.get("kind"), v, sym, d.get("packet"))
        qualified = [sl for sl, ok in elig.items() if ok]
        if not qualified:
            continue
        placed = _placed_in(sym)
        unplaced, silent = {}, False
        for sl in qualified:
            if sl in placed:
                continue
            free, cap, held = open_slots(sl)
            if free <= 0:
                # core_blend is BUDGET-bound with conviction-swap rotation
                # (operator 2026-07-17), not count-bound — "slots full (5/5)"
                # was the pre-07-17 label and it misled the operator into
                # thinking a fixed cap refused DAVE (2026-07-20). Report the
                # sleeve's REAL admission rule: budget spent + what a
                # challenger must do to displace the weakest holding.
                if sl == "core_blend":
                    _bf = next((e for e in reversed(journal)
                                if e.get("event") == "buy_skipped_book_full"
                                and e.get("sleeve") == "core_blend"
                                and e.get("ts", "") >= since), {})
                    # Live margin (fix brief 2026-07-21 #9): the label must
                    # quote the margin the swap gate ACTUALLY applies — the
                    # static +12 read here while _plan_swap clamps to the
                    # measured conviction spread would misstate the bar.
                    from firm import sleeves as _slv_rt
                    unplaced[sl] = (
                        "satellite budget spent "
                        f"({_bf.get('satellite_used_pct', '?')}% of "
                        f"{_bf.get('satellite_budget_pct', '?')}%) — displaces "
                        "the weakest holding only at conviction +"
                        f"{_slv_rt._replace_margin():.0f} "
                        "over 2 cycles")
                else:
                    unplaced[sl] = f"slots full ({held}/{cap})"
            else:
                blk = _recent_block(sym, since, journal)
                if blk:
                    unplaced[sl] = blk
                else:
                    unplaced[sl] = "SILENT DROP — eligible, open slot, no block"
                    silent = True
        row = {"symbol": sym, "rating": v.get("rating"),
               "conviction": v.get("conviction"),
               "qualified": qualified, "placed": placed,
               "unplaced": unplaced, "silent_drop": silent}
        rows.append(row)
        state.journal("sleeve_routing", symbol=sym, conviction=v.get("conviction"),
                      qualified=qualified, placed=placed, unplaced=unplaced,
                      silent_drop=silent)
        # Bench a stock BUY that placed NOWHERE because every qualified book was
        # slots-full (operator 2026-07-16: ALKS was verdicted BUY, skipped
        # book-full in both books, and simply forgotten — the Bench exists for
        # exactly this name; it reopens the moment a slot frees). Other unplaced
        # reasons (sector/liquidity/silent drop) are NOT benched here — they
        # have their own journaled causes and re-checking them on a freed slot
        # would burn research on a still-blocked name.
        # The Bench re-checks a stock BUY against its EQUITY sleeves only
        # (alpha_stocks / core_blend) — calls_only is not a benchable target for a
        # stock, so the bench decision is made over the EQUITY unplaced sleeves
        # alone. A name book-full on every equity book it qualified for earns a
        # seat REGARDLESS of what calls_only reported: the options desk's
        # capital-sizing skips (size_zero / no_contract / overnight_premium_cap)
        # are budget artifacts, not a rejection of the underlying, and must never
        # veto the equity bench seat (operator 2026-07-22: MU — a conv-67 BUY,
        # book-full in alpha AND core — was discarded and simply forgotten only
        # because it ALSO qualified for calls and the options budget rounded to
        # zero contracts, while HPE, book-full in the same two books that cycle,
        # was correctly benched. The prior guard demanded EVERY unplaced sleeve
        # incl. calls be book-full, so one non-book-full calls skip silently
        # denied the seat — the "buy that didn't land and wasn't caught" case).
        _BENCH_SLEEVES = ("alpha_stocks", "core_blend")
        _eq_unplaced = {sl: r for sl, r in unplaced.items()
                        if sl in _BENCH_SLEEVES}
        _bf_sleeves = [sl for sl, r in unplaced.items()
                       if str(r).startswith(("slots full",
                                             "satellite budget spent"))]
        _eq_bf = [sl for sl in _bf_sleeves if sl in _BENCH_SLEEVES]
        if (d.get("kind") == "stock" and not placed and _eq_unplaced
                and _eq_bf and len(_eq_bf) == len(_eq_unplaced)):
            try:
                from firm import watchlist
                watchlist.add_book_full(sym, v, qualified, unplaced,
                                        packet=d.get("packet"))
            except Exception:
                state.journal("book_full_bench_error", symbol=sym)
        elif _bf_sleeves:
            # Fix brief 2026-07-21 #7: a book-full skip that does NOT bench
            # must tie itself off with an explicit discard record — the IG
            # read "buy_skipped_book_full:3, book_full_waiters:[]" as a
            # signal evaporating, when every one of those names had in fact
            # PLACED in another sleeve. Every skip now resolves to exactly
            # one of {waiter, discard-record}; the health desk reconciles
            # the two counts (book-full waiter coverage) so an uncovered
            # skip is a red, not an inference. (The discard reason mirrors the
            # equity-only bench gate above: a genuine equity block —
            # sector/liquidity/correlation — is non_book_full_block_present;
            # a non-stock kind is not_benchable_kind.)
            state.journal(
                "book_full_discarded", symbol=sym, sleeves=_bf_sleeves,
                reason=("placed_in_other_sleeve:" + "/".join(placed)
                        if placed else
                        "non_book_full_block_present" if len(_eq_bf)
                        != len(_eq_unplaced) else "not_benchable_kind"))
    asof = datetime.now(timezone.utc).isoformat()
    state.save("routing_report", {"asof": asof, "rows": rows})
    # DURABLE routing history for the learning loop (operator 2026-07-02: "route
    # this into learning so the way stocks get accepted into each book matches
    # what's best"). learning.acceptance_report() grades the FORWARD return of
    # each routing decision — placed vs the qualified-but-passed counterfactual —
    # so the committee learns which conviction/sleeve acceptance actually works.
    # Capped; only rows carrying a conviction (gradable) are kept.
    if rows:
        hist = state.load("routing_history", {"events": []})
        hist["events"] = (hist.get("events", []) + [{
            "asof": asof,
            "rows": [{"symbol": r["symbol"], "rating": r["rating"],
                      "conviction": r["conviction"], "qualified": r["qualified"],
                      "placed": r["placed"]}
                     for r in rows if r.get("conviction") is not None],
        }])[-80:]
        state.save("routing_history", hist)
    return rows


def parked_capacity(sleeve: str, s: dict | None = None,
                    prices: dict | None = None) -> bool:
    """NAME-full but CAPITAL-empty (operator 2026-07-16: "$16K in SPY parked
    while BUYs were turned away"): True when the sleeve's slot cap should YIELD
    to a new conviction buy — the book is at/over max_positions by name count,
    but real (non-parking) deployment is under ALPHA_PARKED_OVERRIDE_INVESTED_PCT
    and a live parking reservoir can fund the entry. Hard ceiling
    max_positions + ALPHA_PARKED_OVERRIDE_MAX_EXTRA keeps the focus mandate
    binding. Alpha-only: core's satellite math (55% core + 5x9%) is
    capital-complete by construction, so its cap never lies about capital.
    Pure state read with last-price fallbacks so the bench/promotion path can
    call it without live quotes; sleeves passes live prices."""
    if sleeve != "alpha_stocks":
        return False
    try:
        cfg = config.SLEEVES[sleeve]
        max_pos = int(cfg.get("max_positions") or 0)
        if not max_pos:
            return False
        s = s or state.get_sleeve(sleeve)
        prices = prices or {}
        # DE-RISK GATE (fleet re-grade action 10a): in a soft_risk drawdown
        # the risk desk mechanically shrinks deployment — which drops real
        # deployment below this yield's own trigger, so the deeper the
        # drawdown the MORE names the override admitted, funded by selling
        # the defensive reservoir into a falling book. Mirror the equitize
        # rule: when the risk desk says cash IS the position, the slot cap
        # never yields.
        from firm import risk as _rk
        if s.get("halted") or _rk.risk_scale(s) < 1.0:
            return False

        def _px(sym: str, p: dict) -> float:
            v = prices.get(sym) or p.get("last_price") or p.get("entry_price") or 0
            return float(v) if isinstance(v, (int, float)) else 0.0

        pos = s.get("positions") or {}
        real = {k: p for k, p in pos.items()
                if p.get("instrument", "equity") == "equity"
                and not p.get("cash_equitization")}
        if len(real) >= max_pos + int(getattr(
                config, "ALPHA_PARKED_OVERRIDE_MAX_EXTRA", 4)):
            return False
        park_val = sum((p.get("quantity") or 0) * _px(k, p)
                       for k, p in pos.items() if p.get("cash_equitization"))
        if park_val <= 0:
            return False
        real_val = sum((p.get("quantity") or 0) * _px(k, p)
                       for k, p in real.items())
        equity = (s.get("cash") or 0) + park_val + real_val
        if equity <= 0:
            return False
        floor = float(getattr(config, "ALPHA_PARKED_OVERRIDE_INVESTED_PCT", 65.0))
        return real_val / equity * 100 < floor
    except Exception:
        return False    # fail CLOSED: a telemetry error must not widen the book


def route_coverage(window: int = 30) -> dict:
    """Deterministic verdict-class -> execution-route REACHABILITY over the last
    `window` research-history entries (IG 2026-07-16 item 7: the original calls
    gate sat unreachable for weeks because it keyed on a rating the judge never
    emitted — 'route exists but can never fire' must be measured data, not a
    hypothesis). Counts REAL (non-degraded) BUY/SELL-family verdicts, how many
    passed the options-desk entry gates, and option opens from the journal.
    Consumed by scripts/ig_audit.py (snapshot) and health.py (reachability
    check). Fail-soft: an {'error': ...} dict on any failure."""
    try:
        from firm import sleeves
        hist = (state.load("research", {}) or {}).get("history", [])[-window:]
        cov = {"window_cycles": len(hist),
               "buy_family_emitted": 0, "calls_gate_qualified": 0,
               "sell_family_emitted": 0, "puts_gate_qualified": 0}
        for h in hist:
            for _vsym, v in (h.get("verdicts") or {}).items():
                v = v or {}
                if v.get("degraded"):
                    continue
                r = v.get("rating")
                # history rows carry no packet — the gates' earnings leg
                # fails open here; reachability still reflects the veto
                # demotion + tradability legs (fleet re-grade action 1)
                if r in ("BUY", "STRONG_BUY"):
                    cov["buy_family_emitted"] += 1
                    cov["calls_gate_qualified"] += \
                        1 if sleeves._calls_entry_ok(v, _vsym) else 0
                elif r in ("SELL", "STRONG_SELL"):
                    cov["sell_family_emitted"] += 1
                    cov["puts_gate_qualified"] += \
                        1 if sleeves._puts_entry_ok(v, _vsym) else 0
        calls = puts = 0
        # Per-side open-vs-qualified RECONCILE (fix brief 2026-07-17 #1): the
        # gate_qualified numbers above re-evaluate TODAY'S gate against the
        # history window, while opens are journal facts over a different span —
        # "puts opened with ZERO puts qualified" was that window drift, which
        # the IG (reasonably) read as a gate bypass. The authoritative record
        # is the decision-time options_gate_qualified event the desk journals
        # in the same cycle it acts: every open after the deploy date must be
        # preceded by a qualification for its underlying, or this hard-fails.
        _since = str(getattr(config, "OPTIONS_GATE_RECONCILE_SINCE", "2026-07-20"))
        _qualified: dict[str, list] = {"calls": [], "puts": []}
        _opens: list[tuple] = []
        # (2026-08-13 IG audit) sized to the desk's whole life, with MEASURED
        # coverage: journal rotation (config JOURNAL_MAX_BYTES ->
        # JOURNAL_KEEP_LINES=3000) can truncate below any fixed read size, and
        # the old read_journal(2500) silently spanned days while these counts
        # were labeled "full retention"/lifetime. One read serves both event
        # passes below. Covered = the journal still reaches back to the
        # reconcile epoch (_since), or the whole never-rotated journal was read
        # (fewer events than JOURNAL_KEEP_LINES = nothing ever discarded).
        _read_n = 20000
        _jrnl = state.read_journal(_read_n)
        _jts = [str(e.get("ts", "")) for e in _jrnl if e.get("ts")]
        _oldest_read = min(_jts) if _jts else None
        _covered = ((_oldest_read is not None and _oldest_read[:10] <= _since)
                    or len(_jrnl) < min(_read_n,
                                        int(getattr(config, "JOURNAL_KEEP_LINES",
                                                    3000))))
        for e in _jrnl:
            ev = e.get("event")
            if ev == "options_gate_qualified" and e.get("side") in _qualified:
                _qualified[e["side"]].append(
                    (e.get("ts", ""), set(e.get("symbols") or [])))
            elif ev == "order" and e.get("sleeve") == "calls_only" \
                    and e.get("side") == "buy":
                osi = str(e.get("symbol", ""))
                if len(osi) >= 16 and osi[-9] == "P":
                    puts += 1
                    _opens.append((e.get("ts", ""), "puts", osi[:-15]))
                elif len(osi) >= 16 and osi[-9] == "C":
                    calls += 1
                    _opens.append((e.get("ts", ""), "calls", osi[:-15]))
        _unmatched: dict[str, list] = {"calls": [], "puts": []}
        for _ts, _side, _und in _opens:
            if _ts[:10] < _since:
                continue      # predates qualification journaling — unreconcilable
            if not any(_qts <= _ts and _und in _qsyms
                       for _qts, _qsyms in _qualified[_side]):
                _unmatched[_side].append(_und)
        # Forward conversion (fix brief 2026-07-21 #6): unmatched_opens only
        # catches a BYPASS (open without qualification). The inverse failure —
        # a gate that QUALIFIES names which then never open and never journal a
        # skip reason — is the "qualified-but-never-converted" inert-conversion
        # class that kept the calls side at zero opens for the fund's whole
        # life while every reconcile read green. Every decision-time qualified
        # symbol must resolve within its own cycle to an OPEN or an explained
        # skip/block record; a symbol with neither is a silent evaporation.
        # THIS LIST IS THE MEASUREMENT, SO AN OMISSION HERE IS A FALSE ALARM
        # (operator 2026-07-28). It was missing four events the desk really
        # emits, and the first one to fire proved the cost: MU qualified for
        # puts on 07-28, the desk refused it as a chase and journaled
        # `buy_skipped_price_drift` (-5.9% vs a 5.1% band) one second later —
        # an honest, correct, fully-recorded refusal. Because the event was not
        # in this tuple the reconciler classed it "no journaled skip reason",
        # redded `options gate reachability` CRITICAL and filed a fix brief
        # about an unreachable gate that was, in fact, working. Every skip path
        # in sleeves._puts_buy_pass / _run_calls_only must appear here; the
        # audit that regenerates it is `grep -n 'state\.journal' firm/sleeves.py`
        # across those two functions.
        _skip_evs = {
            "calls": ("calls_skipped_no_contract",
                      "calls_skipped_earnings_before_expiry",
                      "calls_skipped_size_zero",
                      "calls_skipped_overnight_premium_cap",
                      "calls_skipped_budget_after_requote",
                      "calls_skipped_book_full",
                      "calls_skipped_already_held",
                      "calls_skipped_reentry_cooldown"),
            "puts": ("puts_skipped_no_contract",
                     "puts_skipped_earnings_before_expiry",
                     "puts_skipped_size_zero",
                     "puts_skipped_overnight_premium_cap",
                     "puts_skipped_budget_after_requote",
                     "puts_skipped_book_full",
                     "puts_skipped_already_held",
                     "puts_skipped_reentry_cooldown"),
        }
        _shared_evs = ("buy_blocked_firm_cap", "buy_blocked_firm_sector",
                       "buy_blocked_firm_cap_requote", "buy_skipped_premium_drift",
                       "order_blocked_market_closed", "entry_queued_for_open",
                       # sleeves._entry_drift_ok / _earnings_ok — the two shared
                       # pre-contract guards, both journaled with sleeve=
                       # "calls_only" on this desk and both previously invisible
                       # to the reconcile.
                       "buy_skipped_price_drift", "buy_skipped_earnings",
                       # (2026-08-13 IG audit, PEB defect) broker.execute's
                       # paper cash check (reason insufficient_cash_after_
                       # slippage) and the desk's logged execute() refusals —
                       # both journal the OSI with sleeve="calls_only"; without
                       # them here the drop classed as inert, not explained.
                       "options_open_skipped", "options_open_failed")
        _skips: list[tuple] = []            # (ts, side_or_None, underlying)
        for e in _jrnl:      # same sized+coverage-measured read (2026-08-13 IG audit)
            ev = e.get("event")
            _symj = str(e.get("symbol", ""))
            if ev in _skip_evs["calls"]:
                _skips.append((e.get("ts", ""), "calls", _symj))
            elif ev in _skip_evs["puts"]:
                _skips.append((e.get("ts", ""), "puts", _symj))
            elif ev in _shared_evs and (
                    e.get("sleeve") in (None, "calls_only")):
                # premium-drift journals the OSI — strip to the underlying
                _u = _symj[:-15] if len(_symj) >= 16 else _symj
                _skips.append((e.get("ts", ""), None, _u))
        # SILENCE MUST AGE OUT ONCE IT IS FIXED (operator 2026-08-19).
        # The BYPASS leg above bounds itself by _since; this CONVERSION leg had
        # NO lower time bound at all, so it re-walked every options_gate_qualified
        # event still in the 20,000-line read — for the desk's whole life. One
        # silent drop therefore pinned 'options gate reachability' CRITICAL
        # forever, with no clearing path no matter how the desk behaved
        # afterwards. That is what happened: PEB qualified 2026-08-04T19:40:02,
        # the desk journaled an Opus exit_plan for PEB260821C00020000 and then
        # dropped it with no order and no skip row (sleeve_routing recorded
        # "SILENT DROP - eligible, open slot, no block"). The defect was REAL and
        # was remediated on 2026-08-13 (this file's requote re-size + the
        # options_open_skipped / options_open_failed / insufficient_cash_after_
        # slippage skip vocabulary), and the very next qualification batch —
        # 2026-08-14T19:24 calls[GEO, IOVA, VAC] — converted 3 of 3 to real
        # orders. The check still went on to fail 53 consecutive sweeps on the
        # 15-day-old PEB row, and health.py's brief escalation turned that into
        # fix briefs 68 and 69 and a CRITICAL alert claiming the gate was
        # "arithmetically unreachable" — about a sleeve that is the fund's
        # best performer (+6.8% lifetime, +$1,672 realised on that same batch).
        # A permanently-latched alarm is not a control: it trains the operator
        # to ignore the line and it manufactures false engineering tickets.
        # So the silent-drop leg now measures a RECENT window, exactly like the
        # bypass leg measures a post-epoch one. Aged rows are NOT deleted —
        # they move to `aged_unexplained` so the history stays auditable and a
        # regression re-reds the line within the window instead of never.
        _conv_days = int(getattr(config, "OPTIONS_GATE_CONVERSION_WINDOW_DAYS", 14))
        _conv_floor = (datetime.now(timezone.utc)
                       - timedelta(days=_conv_days)).isoformat()
        _conversion: dict[str, dict] = {}
        for _side in ("calls", "puts"):
            _qn = _conv = _expl = 0
            _silent: list[str] = []
            _aged: list[str] = []
            for _qts, _qsyms in _qualified[_side]:
                _day_end = _qts[:10] + "~"    # "~" sorts after "T": same-UTC-day bound
                for _u in _qsyms:
                    _qn += 1
                    if any(_s == _side and _u == _o and _qts <= _ots
                           for _ots, _s, _o in _opens):
                        _conv += 1
                    elif any((_s in (None, _side)) and _u == _o
                             and _qts <= _sts <= _day_end
                             for _sts, _s, _o in _skips):
                        _expl += 1
                    elif _qts >= _conv_floor:
                        _silent.append(_u)
                    else:
                        _aged.append(_u)
            _conversion[_side] = {
                "qualified_symbol_cycles": _qn, "converted_opens": _conv,
                "explained_skips": _expl,
                "unexplained_qualified": sorted(set(_silent))[:8],
                "aged_unexplained": sorted(set(_aged))[:8],
                "conversion_window_days": _conv_days}
        cov["gate_conversion"] = _conversion
        # The opened counts live ONLY inside gate_reconcile, keyed with their
        # true span (IG 2026-07-20 re-flag of the resolved 07-17 critical: the
        # auditor kept reading `puts_gate_qualified=0` next to a top-level
        # `puts_opened_journal=2` as a live gate bypass. Those two numbers are
        # NOT comparable — qualified re-evaluates TODAY'S gate over the
        # {window}-cycle history; opens span the FULL journal retention, weeks
        # longer, and the 07-07 PENG puts qualified at decision time under the
        # then-current gate. Structuring the dict so the invalid comparison has
        # no adjacent operands is the permanent fix — the reconcile below is
        # the ONLY bypass detector.)
        cov["gate_reconcile"] = {
            "since": _since,
            "authoritative": "unmatched_opens is the ONLY valid gate-bypass "
                             "signal — an open after `since` with no "
                             "decision-time options_gate_qualified event for "
                             "its underlying. Comparing *_gate_qualified "
                             "(today's gate re-run over the trailing cycle "
                             "window) to opened counts (full journal "
                             "retention, different span AND different gate "
                             "version) is a category error, not a finding.",
            "calls_opened_full_retention": calls,
            "puts_opened_full_retention": puts,
            "calls_qualified_events": len(_qualified["calls"]),
            "puts_qualified_events": len(_qualified["puts"]),
            "calls_unmatched_opens": _unmatched["calls"],
            "puts_unmatched_opens": _unmatched["puts"],
            # (2026-08-13 IG audit) when the (rotated) journal no longer
            # reaches back to `since`, every count above is a JOURNAL-WINDOW
            # figure, not a lifetime one — consumers must label it that way
            # (the *_full_retention key names are kept for shape compat).
            "journal_window_covered": _covered,
            "journal_oldest_read": _oldest_read,
            "span": "lifetime" if _covered else "journal-window (rotation-truncated)",
        }
        return cov
    except Exception as e:
        return {"error": str(e)[:80]}


def routing_health() -> tuple[bool, str]:
    """Health line: OK unless the latest routing report has a SILENT DROP — a
    name that qualified for a book with an open slot yet was never placed and
    left no reason. That is the exact 'a buy didn't land where it should' failure
    the operator asked to be guaranteed against."""
    rep = state.load("routing_report", {}) or {}
    rows = rep.get("rows") or []
    drops = [r for r in rows if r.get("silent_drop")]
    if not rows:
        return True, "no BUY-rated names to route last cycle"
    multi = sum(1 for r in rows if len(r.get("placed") or []) > 1)
    if drops:
        return False, ("SILENT DROP(S) — qualified for an open-slot book but never "
                       "placed: " + ", ".join(
                           f"{r['symbol']}→{'/'.join(r['unplaced'])}" for r in drops[:3]))
    return True, (f"{len(rows)} BUY name(s) routed, {multi} in >1 book, "
                  "no silent drops")
