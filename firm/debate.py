"""The debate room — Bull vs Bear vs Base, then the PM judge rules.

Every candidate that clears research gets argued from three adversarial
positions before the portfolio-manager judge issues the binding verdict
that the sleeves trade on.
"""
import json
import threading
from datetime import datetime, timezone

import config
from firm import analysts, brain

_CASE_SCHEMA = (
    '{"case": "<the strongest possible argument, 4-8 sentences>", '
    '"strongest_points": ["..."], "what_breaks_it": "<one sentence>"}')

# FIELD ORDER IS DECISION ORDER (cognition audit 2026-07-01, #2): for an
# autoregressive model, emitting `rating` first commits the verdict token
# before a word of reasoning exists — the thesis is then written to justify
# the rating (anchoring). Reasoning fields come FIRST; the decision fields
# (rating/conviction/weight) come LAST so they condition on the articulated
# evidence. Parsers read keys, not order — this costs nothing.
_SCHEMA_HEAD = (
    '{"debate_summary": "<2 sentences on how bull/bear/base resolved>", '
    '"thesis": "<3-5 sentences: the binding investment thesis>", '
    '"bear_risk": "<the single risk most likely to kill this trade>", '
    '"what_changed": "<one sentence: what is MATERIALLY different from your '
    'previous verdict on this name (see previous_verdict in the packet), or '
    '"NO_CHANGE", or "FIRST_LOOK" if the packet has no previous_verdict>", '
    '"watch_trigger": "<if not a BUY: the specific, observable change that '
    'would upgrade this to a BUY — used to re-check the name later>", ')
# Structured, machine-evaluable version of watch_trigger so the bench can reopen the
# name the MOMENT the setup is hit — not on a crude timer (a "wait for the dip"
# thesis is invisible to a momentum re-check). kinds: pullback_pct/below_price/
# reclaim_price/rsi_below/cross_sma (technical); catalyst (SCHEDULED earnings or
# macro — FOMC/CPI/PCE/jobs); news (UNSCHEDULED qualitative catalyst); none.
_SCHEMA_REOPEN_LEAF = (
    '"reopen_trigger": {"kind": "pullback_pct|below_price|reclaim_price|rsi_below|'
    'cross_sma|catalyst|news|none", "pct": <float, for pullback_pct>, '
    '"price": <float, for below_price/reclaim_price>, "level": <int, for rsi_below>, '
    '"period": <50|200, for cross_sma>, "dir": "<above|below, for cross_sma>"}, ')
# Flag ON: same single-condition leaf OR a COMPOUND node mirroring a multi-part
# watch_trigger 1:1 (operator 2026-06-23; design + adversarial review wf).
_SCHEMA_REOPEN_COMPOUND = (
    '"reopen_trigger": <EITHER ONE condition {"kind": "pullback_pct|below_price|'
    'reclaim_price|rsi_below|cross_sma|catalyst|news|none", "pct": <float>, '
    '"price": <float>, "level": <int rsi>, "period": <50|200>, "dir": "above|below"} '
    'OR, when your watch_trigger has MORE THAN ONE part, a COMPOUND node mirroring it '
    '1:1 (each leg is one condition object): ALL must hold {"op":"all","legs":[<c>,<c>]}; '
    'ANY one — use for "...OR the earnings" — {"op":"any","legs":[<c>,<c>]}; ONLY AFTER an '
    'event THEN a setup {"op":"after","legs":[<catalyst/news c>,<c or all/any>]}. Map every '
    'AND->all, OR->any, "after/once X then Y"->after; {"kind":"catalyst"}=your awaited '
    'earnings/macro print; max 4 legs, max 2 deep. Unless a BUY, NEVER "none". e.g. '
    '"dip that resets RSI below 55 AND holds the 50-DMA, OR the earnings" -> '
    '{"op":"any","legs":[{"op":"all","legs":[{"kind":"rsi_below","level":55},'
    '{"kind":"cross_sma","period":50,"dir":"above"}]},{"kind":"catalyst"}]}>, ')
_SCHEMA_TAIL = (
    '"suitable_for_calls": <true|false — make this an EXPLICIT decision, not a '
    'default (audit 2026-07-16: 0 true in 575 verdicts left the options desk, '
    '25% of firm capital, inert for 5+ weeks). TRUE when the thesis is a '
    'directional move you expect WITHIN 1-6 weeks, the options are liquid, and '
    'NO binary event (earnings/FDA/court) sits inside that window. FALSE for '
    'slow re-ratings, binary-event straddles-in-disguise, or illiquid chains. '
    'A modest-conviction BUY with a clean near-term setup CAN be true>, '
    '"suitable_for_puts": <true|false — same explicit decision on the SELL side, '
    'and hold it to the SAME bar as calls, not a stricter one (audit 2026-07-26: '
    'this flag has been true ZERO times ever, leaving the firm structurally '
    'unable to profit from a decline — the identical inertia the calls flag was '
    'remediated for in 2026-07-16). TRUE when the thesis is a directional DOWN '
    'move you expect WITHIN 1-6 weeks, the options are liquid, and NO binary '
    'event sits inside that window. FALSE for slow de-ratings, binary-event '
    'straddles-in-disguise, or illiquid chains. A modest-conviction SELL with a '
    'clean breakdown CAN be true; it must be a real short thesis, not merely '
    '"avoid going long">, '
    '"suitable_for_calls_reason": "<ONE short line justifying your calls flag>", '
    '"suitable_for_puts_reason": "<same, for the puts flag>", '
    '"options_veto_calls": <true|false — true ONLY to actively FORBID the '
    'options desk from buying calls on this name even when its own mechanical '
    'screen (options tradable + no earnings print inside the option window) '
    'passes. This is an OVERRIDE, not a restatement of suitable_for_calls: '
    'false is the NORMAL answer and hands the decision to the desk mechanical '
    'screen (fix brief 2026-07-21 #6: requiring a reason line on every verdict '
    'made reason-presence bind as a veto on ~every name — the veto must be '
    'YOUR explicit decision). If true, suitable_for_calls_reason must name the '
    'concrete disqualifier (the binary event, the broken chain)>, '
    '"options_veto_puts": <true|false — same explicit override, puts side>, '
    '"horizon": "<e.g. 3-6 months>", '
    '"rating": "STRONG_BUY|BUY|HOLD|SELL|STRONG_SELL", '
    # BANDED RUBRICS (2026-07-26, operator-found compression). The previous
    # wording named ONE anchor per component ("10 = typical case", "5 =
    # acceptable") and the judge treated that anchor as a CEILING, not a
    # midpoint: across a live 34-name cohort evidence spanned only 7-11 of 20,
    # asymmetry 3-11 of 20, timing 3-7 of 10, differentiation 0-6 of 10 — not
    # one component ever reached even 60% of its cap, and conviction collapsed
    # to p10 60 / p50 68 / p90 70. That flat signal is what starves the calls
    # desk (its bar is the adaptive p90, which sat 2 points above the median)
    # and what makes position sizing discriminate on noise. Every band below is
    # now stated in OBSERVABLE terms so the top of each scale is reachable on
    # merit and the bottom is reachable on weakness.
    '"conviction_components": {'
    '"evidence": <int 0..20 — how VERIFIED the thesis is. 0-4: one desk, thin '
    'or actively contradicted. 5-9: two desks agree, material gaps remain. '
    '10-14: three or more desks agree on verifiable facts. 15-17: four or more '
    'desks confluent AND the bear case concedes the facts and argues only '
    'timing/valuation. 18-20: every desk confluent on hard reported numbers '
    'with no factual dispute left>, '
    '"asymmetry": <int 0..20 — reward/risk skew at CONCRETE levels, scored in '
    'the DIRECTION of your rating. 0-4: skew runs against you. 5-9: roughly '
    'symmetric, near 1:1. 10-14: about 2:1 with a defined invalidation level. '
    '15-17: 3:1 or better with risk capped by a hard structural floor. 18-20: '
    '4:1 or better, risk capped AND a dated catalyst inside the horizon>, '
    '"timing": <int 0..10 — setup freshness. 0-2: late, extended, or the move '
    'already happened. 3-5: no edge either way. 6-7: constructive base or '
    'pullback holding. 8-10: fresh breakout or a confirmed inflection with a '
    'dated near-term catalyst>, '
    '"differentiation": <int 0..10 — what the market is MISSING. 0-2: pure '
    'consensus, the view is already priced. 3-5: a nuance others under-weight. '
    '6-7: a specific mispricing you can point to in the numbers. 8-10: a '
    'variant view contradicting visible consensus, with the evidence for why '
    'consensus is wrong>}, '
    '"target_weight_pct": <float 0..20, of sleeve capital>}')

