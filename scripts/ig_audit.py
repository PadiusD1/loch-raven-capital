"""Inspector General — weekly Opus adversarial self-audit (2026-06-14).

The deterministic self-checks (test suite, health desk, vendor audit, catalyst
watchdog) verify KNOWN invariants. They cannot find the UNKNOWN-UNKNOWNS — the
taxonomy/coverage gaps nobody wrote a check for (the FOMC-only catalyst and the
inert-bench misses were both pure DISCOVERY failures that passed every test).

This is the gap-finder that sits ABOVE the deterministic layer:
  - READ-ONLY: it gathers live state + recent journal + a structural summary and
    asks Opus (via the user's CLI subscription — brain.ask, NOT the API) to
    red-team the system. It has NO trade/broker tools, cannot flip health, cannot
    deploy. Its only outputs are journal entries + operator alerts.
  - ADVERSARIAL: forbidden from concluding "all good" — it must emit ranked GAP
    HYPOTHESES, each with the evidence and the DETERMINISTIC check that would
    close it (so findings become permanent ratchet, and the IG makes itself
    obsolete per-finding once a human ratifies the new check).

Registered as the weekly "LochRaven IG Audit" task (Sundays 18:30, after the
self-test). Fail-soft: an audit failure must never affect trading.
"""
import ast
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


# ── The journal-vocabulary contract (2026-07-28) ──────────────────────────────
# THE DEFECT THIS BLOCK EXISTS TO KILL. The 07-27 IG emitted a CRITICAL saying
# "the entire reopen machinery produced ZERO live reopens in 7 days ...
# journal_7d_event_counts contains no reopen/bench_reopened/bench_evicted event
# of any kind". Ground truth: NOTHING in this codebase has ever emitted
# `bench_reopened`, `reopen_fired` or `bench_evicted` — they are not names, they
# are guesses. The real emitters (watchlist.py:1364 watchlist_promotions,
# watchlist.py:719 watchlist_dropped) fired 10 and 8 times that week with
# substantive reopens (setup hit RSI+SMA cross, stop-out recovery reclaimed +3%,
# HOLD re-check due). The machinery was LIVE; the LOOKUP was name-blind.
#
# This is the same class as learning.BLOCK_EVENTS covering 11 of ~70 emitted
# names (fixed 2026-07-27). It is uniquely dangerous because a dead lookup does
# NOT fail loudly — it returns zero, and zero reads as "inert mechanism", which
# manufactures an inverted CRITICAL: the auditor reports the healthiest part of
# the system as broken and burns a fix-brief slot on a non-problem.
#
# THE STRUCTURAL FIX: every event name this file looks up is declared HERE, in
# one place, and the snapshot itself is built from these constants — so the
# names the auditor reads and the names the self-check verifies can never be two
# different lists. On every run _vocabulary_drift() AST-scans the real emitters
# out of firm/ + app.py and reports BOTH directions as a first-class finding.
_DAILY_EVENTS = ("cycle_done", "watchlist_updated", "screen_done")
_WEEKLY_EVENTS = ("self_test", "vendor_audit", "ig_audit")
_EXCEPTION_EVENTS = ("fundamentals_sanitized", "earnings_actuals_missing",
                     "macro_refresh_error", "research_degraded",
                     "stop_out_to_bench", "watchlist_promotions",
                     "state_restored_from_backup")

# The COMPLETE emitted reopen/bench/eviction vocabulary, each mapped to the
# concept the auditor is actually asking about. Verified 2026-07-28 by AST scan
# of every journal() call under firm/ + app.py — there is no `reopen_*` event
# name in this codebase at all; a reopen is observable ONLY as a promotion off
# the bench, and an eviction ONLY as a drop. `reopen_census_covers_all_emitters`
# below re-proves that claim every run, so a newly-added reopen event that this
# map forgets shows up as drift instead of silently widening the blind spot.
_REOPEN_EVENT_ROLES = {
    "watchlist_promotions": "REOPEN — a benched name promoted back into research "
                            "and re-judged. This IS the reopen event; there is no "
                            "`bench_reopened`/`reopen_fired`.",
    "watchlist_dropped": "EVICTION — a name removed from the bench (incl. the "
                         "MAX_RECHECKS time-cadence age-off). This IS the "
                         "eviction event; there is no `bench_evicted`.",
    "derived_reopen_trigger": "a technical reopen leg armed post-hoc from the verdict",
    "stop_out_to_bench": "a stopped-out position demoted onto the bench",
    "book_full_to_bench": "a qualified BUY parked on the bench for lack of a slot",
    "book_full_admitted_parked_cash": "a parked book-full name admitted on freed cash",
    "book_full_discarded": "a book-full name NOT benched (placed elsewhere / not benchable)",
    "bench_hard_max_trim": "bench trimmed at BENCH_HARD_MAX",
    "bench_wipe_blocked": "a mass-delete of the bench refused as a suspected bug",
    "bench_tuned": "learning retuned the bench sizing",
    "watchlist_added_backfilled": "a bench entry repaired on load",
    "book_full_bench_error": "the book-full -> bench path itself errored",
    "promotion_tech_fetch_failed": "a promotion off the bench could not fetch technicals "
                                   "— a REOPEN-PATH failure, not a bench state change",
    # ETF-SEAT evictions are a DIFFERENT mechanism from bench eviction (the
    # mechanical rotation swaps a seat; it does not bench the name). Censused
    # here anyway, labelled, because an auditor asking "did anything get
    # evicted" must not have to guess which eviction vocabulary it is reading.
    "etf_seat_incumbent_evicted": "ETF-SEAT eviction (rotation swap) — NOT a bench eviction",
    "etf_seat_eviction_no_replacement": "ETF-SEAT eviction found no replacement — NOT a bench eviction",
    "momentum_lane_skip": "a macro reopen DELIBERATELY suppressed (macro-immune name) "
                          "— correct behaviour, not a suppressed reopen",
}
_AUDITOR_EVENT_VOCABULARY = frozenset(
    _DAILY_EVENTS + _WEEKLY_EVENTS + _EXCEPTION_EVENTS) | frozenset(_REOPEN_EVENT_ROLES)

# Substrings that make an emitted event high-signal for an adversarial audit (a
# refusal, a failure, or a degraded path). Used only to RANK the blind-spot list
# so direction (b) of the drift finding stays short and actionable.
_HIGH_SIGNAL_TOKENS = ("error", "fail", "blocked", "halted", "degraded", "corrupt",
                       "missing", "stale", "stuck", "refused", "unroutable",
                       "unrouted", "breaker", "kill_switch", "drift", "anomaly",
                       "inert", "expired", "unpriceable", "unresolvable", "phantom",
                       "skipped", "deferred", "backlog")
# Any emitted name carrying one of these as a WHOLE underscore-separated token is
# reopen/bench vocabulary and MUST be accounted for in _REOPEN_EVENT_ROLES or the
# census is incomplete. Whole-token, not substring: a substring test matches
# "bench" inside "benchmark_feed_status" and pins the completeness flag
# permanently false, and a check that is always red is a check nobody reads —
# the same desensitisation that let the reopen lookup rot in the first place.
# NB no bare "dropped" stem: the only bench drop is watchlist_dropped (named
# explicitly in the census above), while open_queue_dropped_mode_flip is
# ORDER-QUEUE vocabulary — sweeping it in here would force an unrelated event
# into the reopen census or pin the completeness flag false forever.
_REOPEN_NAME_TOKENS = frozenset({"reopen", "bench", "evict", "evicted", "eviction",
                                 "promotion", "promotions"})


