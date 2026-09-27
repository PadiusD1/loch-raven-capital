# Loch Raven Capital — Full-Fund Re-Grade Audit (FINAL)
**Date:** 2026-07-16 (overnight synthesis, post round-2 fixes) · **Mode:** paper · **Scope:** read-only audit of live state + current on-disk code
**Prior grades (2026-07-16 morning):** Alpha D+ · Risk C · Execution D · Learning B- · Info Diet B- · **Overall C-**

## Reconciliation with the same-night operator draft

An earlier draft at this path (written while the verification fleet was down on the account spend limit) graded the fund B- overall on the assumption that all in-session fixes closed their findings. The adversarial verification fleet subsequently completed. Its results, re-checked against the **current** on-disk code tonight:

- **Verified real:** leveraged/inverse ETP screen (`allocation.py:271-284`, `_LEVERAGED_RX` stamps every pool entry), mechanical seat/parking legs bypassing the flush battery (`sleeves.py:1820-1860`), report-card retention 50→120 rows (`cycle.py:1080, 1974`), momentum-lane key offered in the review prompt (`learning.py:1737`), degraded verdicts never recorded, verdict-review watermark preserved.
- **Claimed closed but still open:** the momentum-lane lever's CONSUMER leg (`cycle.py:541` still reads static `config.MOMENTUM_LANE_MOM_MIN`; `risk.tuned_cap` dead-ends for non-CAP_BOUNDS keys) — a tuned value lands in tuning.json, `caps_tuned` is journaled, behavior never changes; and street-research grounding (the "evidence guard" is a bare 20-char length check — the draft itself defers real grounding).
- **New defect not in the draft:** premarket adds-on-strength are voided at the 9:30 flush (verified tonight, item 5 below).
- **Deferred in the draft, confirmed HIGH by verification:** parking outside all firm caps; paper→live funding starvation.

Net: **C+ overall**, not B-. The delta is exactly the fund's own doctrine — a journal line saying "fixed" is not evidence the loop closed.

---

## 1. Verdict and scorecard

**Verdict: NOT YET positioned to beat SPY, but for the first time the gap is fully specified rather than mysterious.** The measurement stack is institutional-grade (verdict report card, acceptance counterfactuals, edge_health feeding a bounded, counterfactual-journaled sizing tilt). The capital-expression stack is not: 24.7% of firm capital (options desk) is structurally inert behind a gate that has passed 0 of 32 real BUY-family verdicts ever, ~40% of firm equity sits in SPY-clone parking outside every firm exposure cap, and the realized book trails the benchmark (+0.93% vs SPY +2.12% since 06-09 inception; -0.83% vs +0.80% since 07-02). Self-improvement demonstrably closes on the verdict side (reviews fire on cadence, lessons reach every judge prompt, the flying-blind alarm is armed and has fired); on the money side it has never fired once (tuning.json = {}) and its newest lever ships journal evidence of tunes that cannot take effect.

