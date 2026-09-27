"""The Bench — near-miss names tracked across cycles, promoted the moment
their setup completes. Catch the wave early, with research instead of FOMO.

Two ways onto the bench:
  near_miss     — ranked just below the tier-slot cutoff in the quant screen
  hold_verdict  — the PM said HOLD with decent conviction; the judge's own
                  "watch_trigger" (what would change my mind) is stored

Two ways off the bench into full research (max 3 promotions per cycle):
  improved      — quant score rose >= 5 points since it was benched
  recheck       — a HOLD name is re-argued after 3+ days

Leaving the bench is BEHAVIOR-based, not a fixed calendar — a name gets
exactly the time its own data deserves:
  graduated     — a promotion earned a BUY (or got rejected with a SELL)
  decayed       — quant score fell >= 6 points below where it was benched:
                  the wave is fading, cut it early
  vanished      — out of the scan's top ranks 3 cycles in a row
  judged out    — a HOLD name was re-argued twice more and still isn't a
                  BUY: three committee looks is enough
  aged out      — hard cap 30 days; nothing earns a permanent seat
"""
from datetime import datetime, timedelta, timezone

import config
from firm import state

BENCH_SIZE = 40            # cap on OPEN-ENDED names (near-miss + no-catalyst HOLD)
# Catalyst-waiters — HOLDs with a KNOWN, not-yet-landed earnings/macro date — are
# EXEMPT from BENCH_SIZE: they self-evict the moment the date lands or passes, and
# they reopen on that DATE (not a fresh quant), so the bounded refresh never starves
# them and only disk bounds them (operator 2026-06-23: "why not unlimited if it has
# expiring dates? I have storage"). BENCH_HARD_MAX is a pure sanity backstop on the
# TOTAL (incl. waiters) against a pathological flood. UNCAP off = legacy hard-40.
BENCH_UNCAP_CATALYST = True
BENCH_HARD_MAX = 150
# Wipe guard (operator 2026-06-23, after a duplicate-process incident collapsed a
# 41-name bench to {}): a single cycle must NEVER shrink a substantially-populated
# bench to (near) empty — that's the fingerprint of an outage/race (empty screen,
# a stray second process, a corrupt-restore), not a real mass-eviction. Keep the
# prior bench + alert instead. A genuine shrink resumes next cycle name-by-name.
BENCH_WIPE_GUARD_MIN = 15      # only protect a prior bench this large
BENCH_WIPE_GUARD_FRAC = 0.2    # block if the new bench < this fraction of the prior
NEAR_MISS_POOL = 25
PROMOTE_PER_CYCLE = 3
IMPROVE_PTS = 5.0
# Per-cycle budget for stamping an earnings date onto near-miss bench entries.
# next_earnings is a per-ticker vendor call; 8/cycle arms a 36-name class in
# ~5 cycles and then costs nothing, because a stamped name is never re-fetched.
NEAR_MISS_EARNINGS_PER_CYCLE = 8
# Days before re-probing a near-miss name whose earnings date came back unknown.
# Without this, two unknowable tickers sit at the front of the ranked list and
# consume the per-cycle budget forever while the rest of the class stays unarmed.
NEAR_MISS_EARNINGS_REPROBE_DAYS = 7.0
RECHECK_HOLD_DAYS = 3
DECAY_DROP_PTS = 6.0       # benched-entry score minus this -> cut early
MISSING_CYCLES_DROP = 3    # absent from the scan ranks N cycles -> gone
MAX_RECHECKS = 2           # initial verdict + 2 re-arguments, then out
MAX_AGE_DAYS = 30          # hard ceiling regardless of behavior
# A HOLD waiting on a KNOWN, not-yet-landed earnings print must not be evicted
# or re-argued on the time cadence BEFORE that print — else the name is forgotten
# before _catalyst_landed can reopen it on the print day (2026-06-13/14 operator:
# "remember past runs' dates... how about earnings later, like August?"; reaffirmed
# 2026-07-08: "don't disappear when they have a date months later"). Window = a
# full quarter + margin: earnings are quarterly (NEXT print always within ~95 days),
# and companies routinely PUSH a report a week or two — 120 days keeps a name safe
# through a rescheduled/delayed print (~4 months out) while still rejecting a
# no-date / absurd far-future date that would pin a name to the bench forever. A
# protected name self-evicts the instant its date lands (reopen -> normal eviction),
# so a wider window is never an immortal seat.
CATALYST_WAIT_MAX_DAYS = 120
MACRO_WAIT_DAYS = 5        # an imminent macro print (FOMC/CPI/jobs) protects ALL HOLDs
MAX_CATALYST_PROMOS = 6    # landed earnings/FOMC reopens get dedicated promotion
                          # slots (a real event must never lose its review to the
                          # routine-recheck cap — esp. post-FOMC when many reopen)

# Stop-outs: a stop-loss is a RISK decision, not a thesis decision. A name
# stopped out of a sleeve goes on the bench with its thesis attached, must
# cool off (no falling-knife re-entry), and is re-argued by the committee
# once price proves recovery by reclaiming the stop-out level with margin.
STOP_OUT_COOLOFF_DAYS = 3
STOP_OUT_RECLAIM_PCT = 3.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_days(iso: str) -> float:
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(iso)).total_seconds() / 86400
        return 1e9 if age < 0 else age   # clock jumped back = treat as stale
    except (ValueError, TypeError):
        return 1e9


def _catalyst_landed(entry: dict) -> str | None:
    """A benched HOLD said 'wait for the print / the Fed' — the bench must
    re-judge it THE MOMENT that catalyst lands, even if the name has since
    fallen out of the scan (2026-06-13: the time-based recheck alone could
    miss the earnings by days; a 'waiting for earnings' name must never be
    forgotten OR re-judged on the wrong day). Returns the catalyst once it
    has occurred AFTER the verdict was made, else None."""
    from datetime import date
    import firm.macro_calendar as macro_calendar
    today = datetime.now(timezone.utc).date()
    since = (entry.get("last_verdict") or {}).get("ts") or entry.get("added", "")
    try:
        since_d = datetime.fromisoformat(since).date() if since else today
    except (ValueError, TypeError):
        since_d = today
    ed = entry.get("earnings_date")
    if ed:
        try:
            edd = date.fromisoformat(ed)
            # The print must have ACTUALLY landed: a past date is out; a
            # today date only if it was a before-open report (an after-close
            # print isn't out yet at the morning cycle). Mirrors the cooldown.
            sess = entry.get("earnings_session")
            landed = edd < today or (edd == today and sess == "BMO")
            if since_d <= edd and landed:
                return f"earnings printed {ed}"
        except (ValueError, TypeError):
            pass
    # Any high-impact macro release since the verdict — FOMC, CPI, or jobs/NFP —
    # reopens a macro-waiting name ("fed numbers aren't only FOMC"; 2026-06-14).
    m = macro_calendar.landed_since(since_d, today)
    if m:
        return f"macro data landed {m.isoformat()}"
    # The SPECIFIC number this thesis NAMED — incl. medium-impact prints
    # (PPI/jobless claims/JOLTS) the global high-impact trigger skips. "If the
    # bench mentions a number, track it" (operator 2026-06-14).
    mm = macro_calendar.mentioned_landed(entry.get("watch_trigger", ""), since_d, today)
    if mm:
        return f"awaited data landed {mm.isoformat()}"
    return None


def _awaiting_near_catalyst(entry: dict, today=None) -> bool:
    """Is this a benched HOLD waiting on a KNOWN, near, NOT-yet-landed catalyst —
    its earnings print OR an imminent macro release (FOMC/CPI/jobs)? Such a name
    must survive (and not be re-argued on the time cadence) until the catalyst
    lands. Once it passes, _catalyst_landed reopens it and normal eviction
    resumes — no immortal seat. Earnings: bounded to a quarter
    (CATALYST_WAIT_MAX_DAYS). Macro: only the short run-up (MACRO_WAIT_DAYS) — a
    macro print reopens EVERY benched HOLD, so none must be evicted the day before
    it (2026-06-14: 'fed numbers aren't only FOMC, CPI came out')."""
    if entry.get("source") != "hold_verdict":
        return False
    from datetime import date as _date
    today = today or datetime.now(timezone.utc).date()
    ed = entry.get("earnings_date")
    if ed:
        try:
            days_to = (_date.fromisoformat(str(ed)[:10]) - today).days
            if 0 <= days_to <= CATALYST_WAIT_MAX_DAYS:
                return True
        except (ValueError, TypeError):
            pass                           # a loose/garbage date is NOT a valid wait
    import firm.macro_calendar as macro_calendar
    if macro_calendar.imminent_within(today, MACRO_WAIT_DAYS):
        return True                        # any high-impact print imminent -> reopens all HOLDs
    if macro_calendar.mentioned_imminent(entry.get("watch_trigger", ""), today, MACRO_WAIT_DAYS):
        return True                        # the SPECIFIC number this thesis names is imminent
    return False


_PRICE_TRIGGERS = ("pullback_pct", "below_price", "reclaim_price")

# Compound reopen-triggers (operator 2026-06-23; design + adversarial review wf). A
# trigger is either a flat LEAF (today's {kind, pct, price, level, period, dir} — the
# live bench stores these and they must evaluate IDENTICALLY) or, when
# config.COMPOUND_TRIGGERS is on, a NODE {op: all|any|after, legs:[...]} mirroring a
# compound thesis ("a pullback AND RSI<55 AND holds the 50-DMA, AFTER the macro
# clears, OR the earnings"). Discriminator: "op" present -> node; else -> leaf.
_PRIMITIVE_KINDS = {"pullback_pct", "below_price", "reclaim_price", "rsi_below", "cross_sma"}
_COMPOUND_OPS = {"all", "any", "after"}
_NONMACRO_TECH = {"pullback_pct", "rsi_below", "cross_sma", "reclaim_price", "below_price"}
_MAX_DEPTH = 2
_MAX_LEGS = 4
_UNKNOWN = object()   # a leg that could NOT be measured this cycle (data absent) != not-met


def _trigger_kinds(rt, depth: int = 0) -> set:
    """Every leaf 'kind' anywhere in a trigger (flat leaf -> {kind}; node -> walks
    legs). ONE walker shared by every legacy consumer so they all read a node the
    same way. Garbage -> empty set. (Flat leaf is identical to today: {kind}.)"""
    out: set = set()
    if not isinstance(rt, dict) or depth > _MAX_DEPTH:
        return out
    if "op" in rt:
        for leg in (rt.get("legs") or []):
            out |= _trigger_kinds(leg, depth + 1)
    elif rt.get("kind"):
        out.add(rt["kind"])
    return out


def _or_reachable_kinds(rt, depth: int = 0) -> set:
    """Leaf kinds that can fire the trigger ON THEIR OWN.

    _trigger_kinds walks the WHOLE tree, so a leg buried under an `all` (or
    `after`, also a conjunction) is reported as present even though it can
    never fire without its partner. That is fine for "does this entry need a
    live price" but wrong for an ARMING census, which is asking "can this path
    reopen the name". Live example (operator 2026-08-21): the only entry the
    bench had armed on news was SLS, whose trigger is
    {any: [{all: [news, cross_sma 50 above]}, catalyst]} — the news leg needs
    the 50-day cross alongside it, so `news_catalyst` counted 1 armed, fired 0
    in 30d, and 'reopen-path liveness' called the path INERT for 428
    consecutive sweeps on the strength of a leg that was never independently
    able to fire. Same OR-reachability rule already applied to the stop-out
    reclaim leg on 08-19."""
    out: set = set()
    if not isinstance(rt, dict) or depth > _MAX_DEPTH:
        return out
    if rt.get("op") == "any":
        for leg in (rt.get("legs") or []):
            out |= _or_reachable_kinds(leg, depth + 1)
    elif "op" in rt:
        return out                    # all / after => conjunctive, cannot fire alone
    elif rt.get("kind"):
        out.add(rt["kind"])
    return out


def _needs_price_trigger(entry: dict) -> bool:
    """A benched HOLD whose reopen_trigger needs the live PRICE (so the promotion
    pass fetches it even when the name has faded out of the scan). Walks nodes too."""
    return bool(_trigger_kinds(entry.get("reopen_trigger")) & set(_PRICE_TRIGGERS))


def _needs_full_tech(entry: dict) -> bool:
    """A trigger carrying a NON-price technical leg (rsi_below / cross_sma) needs real
    RSI/SMA each cycle, else an `all` over it can never confirm on a faded off-scan
    name. (Used by the promotions full-tech refresh — wired before the flag flips.)"""
    return bool(_trigger_kinds(entry.get("reopen_trigger")) & {"rsi_below", "cross_sma"})


def _nonmacro_reopen_ok(rt, depth: int = 0) -> bool:
    """True iff a TECHNICAL leg can reopen this name WITHOUT first clearing a not-yet-
    landed macro/earnings `after`-gate. Fail-CLOSED on an `after`-gated technical so
    the momentum lane never macro-silences a name whose only path is behind a gate."""
    if not isinstance(rt, dict) or depth > _MAX_DEPTH:
        return False
    if "op" in rt:
        legs = [l for l in (rt.get("legs") or []) if isinstance(l, dict)]
        if rt.get("op") == "after":
            return False            # technical 'then' gated by a catalyst -> not standalone
        return any(_nonmacro_reopen_ok(l, depth + 1) for l in legs)
    return rt.get("kind") in _NONMACRO_TECH