def _is_reopen_vocabulary(name: str) -> bool:
    return bool(_REOPEN_NAME_TOKENS & set(name.split("_")))

# The 7-day census was previously computed over read_journal(400). On live state
# 400 lines spans ~1.0 DAY, so the figure labelled "7d" physically could not see
# 7 days: it showed watchlist_promotions=2 of 10 and watchlist_dropped=0 of 8 —
# the truncation that supplied the "zero reopens" evidence. Scan a window that
# actually covers the census period and SHIP the measured coverage next to it.
_JOURNAL_SCAN_LINES = 6000
_CENSUS_DAYS = 7


def _undetermined(why: str, **denominators) -> dict:
    """2026-07-28: a coverage/liveness figure computed over an EMPTY or
    zero-denominator population is not a pass — it is unmeasured. The 07-27 IG
    correctly flagged two bench checks that "pass vacuously on an empty set".
    Reporting 0/0 as clean is how an unmeasured path acquires a green light, so
    every such figure ships as 'undetermined' WITH the denominator that was
    zero, and Opus is told never to read it as a pass."""
    return {"value": "undetermined", "why": why, **denominators}


def _rate(n: int, denominator: int, why: str) -> dict:
    """A count that carries its own denominator, or 'undetermined' when there is
    no population to measure over (never a silent 0/0 pass)."""
    if not denominator:
        return _undetermined(why, n=int(n), denominator=0)
    return {"n": int(n), "denominator": int(denominator),
            "pct": round(100.0 * n / denominator, 1)}


def _journal_emitters() -> dict:
    """AST-scan every journal()/state.journal() call under firm/ + app.py and
    return the event names the code ACTUALLY emits (2026-07-28).

    AST, not grep, deliberately: 7 call sites in this repo put the event literal
    on the line AFTER `state.journal(` (routing.py:307 book_full_discarded,
    monitor.py:98, health.py:2748, sleeves.py x4), so a line-anchored regex —
    the obvious implementation, and the one used to build the first draft of
    this list — silently under-reports the emitter set. Under-reporting emitters
    is precisely how a FALSE 'dead vocabulary' claim would be born, i.e. this
    self-check would reproduce the very bug it exists to prevent.

    Also returns:
      dynamic_sites — call sites whose event name is a VARIABLE (sleeves.py:476
        dispatches four block names through `ev`). Their names are unresolvable
        statically, so a referenced name absent from `literal` cannot be called
        dead on the literal scan alone.
      literal_strings — every string constant in the scanned sources. A name
        that appears NOWHERE in this set cannot be emitted by any path, dynamic
        dispatch included; that is the airtight test for dead vocabulary.

    SCOPE — every process that writes state/journal.jsonl, not just firm/. The
    first cut of this scanner covered firm/ + app.py only and promptly declared
    `vendor_audit` DEAD and `self_test`/`ig_audit` unverifiable: all three are
    emitted from scripts/ (vendor_audit.py:143, self_test.py:33, and this file's
    own main()). A too-narrow scan produces a false 'nothing emits this' exactly
    the way a too-narrow lookup produced the false 'nothing fired this' — so the
    scope is the union of the journal writers, and it ships in the snapshot so
    the boundary can never go invisible again.
    """
    literal: set[str] = set()
    every_string: set[str] = set()
    dynamic_sites: list[str] = []
    errors: list[str] = []
    files = (sorted((BASE / "firm").glob("*.py"))
             + sorted((BASE / "scripts").glob("*.py"))
             + [BASE / "app.py", BASE / "run_cycle.py", BASE / "keepalive.py"])
    scanned = 0
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError) as e:
            errors.append(f"{path.name}: {str(e)[:80]}")
            continue
        scanned += 1
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                every_string.add(node.value)
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (fn.attr if isinstance(fn, ast.Attribute)
                    else fn.id if isinstance(fn, ast.Name) else "")
            if name != "journal" or not node.args:
                continue
            arg0 = node.args[0]
            if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                literal.add(arg0.value)
            else:
                dynamic_sites.append(f"{path.name}:{node.lineno}")
    return {"literal": literal, "literal_strings": every_string,
            "dynamic_sites": sorted(dynamic_sites), "files_scanned": scanned,
            "parse_errors": errors}


def _vocabulary_drift(emitters: dict, last_seen: dict, counts: dict) -> dict:
    """Both directions of auditor-vs-code vocabulary drift, as a FIRST-CLASS
    finding inside the snapshot (2026-07-28).

    (a) dead_vocabulary — names the auditor looks up that appear in NO string
        literal anywhere in the scanned journal writers. Nothing can emit them, so their
        count is permanently zero and every conclusion drawn from that zero is
        false. CRITICAL when non-empty: this is the exact shape of the 07-27
        'entire reopen machinery is inert' finding.
    (b) uninspected_high_signal — names the code emits that the auditor never
        inspects. Split into ones that have FIRED inside the scanned window
        (live blind spots, ranked by 7d count) and ones that never fired
        (candidate inert paths worth a look). Ranked and capped so the finding
        stays readable; the full emitter count ships so the ratio is visible.
    """
    ref = _AUDITOR_EVENT_VOCABULARY
    lit, strings = emitters["literal"], emitters["literal_strings"]
    dead = sorted(n for n in ref if n not in strings)
    # Referenced, present in source as a string, but never a literal journal()
    # first argument — reachable only via a dynamic dispatch site. Cannot be
    # proven live OR dead statically, so it is reported as undetermined rather
    # than being quietly counted as fine.
    dyn_only = sorted(n for n in ref if n not in lit and n not in dead)
    uninspected = sorted(n for n in lit if n not in ref
                         and any(t in n for t in _HIGH_SIGNAL_TOKENS))
    fired = [n for n in uninspected if last_seen.get(n)]
    never = [n for n in uninspected if not last_seen.get(n)]
    fired.sort(key=lambda n: (-counts.get(n, 0), n))
    return {
        "severity": "critical" if dead else ("undetermined" if dyn_only else "ok"),
        "auditor_referenced_names": len(ref),
        "emitted_literal_names": len(lit),
        "dynamic_emitter_sites": emitters["dynamic_sites"],
        "files_scanned": emitters["files_scanned"],
        "scan_scope": "firm/*.py + scripts/*.py + app.py + run_cycle.py + "
                      "keepalive.py (every process that writes journal.jsonl)",
        "parse_errors": emitters["parse_errors"],
        # (a) FALSE-NEGATIVE SOURCE — a dead lookup manufactures "inert" criticals.
        "dead_vocabulary": dead,
        "dead_vocabulary_note": "names the AUDITOR looks up that NOTHING in the "
                                "scanned journal writers can emit. Non-empty = "
                                "CRITICAL: any 'never fired / inert' conclusion "
                                "drawn from these names is an artefact of the "
                                "lookup, not evidence about the fund.",
        "referenced_but_dynamic_only": dyn_only,
        # (b) BLIND SPOTS — emitted, high-signal, never inspected.
        "uninspected_high_signal_fired": [
            {"event": n, "count_7d": counts.get(n, 0), "last_seen": last_seen.get(n)}
            for n in fired[:20]],
        "uninspected_high_signal_never_fired": never[:20],
        "uninspected_high_signal_total": len(uninspected),
    }