# Legacy single-leg schema (byte-identical to before) — kept for any caller.
_VERDICT_SCHEMA = _SCHEMA_HEAD + _SCHEMA_REOPEN_LEAF + _SCHEMA_TAIL


def _verdict_schema() -> str:
    """The verdict JSON schema. Flag OFF returns the legacy single-leg string
    byte-for-byte; flag ON swaps in the compound reopen_trigger guidance."""
    import config
    reopen = _SCHEMA_REOPEN_COMPOUND if config.COMPOUND_TRIGGERS else _SCHEMA_REOPEN_LEAF
    return _SCHEMA_HEAD + reopen + _SCHEMA_TAIL


def _ctx(packet: dict, research: dict) -> str:
    return (f"DATA PACKET:\n{json.dumps(packet, indent=1, default=str)}\n\n"
            f"RESEARCH FLOOR REPORTS:\n{json.dumps(research, indent=1, default=str)}")


def _side_prompt(side: str, packet: dict, research: dict) -> str:
    stance = {
        "bull": "You are the BULL. Argue the strongest case FOR buying. Steelman it.",
        "bear": "You are the BEAR. Argue the strongest case AGAINST. Attack the thesis ruthlessly.",
        "base": "You are the BASE CASE analyst. Argue the most probable boring outcome.",
    }[side]
    return (f"{stance}\nYou are in an institutional investment-committee debate "
            f"about {packet['ticker']}.\n\n{_ctx(packet, research)}\n\n"
            f"Respond with ONLY this JSON:\n{_CASE_SCHEMA}")


def _combined_debate_prompt(packet: dict, research: dict) -> str:
    return ("Run an institutional investment-committee debate about "
            f"{packet['ticker']} with three adversarial voices: BULL (strongest "
            "case for), BEAR (strongest case against), BASE (most probable "
            "outcome). Each voice must genuinely attack the others' weakest "
            f"points.\n\n{_ctx(packet, research)}\n\n"
            'Respond with ONLY this JSON:\n{"bull": ' + _CASE_SCHEMA +
            ', "bear": {...}, "base": {...}} (same schema each)')


# Discernment for a held WINNER — the committee must not reflexively HOLD a
# proven, still-accelerating leader just because it's "extended" (operator
# 2026-06-25: that forfeits the biggest gains). Injected into every judge prompt.
_LEADER_ADD_GUIDANCE = (
    "ADDING TO A WINNER — read carefully: if the firm ALREADY HOLDS this name "
    "(the packet's position_context block shows the current sleeves, entry, and "
    "unrealized P&L; if position_context is absent the firm does NOT hold it) "
    "and it is a CONFIRMED "
    "momentum leader — an intact multi-month uptrend — AND the thesis is "
    "STRENGTHENING (an earnings beat with steady/raised guidance per the 8-K, "
    "10-Q, transcript or news in the packet, or clear sector strength), then "
    "'the stock is extended' is NOT automatically a HOLD. Leaving a validated, "
    "still-accelerating leader at a static weight forfeits the largest gains. In "
    "that specific case prefer BUY with a target_weight_pct ABOVE the current "
    "position so the fund ADDS ON STRENGTH (the risk desk executes it in "
    "measured, capped steps). DISCERN, never chase: this applies ONLY to a proven "
    "winner with momentum intact AND improving fundamentals AND no sector "
    "deterioration; a parabolic move on weakening fundamentals or into a fading "
    "sector still warrants HOLD or a trim, and you never add to a loser to "
    "average down. HARD PRECONDITION (2026-07-26): 'improving fundamentals' "
    "must be VERIFIABLE in this packet — if the fundamentals block is null or "
    "unpopulated you cannot claim it, and this add-on-strength case does not "
    "apply. MIRROR RULE, same force in the other direction: a held name whose "
    "evidence is WEAKENING — thesis pillars failing, momentum rolled over, the "
    "position moving against you with nothing in the packet refuting the bear "
    "case — is a TRIM or SELL, never a re-affirmed BUY at a static weight.\n\n")

# STRONG_BUY semantics (2026-07-01 goal audit, J1): in 339 verdicts across 22
# days the judge NEVER used STRONG_BUY (while using STRONG_SELL 5x) because the
# schema gave the tier no meaning — and the calls desk, which gates on it, sat
# structurally inert. This defines the tier without lowering any bar: the firm's
# highest conviction tier, used rarely, on merit. Injected into every judge
# prompt. The degraded quant fallback deliberately CANNOT emit it.
_STRONG_BUY_GUIDANCE = (
    "RATING TIERS — use the FULL ladder, both directions: STRONG_BUY is a real "
    "tier, not decoration. Issue STRONG_BUY for the EXCEPTIONAL asymmetric setup "
    "you would concentrate capital in: an intact uptrend AND improving "
    "fundamentals AND a specific near-term catalyst or freshly-confirmed thesis "
    "(a beat-and-raise print, a structural demand inflection), where your "
    "conviction is 80+. Do NOT park such an idea in plain BUY out of caution — "
    "under-rating your best idea starves the strategies that key on the top "
    "tier (the options desk trades ONLY STRONG_BUYs). It should be RARE — most "
    "cycles have none — but when the setup is genuinely exceptional, say so.\n\n")