def _eval_single_leg(rt: dict, entry: dict, tech: dict, px):
    """The EXACT legacy per-kind body, taking the leg dict explicitly. Returns a
    reason str (fired), None (not met), or _UNKNOWN (rsi/sma data absent) so an
    enclosing `all` can tell 'unmeasured' from 'not met' and never silently fail."""
    kind = rt.get("kind")
    if kind == "pullback_pct":
        base, pct = entry.get("benched_price"), rt.get("pct")
        if (isinstance(px, (int, float)) and isinstance(base, (int, float)) and base > 0
                and isinstance(pct, (int, float))):
            drop = (base - px) / base * 100
            return f"pulled back {drop:.0f}% (waited for {pct:.0f}%)" if drop >= pct else None
        return None
    if kind == "below_price":
        lvl = rt.get("price")
        if isinstance(px, (int, float)) and isinstance(lvl, (int, float)):
            return f"price ${px:.2f} <= target ${lvl:.2f}" if px <= lvl else None
        return None
    if kind == "reclaim_price":
        lvl = rt.get("price")
        if isinstance(px, (int, float)) and isinstance(lvl, (int, float)):
            return f"price ${px:.2f} reclaimed ${lvl:.2f}" if px >= lvl else None
        return None
    if kind == "rsi_below":
        lvl, rsi = rt.get("level"), tech.get("rsi14")
        if not isinstance(lvl, (int, float)):
            return None
        if not isinstance(rsi, (int, float)):
            return _UNKNOWN
        return f"RSI {rsi:.0f} <= {lvl}" if rsi <= lvl else None
    if kind == "cross_sma":
        per = rt.get("period")
        flag = tech.get("above_sma200") if per == 200 else tech.get("above_sma50")
        if flag is None:
            return _UNKNOWN
        if rt.get("dir") == "above" and flag is True:
            return f"crossed above the {per}d SMA"
        if rt.get("dir") == "below" and flag is False:
            return f"pulled to/below the {per}d SMA"
        return None
    return None


def _eval_leg(leg, entry, tech, px, *, cat_fn, news_fn, ticker, depth=0):
    """Recursive compound evaluator. Returns a reason str (fired), None (not met), or
    _UNKNOWN (could not measure). Pure over data-in-hand + the cat_fn/news_fn the
    cycle already computes per hold (catalyst -> _catalyst_landed, news -> _news_*)."""
    if not isinstance(leg, dict):
        return None
    if "op" in leg:
        if depth >= _MAX_DEPTH:
            return None
        op = leg.get("op")
        legs = [l for l in (leg.get("legs") or []) if isinstance(l, dict)][:_MAX_LEGS]
        if op not in _COMPOUND_OPS or not legs:
            return None
        if op == "after":
            gate = legs[0]
            rest = legs[1] if len(legs) > 1 else None
            g = _eval_leg(gate, entry, tech, px, cat_fn=cat_fn, news_fn=news_fn,
                          ticker=ticker, depth=depth + 1)
            if g is None or g is _UNKNOWN:        # gate hasn't landed -> the whole thing waits
                return None
            if rest is None:
                return f"after {g}"
            r = _eval_leg(rest, entry, tech, px, cat_fn=cat_fn, news_fn=news_fn,
                          ticker=ticker, depth=depth + 1)
            return f"after {g} -> {r}" if (r and r is not _UNKNOWN) else None
        results = [_eval_leg(l, entry, tech, px, cat_fn=cat_fn, news_fn=news_fn,
                             ticker=ticker, depth=depth + 1) for l in legs]
        if op == "any":
            for r in results:
                if r and r is not _UNKNOWN:
                    return r
            return None
        # op == "all": every leg must FIRE; an UNKNOWN means 'cannot confirm' -> hold
        if any(r is None or r is _UNKNOWN for r in results):
            return None
        return " AND ".join(results)
    kind = leg.get("kind")
    if kind in (None, "none"):
        return None
    if kind == "catalyst":
        return cat_fn(entry) if cat_fn else None
    if kind == "news":
        return "news catalyst (compound leg)" if (news_fn and news_fn(entry, ticker)) else None
    if kind in _PRIMITIVE_KINDS:
        return _eval_single_leg(leg, entry, tech, px)
    return None


def _stop_out_open(e: dict) -> bool:
    """Does this bench entry still carry an UNRESOLVED stop-out obligation?

    PROVENANCE LIVES IN THE PAYLOAD, NOT THE LABEL (operator 2026-08-19).
    `source` does two incompatible jobs: it is the provenance class the census
    partitions on AND the dispatch switch the promotion loop branches on. The
    dispatch job requires it to be MUTABLE — a stopped name later re-judged HOLD
    genuinely is a hold-verdict waiter now — so _update_locked overwrites it
    (:1209 hold_verdict, :1261 book_full). The provenance job requires it to be
    DURABLE. Mutability won, and the stop-out class stopped existing: bench
    by_source read {hold_verdict 76, near_miss 36, book_full 4} with
    partition_sums_to_total TRUE — a complete partition over a set missing a
    member — while `stop_out_reclaim` reported armed_now 0 / never_armed True
    and the journal showed the path firing twice. That false-inert verdict is
    what held 'reopen-path liveness' red for 338 consecutive sweeps.

    The `stop_out` block the emitter wrote is never deleted, so IT is the
    durable fact. Live proof on 2026-08-19: TXN (source=hold_verdict,
    stop_out{core_blend, entry 286.13, exit 270.20, 'trailing stop -18.9% from
    peak'}) and PRSU (source=hold_verdict, stop_out{alpha_stocks, entry 53.01,
    exit 37.53, 'hard stop -29.2%'}) both carried live stop-out obligations that
    no arming census, no reclaim heal and no price fetch could see.

    Read the payload for OBLIGATIONS; keep reading `source` for DISPATCH."""
    so = e.get("stop_out")
    return isinstance(so, dict) and bool(so)


def bench_origin(e: dict) -> str:
    """The class this entry ENTERED the bench as — durable, set once, never
    rewritten.

    `source` is the DISPATCH label and is legitimately mutable: a stopped name
    re-judged HOLD really is a hold-verdict waiter now, and _update_locked
    rewrites it accordingly. But the IG census partitions on that same field,
    so every relabel silently moved a name out of the class it came from. On
    2026-08-20 that produced `by_obligation` exceeding `by_source` for
    book_full (5 vs 3) and stop_out (3 vs 1) — DK, SYRE, PRSU and TXN all
    carrying live obligations under a label that hid them — and the IG
    (correctly) filed it CRITICAL: a per-source arming census that cannot see a
    class cannot measure it.

    `origin` answers the provenance question the census actually needs, while
    `source` keeps answering the dispatch question the promotion loop needs.
    Legacy entries written before this field existed are derived from their
    durable payload blocks, so the backfill needs no migration pass."""
    o = e.get("origin")
    if isinstance(o, str) and o:
        return o
    if _stop_out_open(e):
        return "stop_out"
    if _book_full_open(e):
        return "book_full"
    return str(e.get("source") or "UNKNOWN_SOURCE")


def _stamp_origin(e: dict, cls: str) -> dict:
    """Set origin ONCE. A second write would recreate the very mutability that
    made the census blind."""
    e.setdefault("origin", cls)
    return e


def bench_obligations(e: dict) -> set:
    """Every provenance class this bench entry actually belongs to.

    THE PARTITION AND THE PROVENANCE ARE DIFFERENT QUESTIONS (operator
    2026-08-19). `source` is the DISPATCH label and it is legitimately mutable —
    a stopped name re-judged HOLD really is a hold-verdict waiter now. But the
    IG's bench census partitions on that same mutable label, so when a re-judge
    relabels an entry the class it came from silently loses a member. The audit
    then reports `by_source {hold_verdict 76, near_miss 36, book_full 4}` with
    `partition_sums_to_total: true` — an arithmetically complete partition over
    a set that is missing a member. Nothing in the snapshot can reveal that,
    which is why brief 73 arrived as "a reopen path that reports 0 armed while
    firing twice" instead of "the label was overwritten".

    The emitters' payload blocks are never deleted, so they are the durable
    record. A name can carry several obligations at once (stopped once,
    book-full later), so this returns a SET and callers must not treat it as a
    partition. Exported (no underscore) because scripts/ig_audit.py consumes it:
    brief 73 asks that the census derive mechanically from the real emitters,
    and one shared classifier is the only way the two cannot drift apart."""
    out = {str(e.get("source") or "UNKNOWN_SOURCE"), bench_origin(e)}
    if _stop_out_open(e):
        out.add("stop_out")
    if _book_full_open(e):
        out.add("book_full")
    return out


def _book_full_open(e: dict) -> bool:
    """Is there still an UNRESOLVED book-full obligation on this entry?

    Book-full parks a name that the committee wanted and the books had no room
    for; the obligation is "re-compete when a slot frees". That obligation dies
    when the THESIS dies, and until 2026-08-21 nothing retired it. Measured on
    the live bench: all 5 carriers had been re-judged within 9 days and every
    one came back HOLD at LOWER conviction than when it was benched — DK 63->57
    over 3 rechecks, SYRE 61->47, BFLY 60->48, DELL 56->53, SMWB 57->52. They
    were never stranded (the time-cadence path re-judged them on schedule, 20
    fires in 30d); the committee simply kept saying no. But the payload
    persisted, so they counted as an open book-full population forever and the
    IG read "5 armed, 0 fires in 30d" as trapped capital.

    Retiring the obligation on a non-BUY re-judge keeps the PROVENANCE (the
    block stays for audit, and `origin` is durable) while stopping a dead
    thesis from counting as a live claim on a slot.

    Note the deliberate asymmetry with _stop_out_open: a stop-out's reclaim is
    a PRICE level and stays valid whatever the rating does, so it is not
    retired here."""
    bf = e.get("book_full")
    return isinstance(bf, dict) and bool(bf) and not bf.get("resolved_at")


def _reclaim_leg(e: dict, reclaim_pct: float) -> dict | None:
    """The reclaim_price leg an open stop-out is owed, from its stored exit."""
    ex = (e.get("stop_out") or {}).get("exit")
    if not isinstance(ex, (int, float)) or ex <= 0:
        return None
    return {"kind": "reclaim_price", "price": round(ex * (1 + reclaim_pct / 100), 2)}


def _has_reclaim_leg(rt) -> bool:
    """Can a reclaim_price leg fire ON ITS OWN in this trigger tree?

    Only OR-reachable legs count. A reclaim nested under an `all` (or `after`,
    which is also a conjunction) is gated behind another condition and does NOT
    satisfy the stop-out obligation, which is "price reclaimed the exit =>
    reconsider", full stop. PRSU is the live case: its tree is
    {any: [{all: [cross_sma 50 above, reclaim_price 54.57]}, catalyst]} — a
    reclaim that can only fire if the 50-day is ALSO reclaimed, i.e. strictly
    stricter than what add_stop_out armed. Treat that as unarmed and OR a
    standalone leg alongside it."""
    if not isinstance(rt, dict):
        return False
    if rt.get("kind") == "reclaim_price":
        return True
    if rt.get("op") == "any":
        return any(_has_reclaim_leg(l) for l in (rt.get("legs") or []))
    return False        # all / after => conjunctive, cannot fire alone


def _ensure_reclaim_arm(e: dict, reclaim_pct: float) -> bool:
    """Guarantee an open stop-out keeps a reclaim_price leg, COMPOSING with
    whatever trigger the committee last named instead of replacing it.

    The hold_verdict re-judge at :1224-1228 unconditionally rewrites
    reopen_trigger ("Always (re)set so a re-check that CHANGES or drops the
    trigger can't leave a stale technical condition firing", review 2026-06-14).
    That rule is right for the committee's OWN legs and wrong for the reclaim
    leg, which is not a committee opinion — it is the arithmetic consequence of
    where the position was stopped, and it stays true until the name re-enters.
    TXN lost exactly this: add_stop_out armed {reclaim_price 278.31} and the
    re-judge replaced it with {any: [cross_sma 50 above, catalyst]}, leaving the
    name reachable only by generic quant/time paths.

    OR-ing preserves both: the committee's setup fires it, and so does the
    reclaim. Returns True when it changed the entry."""
    if not _stop_out_open(e):
        return False
    rt = e.get("reopen_trigger")
    if _has_reclaim_leg(rt):
        return False
    leg = _reclaim_leg(e, reclaim_pct)
    if not leg:
        return False
    if not isinstance(rt, dict) or not rt:
        e["reopen_trigger"] = leg
    elif config.COMPOUND_TRIGGERS:
        e["reopen_trigger"] = {"op": "any", "legs": [rt, leg]}
    else:
        # Compound triggers off: a single leaf is all the schema allows, and the
        # reclaim is the leg that must survive (the committee's setup is
        # re-derivable next cycle; the stop price is not).
        e["reopen_trigger"] = leg
    return True