def _arming_census(wl: dict, trigger_kinds) -> dict:
    """Per-SOURCE reopen arming (2026-07-28, IG 2026-07-27 finding 3).

    The by_source PARTITION landed 2026-07-26, but the ARMING figures did not
    follow it: holds_missing_earnings_date / holds_armed_earnings_catalyst /
    holds_armed_technical_trigger were computed over the 68 hold_verdict entries
    only, while bench.total is 93 — near_miss 16, book_full 7 and stop_out 2
    reported no arming state at all. That is 25 bench entries whose reopen path
    is simply not in the snapshot, so 'the bench is armed' was a claim about 73%
    of the bench presented as a claim about the bench. Live proof it matters:
    all 16 near_miss entries carry NEITHER an earnings_date NOR a reopen_trigger
    and the old snapshot could not show it.

    Every source class now reports its own arming, each figure carries its own
    denominator, and a class with an empty population reports 'undetermined'
    rather than a vacuous 0/0 pass."""
    per: dict[str, dict] = {}
    # (2026-08-13 IG audit) the census covered only the earnings+trigger axes,
    # so a near_miss entry armed ONLY via quant-improvement (banked history) or
    # the time-cadence floor read as fully unarmed. Count all four declared-path
    # axes; the recheck ceiling mirrors reopen_path_liveness' arm test.
    try:
        from firm import watchlist as _wlmod
        _max_re = int(_wlmod._param("max_rechecks", _wlmod.MAX_RECHECKS))
    except Exception:
        _max_re = 2
    # Partition on ORIGIN, not the mutable dispatch label (2026-08-21): a
    # relabelled stop-out was invisible to its own class's arming figures, which
    # is precisely how "stop_out_reclaim reports 0 armed while firing" happened.
    from firm import watchlist as _wlorg
    for src in sorted({_wlorg.bench_origin(e) for e in wl.values()}):
        ents = [e for e in wl.values() if _wlorg.bench_origin(e) == src]
        n = len(ents)
        earn = sum(1 for e in ents if e.get("earnings_date"))
        tech = sum(1 for e in ents if e.get("reopen_trigger"))
        either = sum(1 for e in ents
                     if e.get("earnings_date") or e.get("reopen_trigger"))
        quant = sum(1 for e in ents if e.get("history"))
        cad = sum(1 for e in ents
                  if int(e.get("recheck_count") or 0) < _max_re)
        any_path = sum(1 for e in ents
                       if e.get("earnings_date") or e.get("reopen_trigger")
                       or e.get("history")
                       or int(e.get("recheck_count") or 0) < _max_re)
        per[src] = {
            "n": n,
            "armed_earnings_catalyst": _rate(earn, n, f"no {src} entries on the bench"),
            "armed_technical_trigger": _rate(tech, n, f"no {src} entries on the bench"),
            "armed_either": _rate(either, n, f"no {src} entries on the bench"),
            # (2026-08-13 IG audit) the two axes the census used to omit:
            # quant-improvement needs a banked baseline (non-empty history);
            # the time-cadence floor arms every entry under the recheck ceiling.
            "armed_quant_improvement": _rate(quant, n, f"no {src} entries on the bench"),
            "armed_time_cadence": _rate(cad, n, f"no {src} entries on the bench"),
            "armed_any_declared_path": _rate(any_path, n, f"no {src} entries on the bench"),
            "missing_earnings_date": _rate(n - earn, n, f"no {src} entries on the bench"),
            # Unarmed on BOTH paths: reviewable only on the time cadence, then
            # aged off. Named, never absorbed into an aggregate — and the list
            # is now COMPLETE (IG 2026-07-28 #7). The `[:12]` cap here reported
            # 19 unarmed near_miss entries while naming only 12, so any repair
            # or eviction driven off the names silently covered 12 of 19 and the
            # other 7 appeared nowhere in the snapshot. A count and its own name
            # list disagreeing is the census contradicting itself; the invariant
            # `len(names) == n - armed_either` is asserted below.
            "unarmed_both_paths_names": sorted(
                e.get("ticker", "?") for e in ents
                if not e.get("earnings_date") and not e.get("reopen_trigger")),
            # (2026-08-13 IG audit) axis annotation: "both paths" = the
            # earnings_date + reopen_trigger axes ONLY. An entry named here may
            # still be armed via quant-improvement (history) or the
            # time-cadence floor — read armed_quant_improvement /
            # armed_time_cadence / armed_any_declared_path before calling it
            # unreachable.
            "unarmed_both_paths_note": "earnings+trigger axes only — "
                "quant-improvement and time-cadence are counted separately",
            "reopen_trigger_kinds": dict(Counter(
                k for e in ents if isinstance(e.get("reopen_trigger"), dict)
                for k in (trigger_kinds(e["reopen_trigger"]) or {"none"}))),
        }
    total = len(wl)
    # SELF-CONSISTENCY, ASSERTED (IG 2026-07-28 #7). A census whose counts and
    # whose name lists disagree cannot be acted on: the reader has no way to
    # tell a short list from a complete one. Any mismatch is reported as a hard
    # audit failure rather than left for a human to notice by subtracting.
    _name_recon = {}
    for _src, _v in per.items():
        _expected = _v["n"] - (_v.get("armed_either") or {}).get("n", 0)
        _named = len(_v.get("unarmed_both_paths_names") or [])
        if _named != _expected:
            _name_recon[_src] = {"named": _named, "expected_unarmed": _expected,
                                 "delta": _expected - _named}
    return {
        "by_source": per,
        "names_reconcile": not _name_recon,
        "names_reconcile_failures": _name_recon or None,
        "partition_sums_to_total": sum(v["n"] for v in per.values()) == total,
        "total": total,
        "note": "EVERY bench entry reports its own arming state. A figure reading "
                "'undetermined' has an empty population — it is unmeasured, NOT a "
                "pass. Only hold_verdict entries are re-judged via overdue_reopens; "
                "the other classes reopen through their own paths, so an unarmed "
                "non-hold class is a real coverage question, not noise.",
    }