# The DOWN half of the ladder (2026-07-26, operator: "make money when the market
# is down"). _STRONG_BUY_GUIDANCE opens with "use the FULL ladder, both
# directions" and then spends every word on STRONG_BUY; _LEADER_ADD_GUIDANCE is
# about adding to winners; _CONVICTION_GUIDANCE's capital bands are all long
# position sizes. Three of the four assembled guidance blocks were long-biased
# and NOTHING told the judge when to go the other way — so it didn't:
# sell_family_emitted = 2 across 30 cycles, puts_gate_qualified = 0 ever.
#
# The graded evidence says the silence is the whole problem, not the judgement:
#   BUY   n=18  avg fwd excess  +3.5%   deep-negative  0
#   HOLD  n=118 avg fwd excess  -6.1%   deep-negative 29   <- the dumping ground
#   SELL  n=3   avg fwd excess -30.7%   deep-negative  2   <- RIGHT when it fires
# The sell side is ACCURATE and nearly silent. 65% of all verdicts land in HOLD,
# and 29 of those cratered. (This also supersedes the 2026-06-14 IG finding that
# STRONG_SELL was "anti-predictive" at +23% — that read n=1 before the report
# card's alphabetical truncation was fixed; the full 182-row grade reverses it.)
_SELL_GUIDANCE = (
    # REWRITTEN 2026-07-26 (forensics): the first version of this block hectored
    # the judge about HOLD over-use with hard-coded stats ("65% of verdicts were
    # HOLD... averaged -6.1%"). Measured effect over the next two cycles: the
    # judge routed AROUND hold in the WRONG direction — the 60-64-conviction band
    # that had been HOLD became BUY (26/27 BUY in cycle #51) while the evidence
    # components stayed identical, and SELL emission did not move. Lesson: anti-
    # HOLD pressure without a direction converts uncertainty into BUYs, because
    # BUY is the modal token on a momentum-screened desk. The rewrite states the
    # DIRECTION RULE symmetrically and leaves the live stats to the scorecard
    # digest, which is calibration-gated and never bakes stale constants.
    "THE DOWN SIDE OF THE LADDER — this firm must make money when names fall, "
    "not merely avoid them. SELL and STRONG_SELL are real tiers.\n"
    "THE RATING MUST MATCH THE EVIDENCE DIRECTION: deteriorating evidence is a "
    "SELL-family rating; genuinely two-sided evidence while you wait for a "
    "named event is a HOLD — a legitimate resting state, never something to "
    "route around; improving evidence is a BUY-family rating. Do NOT convert "
    "an uncertain HOLD into a BUY: an unconvinced BUY is WORSE than an honest "
    "HOLD, because capital acts on it. If the honest read is 'this "
    "deteriorates from here', that is a SELL — say it.\n"
    "Issue SELL when the thesis is BROKEN or the setup is breaking down: "
    "deteriorating fundamentals (a miss, guidance cut, margin compression, "
    "rising channel inventory), a broken multi-month trend, a structural demand "
    "or competitive shift against the name, or a valuation that requires "
    "perfection the evidence no longer supports. Issue STRONG_SELL for the "
    "exceptional NEGATIVE asymmetry — the one you would actively short: broken "
    "trend AND deteriorating fundamentals AND a specific near-term downside "
    "catalyst. It should be RARE, exactly as STRONG_BUY is rare.\n"
    "Two distinct consequences, so rate the NAME not the holding: if the firm "
    "HOLDS it, a sell-family rating exits the position; if it does NOT, the "
    "rating is what feeds the short/put desk and keeps the name out of the "
    "book. Never soften a SELL to HOLD because the firm happens to own it — "
    "that is the single most expensive bias available to you.\n"
    "Score the components in the DIRECTION of the rating: on a SELL, asymmetry "
    "is DOWNSIDE skew (how much further it falls vs how much it can rebound) "
    "and timing is the freshness of the BREAKDOWN.\n\n")

# Conviction must CARRY INFORMATION (2026-07-01 purpose review, rec #3b).
# Escalation ladder, each step measured by the "conviction distribution"
# health line: static "use the full range" (07-01 — failed, 15 days of 60-66);
# dynamic own-distribution mirror (re-grade 07-16 — failed, 07-17 cycle still
# 61-65); component-arithmetic-in-prose (07-17 — failed, 07-20 cycle emitted
# 63-66: prose arithmetic still lets the attractor pick the SUM and back-fill
# components). Root cause: a monolithic 0-100 scale with gate anchors at
# 55/60/70 has a psychological attractor at "decent but not exceptional"
# ~= 61-64. CURRENT step (operator 2026-07-20, escalated same-day): the
# top-level conviction field is REMOVED from the schema — the judge emits
# only the four component scores and _finalize_verdict sums them in code.
# The attractor number no longer exists as an emittable token; variance adds
# across independently-judged components. That step ALSO failed (07-27: stdev
# 3.1, p90-p10 8 over 132 verdicts, 10 consecutive failing cycles) — the named
# next escalation, per-component forced ranking, ships as the ANCHOR TABLE
# below (record_component_anchors / _anchor_table_note).
_CONVICTION_GUIDANCE = (
    "CONVICTION — you do NOT pick the number. You emit the four "
    "conviction_components in the schema and CODE computes conviction = 40 + "
    "evidence + asymmetry + timing + differentiation (40-100). Judge each "
    "component on ITS OWN scale, independently, in the direction of your "
    "rating (SELL-family: asymmetry = downside skew, timing = the "
    "breakdown's freshness). Most ordinary ideas score differentiation 0-3 "
    "and timing 4-6 — score honestly, not politely; an inflated component is "
    "a lie the sizing engine acts on. The capital gates read the computed "
    "sum, and the ladder is SYMMETRIC (2026-07-26: every band here used to "
    "describe a long position, so a bearish view had nowhere to go but HOLD). "
    "On a BUY-family rating: <55 pass; 55-64 starter size only; 65-74 full "
    "position; 75+ concentrate; 80+ on an exceptional setup is STRONG_BUY "
    "territory. On a SELL-family rating the same sum measures how strongly "
    "the name should be REMOVED and how hard the short side should press: "
    "<55 pass (not worth acting on); 55-64 exit a held position but no short; "
    "65-74 exit and a candidate for the puts desk; 75+ is STRONG_SELL "
    "territory — an active short thesis. "
    # FULL-SCALE ANCHORS (2026-08-13 operator decision, sign-off on the IG's
    # options-desk reachability finding): name what the computed sum MEANS so
    # the components stop compressing into the 60-64 attractor. Prompt-text
    # only — the JSON contract (conviction_components) is untouched. Whether
    # this anchoring works is MEASURED by the 'conviction distribution' health
    # check (stdev / p90-p10), the same line that graded every prior rung of
    # this escalation ladder.
    "ANCHOR THE COMPUTED SUM ON THE FULL SCALE: 80+ = a top-decile idea of "
    "the YEAR; 70-79 = strong, clearly above-average edge; 60-69 = moderate "
    "edge; below 60 = weak. Do NOT compress toward the middle of the scale: "
    "if this idea is genuinely stronger than most you have judged recently, "
    "the components — and therefore the number — must say so; if it is "
    "weaker, the number must say that too. Two "
    "names must not tie unless their components genuinely tie — position "
    "sizing, the options desk's bar and the acceptance bands all read this "
    "number, and a constant tells them nothing.\n\n")