| Dimension | Prior | Now | One-line justification |
|---|---|---|---|
| Strategy & Alpha | D+ | **C-** | Measured edge is real (BUY cohort 8/11 correct, +5.0% avg 21-session SPY excess; momentum factor +17.7pts high-vs-low half, n=63) but cannot be expressed: 18.2% firm active share, options desk inert by construction, conviction compressed to a 56-72 band with zero STRONG_BUYs in 693 verdicts. |
| Risk Architecture | C | **C+** | Standing machinery is B- quality (tiered breakers, diagnosed kill switch, verified auto-resume, fail-closed caps, leverage screen present) — held at C+ by two confirmed highs: parking invisible to every firm cap while one shared momentum engine can converge ~50% of firm into one ticker, and a live-mode funding architecture that starves every conviction buy. |
| Execution Mechanics | D | **B-** | Queued-for-open machinery substantially correct and crash-safe (persistent, per-fill idempotent, mode-stamped; tomorrow's 4 seeded intents walked line-by-line — all fill absent a >3% gap); residual defects are one-screen seams: adds voided at flush, final queue save outside the lock, yfinance calls under SLEEVE_LOCK at 9:30. |
| Learning Loops | B- | **B-** (held) | Verdict-side loop closes end-to-end and retention is now correctly sized (120 rows, verified); held at B- because the only mechanical money levers have never moved, and the flagship momentum-lane lever produces FALSE journal evidence of self-improvement (prompt wired, actuator disconnected). |
| Information Diet | B- | **B- (conditional)** | Round 2 genuinely widened the diet (forward consensus, positioning, macro siblings, street desk); conditional on grounding the street-research review BEFORE the first report is ingested — today the credibility engine can mint confabulated "confirmed_since_publication" stamps with no evidence channel (latent: shelf empty). News input remains measurably inverted (-12pts) under a "managed" label. |
| **Overall** | **C-** | **C+** | The machine is close to sound; the fund now knows exactly where its alpha is and exactly why it is not reaching the book. Items 1-5 below convert a well-instrumented SPY tracker into a fund with a fighting chance. |

---

## 2. Top 10 actions (ranked by expected impact on the beat-SPY goal × confidence)

Status key: [C] confirmed by adversarial verification · [V] independently verified by chief auditor against current code tonight · [U] plausible, unverified.

**1. [C-HIGH] Make the options desk reachable — the binding constraint moved from conviction to the never-emitted `suitable_for_calls` flag.**
`firm/sleeves.py:969-1003` hard-requires the flag before either rating path; the p90 re-key computes bar=65 on live data and all 11 of tonight's non-degraded BUY/SELL verdicts carry the flag False (0 qualified in 32 real BUY-family verdicts ever; /api/health is red: "the gate is unreachable in practice"). The new explicit-suitability prompt (`debate.py:56-65`) has zero cycles of evidence against a ~0-true base rate. **Fix:** desk-side mechanical screen (chain liquidity, spread, no binary event inside expiry) with the judge flag demoted to veto; when a BUY clears the bar, require `suitable_for_calls_reason` so False is a justified decision; escalate the watchdog (threshold 25) from "revisit" to an IG brief. Unlocks $25k / 24.7% of firm from fee-free SPY-clone status. sleeve_calls_only.json today: 1 position, $23.7k SPY parking; zero real option entries in the fund's life.

**2. [C-HIGH] Count parking in firm-wide exposure measurement + add a pre-flip convergence check.**
`risk.py:419-421, 454-456` skip cash_equitization from firm name/ETF/sector numerators (and `risk.py:346` from the correlation basis) while counting it in the denominator. Live: $40.6k parked (~40% of firm; alpha SPY $16,509 + calls SPY $24,087) while etf_rotation already holds parking-eligible IWM $8,274. The identical `data_engine.rotation_signals` ranks parking seats (`allocation.py:328`), core seats, and rotation (`sleeves.py:1558/1593`) — convergence at momentum tops is guaranteed by construction, up to ~50% of firm in one ticker that MAX_FIRM_ETF_PCT=25 never sees; a -8% gap in that ETF is a ~-4% firm day no cap ever priced. The new health line (45% bar, total-parking only) is visibility, not a block, and cannot see the per-instrument stack. **Fix:** remove parking's measurement exemption (keep slot/stop exemptions), add a parking-symbol ceiling (≤50% of firm), and in `review_seats` skip a challenger already carrying >X% firm-wide via rotation/core.

**3. [C-HIGH] Fix the paper→live funding architecture before any live flip.**
`sleeves.py:181-202` (`_free_cash_from_equitization`) sells parking fire-and-forget; in live, sells only credit cash at `settle_pending` (`broker.py:748-753`, later monitor sweeps) while buys size against raw `s["cash"]` in the same pass (all four pull sites, plus the flush which requeues the starved intent to research). Live day 2: ~90% of deployable cash parked → every conviction buy starves, settled proceeds re-park at cycle end, spread paid both ways, structurally zero conviction deployment; `parked_capacity` is inert for the same reason; seat flips half-complete (`allocation.py:128-130` counts freed cash from a still-pending sell; the buy-leg failure is not journaled). **Fix:** size against cash + in-flight parking-sell reservations and retry flush intents next sweep, or poll settle synchronously up to EXIT_CHASE_S before sizing.

**4. [C-HIGH] Close the momentum-lane tuning loop — it currently mints false evidence of self-improvement.**
The clamp accepts MOMENTUM_LANE_MOM_MIN (`learning.py:942-950`), the prompt offers it (`learning.py:1736-1738`), the journal records `caps_tuned` — but `risk.tuned_cap` returns the static base for any key not in CAP_BOUNDS (`risk.py:377-380`) and the sole consumer reads `config.MOMENTUM_LANE_MOM_MIN` statically (**re-verified tonight at `cycle.py:541`**). 228 momentum_lane_skip samples feed a lever whose actuator is disconnected; a visible open loop became an invisible one, which is worse. **Fix (three one-liners):** merge MOMENTUM_LANE_BOUNDS into `cap_bounds_txt` and `blocked_audit.current_caps`; make `_is_extended_momentum_hold` read the tuned value (mirror `watchlist._param`).

**5. [V-MEDIUM, high leverage] Premarket adds-on-strength are dead on arrival at the flush.**
Verified in current code tonight: adds queue with `add_on_strength: True` in meta (`sleeves.py:434-437`) but the flush's already-held guard drops ANY held non-parking symbol (`sleeves.py:1771-1775`, journal `open_queue_already_held`) — an ADD is by definition already held. Every premarket "adds size up" the committee decides is silently voided and retries daily (adds_count correctly isn't bumped); position sizes stay permanently under committee intent, and the 8:30 cycle's operator contract (`cycle.py:1053-1062`) is false in code — the journal reads like idempotency working. **Fix:** pass add-stamped intents through to a top-up execute path (or record `intended_total_qty` and treat held_qty ≥ intended as the crash-refill signal); keep the guard for plain entries.