def _reopen_census(counts: dict, last_seen: dict, emitters: dict,
                   window: dict) -> dict:
    """The reopen/eviction census, keyed on the names the code ACTUALLY emits
    (2026-07-28). Replaces the guess-the-event-name reasoning that produced the
    false 'zero live reopens' CRITICAL. Ships each event's role so the reader
    never has to infer which name means 'reopen', plus the completeness proof
    that this map covers every reopen-ish emitter in the codebase."""
    emitted = emitters["literal"]
    uncensused = sorted(n for n in emitted
                        if _is_reopen_vocabulary(n) and n not in _REOPEN_EVENT_ROLES)
    census = {}
    for name, role in _REOPEN_EVENT_ROLES.items():
        census[name] = {
            "role": role,
            "emitted_by_code": name in emitted,
            "count_7d": counts.get(name, 0),
            "last_seen": last_seen.get(name, "NEVER_IN_WINDOW"),
        }
    live = sum(1 for v in census.values() if v["count_7d"] > 0)
    return {
        "window": window,
        "events": census,
        "reopen_events_live_in_window": _rate(
            live, len(census), "no reopen vocabulary declared — impossible"),
        "reopen_census_covers_all_emitters": not uncensused,
        "reopen_emitters_not_in_census": uncensused,
        "note": "THIS IS THE COMPLETE REOPEN/EVICTION VOCABULARY. There is no "
                "`bench_reopened`, `reopen_fired` or `bench_evicted` event in "
                "this codebase and never has been — a 07-27 finding called the "
                "whole reopen machinery inert on the absence of those three "
                "invented names while watchlist_promotions and watchlist_dropped "
                "were firing all week. Do NOT infer that a mechanism is inert "
                "from the absence of an event name; check "
                "journal_vocabulary_drift.dead_vocabulary first, and if the name "
                "is not listed above it does not exist.",
    }