def _technical_trigger_met(entry: dict, tech: dict, last_price=None,
                           cat_fn=None, news_fn=None) -> str | None:
    """Did the SPECIFIC technical setup the committee named (its structured
    reopen_trigger) just occur? Evaluated against the name's CURRENT technicals each
    cycle. The (cat_fn, news_fn) kwargs default None so the legacy positional calls
    _tt(entry, tech) / _tt(entry, tech, px) are byte-identical. A flat leaf (no 'op')
    always routes to the unchanged legacy logic; a node only when the flag is on."""
    rt = entry.get("reopen_trigger")
    rt = rt if isinstance(rt, dict) else {}         # tolerate corrupt/legacy state (str/None)
    if not rt:
        return None
    tech = tech or {}
    px = tech.get("price")
    if px is None:
        px = last_price
    if config.COMPOUND_TRIGGERS and "op" in rt:
        r = _eval_leg(rt, entry, tech, px, cat_fn=cat_fn, news_fn=news_fn,
                      ticker=entry.get("ticker", ""), depth=0)
        return r if (r and r is not _UNKNOWN) else None
    # LEGACY leaf path — identical to today (catalyst/news/none -> None; the
    # promotions loop reopens those via _catalyst_landed / _news_trigger_hit).
    kind = rt.get("kind")
    if not kind or kind in ("none", "catalyst", "news"):
        return None
    r = _eval_single_leg(rt, entry, tech, px)
    return r if (r and r is not _UNKNOWN) else None


def _normalize_trigger(rt, depth: int = 0):
    """Write-time tolerance: fold the committee's imperfect JSON into a clean leaf, a
    clean node, or None. Tolerates the legacy flat leaf, the new node, a hybrid
    {kind,...,op,legs}, a reversed `after`, a non-list `legs`, a `none` leg inside an
    `all`, and over-depth/over-cap — never crashes, never stores-but-dead."""
    if not isinstance(rt, dict):
        return None
    has_op = "op" in rt and rt.get("op") in _COMPOUND_OPS
    if has_op and config.COMPOUND_TRIGGERS and depth < _MAX_DEPTH:
        raw = rt.get("legs")
        raw = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
        op = rt["op"]
        if op == "after":
            norm = [n for n in (_normalize_trigger(l, depth + 1) for l in raw[:2]) if n]
            gate = next((l for l in norm if _trigger_kinds(l) & {"catalyst", "news"}), None)
            then = next((l for l in norm if l is not gate), None)
            if gate is None:
                return then          # no valid gate -> salvage to the technical leg
            return {"op": "after", "legs": [gate, then] if then else [gate]}
        norm = [n for n in (_normalize_trigger(l, depth + 1) for l in raw) if n]
        norm = [l for l in norm if not (("op" not in l) and l.get("kind") == "none")]
        if not norm:
            return None
        if len(norm) == 1:
            return norm[0]           # collapse a 1-leg node to that leaf
        return {"op": op, "legs": norm[:_MAX_LEGS]}
    kind = rt.get("kind")
    if kind in _PRIMITIVE_KINDS or kind in ("catalyst", "news", "none"):
        return rt
    return None


def _derive_leg(ptech: dict, earnings_date) -> dict | None:
    """Derive ONE conservative leaf from the numeric thesis in hand when a non-
    degraded HOLD emitted no usable trigger. Price legs ONLY when price-consistent
    (never an invented number that fires next cycle); prefer wait-for-weakness."""
    rsi = ptech.get("rsi14")
    px = ptech.get("price")
    if isinstance(rsi, (int, float)) and rsi > 55:
        return {"kind": "rsi_below", "level": max(40, int(rsi) - 10)}
    if ptech.get("above_sma50") is True:
        return {"kind": "cross_sma", "period": 50, "dir": "below"}
    if isinstance(px, (int, float)) and px > 0:
        return {"kind": "pullback_pct", "pct": float(_param("derive_pullback_pct", 8.0))}
    if earnings_date:
        return {"kind": "catalyst"}
    return None


def _news_since(entry: dict, ticker: str) -> bool:
    """Freshness body of the news trigger (2026-08-13 IG audit, split out of
    _news_trigger_hit): has fresh trusted news broken since this entry's
    verdict? A news LEG nested inside a COMPOUND trigger must call this
    directly — the old single function re-read the ENTRY's top-level
    reopen_trigger kind, which on a compound entry is the node's `op`, not
    "news", so every nested news leg returned False and could never fire."""
    since = (entry.get("last_verdict") or {}).get("ts") or entry.get("added", "")
    try:
        from firm import data_engine
        return bool(data_engine.has_news_since(ticker, since))
    except Exception:
        return False


def _news_trigger_hit(entry: dict, ticker: str) -> bool:
    """For a HOLD waiting on an UNSCHEDULED news catalyst (sector sentiment, a
    geopolitical/legal/FDA event, new research — anything not on the FRED/earnings
    calendar): reopen when fresh trusted news has broken since the verdict. These
    names are NOT eviction-protected (no date to wait for) — they age out normally
    and reopen if the news lands first. Flat-leaf path only: the top-level kind
    gate stays HERE; compound legs go through _news_since (2026-08-13 IG audit)."""
    rt = entry.get("reopen_trigger")
    rt = rt if isinstance(rt, dict) else {}
    if rt.get("kind") != "news":
        return False
    return _news_since(entry, ticker)


def _refresh_catalyst_dates(wl: dict) -> None:
    """Keep each benched HOLD's earnings_date CURRENT. Capture-at-bench-time
    alone is unreliable: a vendor miss (or, 2026-06-14, names benched before FMP
    was wired) leaves it None and the whole catalyst-reopen + eviction-protection
    machinery goes toothless — the entire live bench had earnings_date=None,
    incl. SNDK whose thesis was literally 'wait for the print'. Refresh only when
    missing or already past, so cost stays bounded (and next_earnings is cached)."""
    from datetime import date as _date
    today = datetime.now(timezone.utc).date()
    for t, e in wl.items():
        # book_full waiters carry a live BUY thesis into their next print too
        # (2026-07-16) — same date hygiene as holds.
        if e.get("source") not in ("hold_verdict", "book_full"):
            continue
        ed = e.get("earnings_date")
        if ed:
            try:
                if _date.fromisoformat(str(ed)[:10]) >= today:
                    continue               # already have a valid upcoming date
            except (ValueError, TypeError):
                pass                       # garbage stored -> re-fetch below
        try:
            from firm import data_engine
            ne = data_engine.next_earnings(t)
        except Exception:
            ne = None
        if ne and ne.get("date"):
            e["earnings_date"] = ne["date"]
            e["earnings_session"] = ne.get("session")
            e.pop("earnings_date_misses", None)
        else:
            # Per-hold miss counter (fix brief 2026-07-17 #4, CLYM): a vendor
            # that simply HAS no date for a name (small biotechs routinely)
            # leaves the earnings-catalyst reopen path inert forever while
            # every aggregate said 'ok' — the exact 'dates never captured'
            # class that burned the whole bench once. Count the failures ON
            # the entry so the health desk names the hold and its dated
            # fallback (technical trigger / recheck-then-eviction clock)
            # instead of absorbing it into a green. Journaled once, at 3
            # misses, so a transient vendor blip never spams.
            e["earnings_date_misses"] = int(e.get("earnings_date_misses") or 0) + 1
            if e["earnings_date_misses"] == 3:
                state.journal("earnings_date_unresolvable", ticker=t,
                              misses=3, source=e.get("source", ""))


def _reconcile_bench_stamps(wl: dict) -> None:
    """Sync each benched HOLD's last_verdict UP to the newest NON-degraded
    verdict in the research history (2026-07-10 stamp-lag heal — see the call
    site). Idempotent: only advances a stamp, never rewinds it, never creates
    one for a name history never judged. Degraded verdicts are excluded (they
    are non-authoritative and must not stamp a name, per _update_locked)."""
    hist = (state.load("research", {}) or {}).get("history", [])
    if not hist:
        return
    latest: dict = {}                      # ticker -> (asof, rating, conviction)
    for h in hist:
        asof = h.get("asof", "")
        for tk, v in (h.get("verdicts") or {}).items():
            if v.get("degraded") or not asof:
                continue
            if tk not in latest or asof > latest[tk][0]:
                latest[tk] = (asof, v.get("rating"), v.get("conviction"))
    for t, e in wl.items():
        if e.get("source") != "hold_verdict":
            continue
        lv = latest.get(t)
        if lv and lv[0] > (e.get("last_verdict") or {}).get("ts", ""):
            e["last_verdict"] = {"rating": lv[1], "conviction": lv[2], "ts": lv[0]}


def overdue_reopens(today=None) -> list[dict]:
    """LIVE-STATE watchdog (2026-06-14): benched HOLDs whose catalyst LANDED and a
    research cycle has SINCE run, yet the name was never re-judged (its
    last_verdict still predates the catalyst). Non-empty = the reopen machinery
    silently FAILED — exactly the class of bug a sandbox test suite cannot see
    (it proves the reopen LOGIC, not that real state actually fired). Cheap, no
    API. Surfaced by health.run_health_sweep() so the operator is told, not
    relied on to notice."""
    from datetime import date as _date
    import firm.macro_calendar as macro_calendar
    today = today or datetime.now(timezone.utc).date()
    research = state.load("research", {})
    last_cycle = None
    try:
        last_cycle = datetime.fromisoformat(research.get("latest_asof", "")).date()
    except (ValueError, TypeError):
        return []                          # no cycle has ever run -> nothing is overdue
    out = []
    for t, e in state.load("watchlist", {}).items():
        if e.get("source") != "hold_verdict":
            continue
        lv = (e.get("last_verdict") or {}).get("ts") or e.get("added", "")
        try:
            vd = datetime.fromisoformat(lv).date()
        except (ValueError, TypeError):
            continue
        landed = []
        ed = e.get("earnings_date")
        if ed:
            edd = macro_calendar._safe_date(ed)
            if edd and vd < edd and last_cycle > edd:   # print passed + a cycle ran after it
                landed.append(f"earnings {edd.isoformat()}")
        # A generic macro print is a reopen path ONLY for a name with no OTHER
        # way back. A benched HOLD carrying its own non-macro trigger (a pending
        # earnings date OR a technical pullback/RSI/SMA leg) is deliberately
        # macro-IMMUNE — it waits for ITS thesis, and _cooldown_state does not
        # reopen it on a generic CPI/PCE/jobs print (momentum lane 2026-06-23 +
        # bench thesis gate 2026-07-09). Flagging such a name "overdue for a
        # macro" it is designed to ignore is a false alarm that can never
        # self-clear (2026-07-10: APPS, momentum factor 100, waiting on its
        # 08-04 earnings, redded 'catalyst readiness' every 30 min for a macro
        # reopen that — correctly — will never fire). Keep the macro branch ONLY
        # for names whose macro IS their lifeline (no non-macro path), so a real
        # macro-reopen failure is still caught; the earnings branch above always
        # fires. Mirrors cycle._has_nonmacro_reopen (kept inline — no import cycle).
        _macro_immune = bool(ed) or _nonmacro_reopen_ok(e.get("reopen_trigger"))
        if not _macro_immune:
            m = macro_calendar.landed_since(vd, today)
            if m and last_cycle > m:                     # macro dropped + a cycle ran after it
                landed.append(f"macro {m.isoformat()}")
        if landed:
            out.append({"ticker": t, "landed": landed, "last_verdict": vd.isoformat()})
    return out


def unreviewable_holds(today=None) -> list[str]:
    """LIVE-STATE watchdog (IG 2026-06-14): bench HOLDs with NO path back to
    review — no earnings_date, no technical reopen_trigger, not awaiting a macro
    catalyst, AND past the recheck budget (so the time-cadence won't re-review
    and they SHOULD already have been evicted). Non-empty = the eviction pass
    silently failed and a name is a permanent zombie. Normally empty: a no-path
    hold is re-reviewed on the time cadence and aged off at MAX_RECHECKS. Cheap,
    no API. Makes the 'stuck hold' concern falsifiable (the IG's ask)."""
    out = []
    max_re = int(_param("max_rechecks", MAX_RECHECKS))
    for t, e in state.load("watchlist", {}).items():
        if e.get("source") != "hold_verdict":
            continue
        has_earn = bool(e.get("earnings_date"))
        rt = e.get("reopen_trigger")
        has_trig = bool(_trigger_kinds(rt))    # walks compound nodes too
        if (not has_earn and not has_trig and not _awaiting_near_catalyst(e)
                and e.get("recheck_count", 0) >= max_re):
            out.append(t)
    return out


_LIVENESS_SAMPLES = {
    # One synthetic, schema-valid leg per supported reopen kind — the exact
    # shapes debate.py's schema tells the judge to emit.
    "pullback_pct": {"kind": "pullback_pct", "pct": 8.0},
    "below_price": {"kind": "below_price", "price": 10.0},
    "reclaim_price": {"kind": "reclaim_price", "price": 10.0},
    "rsi_below": {"kind": "rsi_below", "level": 50},
    "cross_sma": {"kind": "cross_sma", "period": 50, "dir": "below"},
    "catalyst": {"kind": "catalyst"},
    "news": {"kind": "news"},
}