**6. [C-HIGH] Ground street-research claim verification before the first report drops.**
`street_research.py:69-90` resolves claims from the brain's "knowledge of what has occurred since" — a `--safe-mode` print-mode model with no tools, no web, cutoff before the review window; the grounding guard is a bare `len(evidence)>=20` check (`:330-345`, the adjacent comment names the exact risk). Confabulated "supported" mints permanent source credibility (+1, no reversal) and stamps `confirmed_since_publication` into every candidate packet; a false "debunked" destroys a good source and alerts the operator to distrust it. The shelf is empty today — this is the cheapest moment to fix. **Fix:** attach observable ground truth to the review prompt (`data_engine.fetch_history` closes for named tickers, `macro_calendar.landed_since`, trusted headlines — all exist in-process, all unused); restrict supported/debunked to claims resolvable FROM the attached data (schema field `resolved_from` quoting the datum); anything else stays intact until horizon TTL.

**7. [C-MEDIUM] Prove or force conviction/weight decompression, and make regression operator-visible.**
54 non-degraded BUYs: mean conviction 62.7, stdev 3.79, zero STRONG_BUY in 693 rows (STRONG_SELL runs 82-90, so the judge CAN use the scale — just never on the buy side); the acceptance report literally cannot form a second conviction band; the single graded 70-79 trade (MU) lost $179.83 with nothing to compare against. The dynamic calibration note (`debate.py:176-229`, fires its "COMPRESSED" callout on current data) shipped tonight with zero cycles run — and the static "use the full range" guidance of 07-01 demonstrably failed for 15 days. **Fix:** grade the note's efficacy over the next ~10 cycles (does emitted p90-p10 exceed 8?); add a conviction-stdev<~5-across-30-cycles flag to `health.py`'s verdict-distribution check (currently only catches zero-BUY monoculture) so the judge-visible nudge is also operator-visible.

**8. [U-MEDIUM] Flush concurrency hardening before it bites at a bell.**
(a) The final `state.save("pending_open_entries", keep)` executes after SLEEVE_LOCK releases (end of `flush_pending_open_entries`) — last-writer-wins vs a concurrent `_queue_open_entry` from a late cycle thread can silently erase a freshly queued BUY, the exact regression class the queue exists to prevent; move it inside the lock and merge by (symbol, sleeve). (b) The gate battery eagerly evaluates `risk.check_correlation` → per-intent 6-month yfinance downloads UNDER the lock at 9:30 (`sleeves.py:1885-1893`), blocking the 1s calls-exit loop and the day's first gap-down stop sweep — the same pattern the F2 fix removed one screen above; short-circuit the tuple lazily and prefetch histories outside the lock in the same pass as the price fetch.

**9. [U-MEDIUM / V-LOW] Complete the flush gate battery.**
Missing vs the normal entry path: `_earnings_ok` (0-1 day print window, `sleeves.py:205-214`) and the ADV footprint cap — neither is possible today because queued meta carries no `next_earnings`/`avg_dollar_vol` (`broker.py:502-505`). Stamp both at `_queue_open_entry`, re-check at flush (the requeue path exists). Also verified in current code: `config.SLEEVES[sl].get("max_position_pct", 18.0)` at `sleeves.py:1884` judges core_blend intents against 18% instead of its 9% satellite mandate — use `cfg.get("max_position_pct") or cfg.get("satellite_position_pct") or 18.0`. Consider making the ADV check firm-aggregate: alpha and core hold the same small caps (PBI $814 + $2,460; ASTH/OSCR/CON likewise) with no combined footprint bound.