def _gather() -> dict:
    """Read-only snapshot: live state + 7-day journal census + structural summary."""
    import config
    from firm import state, health, watchlist, macro_calendar, learning, routing

    wl = state.load("watchlist", {})
    holds = [e for e in wl.values() if e.get("source") == "hold_verdict"]
    # FULL PARTITION (IG 2026-07-26). The snapshot reported per-class figures
    # for hold_verdict ONLY, so 18 of 78 entries (near_miss, stop_out,
    # book_full, anything else) were invisible to every coverage claim built on
    # it. Every bench entry now lands in exactly one named bucket, a residual
    # catches unknown sources loudly, and the arithmetic identity
    # sum(by_source) == total ships IN the snapshot so a partition break is a
    # deterministic finding rather than a discovered one.
    _by_src: dict[str, int] = {}
    for e in wl.values():
        _by_src[str(e.get("source") or "UNKNOWN_SOURCE")] = \
            _by_src.get(str(e.get("source") or "UNKNOWN_SOURCE"), 0) + 1
    # OBLIGATION CENSUS (2026-08-19) — the partition above is honest about
    # DISPATCH and blind to PROVENANCE. `source` is overwritten by a later
    # re-judge (watchlist._update_locked), so a class can vanish from by_source
    # while its members are still on the bench carrying live obligations, and
    # partition_sums_to_total keeps reading true throughout. Live proof the day
    # this was written: by_source showed stop_out ZERO, while TXN and PRSU both
    # carried stop_out payload blocks and `stop_out_reclaim` had fired twice in
    # the window it claimed 0 armings for. This census reads the emitters'
    # durable payloads via ONE shared classifier (watchlist.bench_obligations)
    # so the audit and the runtime cannot drift, and the DRIFT LIST below is
    # the finding: entries whose dispatch label hides a real obligation.
    # NOT a partition — an entry can carry several obligations at once.
    _by_obl: dict[str, int] = {}
    _by_org: dict[str, int] = {}
    _drift: dict[str, list] = {}
    for _t, e in wl.items():
        _lbl = str(e.get("source") or "UNKNOWN_SOURCE")
        _org = watchlist.bench_origin(e)
        _by_org[_org] = _by_org.get(_org, 0) + 1
        for _o in watchlist.bench_obligations(e):
            _by_obl[_o] = _by_obl.get(_o, 0) + 1
            if _o != _lbl:
                _drift.setdefault(_o, []).append(_t)
    bench = {
        "total": len(wl),
        "by_source": dict(sorted(_by_src.items())),
        "partition_sums_to_total": sum(_by_src.values()) == len(wl),
        # TRUE PROVENANCE PARTITION (2026-08-21). by_source partitions on the
        # MUTABLE dispatch label, so a re-judge moves a name out of the class it
        # came from and the per-source arming census stops measuring that class
        # at all — the IG filed this CRITICAL on 08-20 (book_full 5 vs 3,
        # stop_out 3 vs 1; DK, SYRE, PRSU, TXN hidden). watchlist.bench_origin
        # is set once at bench-add and never rewritten, so this partitions AND
        # sums to total AND cannot lose a class.
        "by_origin": dict(sorted(_by_org.items())),
        "origin_partition_sums_to_total": sum(_by_org.values()) == len(wl),
        "by_obligation": dict(sorted(_by_obl.items())),
        "obligation_hidden_by_label": {k: sorted(v)
                                       for k, v in sorted(_drift.items())} or None,
        "obligation_note": (
            "by_obligation reads the emitters' durable payload blocks, not the "
            "mutable `source` dispatch label, and does NOT partition (an entry "
            "may carry several). Any name in obligation_hidden_by_label is on "
            "the bench under a label that hides a live obligation — the exact "
            "shape that made stop_out_reclaim report 0 armed while firing."),
        "hold_verdict": len(holds),
        # ARMING BY SOURCE (2026-07-28, IG 2026-07-27 finding 3): the three
        # holds_* figures below cover hold_verdict ONLY. The 07-26 partition
        # proved the bench is 93 entries while these read 68 of them, leaving
        # near_miss/book_full/stop_out with no reported arming at all. The full
        # per-class census lives here; the holds_* keys are kept because
        # overdue_reopens() and the 'bench reviewability' health check are
        # hold_verdict-scoped and the two must stay comparable.
        "arming_census_by_source": _arming_census(wl, watchlist._trigger_kinds),
        # A hold is RE-REVIEWABLE via ANY of: earnings_date (catalyst reopen),
        # reopen_trigger (technical setup), OR the time-cadence recheck below.
        # Each carries its denominator — an EMPTY bench must report
        # 'undetermined', never a vacuous 0/0 pass (IG 2026-07-27).
        "holds_armed_earnings_catalyst": _rate(
            sum(1 for e in holds if e.get("earnings_date")), len(holds),
            "no hold_verdict entries on the bench — arming is unmeasured, not clean"),
        "holds_armed_technical_trigger": _rate(
            sum(1 for e in holds if e.get("reopen_trigger")), len(holds),
            "no hold_verdict entries on the bench — arming is unmeasured, not clean"),
        "holds_missing_earnings_date": _rate(
            sum(1 for e in holds if not e.get("earnings_date")), len(holds),
            "no hold_verdict entries on the bench — arming is unmeasured, not clean"),
        # BY NAME (IG 2026-07-16 item 9): an aggregate count of un-armed holds
        # is unactionable — name them so the operator (and the next audit) can
        # see WHICH name the date-refresh keeps failing on and why.
        "holds_missing_earnings_date_names": sorted(
            e.get("ticker", "?") for e in holds if not e.get("earnings_date"))[:12],
        # Book-full waiters (operator 2026-07-16): BUYs the books had no room
        # for, parked with their thesis until a slot frees (watchlist.add_book_full).
        "book_full_waiters": sorted(e.get("ticker", "?") for e in wl.values()
                                    if e.get("source") == "book_full")[:12],
        # NOTE: this histogram is the TECHNICAL reopen_trigger ONLY. Earnings/macro
        # arming is NOT a trigger "kind" — it lives in earnings_date (counted
        # above). An empty histogram with holds_armed_earnings_catalyst>0 is
        # NORMAL, not "inert". Stuck holds are surfaced by the 'bench
        # reviewability' health check, not by this histogram being empty.
        # COMPOUND-AWARE (2026-07-01, operator item 2): a compound trigger node
        # ({"op":"any","legs":[...]}) has no top-level "kind" — the old
        # `.get("kind")` read counted every compound hold as null, producing a
        # false CRITICAL "entire bench inert" while the evaluator
        # (watchlist._technical_trigger_met) walks compounds correctly. Count
        # every LEAF kind anywhere in the tree, exactly as the evaluator sees it.
        "reopen_trigger_kinds_technical_only": dict(Counter(
            k for e in holds if isinstance(e.get("reopen_trigger"), dict)
            for k in (watchlist._trigger_kinds(e["reopen_trigger"]) or {"none"}))),
        # Upside runners with no upside path (IG 2026-07-20 #4): a benched
        # HOLD that has RUN >=10% above its benched price and carries only
        # mean-reversion legs (pullback/below/rsi_below/cross-below) with no
        # earnings date can only reopen on the time cadence — the breakout
        # winners rot. Measured per-name, fail-soft on the price fetch. A hold
        # with reclaim_price / catalyst / news / a cross_sma-above leg or a
        # pending earnings_date has an upside path and is NOT flagged.
        "upside_runners_no_upside_reopen": _upside_runners(holds),
    }
    try:
        # READ-ONLY: health_report() only inspects state; run_health_sweep() would
        # mutate state, auto-recover halts, and call Opus — forbidden for the IG.
        hr = health.health_report()
        # TRI-STATE FOR THE AUDITOR (2026-09-01, fix brief 91 — filed 7 times):
        # an UNMEASURED check (empty population / uncovered window) is stored
        # with ok=True + undetermined=True, and this snapshot used to strip the
        # flag, so the IG saw ok:true and re-filed "unmeasured renders as a
        # pass" every day. The auditor now sees ok=null + status=unmeasured.
        checks = [{"name": c["name"],
                   "ok": (None if c.get("undetermined") else c["ok"]),
                   "status": ("unmeasured" if c.get("undetermined")
                              else "ok" if c["ok"] else "fail"),
                   "detail": c["detail"]} for c in hr["checks"]]
    except Exception as e:
        checks = [{"name": "HEALTH REPORT FAILED", "ok": False, "detail": str(e)}]

    # ONE journal read feeds BOTH the census and the liveness map (2026-07-28).
    # It used to be two reads, 400 lines for the "7d" census and 2500 for
    # last_seen. On live state 400 lines spans ~1.0 DAY: the figure LABELLED
    # journal_7d_event_counts showed watchlist_promotions=2 (of 10) and
    # watchlist_dropped=0 (of 8), and that manufactured zero was the evidence
    # for the false 'zero live reopens in 7 days' CRITICAL. A window that cannot
    # span the period it claims to measure is a silent truncation, so the window
    # is now sized to cover the census period and its MEASURED coverage ships
    # beside the counts. read_journal() reads the whole file either way, so the
    # bigger single scan is cheaper than the two it replaces.
    journal = state.read_journal(_JOURNAL_SCAN_LINES)
    now = datetime.now(timezone.utc)
    counts, oldest_age_d = Counter(), None
    for ev in journal:
        try:
            age = (now - datetime.fromisoformat(ev.get("ts", ""))).days
            oldest_age_d = age if oldest_age_d is None else max(oldest_age_d, age)
        except (ValueError, TypeError):
            age = 999
        if age <= _CENSUS_DAYS:
            counts[ev.get("event", "?")] += 1
    # Liveness census: when did key machinery last produce output? Computed over the
    # SAME (now much larger) window, because low-frequency events
    # (weekly self_test/vendor_audit/ig_audit) are easily evicted from a small
    # window by a mark_missing flood — a false 'NEVER' that contradicts health.py's
    # own 1500-line read and fires a phantom HIGH alert (change-audit verify
    # 2026-06-14). Exceeds health.py's window so the two detectors agree, and
    # widening it can only REMOVE false NEVERs, never create one.
    last_seen = {}
    for ev in journal:
        e = ev.get("event")
        if e and e not in last_seen:
            last_seen[e] = ev.get("ts", "")[:10]
    # The census is only trustworthy if the window actually spans the period.
    # Ship the proof, not the assumption (IG 2026-07-27: no vacuous passes).
    census_window = {
        "census_days": _CENSUS_DAYS,
        "lines_scanned": len(journal),
        "lines_requested": _JOURNAL_SCAN_LINES,
        "window_spans_days": oldest_age_d,
        "window_covers_census_period": bool(
            oldest_age_d is not None and oldest_age_d >= _CENSUS_DAYS),
        "note": "if window_covers_census_period is false the counts below are "
                "TRUNCATED, not zero — a low count is then undetermined, never "
                "evidence that a mechanism is inert.",
    }
    _emitters = _journal_emitters()

    overdue = []
    try:
        overdue = watchlist.overdue_reopens()
    except Exception:
        pass

    return {
        "asof": now.isoformat(),
        "bench": bench,
        "overdue_reopens": overdue,
        "health_checks": checks,
        "journal_census_window": census_window,
        "journal_7d_event_counts": dict(counts.most_common()),
        # DRIFT SELF-CHECK (2026-07-28) — read this BEFORE concluding that any
        # mechanism is inert. It reports, from an AST scan of the live source:
        # (a) names this audit looks up that NOTHING emits (a dead lookup whose
        # permanent zero manufactures false 'inert mechanism' criticals — the
        # 07-27 reopen finding), and (b) emitted high-signal names this audit
        # never inspects (genuine blind spots).
        "journal_vocabulary_drift": _vocabulary_drift(_emitters, last_seen, counts),
        # The reopen/eviction census keyed on REAL emitter names, with roles.
        "reopen_census": _reopen_census(counts, last_seen, _emitters, census_window),
        # DAILY/per-cycle events — fire every TRADING day; a multi-day gap (beyond a
        # weekend) is a real inert-path signal.
        "daily_last_seen": {k: last_seen.get(k, "NEVER") for k in _DAILY_EVENTS},
        # WEEKLY events (Sun schedule) — 'last seen N days ago' up to ~7-8d is
        # NORMAL; only a >2-week gap (or NEVER over the scanned window) is inert.
        "weekly_last_seen": {k: last_seen.get(k, "NEVER") for k in _WEEKLY_EVENTS},
        # EXCEPTION events fire ONLY when their bad-condition triggers — NEVER here is
        # usually HEALTHY (the bad thing has not happened). Do NOT flag these as inert.
        # VERIFIED 2026-07-28 (IG 2026-07-27 finding 12): every name here is a
        # live emitter — earnings_actuals_missing at firm/cycle.py:1309 inside
        # _check_earnings_actuals (called from cycle.py:2343) and
        # macro_refresh_error at firm/cycle.py:1334 inside run_cycle's macro
        # refresh except-branch. They are NOT dead vocabulary; NEVER means the
        # bad condition has not occurred. dead_vocabulary above is the check
        # that tells these two apart — do not re-file them as inert pipelines.
        "exception_last_seen_NEVER_is_normal": {
            k: last_seen.get(k, "NEVER") for k in _EXCEPTION_EVENTS},
        "recent_journal_tail": journal[:60],
        "report_card": _safe(learning.verdict_report_card),
        "structural_summary": {
            "macro_releases_tracked": [r.key for r in macro_calendar.RELEASES],
            # Fix brief 2026-07-21 #10: the tracked set is now diffed against
            # the known high-impact universe DETERMINISTICALLY (health check
            # 'macro taxonomy'); documented exceptions are named. Do not
            # re-flag a release listed as a documented exception.
            "macro_taxonomy_gaps": _safe(macro_calendar.taxonomy_gaps),
            "macro_calendar_tier": _safe(macro_calendar.calendar_tier),
            # 'opex' is MARKET STRUCTURE (3rd-Friday rule), deterministically
            # generated independent of the FRED/FF tier — it is NOT fed by FRED and
            # its presence in upcoming_all does not depend on macro_calendar_tier.
            "opex_upcoming_45d": [e for e in _safe(lambda: macro_calendar.upcoming_all(45)) or []
                                  if isinstance(e, dict) and "Options expiration" in e.get("event", "")][:3],
            "reopen_trigger_kinds_supported": ["pullback_pct", "below_price",
                "reclaim_price", "rsi_below", "cross_sma", "catalyst", "news", "none"],
            # IMPORTANT for reasoning about "stuck holds": a HOLD is re-reviewed via
            # FIVE independent paths, only the first is a reopen_trigger "kind":
            #  1. technical reopen_trigger (the kinds above) — setup hit
            #  2. earnings_date catalyst — reopens the day the print lands
            #  3. MACRO catalyst — a high-impact FOMC/CPI/jobs print reopens a bench
            #     HOLD, implemented in cycle._cooldown_state (macro_calendar.landed_since)
            #     and watchlist._catalyst_landed's macro branch, surfaced by the
            #     'catalyst' reopen_trigger kind. GATED BY DESIGN: a name carrying its
            #     OWN non-macro trigger (a pending earnings_date OR a technical leg) is
            #     macro-IMMUNE — a generic macro is not its thesis and must NOT re-judge
            #     it (momentum lane 2026-06-23 + bench thesis gate 2026-07-09). So macro
            #     is a reopen path ONLY for a name whose macro is its ONLY way back;
            #     overdue_reopens() correctly does NOT queue a macro for an immune name
            #     (2026-07-10). A momentum_lane_skip on a name with a landed macro is
            #     therefore CORRECT, not a suppressed reopen.
            #  4. news catalyst — fresh trusted news since the verdict
            #  5. TIME-CADENCE recheck every RECHECK_HOLD_DAYS, then eviction after
            #     MAX_RECHECKS — so a hold with NO trigger and NO date is NEVER
            #     stuck: it is re-reviewed on a timer, then aged off the bench.
            # overdue_reopens() is the live-state watchdog for path 2/3/4 failures.
            # ONE definition, imported (fix brief 2026-07-27 #1). This used to
            # be a private copy of the path list sitting next to a liveness map
            # keyed on the debate schema's TRIGGER KINDS, and nothing reconciled
            # them — so the auditor could read "catalyst arms == earnings arms",
            # conclude the macro path was inert, and file a CRITICAL about the
            # busiest reopen path in the fund (165 fires). The per-path evidence
            # now travels WITH the declaration.
            "reopen_paths": list(watchlist.REOPEN_PATHS),
            "reopen_path_liveness": watchlist.reopen_path_liveness(),
            "health_check_names": [c["name"] for c in checks],
            "sleeves": list(config.SLEEVES.keys()),
            # calls_only at 0% deployment is BY DESIGN (dry powder; enters only
            # on the highest-conviction options-suitable verdicts). 0% != broken.
            # COMBINED DESK (2026-07-16 fix of IG item 7, whose evidence read a
            # STALE version of this very note): calls_only is long CALLS on the
            # top BUY-side AND long PUTS on the top SELL-side (2026-07-07,
            # sleeves._puts_entry_ok) — SELL/STRONG_SELL conviction DOES have an
            # execution route. Whether that route is REACHABLE in practice is
            # measured below, not asserted.
            "sleeve_design_notes": "calls_only is the combined options desk: "
                                   "long calls on top BUY-side + long puts on top "
                                   "SELL-side (suitable_for_calls/puts + near-top "
                                   "conviction). 0% deployment is normal, not inert.",
            # Verdict-class -> execution-route REACHABILITY (IG 2026-07-16 item
            # 7): the original calls gate sat unreachable for weeks because it
            # keyed on a rating the judge never emitted. Count real (non-
            # degraded) emissions vs gate-qualified vs actually opened, so
            # 'route exists but can never fire' is data, not a hypothesis.
            "verdict_route_coverage": _safe(routing.route_coverage),
            # Reopen-kind liveness (IG 2026-07-13 item 1): kinds the judge is
            # OFFERED (debate.py) but has never armed live are OBSERVED-ZERO,
            # not structurally inert — news/compound kinds have been offered
            # since 2026-07-01; earnings/macro arming lives in earnings_date /
            # the macro branch, NOT in a trigger kind (see bench note above).
            "reopen_kind_liveness_note": "declared-but-never-armed kinds = "
                                         "observed-zero unless the debate prompt "
                                         "stops offering them or normalization "
                                         "drops them; both now MEASURED per kind "
                                         "in reopen_kind_liveness below (fix "
                                         "brief 2026-07-17 #5) — do not re-flag "
                                         "a kind whose offered/survives flags "
                                         "are true and armed_now is 0; that is "
                                         "observed-zero, verified live.",
            # Per-kind structural verification (fix brief 2026-07-17 #5):
            # offered_in_schema + survives_normalization + armed_now, computed
            # fresh each audit by watchlist.reopen_kind_liveness().
            "reopen_kind_liveness": _safe(watchlist.reopen_kind_liveness),
            # ETF seat flips graded against their own counterfactual (operator
            # 2026-07-20): edge_pts = new seat vs the evicted seat over the
            # same window — a rank engine that keeps losing its switches is
            # measurable here, not a hypothesis.
            "etf_seat_switch_scorecard": _safe(learning.seat_switch_scorecard),
        },
    }