# Weight ANCHORS (re-grade audit 2026-07-16: firm active share ran ~18% —
# the only weight guidance the judge ever saw was the schema's "0..20" and
# the starter's "2-4%", so EVERY position converged to starter size and the
# book converged to benchmark-minus-costs by construction).
_WEIGHT_GUIDANCE = (
    "POSITION WEIGHT ANCHORS — target_weight_pct is % of a 16-slot sleeve: "
    "full deployment averages ~6%. Calibrate: 2-4% = STARTER only (timing "
    "objection, add-level encoded in reopen_trigger); 5-8% = STANDARD "
    "conviction BUY (this should be your modal size); 8-12% = high conviction; "
    "12-18% = exceptional, concentrate. A book of nothing but 2-3% positions "
    "is not caution — it is refusing the mandate: capital you do not assign "
    "to conviction sits in benchmark beta and cannot beat the benchmark. "
    "Size the risk with the vol-scaler in mind (it halves weight past 50% "
    "ann. vol for you — do not double-discount volatility).\n\n")


# ── Escalation step 4: the ANCHOR TABLE (fix brief 2026-07-27 #10) ───────────
# Step 3 (components summed in code, 07-20) did NOT decompress the signal:
# BUY-family stdev 3.1, p90-p10 8 over 132 verdicts, 10 consecutive failing
# cycles. The ladder's own comment named the next step as "per-component forced
# ranking across the cycle's candidates" — but the judge sees ONE name per call,
# so true intra-cycle ranking is IMPOSSIBLE in-prompt (the same reason the 07-17
# "spread it" instruction was undecidable in a single sitting). The buildable
# equivalent: rank each component against the firm's OWN measured component
# distribution. record_component_anchors() persists the p10/p50/p90 of the last
# N non-degraded verdicts PER COMPONENT after each cycle, and _anchor_table_note()
# injects that table into the judge prompt. "Typical" stops being a felt word and
# becomes a number this judge must place itself against, component by component —
# which is what forced ranking actually buys, delivered THROUGH the
# one-name-per-call architecture instead of against it.
_ANCHOR_N = 60


def record_component_anchors(verdicts) -> dict:
    """Persist per-component percentile anchors from a cycle's verdicts.

    Pure w.r.t. the caller (no cycle state touched, never raises): pass either
    the cycle's {ticker: verdict} map or a list of verdict dicts. Degraded
    verdicts and legacy replies without conviction_components are ignored —
    an outage cycle must not teach the anchors that "typical" collapsed.
    Keeps a rolling window of the last _ANCHOR_N samples per component and
    stores p10/p50/p90 under state key "conviction_anchors".

    Wiring (orchestrator, NOT edited here): in firm/cycle.py, right after the
    research_log history row is appended, call
        debate.record_component_anchors({k: v["verdict"] for k, v in verdicts.items()})
    """
    try:
        from firm import state as _st
        rows = (list(verdicts.values()) if isinstance(verdicts, dict)
                else list(verdicts or []))
        fresh: dict[str, list[int]] = {k: [] for k in _COMPONENT_CAPS}
        for v in rows:
            if not isinstance(v, dict) or v.get("degraded"):
                continue
            comps = v.get("conviction_components")
            if not isinstance(comps, dict) or not comps:
                continue
            for k, cap in _COMPONENT_CAPS.items():
                c = comps.get(k)
                if isinstance(c, (int, float)):
                    fresh[k].append(int(max(0, min(cap, c))))
        if not any(fresh.values()):
            return {}
        store = _st.load("conviction_anchors", {}) or {}
        samples = store.get("samples") if isinstance(store.get("samples"), dict) else {}
        out_samples, out_pct = {}, {}
        for k in _COMPONENT_CAPS:
            prior = [x for x in (samples.get(k) or []) if isinstance(x, (int, float))]
            window = (prior + fresh[k])[-_ANCHOR_N:]
            out_samples[k] = window
            if len(window) >= 8:        # same small-sample floor as _calibration_note
                srt = sorted(window)
                out_pct[k] = {q: srt[int(f * (len(srt) - 1))]
                              for q, f in (("p10", 0.1), ("p50", 0.5), ("p90", 0.9))}
        store = {"asof": datetime.now(timezone.utc).isoformat(),
                 "n": max(len(v) for v in out_samples.values()),
                 "samples": out_samples, "percentiles": out_pct}
        _st.save("conviction_anchors", store)
        return store
    except Exception:
        # An anchor-keeping failure must never take a cycle down.
        return {}


def _anchor_table_note() -> str:
    """Inject the live anchor table into the judge prompt (fix brief #10).
    Fail-soft: no anchors on file (or too few samples) -> NO injection, so a
    fresh install / post-outage cycle simply runs the prior guidance."""
    try:
        from firm import state as _st
        pct = ((_st.load("conviction_anchors", {}) or {}).get("percentiles") or {})
        rows = [(k, pct[k]) for k in _COMPONENT_CAPS
                if isinstance(pct.get(k), dict)
                and all(q in pct[k] for q in ("p10", "p50", "p90"))]
        if not rows:
            return ""
        table = "; ".join(
            f"{k} (0-{_COMPONENT_CAPS[k]}): p10 {v['p10']}, p50 {v['p50']}, p90 {v['p90']}"
            for k, v in rows)
        return (
            "COMPONENT ANCHOR TABLE — the firm's OWN measured component "
            f"distribution over its last {_ANCHOR_N} non-degraded verdicts: "
            f"{table}. You judge one name per sitting, so rank against THIS "
            "table instead of against a remembered sense of 'typical'. For "
            "EACH of the four components, state where this name sits relative "
            "to those percentiles and score accordingly: below p10 means this "
            "is among the weakest tenth you have seen on that dimension, above "
            "p90 among the strongest tenth — both must actually happen, at "
            "roughly that frequency, or the component is not measuring "
            "anything. A component landing within +/-1 of its p50 must be "
            "justified in ONE line naming what makes this name genuinely "
            "indistinguishable from your median case on that dimension — "
            "otherwise MOVE it up or down. 'Typical' is now a measured number, "
            "not a feeling.\n\n")
    except Exception:
        return ""