# The DECLARED reopen paths — the design's five ways a benched name gets back
# to research. ONE definition, imported by scripts/ig_audit.py and firm/health.py
# (fix brief 2026-07-27 #1): the audit used to keep its own copy of this list in
# its snapshot while reopen_kind_liveness reported the debate schema's seven
# TRIGGER KINDS, and nothing reconciled the two taxonomies. That gap is how the
# auditor filed a CRITICAL declaring the macro path "inert" — it saw
# `reopen_trigger_kinds.catalyst == holds_armed_earnings_catalyst` and inferred
# zero macro arms, when in fact ONE `{"kind": "catalyst"}` arm resolves through
# EITHER branch of _catalyst_landed, so those are the same entries armed twice
# over. The journal says the macro branch had fired 165 times by then — the most
# active reopen path in the fund. A measurement that cannot express a path
# cannot exonerate one either.
# SEVEN paths, not five (IG 2026-07-28 #6/#8, operator). The original list was
# copied from the audit's prose and omitted the two paths that actually reopen
# the classes it kept flagging: `quant_improvement` is the ONLY way a near_miss
# entry returns (which is why the census read "near_miss unarmed on every path"
# and looked like a stranded cohort), and `stop_out_reclaim` is how a stopped
# name comes back. A declaration that omits real paths cannot certify anything.
REOPEN_PATHS = ("technical_trigger", "earnings_catalyst",
                "macro_catalyst_gated_to_non_immune", "news_catalyst",
                "time_cadence_recheck_then_eviction",
                "quant_improvement", "stop_out_reclaim",
                # Found by the regeneration test itself, not by reading the
                # prose a third time: a book_full entry re-competes when a slot
                # frees. Eight emitters, eight declared paths.
                "book_full_slot_reopened")

# THE LITERAL STRINGS promotions() EMITS — not a guess at them (IG #8; this is
# the same defect I had just written up as a lesson and then repeated). The
# first version matched on "recheck", while the emitter at watchlist.py:1460
# writes "HOLD re-check due" WITH A HYPHEN, so the time-cadence path reported 0
# fires having fired 7 times, and `stop-out recovery` + `quant improved` both
# fell through to technical_trigger. Regenerate with:
#   grep -nE 'picks\.append|cat_picks\.append' firm/watchlist.py
# Each entry is (path, literal prefix as emitted). Prefix-anchored, because a
# substring search over free text is what produced the miscount.
_PATH_REASON_MARKERS = (
    ("earnings_catalyst", ("catalyst: earnings printed", "earnings printed")),
    ("macro_catalyst_gated_to_non_immune", ("catalyst: macro data landed",
                                            "catalyst: awaited data landed",
                                            "macro data landed",
                                            "awaited data landed")),
    ("news_catalyst", ("catalyst: news", "news")),
    ("time_cadence_recheck_then_eviction", ("hold re-check due",)),
    ("stop_out_reclaim", ("stop-out recovery",)),
    # Two variants of the same rule: a normal quant gain, and the saturated-
    # scale fallback when the ranking pins at its ceiling.
    # THREE variants of the same rule now: a normal quant gain, the
    # saturated-scale fallback, and the tier-cut re-entry that is the near-miss
    # class's actually-reachable route (operator 2026-08-21). Registering the
    # literal HERE in the same change that emits it is the point — the
    # regeneration test caught this within one run, which is exactly what it is
    # for: an unregistered reason reports `unattributed` and the path it really
    # belongs to silently under-counts its own fires.
    ("quant_improvement", ("quant improved", "quant at ceiling",
                           "back inside the tier cut")),
    ("book_full_slot_reopened", ("book-full buy",)),
    ("technical_trigger", ("setup hit",)),
)


def _path_of_reason(why: str) -> str:
    """Map an emitted promotion reason to its declared path, or `unattributed`.

    Deliberately NOT defaulting to technical_trigger any more: a reason this
    function does not recognise is an INSTRUMENTATION GAP, and silently filing
    it under a real path is what let three of the five emitters hide inside
    technical_trigger's count."""
    low = str(why or "").strip().lower()
    for path, markers in _PATH_REASON_MARKERS:
        if any(low.startswith(m) for m in markers):
            return path
    for path, markers in _PATH_REASON_MARKERS:      # tolerate a prefixed wrapper
        if any(m in low for m in markers):
            return path
    return "unattributed"