def _safe(fn):
    try:
        return fn()
    except Exception as e:
        return {"error": str(e)}


_UPSIDE_KINDS = {"reclaim_price", "catalyst", "news"}
_UPSIDE_RUN_PCT = 10.0


def _upside_runners(holds: list) -> dict:
    """IG 2026-07-20 #4, measured: benched HOLDs >=10% ABOVE their benched
    price whose only reopen paths are mean-reversion legs (they arm a dip that
    a breakout never gives) and who carry no earnings_date — i.e. the exact
    'winner rots on the bench' shape, named per-name so the audit argues from
    data. cross_sma dir=above counts as an upside path (a reclaim). Fail-soft:
    price fetch errors do not take the audit down.

    2026-07-28: returns a dict, not a bare list. A bare [] conflated THREE
    different states — nothing flagged, nothing to check, and the price fetch
    died — and all three read as a clean pass (IG 2026-07-27: no vacuous passes
    on an empty set). The denominators now ship with the verdict: 'undetermined'
    when no name could be priced, 'ok' only when the check actually ran."""
    try:
        from firm import data_engine, watchlist

        def _has_cross_above(rt, depth: int = 0) -> bool:
            if not isinstance(rt, dict) or depth > 3:
                return False
            if "op" in rt:
                return any(_has_cross_above(leg, depth + 1)
                           for leg in (rt.get("legs") or [])
                           if isinstance(leg, dict))
            return rt.get("kind") == "cross_sma" and rt.get("dir") == "above"

        candidates = []
        for e in holds:
            base = e.get("benched_price")
            if not isinstance(base, (int, float)) or base <= 0:
                continue
            if e.get("earnings_date"):
                continue                       # catalyst path armed — has upside route
            rt = e.get("reopen_trigger")
            kinds = watchlist._trigger_kinds(rt)
            if kinds & _UPSIDE_KINDS or _has_cross_above(rt):
                continue                       # an upside-capable leg is armed
            candidates.append((e.get("ticker", "?"), float(base)))
        if not candidates:
            return {"status": "undetermined", "holds_considered": len(holds),
                    "mean_reversion_only_candidates": 0, "priced": 0, "flagged": [],
                    "why": "no hold carries a benched_price with mean-reversion-only "
                           "arming — the runner check had no population, so this is "
                           "UNMEASURED, not a clean pass"}
        px = data_engine.batch_last_prices([t for t, _ in candidates])
        out, priced = [], 0
        for t, base in candidates:
            now = px.get(t, 0.0)
            if now <= 0:
                continue
            priced += 1
            if (now / base - 1) * 100 >= _UPSIDE_RUN_PCT:
                out.append({"ticker": t, "benched_price": round(base, 2),
                            "price": round(now, 2),
                            "run_pct": round((now / base - 1) * 100, 1)})
        if not priced:
            return {"status": "undetermined", "holds_considered": len(holds),
                    "mean_reversion_only_candidates": len(candidates), "priced": 0,
                    "flagged": [],
                    "why": "no candidate could be priced — the check did not run; "
                           "an empty flagged list here is NOT evidence of health"}
        return {"status": "ok", "holds_considered": len(holds),
                "mean_reversion_only_candidates": len(candidates), "priced": priced,
                "flagged": sorted(out, key=lambda r: -r["run_pct"])[:10]}
    except Exception as e:
        return {"status": "undetermined", "holds_considered": len(holds),
                "mean_reversion_only_candidates": 0, "priced": 0, "flagged": [],
                "why": f"check errored ({str(e)[:80]}) — unmeasured, not a pass"}