def _calibration_note() -> str:
    """DYNAMIC self-calibration for the judge (re-grade audit 2026-07-16):
    static 'use the full range' guidance shipped 2026-07-01 and the emitted
    distribution stayed 60-66 x 2.5-3.5%. Feed the judge its OWN measured
    distribution + the book's live deployment so compression is a visible,
    named failure each cycle rather than an abstract instruction. Fail-silent:
    no history -> no note."""
    try:
        from firm import state as _st
        hist = (_st.load("research", {}) or {}).get("history", [])[-30:]
        convs, wts = [], []
        for h in hist:
            for v in (h.get("verdicts") or {}).values():
                if (v or {}).get("degraded"):
                    continue
                if v.get("rating") in ("BUY", "STRONG_BUY"):
                    if isinstance(v.get("conviction"), (int, float)):
                        convs.append(v["conviction"])
                    if isinstance(v.get("target_weight_pct"), (int, float)):
                        wts.append(v["target_weight_pct"])
        if len(convs) < 8:
            return ""
        convs.sort()

        def _p(q: float) -> float:
            return convs[int(q * (len(convs) - 1))]
        note = (f"YOUR OWN RECENT DISTRIBUTION (last {len(convs)} BUY-family "
                f"verdicts): conviction p10 {_p(0.1)}, median {_p(0.5)}, "
                f"p90 {_p(0.9)}")
        if wts:
            note += (f"; mean target weight {sum(wts) / len(wts):.1f}% "
                     f"(anchors above say modal should be 5-8%)")
        try:
            _s = _st.get_sleeve("alpha_stocks")
            _inv = sum((p.get("quantity") or 0)
                       * (p.get("last_price") or p.get("entry_price") or 0)
                       for p in (_s.get("positions") or {}).values()
                       if not p.get("cash_equitization"))
            _eq = (_s.get("cash") or 0) + _inv + sum(
                (p.get("quantity") or 0)
                * (p.get("last_price") or p.get("entry_price") or 0)
                for p in (_s.get("positions") or {}).values()
                if p.get("cash_equitization"))
            if _eq > 0:
                note += (f". The alpha book is {(_inv / _eq * 100):.0f}% "
                         "deployed in conviction names — the remainder is "
                         "parked benchmark beta earning benchmark returns.")
        except Exception:
            pass
        # TRIGGER CALIBRATION (2026-07-26). This corrective note is the judge's
        # only self-correcting signal on compression — and its threshold was set
        # BELOW the compression actually observed, so it never fired. Live
        # 2026-07-25/26: p10 60, p50 68, p90 70 -> spread 10, which sailed past
        # a `<= 8` trigger while the health desk simultaneously failed the
        # "conviction distribution" check for 5 consecutive cycles on the SAME
        # data. A detector whose bar is looser than the failure it exists to
        # catch is decoration. On a 0-100 scale whose components can legitimately
        # span 40-100, a BUY-family p90-p10 under ~20 is not ranking.
        if _p(0.9) - _p(0.1) <= float(getattr(config, "CONVICTION_MIN_SPREAD", 20.0)):
            # Concrete per-verdict rule, not an exhortation (2026-07-17): the
            # judge sees ONE name per call, so "spread it" is undecidable in a
            # single sitting — but "compare THIS case to your own median BUY"
            # is decidable every time.
            _med = _p(0.5)
            note += (
                " Your conviction spread is COMPRESSED — you are not ranking "
                "your own ideas. Concrete rule for THIS verdict: derive the "
                "component sum honestly, then check it against your own median "
                f"BUY ({_med}): a case clearly STRONGER than your typical "
                f"recent BUY must land >= {min(_med + 5, 95)}; clearly WEAKER "
                f"must land <= {max(_med - 5, 40)}; within +/-2 of {_med} is "
                "reserved for a case genuinely indistinguishable from your "
                "median idea.")
        return note + "\n\n"
    except Exception:
        return ""


# STAGED (flag off ~1 week for clean attribution of the STRONG_BUY fix —
# adversarial verify 2026-07-01, flag 3): the quality-vs-timing split. When
# desks are net-bullish but the only objection is ENTRY TIMING, abstaining
# entirely proved the dominant loss mechanism (43 desk-bullish HOLDs; 8
# overlooked winners +20-88%). Requires fractional sizing (shipped) so a
# 2-3% starter can actually fill on $500-1000 stocks.
_STARTER_TIMING_GUIDANCE = (
    "QUALITY vs TIMING — when the desks are net-bullish on the BUSINESS and "
    "the TREND but your objection is only entry timing ('extended', 'wait for "
    "a pullback'), do NOT default to HOLD: either (a) issue a BUY with "
    "conviction 60-64 and a small target_weight_pct (2-4%) as a STARTER "
    "position, encoding your desired pullback as the reopen_trigger so the "
    "add happens at your level; or (b) defend specifically why the pullback "
    "will arrive before the move completes. Your scorecard shows names you "
    "rated HOLD on timing grounds beat SPY by wide margins while the pullback "
    "never came.\n\n")


def _news_edge_note() -> str:
    """DYNAMIC news-edge line for the judge (audit 2026-07-04 F2): the fund's
    own measured news edge is currently INVERTED — verdicts informed by news
    underperform news-blind ones (trusted −7.9 n=13, aggregator −8.9 n=8 at
    ratification). The digest carries a down-weight directive too, but it goes
    empty when >24h stale (digest_context) — this line reads the edge_health
    snapshot DIRECTLY so the judge always sees a live inversion. Fires ONLY
    while edge_health lists inverted news:* edges; small-n discipline is in the
    text itself (discount, never amputate the desk). Fail-soft: an unreadable
    snapshot must never take the judge prompt down."""
    from firm import state
    try:
        eh = state.load("edge_health", {}) or {}
        inv = [e for e in (eh.get("inverted") or [])
               if str(e.get("edge", "")).startswith("news:")
               and isinstance(e.get("value"), (int, float))]
        if not inv:
            return ""
        # NEUTRALIZED tiers (operator 2026-07-07): measured NET-HARMFUL over a real
        # sample (value <= floor, n >= min). For those the instruction escalates from
        # "discount" to "IGNORE for conviction" — the down-weight was too soft while
        # the tape still dragged every verdict toward HOLD. Merely-inverted (small-n)
        # tiers keep the discount + small-sample discipline.
        neu = {str(e.get("edge", "")) for e in (eh.get("neutralized") or [])}
        parts = ", ".join(
            f"{str(e['edge']).split(':', 1)[1]} {e['value']:+.1f}pts (n={e.get('n', '?')})"
            for e in inv)
        if any(str(e.get("edge", "")) in neu for e in inv):
            return ("MEASURED NEWS EDGE — NET-HARMFUL (auto-neutralized): the firm's "
                    f"own scorecard shows news-informed verdicts DESTROY value over a "
                    f"real sample ({parts}). IGNORE the news tape when setting "
                    "CONVICTION — decide on fundamentals, technicals and factor "
                    "evidence ALONE. Headlines tagged '[SPECULATIVE …]' in the packet "
                    "are context only and must NEVER move the number. Use news solely "
                    "to avoid an obvious landmine (a real, confirmed, adverse event), "
                    "never to add conviction.\n\n")
        return ("MEASURED NEWS EDGE — CURRENTLY INVERTED: the firm's own "
                f"scorecard shows verdicts informed by news UNDERPERFORM "
                f"news-blind ones ({parts}) — by the time news is ingestible "
                "it is priced. Weigh sentiment-desk claims sourced from "
                "headlines accordingly: DISCOUNT them and let fundamentals/"
                "technicals carry the decision. Small sample — discount, do "
                "NOT ignore the desk outright.\n\n")
    except Exception:
        return ""