def reopen_path_liveness(window_days: int = 30) -> dict:
    """One row per DECLARED reopen path — what brief #1 actually asked for.

    `armed_now` counts bench entries for which the path is currently AVAILABLE;
    `fired_in_window` counts reopens the journal attributes to it. The second
    number is the one that settles an inert-path argument, because a path can
    be armed on every entry and still never resolve (the failure mode this
    whole family of briefs keeps circling), and — as the macro path showed — it
    can be invisible to the arm census and still be the busiest path in the
    system. Read-only; safe to call from a health sweep."""
    wl = state.load("watchlist", {}) or {}
    entries = [e for e in wl.values() if isinstance(e, dict)]
    armed = {p: 0 for p in REOPEN_PATHS}
    # Entries that CARRY each obligation, whether or not they can fire it now.
    # Paths without an eligibility predicate keep population == armed.
    population = {p: 0 for p in REOPEN_PATHS}
    for e in entries:
        # ARM ON WHAT CAN FIRE ALONE (operator 2026-08-21). `kinds` walks the
        # whole tree, so a leg nested under an `all`/`after` counted as armed
        # even though it cannot reopen the name without its partner. `reach` is
        # the OR-reachable subset — the legs that can actually fire the path.
        # Population keeps using the full walk, so an entry that CARRIES a kind
        # is still visible as population even when that kind is conjunctive:
        # that is exactly the idle-vs-wiring distinction below.
        kinds = _trigger_kinds(e.get("reopen_trigger"))
        reach = _or_reachable_kinds(e.get("reopen_trigger"))
        if kinds & _NONMACRO_TECH:
            population["technical_trigger"] += 1
        if reach & _NONMACRO_TECH:
            armed["technical_trigger"] += 1
        if "catalyst" in kinds:
            population["macro_catalyst_gated_to_non_immune"] += 1
            if e.get("earnings_date"):
                population["earnings_catalyst"] += 1
        if "news" in kinds:
            population["news_catalyst"] += 1
        if "news" in reach:
            armed["news_catalyst"] += 1
        if "catalyst" in reach:
            # Both catalyst branches ride the SAME arm — see _catalyst_landed.
            # Earnings additionally needs a date to resolve against; macro
            # resolves off the firm-wide calendar, so any catalyst arm has it.
            armed["macro_catalyst_gated_to_non_immune"] += 1
            if e.get("earnings_date"):
                armed["earnings_catalyst"] += 1
        # The timer needs no arm at all: it is the floor that guarantees no
        # benched name is ever stuck, bounded by MAX_RECHECKS then eviction.
        if int(e.get("recheck_count") or 0) < int(_param("max_rechecks", MAX_RECHECKS)):
            armed["time_cadence_recheck_then_eviction"] += 1
        # The quant-improvement rule needs a banked baseline to improve FROM;
        # it is the near_miss class's only route back (promotions(): cur_q -
        # first_q >= improve_pts).
        if e.get("history"):
            armed["quant_improvement"] += 1
        # Count the OBLIGATION, not the label (operator 2026-08-19). Reading
        # `source` here made a whole population invisible the moment a re-judge
        # relabelled it, so this line reported "never armed" while the path was
        # firing — the false-inert verdict behind 338 sweeps of red.
        if _stop_out_open(e):
            armed["stop_out_reclaim"] += 1
            population["stop_out_reclaim"] += 1
        # ARM ONLY WHAT CAN ACTUALLY FIRE (operator 2026-08-21). This counted
        # every entry carrying a book_full payload, but _promotions_locked only
        # takes that branch when the entry's LAST verdict is still BUY-family:
        # "If the last real verdict is no longer BUY-family, this branch is
        # skipped and the generic paths govern." Measured live — all 5 armed
        # entries (DK, SYRE, SMWB, BFLY, DELL) had since been re-rated HOLD at
        # conviction 47-57, so ZERO of the 5 were eligible, yet the census
        # reported "armed on 5 entries but 0 fires in 30d" and the liveness
        # check called the path INERT for 390 consecutive sweeps. A path is not
        # inert when nothing is eligible to fire it; it is simply idle, and
        # counting ineligible entries as armed manufactures a permanent red.
        # Same class as the stop_out false-inert fixed on 08-19, opposite sign:
        # that one under-counted a live population, this one over-counts a dead
        # one.
        if _book_full_open(e):
            # POPULATION vs ARMED (operator 2026-08-21). Population = entries
            # that carry this obligation at all. Armed = entries that could
            # fire the path TODAY. Separating them is what tells a WIRING
            # defect ("nothing can ever arm this") apart from an ordinary IDLE
            # state ("the obligation exists, nothing currently qualifies").
            # Collapsing the two is what made book_full_slot_reopened read
            # INERT for 390 sweeps while behaving exactly as designed.
            population["book_full_slot_reopened"] += 1
            if str((e.get("last_verdict") or {}).get("rating")
                   or "") in ("BUY", "STRONG_BUY"):
                armed["book_full_slot_reopened"] += 1

    fired = {p: 0 for p in REOPEN_PATHS}
    fired['unattributed'] = 0
    last_fired: dict[str, str] = {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    # WINDOW COVERAGE (2026-08-13 IG audit — the load-bearing half of this
    # fix). The old read_journal(3000) claimed a 30d census but spanned only
    # ~8 days of a busy journal, and journal rotation (config JOURNAL_MAX_BYTES
    # -> JOURNAL_KEEP_LINES=3000) can truncate below ANY fixed read size — so
    # the read is sized generously AND coverage is measured from the events
    # actually returned. A fund/journal YOUNGER than the window still counts as
    # covered when the whole never-rotated journal was read (fewer events than
    # JOURNAL_KEEP_LINES back = rotation has never discarded anything, so
    # "never fired" is a fact about the fund's whole life, not a window gap).
    _read_n = 20000
    oldest_read = None
    window_covered = False
    try:
        _evs = state.read_journal(_read_n)
        _tss = [str(ev.get("ts", "")) for ev in _evs if ev.get("ts")]
        oldest_read = min(_tss) if _tss else None
        window_covered = (
            (oldest_read is not None and oldest_read <= cutoff)
            or len(_evs) < min(_read_n,
                               int(getattr(config, "JOURNAL_KEEP_LINES", 3000))))
        for ev in _evs:
            if ev.get("event") != "watchlist_promotions" or ev.get("ts", "") < cutoff:
                continue
            for item in (ev.get("tickers") or []):
                why = item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else item
                p = _path_of_reason(why)
                fired[p] = fired.get(p, 0) + 1
                last_fired[p] = max(last_fired.get(p, ""), str(ev.get("ts", ""))[:10])
    except Exception:
        pass
    out = {p: {"armed_now": armed[p], "fired_in_window": fired[p],
               "last_fired": last_fired.get(p) or None,
               "window_days": window_days,
               "window_covered": window_covered,
               "oldest_journal_read": oldest_read,
               # TWO DISTINCT FAILURES, not one boolean (IG 2026-07-28 #8, and
               # the IG was right). The first cut called a path healthy whenever
               # it had arms, so `armed on 69 entries, fired 0 times in 30 days`
               # read as fine — which is precisely the declared-but-inert shape
               # this function exists to catch. A path nothing can arm and a
               # path that arms but never converts are different bugs with
               # different fixes, and neither is health.
               # (2026-08-13 IG audit) never_armed is journal-independent (a
               # census of the live bench), so it always asserts; the fires-
               # based verdicts may only be DECLARED over a covered window —
               # an armed path over a part-window is undetermined, not inert.
               "population": max(population[p], armed[p]),
               "never_armed": armed[p] == 0 and max(population[p], armed[p]) == 0,
               # The obligation exists but nothing qualifies right now — a
               # legitimate idle state, NOT a wiring defect and NOT inert.
               "idle_no_eligible": (max(population[p], armed[p]) > 0
                                    and armed[p] == 0),
               "armed_but_never_fires": (window_covered
                                         and armed[p] > 0 and fired[p] == 0),
               "inert": ((max(population[p], armed[p]) == 0)
                         or (window_covered and armed[p] > 0 and fired[p] == 0)),
               **({"undetermined": True,
                   "why": (f"journal window does not cover the full "
                           f"{window_days}d (oldest event read: "
                           f"{str(oldest_read)[:10]}) — fires unmeasurable")}
                  if (armed[p] > 0 and not window_covered) else {})}
           for p in REOPEN_PATHS}
    # Reasons the attribution could not place are an instrumentation gap, and
    # must be visible rather than absorbed into a real path's count.
    if fired.get("unattributed"):
        out["unattributed"] = {
            "armed_now": 0, "fired_in_window": fired["unattributed"],
            "last_fired": last_fired.get("unattributed"), "window_days": window_days,
            "never_armed": False, "armed_but_never_fires": False, "inert": False,
            "note": ("promotion reasons that matched no declared path — "
                     "_PATH_REASON_MARKERS is stale against the emitters")}
    return out


def reopen_kind_liveness() -> dict:
    """Fix brief 2026-07-17 #5: 'declared-but-inert' must be MEASURED, not
    re-argued every audit. For each supported reopen kind, verify the TWO ways
    a kind can go structurally dead: (a) the debate schema no longer OFFERS it
    to the judge, (b) _normalize_trigger silently DROPS a valid leg of it at
    write time. A kind failing either is a real defect (health reds). A kind
    passing both with zero armed instances is OBSERVED-ZERO — the committee
    simply hasn't chosen it — which the IG kept re-flagging as suspicious;
    now the snapshot carries the proof it CAN fire, per kind. Deliberately no
    K-day never-armed alarm (validate-before-harden: below_price/reclaim_price
    have legitimately never fit a thesis) — observed-zero stays permanently
    visible in the health detail instead."""
    try:
        from firm import debate
        schema = debate._verdict_schema()
    except Exception:
        schema = ""
    armed: dict[str, int] = {}
    for e in state.load("watchlist", {}).values():
        for k in _trigger_kinds(e.get("reopen_trigger")):
            armed[k] = armed.get(k, 0) + 1
    out = {}
    for k, sample in _LIVENESS_SAMPLES.items():
        norm = _normalize_trigger(dict(sample))
        out[k] = {"offered_in_schema": bool(schema) and k in schema,
                  "survives_normalization": bool(norm) and k in _trigger_kinds(norm),
                  "armed_now": int(armed.get(k, 0))}
    return out


def holds_missing_earnings_date() -> list[dict]:
    """Fix brief 2026-07-17 #4 (CLYM): every benched HOLD whose earnings-
    catalyst reopen path is INERT (no earnings_date after the refresh pass),
    named individually WITH its dated fallback — never absorbed into an
    aggregate 'ok'. Distinct from unreviewable_holds (the zombie list): each
    of these IS reviewable via its fallback; the defect being surfaced is that
    the catalyst path the bench design assumes is armed, isn't, and only a
    per-name line makes the operator (and the next IG) see WHICH name the
    vendor keeps failing on."""
    out = []
    max_re = int(_param("max_rechecks", MAX_RECHECKS))
    for t, e in state.load("watchlist", {}).items():
        if e.get("source") != "hold_verdict" or e.get("earnings_date"):
            continue
        kinds = sorted(_trigger_kinds(e.get("reopen_trigger")))
        out.append({
            "ticker": t,
            "vendor_misses": int(e.get("earnings_date_misses") or 0),
            "fallback": (f"technical trigger ({'/'.join(kinds)})" if kinds
                         else (f"recheck clock "
                               f"{int(e.get('recheck_count') or 0)}/{max_re}, "
                               "then eviction")),
        })
    return out


def _backfill_added(wl: dict) -> None:
    """`added` is the age-ceiling's clock: an entry whose `added` is missing or
    unparseable reads age 0 forever (`_age_days(e.get("added", now))`) and can
    NEVER age off the bench (IG 2026-07-03 high, ratified — WDC sat live with
    no `added`). Backfill from the oldest evidence carried on the entry so the
    30-day ceiling is a real guarantee for EVERY entry, not an assumption."""
    for t, e in wl.items():
        try:
            datetime.fromisoformat(str(e.get("added") or ""))
        except (ValueError, TypeError):
            e["added"] = ((e.get("history") or [{}])[0].get("ts")
                          or (e.get("last_verdict") or {}).get("ts") or _now())
            state.journal("watchlist_added_backfilled", ticker=t,
                          added=e["added"])


def unreviewable_entries() -> list[str]:
    """LIVE-STATE watchdog (IG 2026-07-03 high, ratified): the reviewability
    guarantee must cover EVERY bench entry, not just holds. Non-hold entries
    (near_miss, stop_out) are evicted by the age ceiling / vanish / decay pass;
    the universal backstop is the age ceiling, whose clock is `added` — an
    entry with no parseable `added` escapes it forever. Normally empty:
    update() backfills via _backfill_added on every cycle; non-empty means an
    unrepaired entry is un-evictable RIGHT NOW (surfaced by health so the
    operator is told, not relied on to notice)."""
    out = []
    for t, e in state.load("watchlist", {}).items():
        try:
            datetime.fromisoformat(str(e.get("added") or ""))
        except (ValueError, TypeError):
            out.append(t)
    return out


def _etf_like() -> set[str]:
    return (set(config.ETF_UNIVERSE) | set(config.ROTATION_UNIVERSE)
            | {config.CASH_ETF, config.BENCHMARK})


def _param(key: str, default: float) -> float:
    """Bench thresholds are LEARNED: the Opus performance review may re-tune
    them (within hard bounds in learning.BENCH_BOUNDS) based on what actually
    happened to dropped and promoted names. Overrides live in state/tuning.json."""
    v = (state.load("tuning", {}).get("bench") or {}).get(key)
    return float(v) if isinstance(v, (int, float)) else default


def _note_quant(e: dict, ts: str, q: float) -> None:
    """Record ONE quant observation on an entry, idempotent within a cycle.
    Several paths touch the same name in one update() (near-miss pool, the
    in-scan refresh, the HOLD verdict loop) — without the same-ts guard a name
    would bank two or three identical points per cycle and blow its 30-point
    window out in a third of the time (audit 2026-07-26 #3, added with the
    missing-history fix below)."""
    h = e.get("history") or []
    if h and h[-1].get("ts") == ts:
        return
    e["history"] = (h + [{"ts": ts, "quant": q}])[-30:]


def _scale_saturated(scores: dict) -> bool:
    """Is the quant scale piled onto its ceiling this cycle (2026-07-28)?

    True when a material share of the scored universe ties at the maximum, which
    means point-differences at the top carry no information. Measured live on
    2026-07-28: 142 of 300 names sat at exactly 93.0 and the top 60 were all
    identical, so any absolute "+N points" gate above the cap is unreachable by
    construction rather than merely demanding. Deliberately a RATIO test, not a
    hard-coded 93.0 — the clamp value is not ours to assume and a future rescale
    must not silently re-arm an impossible bar.
    """
    try:
        vals = [v for v in (scores or {}).values() if isinstance(v, (int, float))]
        if len(vals) < 20:
            return False
        top = max(vals)
        return (sum(1 for v in vals if v >= top - 0.05) / len(vals)) >= float(
            getattr(config, "QUANT_SATURATION_SHARE", 0.10))
    except Exception:
        return False


def _benched_quant(e: dict) -> float | None:
    """IMMUTABLE entry-quality baseline for the decay cut and the improve
    promotion (audit 2026-07-26 #2). Both used to read hist[0] of a ROLLING
    [-30:] window: once an entry banked more than 30 points the anchor silently
    SLID forward, so "decay since benched" quietly became "decay since a month
    ago" and the improve bar re-based against a moving target (INTC sat at
    exactly 30 points; all 34 live decay drops carried baseline 93.0/90.0 —
    the quant_score 100-clamp saturating the head of the window). Median of the
    FIRST 3 observations so one spiky print can't set the bar, FROZEN onto the
    entry the moment 3 exist and never rewritten; pre-existing entries are
    backfilled on first touch. None only when the name has no history at all."""
    v = e.get("benched_quant")
    if isinstance(v, (int, float)):
        return float(v)
    qs = [h["quant"] for h in (e.get("history") or [])
          if isinstance(h.get("quant"), (int, float))]
    if not qs:
        return None
    # The baseline IS the first observation, frozen at first touch. A median
    # of the first 3 was tried for spike protection and rejected by the suite.
    # The update flow appends the CURRENT print before the decay check runs,
    # so a 90 then 83 entry became history 90, 83, 83 whose median is 83 and
    # genuine early decay read as zero. The audit's complaint was the SLIDING
    # window anchor, never the first point choice, and the named test
    # "decayed setup cut early" is the spec for 90 to 83 equals cut. A spiky
    # first print that immediately decays is exactly a setup that decayed.
    bq = float(qs[0])
    e["benched_quant"] = bq            # frozen — from here it can never slide
    return bq


def _bench_event(event: dict) -> None:
    led = state.load("bench_ledger", {"events": []})
    led["events"] = (led.get("events", []) + [{**event, "ts": _now()}])[-300:]
    state.save("bench_ledger", led)


def _drop(ticker: str, reason: str, last_quant: float | None = None) -> None:
    state.journal("watchlist_dropped", ticker=ticker, reason=reason)
    _bench_event({"type": "drop", "ticker": ticker, "reason": reason,
                  "last_quant": last_quant})


def add_stop_out(ticker: str, sleeve: str, entry: float, exit_px: float,
                 reason: str, thesis: str = "", conviction: int | None = None) -> None:
    """A stopped-out position lands on the bench for re-entry consideration —
    the thesis travels with it so the committee can judge if it's broken."""
    reclaim = exit_px * (1 + _param("stop_reclaim_pct", STOP_OUT_RECLAIM_PCT) / 100)
    with state.SLEEVE_LOCK:
        wl = state.load("watchlist", {})
        prior = wl.get(ticker) or {}
        wl[ticker] = {
            "ticker": ticker,
            # A repeat stop-out keeps its original bench tenure and history;
            # only the cool-off clock (stop_out.ts) restarts — by design.
            "added": prior.get("added", _now()),
            "history": prior.get("history", []),
            "recheck_count": prior.get("recheck_count", 0),
            "source": "stop_out",
            "origin": prior.get("origin", "stop_out"),
            "watch_trigger": (f"re-entry: cool off {STOP_OUT_COOLOFF_DAYS}d+, then "
                              f"reclaim ${reclaim:.2f} (stopped: {reason}; "
                              "thesis may still be intact)"),
            # Arm the reclaim as a STRUCTURED trigger, not just prose (2026-07-13):
            # the dedicated stop_out promotion branch already recovers on price, but
            # with no reopen_trigger the IG coverage audit read reclaim_price as
            # "0 armings / inert" (a false-inert alarm) and _technical_trigger_met
            # had no price leg to back the branch up. This makes the wiring explicit
            # and the telemetry honest; the cool-off + branch still gate re-entry.
            "reopen_trigger": {"kind": "reclaim_price", "price": round(reclaim, 2)},
            "missing": 0,
            "stop_out": {"sleeve": sleeve, "entry": entry, "exit": exit_px,
                         "reason": reason, "thesis": thesis,
                         "conviction": conviction, "ts": _now()},
        }
        state.save("watchlist", wl)
        # INSIDE the lock (audit 2026-07-26 #5): _bench_event read-modify-writes
        # bench_ledger.json. Called out here it raced the cycle thread's own
        # _bench_event calls (_drop / graduations, which run under this same
        # RLock inside _update_locked) — two interleaved load->append->save
        # rounds silently LOSE one event, and the monitor thread benching a
        # stop-out is exactly the concurrent writer that trips it. The lock is
        # reentrant, so nesting the call is free.
        _bench_event({"type": "stop_out_benched", "ticker": ticker, "reason": reason})
    state.journal("stop_out_to_bench", ticker=ticker, sleeve=sleeve, reason=reason)


def _held_anywhere(ticker: str) -> bool:
    """Is this name actually IN a book right now (any sleeve, real position)?
    Cash parking never counts; a call position counts via its underlying."""
    for sl in ("alpha_stocks", "core_blend", "etf_rotation"):
        p = (state.get_sleeve(sl).get("positions") or {}).get(ticker)
        if p and not p.get("cash_equitization"):
            return True
    return any(p.get("underlying") == ticker
               for p in (state.get_sleeve("calls_only").get("positions") or {}).values())


def add_book_full(ticker: str, verdict: dict, qualified: list[str],
                  slots: dict, packet: dict | None = None) -> None:
    """A BUY the books had no room for lands on the bench (operator 2026-07-16:
    ALKS earned a BUY verdict, every qualified sleeve was slots-full, and the
    name was simply forgotten — 'liked but didn't buy' is exactly what the
    Bench is for). The thesis travels with it; promotion fires the moment a
    qualified sleeve has a free entry slot (_promotions_locked), and the
    standard decay/age exits still cut a fading setup."""
    with state.SLEEVE_LOCK:
        wl = state.load("watchlist", {})
        prior = wl.get(ticker) or {}
        e = {
            "ticker": ticker,
            "added": prior.get("added", _now()),
            "history": prior.get("history", []),
            "recheck_count": prior.get("recheck_count", 0),
            "source": "book_full",
            "origin": prior.get("origin", "book_full"),
            "qualified_sleeves": list(qualified),
            "watch_trigger": (f"book-full BUY (conv {verdict.get('conviction')}): "
                              f"waiting for a free slot in {'/'.join(sorted(slots))} "
                              "or a conviction rise that wins a swap"),
            "last_verdict": {"rating": verdict.get("rating"),
                             "conviction": verdict.get("conviction"), "ts": _now()},
            "book_full": {"thesis": verdict.get("thesis", ""),
                          "conviction": verdict.get("conviction"),
                          "slots": dict(slots), "ts": _now()},
            "missing": 0,
        }
        pk = packet or {}
        ne = pk.get("next_earnings") or {}
        if ne.get("date"):
            e["earnings_date"] = ne["date"]
            e["earnings_session"] = ne.get("session")
        _ptech = pk.get("technicals") or {}
        if isinstance(_ptech.get("price"), (int, float)):
            e["benched_price"] = _ptech["price"]
        wl[ticker] = e
        state.save("watchlist", wl)
        # INSIDE the lock — same lost-event race as add_stop_out (audit 2026-07-26 #5).
        _bench_event({"type": "book_full_benched", "ticker": ticker})
    state.journal("book_full_to_bench", ticker=ticker, qualified=list(qualified),
                  slots=dict(slots), conviction=verdict.get("conviction"))


def update(screen: dict, verdicts: dict) -> dict:
    """After each cycle: refresh the bench from the scan + new HOLD verdicts.
    Serialized under SLEEVE_LOCK — the monitor thread benches stop-outs on the
    same file and a lost write here means a lost stop-out record."""
    with state.SLEEVE_LOCK:
        return _update_locked(screen, verdicts)


def _refresh_quants(tickers: list[str], out_tech: dict | None = None) -> dict[str, float]:
    """Direct quant scores for bench names OUTSIDE the scan's ranked window
    (2026-06-12 operator catch: every near-miss froze at its add-day score,
    promotions could never fire, and the whole bench aged toward a silent
    mass-drop — the farm system was decorative). One batched download for
    the entire bench; {} on failure, and callers treat absent tickers as
    truly missing (delisted/halted) — the only honest reason to age out."""
    # Preserve caller PRIORITY order (refresh-critical names first — near-misses,
    # which vanish-evict on a missed refresh, before catalyst-waiters, which reopen
    # on their DATE and tolerate a stale score). Dedup keeping first occurrence so
    # the 80-budget always covers the names that actually NEED it (operator 2026-06-23).
    seen: set[str] = set()
    syms: list[str] = []
    for t in tickers:
        if t and t not in seen:
            seen.add(t)
            syms.append(t)
    syms = syms[:80]
    if not syms:
        return {}
    try:
        from firm import data_engine
        # benchmark exactly once even if a bench name == BENCHMARK (order-preserving
        # dedup above could otherwise leave it in syms AND re-append it here)
        fetch = [s for s in syms if s != config.BENCHMARK] + [config.BENCHMARK]
        closes = data_engine.fetch_history(fetch, period="1y")
        bench = closes[config.BENCHMARK].dropna()
        cols = [t for t in syms if t in closes.columns]
        if not cols:
            return {}
        tech = data_engine.compute_technicals(closes[cols], bench)
        if out_tech is not None:
            out_tech.update(tech)   # surface live RSI/SMA so a faded compound name's
            #                         rsi_below/cross_sma legs can actually evaluate
        return {t: data_engine.quant_score(m) for t, m in tech.items()}
    except Exception:
        return {}


def _apply_bench_cap(wl: dict) -> dict:
    """Cap the bench. PROTECTED names — catalyst-waiters (a KNOWN, not-yet-landed
    earnings/macro date, reopen on the DATE not a fresh quant) and names promoted
    to research THIS cycle (their bench record must survive until they re-bench) —
    are EXEMPT from BENCH_SIZE when BENCH_UNCAP_CATALYST. Only the OPEN-ENDED names
    (near-misses + no-catalyst HOLDs), which rely on the 80/cycle refresh to stay
    live, are capped at BENCH_SIZE. BENCH_HARD_MAX is a pure sanity backstop on the
    total — and it ranks PROTECTED status PRIMARY so the backstop drops open-ended
    names before the expiring-date names it exists to keep (operator 2026-06-23).
    Rebuilds on the dict key (never e['ticker']) so legacy/corrupt entries missing
    a 'ticker' field can't KeyError the whole cycle's bench update."""
    def _protected(e) -> bool:
        return _awaiting_near_catalyst(e) or bool(e.get("promoted_at"))

    def _keep_rank(kv):                       # kv = (ticker_key, entry)
        e = kv[1]
        h = e.get("history", [])
        q = h[-1]["quant"] if h else 0
        return (1 if _protected(e) else 0, q)  # protected PRIMARY, quant tiebreak
    if BENCH_UNCAP_CATALYST:
        protected = {t: e for t, e in wl.items() if _protected(e)}
        others = {t: e for t, e in wl.items() if t not in protected}
    else:
        protected, others = {}, dict(wl)
    cap = int(_param("bench_size", BENCH_SIZE))
    if len(others) > cap:
        others = dict(sorted(others.items(), key=_keep_rank, reverse=True)[:cap])
    out = {**protected, **others}
    hard = int(_param("bench_hard_max", BENCH_HARD_MAX))
    if len(out) > hard:
        kept = dict(sorted(out.items(), key=_keep_rank, reverse=True)[:hard])
        dropped = [t for t in out if t not in kept]
        if dropped:   # never silent: surface what the pathological backstop cut
            state.journal("bench_hard_max_trim", dropped=dropped, kept=len(kept))
        out = kept
    return out


def _update_locked(screen: dict, verdicts: dict) -> dict:
    wl = state.load("watchlist", {})
    _prior_n = len(wl)        # bench count BEFORE this cycle — for the wipe guard
    scores = screen.get("quant_scores", {})
    candidates = set(screen.get("stock_candidates", []))
    now = _now()

    # Near-miss bench: the strongest names that did NOT make the tier slots
    ranked = sorted((t for t in scores if t not in candidates and t not in _etf_like()),
                    key=lambda t: scores.get(t) or 0.0, reverse=True)
    # NEAR-MISS EARNINGS ARMING (operator 2026-08-21, IG brief). The class had
    # 100% missing_earnings_date and 0% technical arming, so `earnings_catalyst`
    # — the firm's second-busiest reopen path — could never reach 31% of the
    # bench. A near-miss name never goes to the committee, so nothing was ever
    # building it a packet and nothing stamped a date.
    #
    # NOT a derived technical leg. _derive_leg prefers WAIT-FOR-WEAKNESS
    # (rsi_below / cross_sma below / pullback), which is right for a HOLD the
    # committee called too extended and WRONG here: a near-miss is on the bench
    # because its SCORE missed the cut, so reopening it as it falls would
    # re-research names whose score is getting worse. An earnings print is the
    # honest name-specific catalyst — it is the single event most likely to
    # move a small-cap's quant picture.
    #
    # BOUNDED: next_earnings is a per-ticker vendor call, so arm at most
    # NEAR_MISS_EARNINGS_PER_CYCLE per pass, highest-scoring first, and never
    # re-fetch a name that already carries a date. 36 names at 8/cycle amortise
    # in ~5 cycles and then cost nothing.
    _arm_budget = int(_param("near_miss_earnings_per_cycle",
                             NEAR_MISS_EARNINGS_PER_CYCLE))
    for t in ranked[:NEAR_MISS_POOL]:
        e = wl.get(t) or {"ticker": t, "added": now, "source": "near_miss",
                          "origin": "near_miss",
                          "watch_trigger": "quant screen score breaks into tier "
                                           "slots, or its next earnings print lands",
                          "history": []}
        _note_quant(e, now, scores[t])
        e["missing"] = 0
        # Re-probe a name whose date is unknown only after a cool-off, so two
        # unknowable tickers cannot eat the whole budget every cycle and starve
        # the rest of the class.
        _probe_age = _age_days(e.get("earnings_probe_ts", "")) if e.get(
            "earnings_probe_ts") else 1e9
        if (_arm_budget > 0 and not e.get("earnings_date")
                and _probe_age >= float(_param("near_miss_earnings_reprobe_days",
                                               NEAR_MISS_EARNINGS_REPROBE_DAYS))):
            try:
                from firm import data_engine as _de
                _ne = _de.next_earnings(t) or {}
                _arm_budget -= 1          # spend the budget on the ATTEMPT, not
                                          # the hit, or an unknowable name is
                                          # retried every cycle forever
                e["earnings_probe_ts"] = now
                _days = _ne.get("days_away")
                # ONLY A FUTURE DATE ARMS ANYTHING. next_earnings can return the
                # most recent PAST print when no future one is scheduled (live:
                # PMI came back 2026-08-14, days_away -7). Stamping that arms a
                # catalyst that has already landed, which _catalyst_landed would
                # read as satisfied immediately — arming on something already
                # true is not arming, it is a spurious reopen.
                if _ne.get("date") and isinstance(_days, (int, float)) and _days >= 0:
                    e["earnings_date"] = _ne["date"]
                    e["earnings_session"] = _ne.get("session")
                    state.journal("near_miss_earnings_armed", ticker=t,
                                  date=_ne["date"], days_away=_days)
            except Exception:
                _arm_budget -= 1
                e["earnings_probe_ts"] = now
        wl[t] = e

    # A bench outside the scan's ranked window must NOT go blind: names the
    # scan didn't rank get their quant refreshed DIRECTLY, so scores stay
    # live, trends are real, and promotions can actually trigger. "missing"
    # now means the DATA is gone (delisted/halted), not merely out-ranked.
    # Non-catalyst names FIRST (they vanish-evict on a missed refresh); catalyst-
    # waiters tolerate a stale score (they reopen on their DATE) so they fill the
    # remaining refresh budget — a bigger bench never starves a near-miss.
    unranked = ([t for t in wl if t not in scores and not _awaiting_near_catalyst(wl[t])]
                + [t for t in wl if t not in scores and _awaiting_near_catalyst(wl[t])])
    fresh = _refresh_quants(unranked) if unranked else {}
    for t in wl:
        if t in scores:
            # BANK the observation, don't just clear the counter (audit 2026-07-26
            # #3): a bench name present in quant_scores but OUTSIDE the near-miss
            # pool above took this branch, so its `missing` reset with NO new data
            # point — decay and improve froze on a stale history and the vanish
            # rule could never fire for it (once in 55 cycles live; EVC's last
            # point was 07-06). _note_quant is same-ts idempotent, so the pool
            # names already recorded above are not double-counted.
            _note_quant(wl[t], now, scores[t])
            wl[t]["missing"] = 0
        elif fresh.get(t) is not None:
            _note_quant(wl[t], now, fresh[t])
            wl[t]["missing"] = 0
        else:
            wl[t]["missing"] = wl[t].get("missing", 0) + 1

    # Verdict feedback: HOLDs with conviction go on the bench with the judge's
    # own trigger; BUY/SELL verdicts graduate off it
    for t, d in verdicts.items():
        v = d.get("verdict") or {}
        # A DEGRADED (quant-only, Claude unavailable) verdict is NON-authoritative:
        # it must NEVER bench a name, graduate one off, or re-rank the bench. Only a
        # real committee run governs the bench. (Operator caught 2026-06-25: during a
        # subscription-quota outage every cycle returned all-BUY degraded verdicts,
        # which were GRADUATING benched names — wiping full-committee picks — and
        # benching quant-only HOLDs that then out-ranked real picks on the cap.)
        if v.get("degraded"):
            continue
        if d.get("kind") != "stock":
            continue
        rating = v.get("rating")
        # BOOKKEEPING FIRST (2026-07-09 APPS false alarm): ANY real committee
        # verdict on a benched name refreshes its last_verdict stamp, whatever
        # the branches below decide about bench MEMBERSHIP. APPS was re-judged
        # HOLD conviction 54 — one point under the 55 bench bar — so neither
        # branch ran and the stamp stayed 3 days stale; overdue_reopens() then
        # cried "silent reopen failure" every 30 minutes for a name the
        # committee had judged THAT MORNING (a false alarm that could never
        # self-clear). The stamp must record every re-judge: _catalyst_landed's
        # "since", the news-trigger "since", and the overdue watchdog all read
        # it. Degraded verdicts stay excluded above (non-authoritative,
        # operator 2026-06-25); a BUY/SELL graduation deletes the entry below.
        if t in wl:
            wl[t]["last_verdict"] = {"rating": rating,
                                     "conviction": v.get("conviction"),
                                     "ts": now}
        if rating == "HOLD" and (v.get("conviction") or 0) >= 55:
            e = wl.get(t) or {"ticker": t, "added": now, "source": "hold_verdict",
                              "origin": "hold_verdict", "history": []}
            if (e.get("recheck_count", 0) >= int(_param("max_rechecks", MAX_RECHECKS))
                    and not _awaiting_near_catalyst(e)):
                # Argued max times, still not a BUY — the committee has spoken.
                # Exception: a name explicitly waiting on a near earnings print
                # is kept until that print lands (then re-judged with actuals).
                wl.pop(t, None)
                _drop(t, "judged out: still HOLD after max re-checks")
                continue
            e["source"] = "hold_verdict"
            e["watch_trigger"] = (v.get("watch_trigger")
                                  or f"resolve: {v.get('bear_risk', 'thesis uncertainty')}")
            e["last_verdict"] = {"rating": rating, "conviction": v.get("conviction"),
                                 "ts": now}
            # Remember the catalyst the judge is waiting on, so the bench can
            # reopen the name the day it lands even if it leaves the scan
            ne = (d.get("packet") or {}).get("next_earnings") or {}
            if ne.get("date"):
                e["earnings_date"] = ne["date"]
                e["earnings_session"] = ne.get("session")
            # Structured technical trigger + the price at bench time, so the bench
            # can reopen the MOMENT the named setup (pullback/RSI/SMA/reclaim) is
            # hit — not just on a momentum re-check that misses wait-for-weakness.
            # Always (re)set so a re-check that CHANGES or drops the trigger
            # can't leave a stale technical condition firing (review 2026-06-14).
            rtrig = v.get("reopen_trigger")
            if config.COMPOUND_TRIGGERS:
                e["reopen_trigger"] = _normalize_trigger(rtrig)
            else:
                e["reopen_trigger"] = rtrig if isinstance(rtrig, dict) and rtrig.get("kind") else None
            _ptech = (d.get("packet") or {}).get("technicals") or {}
            # A non-degraded HOLD that emitted no usable trigger gets a conservative
            # derived leg so it reopens on a real setup, not only the slow recheck
            # (flag-gated; a degraded/quant-only verdict stays trigger-less by design).
            if (config.COMPOUND_TRIGGERS and not v.get("degraded")
                    and not e["reopen_trigger"]):
                _leg = _derive_leg(_ptech, e.get("earnings_date"))
                if _leg:
                    e["reopen_trigger"] = _leg
                    state.journal("derived_reopen_trigger", ticker=t, leg=_leg)
            # A re-judge must not disarm a stop-out (operator 2026-08-19).
            # The unconditional rewrite above is correct for the committee's own
            # legs and wrong for the reclaim leg, which is arithmetic from the
            # stop price rather than an opinion. Re-compose it here so the arm
            # survives every future HOLD re-check, not just the promotions-time
            # heal.
            if _ensure_reclaim_arm(e, _param("stop_reclaim_pct",
                                             STOP_OUT_RECLAIM_PCT)):
                state.journal("stop_out_reclaim_preserved", ticker=t,
                              trigger=e.get("reopen_trigger"))
            # RETIRE a book-full obligation the committee has walked away from
            # (operator 2026-08-21). This branch only runs for a HOLD, so
            # reaching it means the name is no longer BUY-family and the
            # "re-compete when a slot frees" claim no longer has a thesis
            # behind it. Stamp once; the block itself is kept for provenance.
            _bfo = e.get("book_full")
            if isinstance(_bfo, dict) and _bfo and not _bfo.get("resolved_at"):
                _bfo["resolved_at"] = now
                _bfo["resolved_reason"] = (
                    f"re-judged {rating} conv {v.get('conviction')} — no longer "
                    "BUY-family, so the slot claim is retired")
                e["book_full"] = _bfo
                state.journal("book_full_obligation_retired", ticker=t,
                              benched_conviction=_bfo.get("conviction"),
                              now_rating=rating, now_conviction=v.get("conviction"))
            if isinstance(_ptech.get("price"), (int, float)):
                e["benched_price"] = _ptech["price"]
            if t in scores:
                _note_quant(e, now, scores[t])
            wl[t] = e
        elif rating in ("BUY", "STRONG_BUY", "SELL", "STRONG_SELL") and t in wl:
            # A BUY only truly graduates if it got INTO a book (operator
            # 2026-07-16: 4 BUYs, books full, 3 placed nowhere). A benched name
            # upgraded to BUY that placed NOWHERE because every qualified book
            # was slots-full stays benched as a book-full waiter — deleting it
            # here was the same forgetting bug through the graduation door.
            # Any other unplaced reason (sector/liquidity/correlation block)
            # keeps the old delete: those have their own journaled causes and
            # a slot-open promotion loop would burn research on a still-blocked
            # name. SELL-family always deletes — the thesis is dead.
            if rating in ("BUY", "STRONG_BUY") and not _held_anywhere(t):
                from firm import routing
                _q = [sl for sl in (wl[t].get("qualified_sleeves")
                                    or ["alpha_stocks", "core_blend"])]
                if not any(routing.open_slots(sl)[0] > 0 for sl in _q):
                    e = wl[t]
                    e["source"] = "book_full"
                    e.setdefault("qualified_sleeves", _q)
                    e["watch_trigger"] = (f"book-full BUY (conv {v.get('conviction')}): "
                                          "waiting for a free slot or a conviction "
                                          "rise that wins a swap")
                    bf = e.get("book_full") or {}
                    bf.update({"thesis": v.get("thesis", ""),
                               "conviction": v.get("conviction"), "ts": now})
                    e["book_full"] = bf
                    wl[t] = e
                    _bench_event({"type": "book_full_kept", "ticker": t})
                    continue
            was_promoted = bool(wl[t].get("promoted_at"))
            del wl[t]   # graduated (or rejected) — off the bench
            _bench_event({"type": "graduated", "ticker": t, "rating": rating,
                          "from_promotion": was_promoted})

    # Heal any bench stamp that lags the research history BEFORE the watchdogs
    # read it (2026-07-10): a real committee verdict below the old conv>=55
    # bench-stamp bar (bar removed in the verdict loop above, 2026-07-09) left
    # some benched HOLDs with a last_verdict OLDER than their true last look, so
    # overdue_reopens / _catalyst_landed read a stale 'since' and false-flagged
    # a name overdue for a catalyst that predated its real re-judge (APPS: stamp
    # 07-06, actually re-judged HOLD 07-09). Sync each stamp UP to the latest
    # NON-degraded history verdict; never moves one backward, never invents one.
    _reconcile_bench_stamps(wl)

    # Keep every benched HOLD's earnings date current BEFORE the eviction pass —
    # a name with a fresh upcoming date is protected and reopens on the print.
    # SNAPSHOT the protection FIRST (audit 2026-07-26 #1): the refresh renews a
    # landed earnings_date to the NEXT print, so by the time the eviction loop
    # read _awaiting_near_catalyst every hold was ALWAYS waiting on something and
    # the age ceiling + decay cut were dead for the entire class — live, 60/60
    # hold_verdict entries carried a date and 25 sat past the 30-day ceiling
    # (WDC/STX/VSH/COHU/DELL at 32.8d). Evaluated pre-refresh, a name whose print
    # has actually landed reads UNPROTECTED for exactly the cycle after it lands,
    # which is the self-eviction window the design always assumed existed. A name
    # re-judged HOLD this cycle already re-stamped its date from the packet in the
    # verdict loop above, so a genuine "wait for the NEXT print" stays protected.
    _pre_catalyst = {t: _awaiting_near_catalyst(e) for t, e in wl.items()}
    _refresh_catalyst_dates(wl)

    # Behavior-based exits: cut faders early, never hold anything forever.
    # Thresholds are live-tunable by the learning loop (_param).
    _backfill_added(wl)
    max_age = _param("max_age_days", MAX_AGE_DAYS)
    missing_drop = int(_param("missing_cycles_drop", MISSING_CYCLES_DROP))
    decay_pts = _param("decay_drop_pts", DECAY_DROP_PTS)
    for t in list(wl):
        e = wl[t]
        hist = e.get("history", [])
        last_q = hist[-1]["quant"] if hist else None
        base_q = _benched_quant(e)          # immutable baseline; backfills on touch
        # A name waiting on a near earnings print is exempt from the age ceiling
        # and the decay cut — don't forget it before its catalyst lands. Read the
        # PRE-refresh snapshot, not the renewed date (see _pre_catalyst above).
        _awaiting = _pre_catalyst.get(t, _awaiting_near_catalyst(e))
        if _age_days(e.get("added", now)) > max_age and not _awaiting:
            del wl[t]
            _drop(t, f"aged out: {max_age:.0f} day ceiling", last_q)
        elif (e.get("missing", 0) >= missing_drop
              and e.get("source") == "near_miss"):
            # vanish rule only for near-misses: a stop-out is EXPECTED to be
            # out of the top ranks after a -20% move — that's why it's benched
            del wl[t]
            _drop(t, f"vanished from scan ranks {missing_drop} cycles", last_q)
        elif (len(hist) >= 2 and base_q is not None and last_q is not None
              and base_q - last_q >= decay_pts
              and not _awaiting):
            # Measured from the FROZEN benched_quant, not hist[0] of the rolling
            # window (audit 2026-07-26 #2) — see _benched_quant.
            del wl[t]
            _drop(t, f"setup decayed: quant {base_q} -> {last_q}", last_q)
    wl = _apply_bench_cap(wl)

    # WIPE GUARD: never persist a collapse of a substantial bench to (near) empty —
    # keep the prior bench intact and alert (the 2026-06-23 incident wiped 41 -> {}).
    if (_prior_n >= BENCH_WIPE_GUARD_MIN
            and len(wl) < max(1, int(_prior_n * BENCH_WIPE_GUARD_FRAC))):
        state.journal("bench_wipe_blocked", prior=_prior_n, attempted=len(wl))
        try:
            from firm import alerts
            alerts.alert(
                "BENCH WIPE BLOCKED",
                f"A cycle tried to collapse the bench {_prior_n} -> {len(wl)} names; "
                f"kept the prior bench. Likely an empty-screen outage or a stray "
                f"second process — investigate.", severity="warning")
        except Exception:
            pass
        return state.load("watchlist", {})    # leave the on-disk bench intact
    state.save("watchlist", wl)
    state.journal("watchlist_updated", bench=len(wl))
    return wl


def promotions(screen: dict, researching: set | None = None) -> list[dict]:
    """Names whose setup completed -> promote into this cycle's research.

    `researching` is the set of tickers the cycle will ACTUALLY research this
    run (post-cooldown, post-backfill). Without it we fall back to the screen's
    raw candidate list — but that list is PRE-cooldown (2026-07-10 APPS trap):
    a benched HOLD that is simultaneously a scan candidate AND inside its
    verdict cooldown was skipped here ("it's a candidate, the committee will
    see it") while the cycle's cooldown gate dropped it from research — so
    NEITHER path re-judged it, its landed catalyst could never fire, and
    health's 'catalyst readiness' redded every 30 minutes with no self-clear
    (APPS: stamp 07-06, macro landed 07-07, red from 07-09 18:52 onward)."""
    with state.SLEEVE_LOCK:
        return _promotions_locked(screen, researching)


def _promotions_locked(screen: dict, researching: set | None = None) -> list[dict]:
    wl = state.load("watchlist", {})
    # Expire stale promotion flags UP FRONT — before the skip loop, not only after
    # it (2026-07-13). A name promoted right before a no-cycle gap (weekend/holiday)
    # kept its promoted_at set through the gap, so the FIRST cycle back skipped it
    # (line "if ... e.get('promoted_at'): continue") and only cleared the flag at
    # end-of-run, wasting an eligible re-promotion. Observed live: AMAT reclaimed
    # its stop level Fri 07-10 (promoted), sat over the weekend, and Mon 07-13's
    # cycle skipped it while its Friday promoted_at still lingered.
    for _t in list(wl):
        if wl[_t].get("promoted_at") and _age_days(wl[_t]["promoted_at"]) > 1:
            wl[_t].pop("promoted_at", None)
    # Backfill legacy stop-outs filed before triggers were armed (2026-07-13):
    # arm reclaim_price from the stored exit so the IG coverage audit and the
    # committee packet both see the real price trigger. Purely additive — the
    # dedicated stop_out branch below still drives the actual price promotion.
    _rc = _param("stop_reclaim_pct", STOP_OUT_RECLAIM_PCT)
    for _t, _e in wl.items():
        # Payload, not label — and COMPOSE rather than only filling a blank.
        # The old guard required BOTH source=="stop_out" (destroyed by a
        # re-judge) and an EMPTY reopen_trigger (also overwritten by the same
        # re-judge), so the two failure modes covered for each other and the
        # heal could never fire for the names that needed it most.
        if _ensure_reclaim_arm(_e, _rc):
            state.journal("stop_out_reclaim_rearmed", ticker=_t,
                          trigger=_e.get("reopen_trigger"),
                          source_label=_e.get("source"))
    scores = screen.get("quant_scores", {})
    # Skip only names that will truly reach the committee this cycle — the
    # caller's post-cooldown research set when provided, else the raw screen
    # list (legacy callers/tests; see promotions() docstring for the trap).
    candidates = (set(researching) if researching is not None
                  else set(screen.get("stock_candidates", [])))
    improve_pts = _param("improve_pts", IMPROVE_PTS)
    reclaim_pct = _param("stop_reclaim_pct", STOP_OUT_RECLAIM_PCT)
    # THE TIER CUT — the bar a near-miss was benched against in the first place
    # (operator 2026-08-21). Measured on the live bench: all 36 near_miss
    # entries carry 10-15 banked quant observations over up to 58 days, entry
    # scores 84.7-93.0 on a clamped 0-100 scale, and the BEST delta any of them
    # has EVER achieved is +4.40 against a +5.0 bar. Zero of 36 have ever
    # cleared it, and they hold no earnings_date and no reopen_trigger, so the
    # absolute-delta rule is their ONLY name-specific route back. It is
    # unreachable by construction: near_miss selects the HIGHEST scorers that
    # just missed the slots, so the population is chosen for sitting near the
    # ceiling and then asked to climb 5 more points off it. The
    # saturated-scale fallback below was written for exactly this and no longer
    # engages — it needs _scale_saturated(), which was true when 142 of 300
    # names tied at the cap (2026-07-28) and is False today with only 5 of 300
    # there. Both routes shut, so 31% of the bench became write-only storage.
    # The entry's own watch_trigger already states the honest condition: "quant
    # screen score breaks into tier slots". Measure THAT — it is
    # self-calibrating, needs no constant, and is reachable by definition
    # because it is the same comparison that benched the name.
    _cut_scores = [scores[_t] for _t in candidates
                   if isinstance(scores.get(_t), (int, float))]
    _tier_cut = (min(_cut_scores)
                 if len(_cut_scores) >= int(_param("tier_cut_min_n", 3))
                 else None)
    tech = screen.get("technicals", {})

    # Effective scores: scan window where available, direct refresh for the
    # rest — a bench name that improved OUTSIDE the top ranks must still be
    # promotable (same blindness as _update_locked, fixed 2026-06-12).
    # fetch_history caches hourly, so update() reuses this download free.
    _un = [t for t, e in wl.items()
           if t not in scores and e.get("source") != "stop_out"
           and not e.get("promoted_at")]
    # non-catalyst names first (vanish-evict on a missed refresh), then waiters
    # (reopen on their DATE — the catalyst path below is score-independent), so a
    # bigger bench never starves a near-miss of the 80/cycle refresh budget.
    # Compound names with rsi/sma legs FIRST (they NEED live RSI/SMA to ever fire on
    # a faded off-scan name), then non-catalyst near-misses (vanish-evict on a missed
    # refresh), then waiters (reopen on their DATE — score-independent). (_needs_full_tech
    # is False for every name when COMPOUND_TRIGGERS is off, so order is unchanged then.)
    unranked = ([t for t in _un if _needs_full_tech(wl[t])]
                + [t for t in _un if not _needs_full_tech(wl[t]) and not _awaiting_near_catalyst(wl[t])]
                + [t for t in _un if not _needs_full_tech(wl[t]) and _awaiting_near_catalyst(wl[t])])
    _full_tech: dict = {}    # live RSI/SMA surfaced for faded compound technical legs
    eff_scores = dict(_refresh_quants(unranked, out_tech=_full_tech)) if unranked else {}
    eff_scores.update(scores)

    # Fetch live prices for names that have faded out of the scan but still need
    # one: stop-outs (recovery detection) AND benched HOLDs whose technical
    # reopen_trigger is price-based (pullback/below/reclaim).
    fetch_px = [t for t, e in wl.items()
                if not e.get("promoted_at") and t not in tech
                and (_stop_out_open(e) or _needs_price_trigger(e))]
    extra_px = {}
    if fetch_px:
        from firm import data_engine
        extra_px = data_engine.batch_last_prices(fetch_px)

    cat_picks: list[tuple[float, str, str]] = []     # landed earnings/FOMC — priority
    other_picks: list[tuple[float, str, str]] = []   # quant-improved / recheck / stop-out
    for t, e in wl.items():
        if t in candidates or e.get("promoted_at"):
            continue

        if e.get("source") == "stop_out":
            so = e.get("stop_out", {})
            # Fail CLOSED on a missing/corrupt stop timestamp (audit 2026-07-26
            # #10): _age_days returns 1e9 on unparseable input, which read as
            # "ancient" and PASSED the cool-off — an unstamped stop-out could
            # re-enter immediately, the exact falling-knife this gate exists to
            # prevent. An unknown stop age is treated as age 0 (just stopped).
            _so_age = _age_days(so.get("ts", e.get("added", "")))
            if _so_age >= 1e8:
                _so_age = 0.0
            if _so_age < STOP_OUT_COOLOFF_DAYS:
                continue   # cooling off — no falling-knife re-entry
            px = (tech.get(t, {}) or {}).get("price") or extra_px.get(t, 0)
            exit_px = so.get("exit") or 0
            if px and exit_px and px >= exit_px * (1 + reclaim_pct / 100):
                other_picks.append((eff_scores.get(t, 50.0), t,
                                    f"stop-out recovery: ${px:.2f} reclaimed "
                                    f"${exit_px:.2f} +{reclaim_pct:.0f}%"))
            elif exit_px and not px:
                # Past cool-off but no price this cycle (fetch miss): a reclaimed
                # name could be silently skipped and never re-enter. Make it VISIBLE
                # so a data hiccup can't hide a missed recovery (2026-07-13).
                state.journal("stop_out_recovery_unpriced", ticker=t,
                              reclaim=round(exit_px * (1 + reclaim_pct / 100), 2))
            continue

        if (e.get("source") == "book_full"
                and ((e.get("last_verdict") or {}).get("rating") or "")
                in ("BUY", "STRONG_BUY")):
            # A BUY the books had no room for: reopen the MOMENT a qualified
            # sleeve has a free entry slot, so fresh research competes for the
            # slot the same cycle it opened (operator 2026-07-16, ALKS). No
            # free slot -> fall through to the generic quant-improve path (a
            # conviction-raising re-look can still win a swap). If the last
            # real verdict is no longer BUY-family, this branch is skipped and
            # the generic paths govern. Decay/age exits still cut faders.
            from firm import routing as _routing
            _qs = [sl for sl in (e.get("qualified_sleeves")
                                 or ["alpha_stocks", "core_blend"])]
            # A parked-cash reservoir on a name-full-but-capital-empty book IS
            # capacity too (operator 2026-07-16) — same yield the entry gate
            # applies (routing.parked_capacity).
            if any(_routing.open_slots(sl)[0] > 0
                   or _routing.parked_capacity(sl) for sl in _qs):
                _h = e.get("history") or []
                _sq = eff_scores.get(t)
                if _sq is None:
                    _sq = _h[-1]["quant"] if _h else 50.0
                other_picks.append((_sq, t, "book-full BUY: a slot opened — "
                                    "re-judge to compete for it"))
                continue

        hist = e.get("history", [])
        # The improve bar is measured from the FROZEN entry-quality baseline, not
        # hist[0] of the rolling [-30:] window (audit 2026-07-26 #4): with the
        # anchor sliding forward on long-lived entries the "+5 since benched" test
        # was scored against a moving, saturated head (93.0 for 74/78 entries), and
        # 0 of 201 promotions ever came through this path. See _benched_quant.
        first_q = _benched_quant(e)
        cur_q = eff_scores.get(t)
        is_hold = e.get("source") == "hold_verdict"
        catalyst = _catalyst_landed(e) if is_hold else None
        _cat = (lambda _e: _catalyst_landed(_e)) if is_hold else None
        # _news_since directly (2026-08-13 IG audit): _eval_leg's news branch
        # passes the same entry, so routing through _news_trigger_hit re-read
        # the TOP-LEVEL trigger kind and a news leg nested in a compound
        # trigger (top-level kind = the op) could never fire.
        _news = (lambda _e, _tk: _news_since(_e, _tk)) if is_hold else None
        tmet = _technical_trigger_met(e, tech.get(t) or _full_tech.get(t) or {}, extra_px.get(t),
                                      cat_fn=_cat, news_fn=_news) if is_hold else None
        # A LANDED catalyst / hit technical setup / fresh news reopens the name EVEN
        # IF its quant wasn't refreshed this cycle. With catalyst-waiters uncapped
        # (operator 2026-06-23) a busy bench can exceed the 80/cycle refresh budget;
        # a REAL event must NEVER be missed for lack of a fresh score. The quant-
        # improvement and routine-recheck paths still REQUIRE a fresh score.
        sort_q = cur_q if cur_q is not None else (hist[-1]["quant"] if hist else 50.0)
        if catalyst:
            # The exact event the judge was waiting on just happened —
            # reopen NOW (even before the time-based recheck, even if faded)
            cat_picks.append((sort_q, t, f"catalyst: {catalyst} — re-judge with actuals"))
        elif tmet:
            # the SPECIFIC technical setup the committee named was hit (the dip /
            # RSI / SMA / reclaim) — reopen NOW, a priority over routine rechecks
            cat_picks.append((sort_q, t, f"setup hit: {tmet}"))
        elif is_hold and _news_trigger_hit(e, t):
            # an UNSCHEDULED news catalyst the thesis was waiting on just broke
            # (sector sentiment / geopolitical / research) — reopen now
            cat_picks.append((sort_q, t, "news catalyst: fresh trusted news since verdict"))
        elif cur_q is None:
            continue                 # no fresh score -> only date/trigger paths apply
        elif first_q is not None and cur_q - first_q >= improve_pts:
            other_picks.append((cur_q, t, f"quant improved {first_q} -> {cur_q}"))
        elif (_tier_cut is not None and first_q is not None
              and cur_q >= _tier_cut and cur_q >= first_q):
            # BACK INSIDE THE CUT. Requires cur_q >= first_q as well, so a name
            # that FELL while the cut fell further does not promote on the
            # cut's weakness — the name has to have held or improved on its own
            # banked baseline. This is the near-miss class's reachable path.
            other_picks.append((cur_q, t,
                                f"back inside the tier cut: {cur_q} >= {_tier_cut}"))
        elif (first_q is not None and _scale_saturated(eff_scores)
              and cur_q >= max(eff_scores.values()) and cur_q >= first_q):
            # SATURATED-SCALE FALLBACK (2026-07-28, operator-confirmed). Freezing
            # the baseline (audit #4) fixed a sliding anchor but not the deeper
            # defect: `quant_score` is CLAMPED and the whole universe piles onto
            # the ceiling. Measured live — of 300 scanned names max = p99 = p95 =
            # 93.0, the top 60 are ALL exactly 93.0, and 142 of 300 tie at the
            # cap. A benched entry therefore anchors at 93.0 and the +5 bar asks
            # for 98.0, which NOTHING in the universe can reach: 0 of 300 names.
            # The improve path was not merely strict, it was ARITHMETICALLY
            # UNREACHABLE — which is why 0 of 201 promotions ever came through it
            # even after the anchor was frozen. When the scale is saturated the
            # points test carries no information, so reaching the ceiling (and
            # holding at or above the entry anchor) IS the improvement signal
            # available. Ranking still decides WHICH of the tied names promote —
            # the per-cycle promotion cap is unchanged, so this widens the
            # candidate pool, never the number promoted.
            other_picks.append((cur_q, t, f"quant at ceiling {cur_q} "
                                          f"(scale saturated, benched {first_q})"))
        elif (e.get("source") == "hold_verdict"
              and (not _awaiting_near_catalyst(e)
                   or _age_days(e.get("added", "")) > 2 * RECHECK_HOLD_DAYS)
              and _age_days((e.get("last_verdict") or {}).get("ts", e.get("added", "")))
              >= RECHECK_HOLD_DAYS):
            # NOT while waiting on a near print: re-arguing the same pre-earnings
            # setup is cost without information — wait for the catalyst above.
            # UNLESS the name has now sat on the bench longer than 2x the recheck
            # window (audit 2026-07-26 #4): with _refresh_catalyst_dates renewing
            # every hold's date, "awaiting a catalyst" was permanently true and
            # this path could NEVER fire — 0 of 201 promotions came through it. A
            # name parked for weeks on a print that keeps moving out is exactly
            # the one the committee should look at again; the >= RECHECK_HOLD_DAYS
            # staleness gate still keeps the cadence at one look per 3 days.
            other_picks.append((cur_q, t, "HOLD re-check due"))
    # Anti-starvation drain (IG 2026-07-06 critical, ratified): drain the
    # OLDEST-overdue landed-catalyst reopens FIRST, not the highest-quant ones.
    # The old quant sort let low-score names (e.g. SNX/CTOS) sit un-re-judged for
    # 9-11 days while the same high-quant names recycled through the 6 catalyst
    # slots every cycle — the reopen queue never drained. Ordering by staleness
    # (days since last_verdict) first guarantees every overdue name is re-judged
    # within ceil(n / MAX_CATALYST_PROMOS) cycles regardless of its quant rank;
    # quant only breaks ties among equally-stale names.
    def _stale_days(pick):
        _e = wl.get(pick[1], {})
        return _age_days((_e.get("last_verdict") or {}).get("ts", _e.get("added", "")))
    cat_picks.sort(key=lambda p: (_stale_days(p), p[0]), reverse=True)
    other_picks.sort(reverse=True)
    # Landed catalysts (the actual event happened — highest information) get their
    # OWN dedicated slots so a real earnings/FOMC reopen is never starved by
    # routine rechecks; routine picks keep their own per-cycle cap.
    chosen = cat_picks[:MAX_CATALYST_PROMOS] + other_picks[:PROMOTE_PER_CYCLE]
    out = []
    for q, t, why in chosen:
        wl[t]["promoted_at"] = _now()
        if wl[t].get("source") == "hold_verdict":
            wl[t]["recheck_count"] = wl[t].get("recheck_count", 0) + 1
        _bench_event({"type": "promotion", "ticker": t, "why": why, "quant": q})
        out.append({"ticker": t, "why": why,
                    "watch_trigger": wl[t].get("watch_trigger", ""),
                    "benched_since": wl[t].get("added", ""),
                    "last_verdict": wl[t].get("last_verdict"),
                    "stop_out": wl[t].get("stop_out")})
    # clear promoted flag so un-graduated names can be re-promoted later
    for t in list(wl):
        if wl[t].get("promoted_at") and _age_days(wl[t]["promoted_at"]) > 1:
            wl[t].pop("promoted_at", None)
    state.save("watchlist", wl)   # unconditional — flag cleanup must persist
    if out:
        state.journal("watchlist_promotions",
                      tickers=[(p["ticker"], p["why"]) for p in out])
    return out