**10. [U-MEDIUM] Make the parked-capacity yield and sector caps drawdown-honest.**
(a) `routing.parked_capacity` (`routing.py:184-229`) checks no de-risk state: a soft_risk drawdown (engages at 70% of the kill threshold, `risk.py:74-79`) mechanically drops real deployment below the 65% trigger, and the override then admits up to 4 names BEYOND max_positions funded by selling the defensive reservoir into a falling book — directly contradicting `_equitize_idle_cash`'s own "cash IS the position" gate (`sleeves.py:127-134`); the deeper the drawdown, the more it yields. Mirror that gate. (b) Backfill the four sector-blank legacy positions (alpha CSCO/AMD/CAT + core TXN, ~$5k / ~5% of firm) in the health sweep: blank sectors are invisible to sector caps (`risk.py:457`, F5 fallback at `:479` reads the empty field) and blind `_dominant_sectors` (`riskbrain.py:57-62`), which can misclassify a sector rout as idiosyncratic and full-halt the firm on a misdiagnosis.

---

## 3. The deployment-math truth

All figures from live state tonight:

| Bucket | $ | % of firm |
|---|---|---|
| Firm equity | ~$101k | 100% |
| Active single-name risk (alpha + core satellites) | ~$18.4k | **18.2%** |
| SPY parking (alpha $16,509 + calls $24,087) | $40.6k | **40.2%** |
| Mandated index core + mechanical rotation | ~$38k | ~37.7% |
| Cash | ~$4k | 3.9% |

**The arithmetic.** Firm excess ≈ (active share) × (active-book excess); parking and the index core earn SPY minus slippage ≈ 0 excess by construction. Therefore:

- Beat SPY by **2pts/yr** → the 18.2% active book must deliver **2 / 0.182 ≈ 11.0pts** of annual excess.
- Beat by **5pts/yr** (a real institutional alpha mandate) → **27.5pts** on the active book.