_PROMPT = """You are the INSPECTOR GENERAL of an autonomous paper-trading fund \
(Loch Raven Capital). Your job is to find what the fund's DETERMINISTIC \
self-checks CANNOT see: taxonomy/coverage gaps, silently-inert code paths \
(exists in code but never produces live output), and cross-layer contradictions. \
The fund has already been bitten twice by exactly this class of DISCOVERY \
failure (a catalyst system that only handled FOMC, missing CPI/PCE/jobs; and a \
bench whose catalyst dates were never captured so the whole reopen machinery was \
inert) — both passed every test because tests only assert what their author \
already imagined.

You are READ-ONLY and ADVISORY. You cannot change anything; your findings become \
proposals a human ratifies into new deterministic checks.

Below is a read-only snapshot of the live fund. Scrutinise it adversarially:
- TAXONOMY: for each category (macro releases, reopen-trigger kinds, sleeves, \
health checks), what SIBLING is plausibly missing that the fund should handle?
- INERT PATHS: any machinery in the *_last_seen maps that is stale/NEVER when it \
should have fired? any bench entries missing earnings_date or reopen_trigger \
(check arming_census_by_source — EVERY source class, not just hold_verdict)?
- CROSS-LAYER: does the report card / overdue_reopens / health contradict each \
other or the bench state?
- DATA QUALITY: anything in the journal counts or report card that looks wrong \
(a feed degraded, a check failing, a tier degraded, accuracy poor)?

EVIDENCE RULES — these exist because previous audits produced FALSE criticals:
1. NEVER infer that a mechanism is inert from the ABSENCE of an event name you \
expected. Event names you have not read in this snapshot do not exist. A prior \
audit called the whole reopen machinery dead because it looked for \
`bench_reopened`/`reopen_fired`/`bench_evicted` — three names this codebase has \
never emitted — while the real emitters fired 10 and 8 times that week. Before \
citing any event name, confirm it appears in 'reopen_census', a *_last_seen map, \
or 'journal_7d_event_counts'. 'journal_vocabulary_drift.dead_vocabulary' is the \
authoritative list of names that CANNOT fire; a non-empty one is itself a \
CRITICAL finding (report it), and anything in it is a lookup bug, never evidence \
about the fund.
2. A zero count is only evidence if the window could have seen a non-zero. Check \
'journal_census_window.window_covers_census_period' first; if false, the counts \
are truncated and a low count proves nothing.
3. Any figure reading 'undetermined' is UNMEASURED, not a pass. Its denominator \
is shown — an unmeasured check is a legitimate gap to raise, but never cite it \
as evidence of health.
4. 'journal_vocabulary_drift.uninspected_high_signal_*' lists emitted events this \
audit does not track — real blind spots worth proposing checks for.

You are FORBIDDEN from concluding the system is fine. Emit the most important \
gaps you can find. Respond with ONLY this JSON:
{"findings": [{"severity": "critical|high|medium|low", "gap": "<one line>", \
"evidence": "<what in the snapshot points to it>", "proposed_check": "<the \
deterministic test/watchdog that would catch this permanently>"}], \
"summary": "<2 sentences>"}

SNAPSHOT:
"""