def _judge_guidance() -> str:
    """The composed judge-prompt guidance stack + the live scorecard digest."""
    from firm import learning
    import config as _cfg
    g = (_LEADER_ADD_GUIDANCE + _STRONG_BUY_GUIDANCE + _SELL_GUIDANCE
         + _CONVICTION_GUIDANCE + _WEIGHT_GUIDANCE)
    if getattr(_cfg, "STARTER_TIMING_GUIDANCE", False):
        g += _STARTER_TIMING_GUIDANCE
    # The fund's own re-affirmation record on names it already holds (2026-07-26).
    # Specific and checkable, unlike the generic sell guidance that measurably did
    # not move the distribution: "you rated MU BUY 4 cycles running while it fell
    # 7%, conviction never moving off 67" is a fact about THIS name.
    try:
        g += learning.held_deterioration_context()
    except Exception:
        pass
    # Anchor table BEFORE the calibration note (fix brief 2026-07-27 #10): the
    # table sets each component's measured scale, then _calibration_note's
    # median-BUY rule checks the resulting SUM. Per-component first, sum second
    # — the same order the schema and _finalize_verdict use.
    g += _anchor_table_note()
    g += _calibration_note()
    g += _news_edge_note()
    try:
        g += learning.digest_context()
    except Exception:
        pass
    return g


_MEM = threading.local()     # what the judge was shown, per research worker thread


def _precedent_note(packet: dict, research: dict | None) -> str:
    """Institutional Memory (hackUMBC 2026). The most similar past committee
    decisions and what the market did next, strictly point in time, cited as
    [P#]. Fail soft. The committee never waits on, or breaks over, its memory."""
    if not getattr(config, "MEMORY_RAG_ENABLED", True):
        return ""
    try:
        from firm import memory_rag
        from firm.research_provenance import decision_time
        from zoneinfo import ZoneInfo
        when = decision_time(str(packet["asof"])) if packet.get("asof") else datetime.now(timezone.utc)
        today = when.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        ticker = str(packet.get("ticker") or "")
        query = memory_rag.query_from_research(packet, research)
        kblock, passages = memory_rag.knowledge_block(query, k=2, asof=today)
        block, hits, _signal = memory_rag.precedent_block(
            query, ticker=ticker, asof=today, k=int(getattr(config, "MEMORY_K", 6)))
        from firm.research_rag import source_block
        external, sources = source_block(packet)
        _MEM.last = {
            "external_sources": sources,
            "ticker": ticker,
            "precedents": [{"id": h.memo.id, "ticker": h.memo.ticker, "asof": h.memo.asof,
                            "rating": h.memo.rating,
                            "outcome": h.memo.label if h.outcome_visible else "not yet knowable",
                            "excess_20d": h.memo.excess_20d if h.outcome_visible else None}
                           for h in hits],
            "playbook": [{"id": p.id, "chapter": p.chapter, "section": p.section} for p in passages],
            "evidence": kblock + block + external,
        }
        return kblock + block + external
    except Exception:
        return ""


def _debate_and_judge_prompt(packet: dict, research: dict) -> str:
    """Standard depth: debate + verdict in ONE call (2026-06-10 audit — the
    separate debate call added no information the judge didn't regenerate;
    folding them cuts a third of standard-depth cost with the same output)."""
    from firm import learning
    return (learning.lessons_context()
            + "You are an institutional investment committee in one sitting: "
            "first a genuine adversarial debate — BULL (strongest case for), "
            "BEAR (strongest case against), BASE (most probable outcome), each "
            "attacking the others' weakest points — then the PORTFOLIO MANAGER "
            "judge, whose mandate is beating the S&P 500 risk-adjusted, weighs "
            "the desks and the debate, punishes weak evidence, rewards "
            "asymmetric setups, and issues the binding verdict. HOLD is a "
            "legitimate verdict.\n\n" + _judge_guidance()
            + _precedent_note(packet, research)
            + f"TICKER: {packet['ticker']}\n\n"
            f"{_ctx(packet, research)}\n\n"
            "Reason through the debate briefly in prose first if it helps "
            "(the prose is discarded), then END your reply with exactly one "
            'JSON code block matching:\n{"bull": ' + _CASE_SCHEMA +
            ', "bear": {...}, "base": {...}, "verdict": ' + _verdict_schema() + "}")


def _judge_prompt(packet: dict, research: dict, cases: dict) -> str:
    from firm import learning
    return (learning.lessons_context()
            + "You are the PORTFOLIO MANAGER and final judge at an institutional "
            "asset manager whose mandate is to outperform the S&P 500 on a "
            "risk-adjusted basis. You have the research floor's five desk "
            "reports and the bull/bear/base debate. Weigh them, punish weak "
            "evidence, reward asymmetric setups, and issue the binding verdict. "
            "Be decisive but honest about uncertainty — HOLD is a legitimate "
            "verdict.\n\n" + _judge_guidance()
            + _precedent_note(packet, research)
            + f"TICKER: {packet['ticker']}\n\n{_ctx(packet, research)}\n\n"
            f"DEBATE:\n{json.dumps(cases, indent=1, default=str)}\n\n"
            "Weigh the evidence briefly in prose first if it helps (the prose "
            "is discarded), then END your reply with exactly one JSON code "
            f"block matching:\n{_verdict_schema()}")