**Is the measured edge big enough?** In principle, yes: the BUY cohort runs +5.0% average 21-session SPY excess (n=11, 8/11 correct) — ~12 non-overlapping windows a year means even 25% harvest efficiency clears the 11pt bar. **In practice the harvest is negative:** 10 closed trades, +$124.88 total; the two hard stops (-$846.19 — including MU, the fund's highest-conviction trade at 70, stopped 07-16 while still rated BUY) erased more than the four trailing stops earned (+$675.82); realized BUY-entered trades ran 0W/3L. Firm since 07-02: **-0.83% vs SPY +0.80%**, a -1.63pt two-week gap.

**Conclusion:** this is not an alpha-discovery problem; it is an alpha-expression problem with three named components: (1) the options desk contributes exactly 0 by construction (action 1); (2) active share is capped by conviction/weight compression and slot under-fill — the deployment scaler (floor 60%, ×1.5, `sleeves.py:679-702`) and judge weight anchors shipped tonight are the right levers with zero fires so far (action 7 grades them); (3) stop geometry is clipping the winners the edge identifies — unmeasurable until post-exit excess matures 07-29 (oldest close 06-29 + 30d), so no code change yet; interim guardrail: auto-bench high-conviction stop-outs with a recovery reopen trigger (3 of 7 already re-promoted organically).

At current deployment the fund is mathematically an expensive SPY tracker with a small, well-instrumented, edge-positive active experiment attached. Scale the experiment; do not widen the intake funnel further (693 verdicts already became only 14 names ever bought).

---

## 4. Watch-list for tomorrow (2026-07-17)

**8:30 ET held-book cycle**
- First cycles on the new explicit-suitability prompt: count `suitable_for_calls=True` among conviction≥65 BUY-family verdicts. Watchdog bar is 25 qualified verdicts — do not let the red gate-reachability health line age past it without an IG brief (action 1).
- First weekly ETF pool build (`cycle.py:917-925` → `state/etf_pool.json`): verify TQQQ/SQQQ-class instruments arrive stamped `leveraged: true` and are excluded from parking/core eligibles. The screen exists (verified) but has never run against a real pool.
- Any `entry_queued_for_open` carrying `add_on_strength` meta will die at 9:30 as `open_queue_already_held` (action 5) — count each as a voided committee intent, not idempotency working.
- The 50-min deadline cap should land the cycle before the bell; if it runs past 9:30 anyway, app monitors stand down and the flush waits for the held cycle's end sweep — a late first fill is expected, not a defect.

**9:30 ET — first production exercise of `flush_pending_open_entries` (DVA / ARMK / ALKS / FLXS)**
- Expected: 4× `open_queue_filled` ~9:30-9:31. DVA (2sh, $459) and ARMK (16sh, $882) fill from raw cash; ALKS (17sh, $884) and FLXS (6sh, $559) each pull 1 SPY parking share; end state ≈ 20 SPY + ~$660 alpha cash. All gates clear (sizes 1.8-3.5% vs 18.5% cap; Healthcare ≈18.5% vs 35% via the `pos['sector']` fallback).
- A >3% overnight gap on any name requeues it to research — by design (`open_queue_gate_blocked`/drift), not a failure. The four seeded intents lack `vol_ann` so the band is the flat 3%; at their 27-52% vols the designed band is 1.3-2.5%, so no impact tomorrow — but future seeding scripts must copy full entry meta.
- Watch first stop-sweep latency: correlation fetches run under SLEEVE_LOCK (action 8b); a Yahoo throttle stall here delays gap-down stops firm-wide.
- `routing_report.json` silent_drop=true (health red) should clear on the next routing pass; if it persists past 9:45, investigate.

**9:45 ET new-names cycle**
- TIER_SLOTS 4/4/4: watch conversion, not volume — 0-6 BUYs per ~29 candidates has been the bottleneck, not intake width.
- Calibration-note efficacy sample #1: emitted BUY-family p90-p10 vs the ≤8 compressed trigger (action 7).
- Drift sanity: any |drift| >~15% (cf. INNV -30.19% on a $1.5B small cap) is probably a stale packet reference price — triage as a data bug, not an honest skip.

**EOD**
- Journal audit: any `caps_tuned` row naming MOMENTUM_LANE_MOM_MIN is NOT evidence the lever moved until action 4 lands — the consumer still reads static config.
- First fires, if any entries execute: `deployment_scale_applied` and edge-tilt counterfactual weights journaled correctly.
- Spend-limit posture: the research brain shares the account subscription. Until the limit resets/raises, committee workups fail and are BACKLOGGED (never faked — degraded-verdict elimination guarantees no fabricated trading); mechanical layers (stops, rotation, seats, queued fills) run without Opus.

---

## 5. Appendix

### A. Refuted findings (do not re-file)

1. **"18.2% active share proves no deployment mechanism exists"** — arithmetic correct (used in §3), claim false: the bounded deployment scaler (`sleeves.py:679-702`) + judge weight anchors + calibration note shipped in round 2; survives only as a zero-fires-yet monitoring note.
2. **"BUY gate is negatively selecting and nothing acts on HOLD-parked winners"** — the scorecard shows the opposite (BUY +5.0% vs base -4.4%; 52/72 HOLDs correctly lagged); bench-with-reopen-trigger (ATEX benched now), evidence-gated HOLD-miss lever (correctly quiet at 11% vs its 25% bar), lesson injection, and `audit_bench` all exist. Survives only as a magnitude-weighting tuning note (a few very large misses never trip a rate-based lever).
3. **"Liquidity-only seat screen admits 3x/inverse ETPs"** — `_LEVERAGED_RX`/`_is_leveraged` at `allocation.py:271-284` stamp every pool entry at build time (re-verified against current code tonight); the parallel "medium: no leverage screen" filing was against a mid-edit snapshot and is superseded. Residual: watch the first real pool build.
4. **"Seat-flip buy legs deterministically die at the flush gate battery"** (carried in the risk/execution dimension summaries) — refuted against current code: mechanical parking/seat legs bypass the battery via a dedicated fill path (`sleeves.py:1820-1860`); another mid-audit fix. Residual: that path has never run in production.
5. **"Report-card retention evicts verdicts before 21-session maturity"** (learning summary) — fixed and verified: history retained at 120 rows (`cycle.py:1080, 1974`).

### B. Absences per dimension (what a top firm has that this fund does not)

**Alpha:** (1) a portfolio-construction layer converting signal into risk — risk-budgeted, conviction/edge-proportional sizing against a target active share (e.g. vol-targeted 60-80% real deployment for the alpha book), instead of the cash reservoir being the largest position for five straight weeks; (2) a calibrated, enforced conviction distribution — forced relative ranking with populated tails; zero STRONG_BUYs in 693 verdicts means every conviction-keyed mechanism runs on a constant; (3) a mechanical promote-or-explain lane charging missed winners to the process — the 8-for-8 overlooked-winner cohort (+13.7% to +82.7%) is measured beautifully and converted by nothing.

**Risk:** (1) an approved-products/instrument-class gate independent of liquidity+momentum for anything mechanically seated into stop-exempt buckets (the regex screen is necessary, not sufficient — no leverage-factor list or SPY-correlation assertion for the parking seat); (2) a firm-level LOOK-THROUGH exposure and beta budget — nothing aggregates TRUE market exposure across sleeves (parking excluded by design, options at premium not delta, no gross/net or factor limits), which would have caught actions 2 and 3 structurally; (3) ex-ante risk throttling and scenario math — every breaker reacts to realized day P&L; nothing bounds how much new risk one cycle can add (the 16+4-slot alpha book can fill in a single pass) and no gap/crash scenario testing exists for the combined book (the options desk has no aggregated greeks; the premium cap is the only aggregate).

**Execution:** (1) opening-auction awareness and execution scheduling — 100% of queued flow concentrates at the single worst-liquidity minute of the day at a flat 5bps paper haircut; (2) a real OMS lifecycle for live entries — no limit-repricing/chase policy, no intent-vs-fill reconciliation feeding the queue, no per-intent TIF/limit-band policy; (3) TCA — fills never graded vs arrival/open/NBBO, so execution quality is invisible to the learning loop and the 3% drift floor is untested against realized gap distributions.

**Learning:** (1) evidence versioning / regime segmentation — tonight the fund changed its own information diet and pipeline, yet every learning stat (edges, news neutralization n=41 from the old pipeline, lessons) pools pre- and post-change rows into one undifferentiated sample; stamp process-version on every graded row so a directive like "ignore the news tape" cannot outlive the pipeline it was measured on; (2) statistical significance and multiple-testing control on edge attribution — edges are raw mean differences with no n-adjusted confidence; (3) magnitude-weighted regret — rate-based calibration levers are blind to a small number of very large misses.

**Info diet:** (1) an evidence channel for every claim-resolution loop (street research is the live gap — action 6); (2) end-to-end verification that "managed/DOWN-WEIGHT" labels change actual packet/prompt content and that post-neutralization cohort excess improves (trusted news remains -12.3pts inverted, n=41, under a green label; if still inverted at n≥60, physically cap headlines in verdict packets and keep news for the risk/exit side only, journaling the counterfactual); (3) scheduled input-vs-mandate coverage audits — the diet widened by operator order, not by a mechanism that notices what a real PM consumes and the fund does not.

### C. Register of open unverified findings (for the next IG pass)

| # | Sev | Finding | Anchor |
|---|---|---|---|
| U1 | M | News input inverted -12/-13pts, still feeds every verdict under a "managed" label; verify neutralization end-to-end | edge_health.json; verdict_report_card |
| U2 | M | Stop geometry unmeasurable until 07-29; hard stops clipped the two best names; interim auto-bench of high-conviction stop-outs | ledger.json; learning.py:1056-1097 |
| U3 | M | Research→position conversion institutionally weak (693 verdicts → 14 names, ~$18k active; 28-111 min cycles); report cost-per-deployed-dollar in cycle_stats | research.json; cycle_stats.json |
| U4 | M | Flush final save outside SLEEVE_LOCK (last-writer race) + eager network gates under the lock | action 8 |
| U5 | M | Flush battery gaps: earnings window, ADV footprint, core max-position key (18% vs 9% mandate — verified) | action 9 |
| U6 | M | parked_capacity ignores soft-risk/de-risk states; sector-blank legacy positions blind caps + kill-switch diagnosis | action 10 |
| U7 | L | Operator-seeded intents lack vol_ann → drift band floors at 3% (no impact on tomorrow's four); seeding scripts must copy full entry meta | pending_open_entries.json |
| U8 | L | Edge-tilt dose proportionality: a 17.7pt measured edge expressed through a ±2pt tilt (~$20-50/trade) learns slower than it decays; widen via the existing bounds pattern once the counterfactual ledger is positive at n≥20 | sleeves.py:249-277 |

---
*Chief Auditor synthesis, 2026-07-16 overnight. Six findings confirmed by adversarial verification (5 high, 1 downgraded to medium), two dimension-summary claims and three prior filings refuted against current code, one unverified medium independently verified and promoted (premarket adds), three in-session fix claims independently confirmed real and one confirmed still open. All evidence cites file:line or a concrete state/journal record; line numbers reflect on-disk code at audit close and may drift with continued edits.*

---

## 6. Fix disposition — same night (2026-07-17 pre-dawn), all 10 actions shipped

Every ranked action above was implemented, regression-tested (test_firm **748/748** including a new §81 covering each fix; week_sim **51/51**), and deployed (app restarted before the 07-17 8:30 cycle). Per action:

1. **Options desk reachable** — judge flag DEMOTED to veto (`sleeves._judge_options_veto`: explicit False binds only WITH a stated reason ≥15 chars; schema now requires `suitable_for_calls_reason`/`suitable_for_puts_reason`); desk-side mechanical screen (`_mech_options_ok`: options tradable on Public + no earnings inside max_dte; chain quality still enforced by pick_call/pick_put); watchdog escalates a dead gate to a FIX BRIEF (deduped weekly) instead of a passive red. **The reachability health line went GREEN on restart** — historical conv≥bar BUYs now qualify under the demotion.
2. **Parking priced by the firm** — `check_firm_name_cap` numerator now counts parking; new `risk.firm_symbol_exposure_pct` look-through; `PARKING_SYMBOL_MAX_FIRM_PCT=50` ceiling stops the equitizer; `ETF_SEAT_CONVERGENCE_MAX_PCT=12` pre-flip check refuses to hand the reservoir to a challenger the firm already holds in size (parking AND core seats).
3. **Paper→live funding** — `_seat_trade` live buy legs now QUEUE as mechanical open intents (fill after settle) and failures journal (`etf_seat_buy_failed` — previously silent); flush keeps starved intents when a sell is settling (`open_queue_waiting_settle`) for both mechanical and conviction paths; a 5-day TTL (`open_queue_expired`) bounds retries.
4. **Momentum-lane loop CLOSED at all 4 legs** — `risk.tuned_cap` resolves MOMENTUM_LANE_BOUNDS keys; the sole consumer (`cycle._is_extended_momentum_hold`) reads the tuned value; `current_caps` and the review prompt's bounds text include the lane. `caps_tuned` journal rows are no longer false evidence.
5. **Premarket adds execute** — the flush's already-held guard now routes `add_on_strength` intents to a top-up path (add-battery gates: whole-position cap, sector/firm caps; bumps adds_count; crash-idempotent via last_add_ts; base-gone → committee re-look).
6. **Street research grounded** — the claim review now receives an OBSERVED DATA block (named-ticker price moves since ingestion via data_engine; landed macro releases via macro_calendar); `resolved_from` must quote an anchor actually present in that block or supported/debunked is refused (`street_review_ungrounded`) — confabulated credibility can no longer mint.
7. **Compression operator-visible** — new `conviction distribution` health line (red when BUY-family stdev <5 AND p90−p10 ≤8 over 30 cycles). Red on deploy, as it should be — it greens only when the calibration note actually decompresses the judge.
8. **Flush hardened** — final queue save moved INSIDE the lock and merges concurrent appends; gate battery evaluates lazily with correlation LAST; per-intent history warmed OUTSIDE the lock (exact cache-key shapes).
9. **Battery completed** — entry metas stamp `next_earnings_date` + `avg_dollar_vol`; flush re-runs the earnings-window and ADV-footprint gates; core_blend flush intents judged against the 9% satellite mandate, not 18%.
10. **Drawdown-honest** — `parked_capacity` stands down when halted or risk_scale<1 (the yield no longer widens the book INTO a drawdown); health sweep backfills blank position sectors (cap 4/sweep) so sector caps and the kill-switch diagnosis see the whole book.

**Bonus defect found by lint during the fix pass:** `cycle.py` used `broker.equity_quotes_public` without importing broker — the 8:30 premarket-quote overlay would have silently no-opped inside its try/except on its first real run. Fixed (import added).

**Grade effect (engineering-side, honest):** items 1–5 close all six confirmed HIGHs; alpha's ceiling now rests on live evidence (first options qualifier, deployment lift, conviction spread) rather than structural blockers. Engineering-verifiable state ≈ **B/B+**; the remaining distance to A- and beyond can only be minted by market days, per §3's arithmetic.
