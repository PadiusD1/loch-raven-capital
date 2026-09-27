# Audit 2026-07-04 — "How the fund thinks" (operator-requested full audit, Phase 1)

Scope: the decision/research layer — doctrine, prompts, learning loop, and what the live
evidence says about verdict quality. Auditor: Claude (session 1b0c9c74), fresh eyes over
code + live state + the fund's own IG findings. Phase 2 (operator's skills/knowledge/
processes stack) follows separately.

## Verdict up front

The thinking architecture is genuinely institutional-grade and better-instrumented than
most human shops: isolated desk views force real disagreement, the debate is adversarial
by construction, the judge has an explicit risk-adjusted mandate, lessons/tuning are
evidence-gated, the bench grades its own counterfactuals, and an IG subsystem audits the
auditor. Books confirm it: all four sleeves positive, alpha +3.14% vs SPY +1.01%
(+2.12pt edge), no active halts. The problems found are in the FEEDBACK PLUMBING —
findings and evidence that nothing acts on — not in the reasoning itself.

## Strengths verified (leave alone)

- **Desk isolation** (`analysts._DESK_VIEWS`) — each desk sees only its lens's slice;
  disagreement is real, not stylistic. Rare even in real firms.
- **Doctrine injection** — `BRAIN_FINANCE_DOCTRINE` (upgraded 2026-07-04 with the completed
  WSP ERC certification: variant-view thesis, catalyst/kill-criteria, DuPont/driver
  decomposition, re-rating discipline) rides EVERY Opus call, prompt-cached.
- **Cold-start humility** — first lessons review gated on 20 trades AND 21 days
  (6 trades/4 days today → correctly silent). Matches "small samples lie" doctrine.
  NOT a bug; do not "fix".
- **Counterfactual grading** — acceptance_report grades names the book PASSED on, not just
  what it bought; bench audit measures forward return of dropped names.

## Findings (ranked)

### F1 — HIGH: IG findings have no path to action
`state/ig_findings_latest.json` (2026-07-03) holds 11 findings incl. 2 CRITICAL, but no
fix-brief, alert, or task is generated from them — the fund files complaints about itself
that nobody reads. The two criticals are cheap, additive health checks:
  a) **No RAM health check** while RAM starvation is the box's chronic, documented failure
     mode (workers scale down, calls time out into degraded mode).
  b) **CLI health green while zero real verdicts** — "research brain reachable" passes on a
     ping while a full cycle produced no real committee verdicts (exactly the 07-03 outage
     signature). Health must key on real_verdicts count, not reachability.
ACTION: implement (a) and (b) as health checks; wire IG criticals into the existing
fix-briefs channel so they surface on the dashboard.

### F2 — HIGH (evidence): news inputs are an INVERTED edge
`state/edge_health.json`: verdicts informed by trusted news underperform blind verdicts by
−7.9 (n=13); aggregator news −8.9 (n=8). Factor edges are healthy (momentum +19.8 n=21,
value +12.3 n=15, quality +3.2 n=15). Interpretation: by the time news is ingestible it is
priced; leaning on it chases stale information — the machine is empirically rediscovering
the doctrine's "visible consensus has no edge." `compensated: true` suggests some
down-weighting exists; VERIFY what it does and whether the sentiment desk's weight in the
judge's synthesis should fall further until n grows. Small-n caveat applies — do not
amputate the desk on 13 trades; DO make the judge see this inversion explicitly.

### F3 — MEDIUM: degraded-fallback verdicts may be value-destroying (IG claim)
When Opus times out, a quant-composite fallback verdict still trades. IG calls it
"systematically value-destroying, share rising with RAM starvation" but no alarm tracks
its share or its accuracy vs real verdicts. ACTION: quantify from ledger (by-mode style
split); if confirmed, degraded verdicts should manage EXITS only, never open new
positions. (Consistent with existing philosophy: "a real standard verdict beats a fake
full-depth degraded one.")

### F4 — OPERATIONAL: research outage since 2026-07-03 15:09
Direct CLI test 2026-07-04 succeeds — the outage cause (quota window or transient) has
passed. Today is a market holiday (July 4), so no cycle was missed. The catch-up logic
(research >26h stale on a weekday) should fire Monday; VERIFY it does. F1(b) is the
structural fix that stops this class of outage from lingering invisibly.

### F5 — LOW: inert code paths on the bench
IG: `below_price` reopen-trigger kind is code-present but has never armed; 18/59 bench
entries were outside the reviewability guarantee (bench currently empty, so latent).
Fold into F1's check-wiring work.

## What was NOT found (searched for and absent — absence audit)
- No evidence of thesis-free buying: verdict schema enforces thesis + conviction; buys
  carry watch_triggers ("what would change my mind").
- No committee meddling with the mechanical rotation sleeve (correctly exempt).
- No unhedged single-name concentration breach; correlation guard present and tested.
- No stale-lesson risk: lessons empty by design (cold-start), not by failure.

## Phase-1 fix queue (next loop iterations)
1. ✅ DONE — RAM health check + real-verdict-keyed CLI health (F1a/F1b) + IG→fix-brief
   wiring. Both suites green.
2. ✅ DONE (2026-07-04 IG-triage session) — degraded P&L split was already quantified by
   the report card (n=34, 32.4% correct, −12.2% avg excess) and buys were already gated
   (sleeves._ranked_buys blocks degraded buys; evidence gates quarantine the cohort).
   What was missing was the ALARM: learning now persists a `degraded_health` snapshot
   beside edge_health, and health.py has a 'degraded verdicts' check (share of last
   cycle ≥ HEALTH_MAX_DEGRADED_SHARE_PCT, or cohort accuracy ≥
   HEALTH_DEGRADED_ACC_GAP_PTS below real verdicts, both n≥15 → RED).
3. ✅ DONE — news-edge inversion surfaced in judge guidance (test_firm §54).
4. Monday: verify catch-up cycle fires and produces real verdicts (F4). NOTE: restart
   app.py first — the long-lived process still runs pre-fix code until relaunched.

## IG 2026-07-03 triage (2026-07-04, ig_reviewed_asof set)
- CRIT #1 RAM check → fixed (F1a). CRIT #2 cli-green-on-zero-verdicts → fixed (F1b).
- HIGH degraded-verdict alarm → fixed (item 2 above, test_firm §55).
- HIGH persistent display-recon drift → fixed: `health._escalate_recon_drift()` raises
  ONE latched critical alert after HEALTH_RECON_ESCALATE_H (4h) of continuous failure,
  clears on recovery (state/recon_drift_incident). Deliberately no auto-halt: display-
  layer defect must not fire trades (validate-before-harden). The 07-03 calls_only
  drift itself resolved at 20:32 (reconcile_display ok:true).
- HIGH bench reviewability gap (18/59 non-hold entries) → fixed: guarantee extended to
  EVERY entry via `watchlist.unreviewable_entries()` (universal backstop = the 30-day
  age ceiling, whose clock is `added`); `_backfill_added()` runs in every update pass;
  live WDC entry (missing `added` — un-evictable) repaired to 2026-06-24.
- MEDIUMs/LOWs (below_price never armed, inert-sleeve naming, news-layer net value,
  macro-release siblings, heartbeat/backup dismissals, HOLD under-rating): triaged, not
  implemented — news inversion already down-weighted + surfaced to judge; the rest are
  candidates for the next IG cycle. Suites after this session: test_firm 571,
  test_week_sim 51 — all green.