def _fast_prompt(packet: dict) -> str:
    from firm import learning
    return (learning.lessons_context()
            + "You are an entire institutional investment committee in one pass: "
            "research desks (fundamental, technical, sentiment, macro, quant), "
            "a bull/bear/base debate, and a PM judge whose mandate is beating "
            "the S&P 500 risk-adjusted.\n\n" + _judge_guidance()
            + _precedent_note(packet, None)
            + f"DATA PACKET:\n{json.dumps(packet, indent=1, default=str)}\n\n"
            "Argue the desks and the debate briefly in prose FIRST (the prose "
            "is discarded — it is your working space), then END your reply "
            f"with exactly one JSON code block matching:\n{_verdict_schema()}")


def _fallback_verdict(packet: dict, research: dict) -> dict:
    comp = analysts.composite_score(research)
    rating = ("BUY" if comp >= 35 else "HOLD" if comp > -20 else "SELL")
    return {
        "rating": rating, "conviction": int(min(95, max(5, 50 + comp / 2.5))),
        "target_weight_pct": 10.0 if rating == "BUY" else 0.0,
        "horizon": "1-3 months", "thesis": "Degraded mode: quant composite only.",
        "bear_risk": "No qualitative debate performed.",
        "suitable_for_calls": comp >= 55, "suitable_for_puts": comp <= -55,
        "debate_summary": "Skipped (degraded mode).",
        "degraded": True,
    }


_COMPONENT_CAPS = {"evidence": 20, "asymmetry": 20, "timing": 10,
                   "differentiation": 10}