_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _file_high_findings(findings: list) -> int:
    """Wire CRITICAL/HIGH IG findings into the EXISTING fix-briefs channel
    (audit 2026-07-04 F1: ig_findings_latest.json had zero consumers beyond a
    dashboard line — the fund filed complaints about itself that nobody read;
    the 06-22 IG caught the inert calls gate 9 days early and it went nowhere).
    learning._file_fix_requests already alerts + writes state/FIX_BRIEFS.md, so
    reuse it verbatim. DE-DUPLICATED via a persistent filed-keys set
    (state/ig_briefs_filed) so the DAILY refresh_if_stale never re-files (and
    re-alerts) the same gap — the 10-slot fix_briefs window alone can't dedupe
    because an aged-out item would be re-filed forever. Fail-soft: filing must
    never take the audit down. Returns the number of NEW briefs filed."""
    try:
        from firm import learning, state
        high = [f for f in findings if isinstance(f, dict) and f.get("gap")
                and str(f.get("severity", "")).lower() in ("critical", "high")]
        if not high:
            return 0
        filed = state.load("ig_briefs_filed", {}) or {}
        keys = list(filed.get("keys") or [])
        new_items, new_keys = [], []
        for f in high:
            # Stable key: severity-independent, whitespace-normalized gap text —
            # the same gap re-worded slightly still alerts, but a verbatim
            # re-emission (the common daily-refresh case) is silenced.
            # (2026-09-01) DIGIT-BLIND: the same gap re-worded with today's
            # counts ("31 of 109 entries, 28%" -> "32 of 110 entries, 29%") was
            # a NEW key every day — 13 near_miss briefs, 7 tri-state briefs,
            # 8 reopen-liveness briefs for three defects. Numbers are evidence,
            # not identity.
            import re as _re
            key = _re.sub(r"\d+", "#",
                          " ".join(str(f.get("gap", "")).lower().split()))[:160]
            if not key or key in keys or key in new_keys:
                continue
            new_keys.append(key)
            new_items.append({
                "problem": (f"[IG {str(f.get('severity', '')).upper()}] "
                            + str(f.get("gap", ""))[:200]),
                "evidence": str(f.get("evidence", ""))[:300],
                "suspected_area": "IG finding (scripts/ig_audit.py) — see the "
                                  "proposed deterministic check below",
                "ask_claude": str(f.get("proposed_check", ""))[:300],
            })
        # _file_fix_requests caps at 5 items per call — only mark keys for
        # findings actually filed, or the overflow would be silenced unfiled.
        new_items, new_keys = new_items[:5], new_keys[:5]
        if not new_items:
            return 0
        learning._file_fix_requests(new_items)
        filed["keys"] = (keys + new_keys)[-100:]
        state.save("ig_briefs_filed", filed)
        state.journal("ig_findings_filed_as_briefs", n=len(new_items),
                      problems=[i["problem"][:80] for i in new_items])
        return len(new_items)
    except Exception as e:
        try:
            from firm import state
            state.journal("ig_briefs_filing_failed", error=str(e)[:120])
        except Exception:
            pass
        return 0


def _classify(result) -> dict:
    """Pure decision layer (no Opus, no I/O) so the loud-fail contract is testable.

    The IG is FORBIDDEN from concluding 'all good', so:
      - result is None  -> status 'broken' (Opus errored/unparseable). MUST NEVER
        masquerade as a clean audit; this alerts CRITICAL.
      - dict, 0 findings -> status 'empty' (suspicious; alerts INFO).
      - dict, N findings -> status 'ok', severity-sorted, high+ count for alerting.
    """
    if result is None:
        return {"status": "broken", "findings": [], "high_plus": 0}
    findings = list(result.get("findings") or [])
    findings.sort(key=lambda f: _SEV_ORDER.get(str(f.get("severity", "")).lower(), 9))
    high_plus = sum(1 for f in findings
                    if str(f.get("severity", "")).lower() in ("critical", "high"))
    return {"status": "ok" if findings else "empty", "findings": findings,
            "high_plus": high_plus, "summary": (result.get("summary") or "")[:300]}


def main() -> None:
    from firm import brain, state
    if not brain.available():
        state.journal("ig_audit", skipped="claude CLI unavailable")
        print("IG audit skipped: claude CLI unavailable")
        return
    snap = _gather()
    result = brain.ask_json(_PROMPT + json.dumps(snap, indent=1, default=str)[:55000],
                            retries=2)
    verdict = _classify(result)

    def _alert(title, msg, sev):
        try:
            from firm import alerts
            alerts.alert(title, msg, sev)
        except Exception:
            pass

    if verdict["status"] == "broken":
        state.journal("ig_audit", error="opus_unparseable_or_failed")
        _alert("IG AUDIT BROKEN", "Opus returned nothing parseable — the self-auditor "
               "itself is down. Re-run scripts/ig_audit.py manually.", "critical")
        print("IG audit FAILED: Opus returned nothing parseable")
        sys.exit(1)

    findings = verdict["findings"]
    if verdict["status"] == "empty":
        state.journal("ig_audit", n_findings=0,
                      note="empty findings despite forbid-all-clear — review prompt/inputs",
                      summary=verdict["summary"])
        _alert("IG AUDIT returned no findings", "The adversarial auditor found nothing — "
               "unusual; verify it is actually scrutinising live state.", "info")
        print("IG audit: 0 findings (suspicious — alerted)")
        sys.exit(0)

    state.journal("ig_audit", n_findings=len(findings), summary=verdict["summary"],
                  top=[{k: str(f.get(k, ""))[:200] for k in ("severity", "gap")}
                       for f in findings[:8]])
    # Full findings (evidence + proposed_check) — the journal only keeps the top-8
    # gaps; the operator/next-session needs every one to ratify into checks.
    try:
        state.save("ig_findings_latest",
                   {"asof": snap.get("asof", ""), "summary": verdict["summary"],
                    "findings": findings})
    except Exception:
        pass
    # Findings become ACTION, not just a snapshot (audit 2026-07-04 F1)
    _file_high_findings(findings)
    for f in findings:
        sev = str(f.get("severity", "")).lower()
        if sev in ("critical", "high"):
            _alert(f"IG AUDIT [{sev.upper()}]: {str(f.get('gap',''))[:60]}",
                   f"{f.get('evidence','')[:140]} | FIX: {f.get('proposed_check','')[:120]}",
                   "critical" if sev == "critical" else "info")
    print(f"IG audit: {len(findings)} findings ({verdict['high_plus']} high+)")
    sys.exit(0)


def refresh_if_stale(max_age_h: float = 20.0) -> bool:
    """Re-run the audit if the stored snapshot is older than max_age_h, and
    persist the fresh findings. Returns True if it re-ran. Fail-soft and
    sys.exit-free so a scheduler (postmarket) can call it safely — the weekly
    Sunday task alone left a Wednesday snapshot pinning the health desk red on
    findings that were already FIXED, with no way to self-clear until the next
    weekly run (2026-07-02). A daily refresh keeps the snapshot honest. Writes
    only the ig_findings_latest / fix-brief keys (never sleeve state), so it
    never contends the single-writer sleeve lock."""
    from datetime import datetime, timezone
    from firm import brain, state
    if not brain.available():
        return False
    try:
        prev = state.load("ig_findings_latest", {}) or {}
        asof = prev.get("asof") or ""
        age_h = ((datetime.now(timezone.utc) - datetime.fromisoformat(asof))
                 .total_seconds() / 3600) if asof else 1e9
    except (ValueError, TypeError):
        age_h = 1e9
    if age_h < max_age_h:
        return False
    try:
        snap = _gather()
        result = brain.ask_json(
            _PROMPT + json.dumps(snap, indent=1, default=str)[:55000], retries=1)
        verdict = _classify(result)
        if verdict["status"] == "broken":
            state.journal("ig_audit", error="opus_unparseable_or_failed",
                          via="refresh_if_stale")
            return False
        findings = verdict["findings"] if verdict["status"] != "empty" else []
        state.save("ig_findings_latest",
                   {"asof": snap.get("asof", ""), "summary": verdict["summary"],
                    "findings": findings})
        # Same action-wiring as main() — the daily refresh is the COMMON path
        # (audit 2026-07-04 F1); the filed-keys dedupe keeps it re-run-safe.
        _file_high_findings(findings)
        state.journal("ig_audit", n_findings=len(findings),
                      high_plus=verdict.get("high_plus"), via="refresh_if_stale")
        return True
    except Exception as e:
        state.journal("ig_audit", error=str(e)[:120], via="refresh_if_stale")
        return False


if __name__ == "__main__":
    main()