def _calibrate_conviction(raw: int) -> tuple[int, dict]:
    """Rescale a raw component sum against the firm's OWN trailing distribution.

    ESCALATION STEP 6, and the first one that does not ask the judge for
    anything (operator 2026-07-28). Steps 1-5 all tried to change what the
    model EMITS — static range guidance (07-01), a dynamic own-distribution
    mirror (07-16), arithmetic-in-prose (07-17), deleting the top-level field
    so only components are emitted (07-20), per-component anchor tables
    (07-26). Every one failed the same way, and the reason is structural: the
    judge scores each name in ISOLATION, with no view of the other 131
    verdicts in the window, so it cannot produce cross-sample variance no
    matter how it is asked. Worse, the top of every component band describes
    an epistemic state real equity research never reaches ("every desk
    confluent on hard reported numbers with no factual dispute left"), so the
    upper 40% of each scale is unreachable BY CONSTRUCTION rather than by
    timidity. Measured: evidence 6-12 of 20, asymmetry 4-8 of 20, timing 2-6
    of 10, differentiation 1-4 of 10.

    What the judge DOES provide reliably is ORDER — it discriminates within
    each component (evidence alone spans 6 points). So the fix stops asking
    for spread and instead recovers the spread already present in the ranking:
    a z-rescale about the trailing MEDIAN, which preserves the ordering
    exactly and preserves the LEVEL (a name at the median stays at the
    median), while stretching deviations to a usable width.

    WHAT THIS DOES NOT DO: it does not open the options desk. That gate is the
    rolling p90 of this same distribution, so it is scale-INVARIANT — measured
    over 900 verdicts, exactly 110 names clear it before and after. This fixes
    the two consumers that read conviction on an ABSOLUTE scale: position
    sizing (643 of 900 verdicts sat in the 2-4% "starter" band, which is how a
    16-slot book converges to benchmark-minus-costs) and the conviction-swap
    threshold, where a +6 gap was 2 sigma and therefore never happened.

    Returns (conviction, provenance). Fail-static: any error returns raw."""
    prov = {"raw": raw, "mode": "raw"}
    try:
        from firm import state
        cfg_n = int(getattr(config, "CONVICTION_CALIBRATION_MIN_N", 40))
        hist = (state.load("research", {}) or {}).get("history", [])
        # THE REFERENCE MUST BE RAW, NEVER CALIBRATED. Reading back a
        # calibrated number and rescaling it again is a positive feedback loop
        # that widens the scale every cycle until everything clamps to 40/100.
        # `conviction_raw` is stamped below precisely so this can never happen;
        # rows predating it hold a raw value in `conviction`.
        ref = [int(r) for h in hist
               for v in (h.get("verdicts") or {}).values()
               if isinstance(v, dict) and not v.get("degraded")
               for r in [v.get("conviction_raw", v.get("conviction"))]
               if isinstance(r, (int, float))]
        if len(ref) < cfg_n:
            prov["note"] = f"only {len(ref)} reference verdicts, need {cfg_n}"
            return raw, prov
        ref.sort()
        med = ref[len(ref) // 2]
        mean = sum(ref) / len(ref)
        sd = (sum((x - mean) ** 2 for x in ref) / len(ref)) ** 0.5
        if sd <= 0.5:
            prov["note"] = "reference distribution has no width to rescale from"
            return raw, prov
        target = float(getattr(config, "CONVICTION_TARGET_STDEV", 8.0))
        gain = min(float(getattr(config, "CONVICTION_MAX_GAIN", 2.5)), target / sd)
        if gain <= 1.0:
            prov["note"] = f"already at or above target spread (stdev {sd:.1f})"
            return raw, prov
        cal = int(max(40, min(100, round(med + (raw - med) * gain))))
        prov.update({"mode": "calibrated", "calibrated": cal, "gain": round(gain, 3),
                     "ref_n": len(ref), "ref_median": med, "ref_stdev": round(sd, 2)})
        return cal, prov
    except Exception as e:
        prov["note"] = f"fail-static: {type(e).__name__}"
        return raw, prov


def _finalize_verdict(v: dict) -> dict:
    """Mechanical conviction (escalation step 3, operator 2026-07-20): the
    judge emits four independent component scores and CODE sums them —
    conviction = 40 + evidence + asymmetry + timing + differentiation. Two
    prompt-level steps (07-01 static, 07-17 arithmetic-in-prose) both left
    the emitted number at the 61-66 attractor; with the top-level field gone
    from the schema the attractor is no longer an emittable token. Each
    component is clamped to its cap (a hallucinated 35/20 must not mint a
    97). Back-compat: a legacy reply carrying only "conviction" keeps it
    (source "judge"); neither present -> neutral 50, journaled — never
    fail-open to a fabricated high number, never crash the cycle."""
    if not isinstance(v, dict) or v.get("degraded"):
        return v
    comps = v.get("conviction_components")
    if isinstance(comps, dict) and comps:
        total = 40
        clean: dict[str, int] = {}
        for k, cap in _COMPONENT_CAPS.items():
            try:
                c = int(float(comps.get(k, 0)))
            except (TypeError, ValueError):
                c = 0
            clean[k] = max(0, min(cap, c))
            total += clean[k]
        v["conviction_components"] = clean
        # The RAW sum is the permanent record and the only thing the
        # calibration reference is ever built from (see _calibrate_conviction).
        v["conviction_raw"] = total
        _cal, _prov = _calibrate_conviction(total)
        v["conviction"] = _cal
        v["conviction_source"] = ("components+calibrated"
                                  if _prov.get("mode") == "calibrated"
                                  else "components")
        v["conviction_calibration"] = _prov
    elif isinstance(v.get("conviction"), (int, float)):
        v["conviction"] = int(max(0, min(100, v["conviction"])))
        v["conviction_source"] = "judge"
    else:
        v["conviction"] = 50
        v["conviction_source"] = "default_missing"
        try:
            from firm import state
            state.journal("conviction_missing", rating=str(v.get("rating", "?")))
        except Exception:
            pass
    return v


def _single_channel_guard(v: dict, packet: dict) -> dict:
    """Deterministic BUY demotion when only ONE evidence channel exists.

    2026-07-26 forensics: with fundamentals null AND the news tape neutralized
    (edge-health measured it anti-predictive), the only signal left in the
    packet is the same momentum that put the name on the desk — a BUY there is
    the quant screen re-scoring itself through the judge, not independent
    judgement. Demote BUY-family to HOLD unless the judge demonstrated a real
    variant view (differentiation >= SINGLE_CHANNEL_MIN_DIFF, default 6 — its
    own rubric's 'specific mispricing you can point to'). Code, not prompt:
    the three prompt-level attempts at rating discipline each bent under the
    modal-token pull; this one cannot. Journaled so the learning loop grades
    what the demotions actually cost (evidence-over-constants)."""
    try:
        if not isinstance(v, dict) or v.get("degraded"):
            return v
        if v.get("rating") not in ("BUY", "STRONG_BUY"):
            return v
        f = packet.get("fundamentals") or {}
        _informative = sum(1 for k, val in f.items()
                           if k not in ("ticker", "source", "_data_quality")
                           and val not in (None, "", 0))
        _nq = packet.get("news_quality") or {}
        _news_dead = (not packet.get("news")) or (
            _nq.get("n_material", 1) == 0 or _nq.get("n_neutralized", 0) > 0)
        if _informative >= 3 or not _news_dead:
            return v
        _diff = (v.get("conviction_components") or {}).get("differentiation") or 0
        if _diff >= int(getattr(config, "SINGLE_CHANNEL_MIN_DIFF", 6)):
            return v
        from firm import state
        state.journal("buy_demoted_single_channel", ticker=packet.get("ticker"),
                      was=v.get("rating"), conviction=v.get("conviction"),
                      differentiation=_diff, informative_fund_fields=_informative)
        v = dict(v)
        v["rating"] = "HOLD"
        v["demoted_single_channel"] = True
        v["what_changed"] = ((v.get("what_changed") or "") +
                             " [code: BUY->HOLD, momentum-only evidence]").strip()
        return v
    except Exception:
        return v          # guard must never break a verdict


def run_debate_and_verdict(packet: dict, research: dict, depth: str = "standard") -> dict:
    """Returns {"cases": {...}, "verdict": {...}}.

    Institutional Memory (hackUMBC 2026). The precedents and playbook passages
    the judge was shown are attached to a real verdict as verdict["memory"], so
    every [P#] and [K#] citation in the thesis can be traced to its source."""
    _MEM.last = None
    out = _run_debate_and_verdict(packet, research, depth)
    try:
        mem = getattr(_MEM, "last", None)
        v = out.get("verdict") or {}
        if mem and mem.get("ticker") == str(packet.get("ticker") or "") and not v.get("degraded"):
            from firm import memory_guard
            text = " ".join(str(v.get(k) or "") for k in ("thesis", "bear_risk", "watch_trigger"))
            evidence = " ".join([mem.get("evidence", ""), json.dumps(packet, default=str),
                                 json.dumps(research or {}, default=str),
                                 json.dumps(out.get("cases") or {}, default=str),
                                 f" {v.get('conviction')} {v.get('target_weight_pct') or ''}"])
            checks = [memory_guard.check_citations(text, len(mem["precedents"]), len(mem["playbook"]),
                        [r["citation"] for r in mem.get("external_sources", [])]),
                      memory_guard.check_numbers(text, evidence)]
            status = ("fail" if any(c["status"] == "fail" for c in checks)
                      else "warn" if any(c["status"] == "warn" for c in checks) else "pass")
            v["memory"] = {"precedents": mem["precedents"], "playbook": mem["playbook"],
                           "external_sources": [{k:r.get(k) for k in ("id", "citation", "source_url", "observed_at", "content_sha256")}
                                                for r in mem.get("external_sources", [])],
                           "checks": {"status": status,
                                      "detail": [f"{c['name']} {c['status']}, {c['detail']}" for c in checks]}}
            memory_guard.audit_append({
                "kind": "committee_verdict", "ticker": mem["ticker"],
                "external_source_ids": [r["id"] for r in mem.get("external_sources", [])],
                "precedent_ids": [p["id"] for p in mem["precedents"]],
                "passage_ids": [p["id"] for p in mem["playbook"]],
                "rating": v.get("rating"), "conviction": v.get("conviction"), "checks": status})
    except Exception:
        pass                      # memory bookkeeping must never break a verdict
    return out


def _run_debate_and_verdict(packet: dict, research: dict, depth: str = "standard") -> dict:
    if not brain.available():
        return {"cases": {}, "verdict": _fallback_verdict(packet, research)}

    if depth == "fast":
        verdict = brain.ask_json(_fast_prompt(packet), retries=1)
        # Same guard as standard/full (2026-07-02 review): a truthy-but-malformed
        # dict (e.g. a prose sub-object extract_json returned) must NOT flow out
        # as a real, non-degraded verdict with no rating — it would be stored,
        # counted in _real_n, and injected as the judge's own prior next cycle.
        if not verdict or "rating" not in verdict:
            verdict = _fallback_verdict(packet, research)
        return {"cases": {}, "verdict": _single_channel_guard(_finalize_verdict(verdict), packet)}

    if depth == "full":
        cases = {}
        for side in ("bull", "bear", "base"):
            cases[side] = brain.ask_json(_side_prompt(side, packet, research)) or {}
        verdict = brain.ask_json(_judge_prompt(packet, research, cases), retries=1)
        if not verdict or "rating" not in verdict:
            verdict = _fallback_verdict(packet, research)
        return {"cases": cases, "verdict": _single_channel_guard(_finalize_verdict(verdict), packet)}

    # standard: debate + judge in ONE call (33% fewer calls, same output)
    combined = brain.ask_json(_debate_and_judge_prompt(packet, research), retries=1)
    cases = {k: (combined or {}).get(k) or {} for k in ("bull", "bear", "base")}
    verdict = (combined or {}).get("verdict")
    if not verdict or "rating" not in verdict:
        verdict = _fallback_verdict(packet, research)
    return {"cases": cases, "verdict": _single_channel_guard(_finalize_verdict(verdict), packet)}
