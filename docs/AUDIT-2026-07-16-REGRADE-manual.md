# Loch Raven Capital — Re-Grade Audit (2026-07-16, late night)

**Method:** 8-dimension multi-agent audit (same harness as the C- morning audit).
7 of 8 finders completed (59 findings); the state-coherence finder, the
adversarial verification fleet, and the synthesis agent were cut short by the
account's monthly spend limit. The 17 critical/high findings were therefore
**self-verified by the operator session against the code** the same night, and
every confirmed defect was fixed and regression-tested before this report
(test_firm 725/725, week_sim 51/51). Findings the fleet raised that were
already fixed in-session are marked RESOLVED.

## Grades (prior → now)

| Dimension | Was | Now | Finder's own assessment + post-fix adjustment |
|---|---|---|---|
| Strategy / alpha | D+ | **C-** | Round-2 machinery "real and mostly correct"; the drag is structural: ~18.2% firm-level active share, options desk unproven until the new gate produces its first trade, conviction/weight compression (62±4 conviction, 2.5–3.5% weights) starves the learning channels. |
| Risk | C | **B-** | "B- standing machinery" gated on seat/flush hardening — the gating items (leveraged ETPs in seat eligibility [CRITICAL], flush gate battery killing mechanical legs, mode-blind intents) were all fixed in-session. Residual: parking sits outside firm-wide caps by design; shared momentum engine convergence noted for the next review. |
| Execution | D | **B** | Finder graded B- with one HIGH (seat/parking buys dying at the flush battery) — fixed: mechanical allocation legs now bypass the conviction battery with a 10% sanity band and never requeue to research. |
| Learning loop | B- | **B+** | "Demonstrably closes end-to-end; the strongest it has been." Fixed in-session: momentum-lane lever now proposed in the review prompt (was clamp-only), report-card retention doubled (50→120 rows) so 21-session forward windows survive the 2-cycle day. |
| Info diet | B- | **B** | Forward consensus + positioning landed; street-research reviews evidence-gated. Fixed in-session: 8:30 packets now carry premarket quotes + gap % (were judging yesterday's close blind). |
| Ops / reliability | D | **B-** | "Core discipline genuinely institutional for a one-box shop." All three HIGHs fixed: A13 lock on the in-app held cycle, locks on headless monitor/postmarket sweeps, 50-min deadline cap so the 8:30 cycle lands before the bell. |
| Live parity (new) | — | **B+** | Public's own instrument list drives the universe: 11,980 buyable, 7,371 options-enabled, gates at every entry path, full ETF pool (5,177 screened → 150 by liquidity) with leveraged/inverse products excluded from mechanical seats. |
| State coherence | — | *ungraded* | Finder lost to the spend limit — run with the next audit. |
| **Overall** | **C-** | **B-** | The machine is now sound; the remaining gap to "beats SPY" is strategy-level, not engineering-level. |

## What stands between B- and beating SPY (operator decisions, not bugs)

1. **Active share ~18%**: with 54% benchmark beta and 2.5–3.5% positions, the
   fund mathematically converges to SPY-minus-noise. Levers: committee weight
   guidance upward (the ±2pt edge tilt is live but bounded), a target
   deployment floor for alpha, or accepting the beta core as the mandate.
2. **Options desk** (25% of capital): re-keyed gate + explicit suitability
   prompt shipped tonight; the desk finally trades only if the committee
   starts marking suitability true. Watchdog is armed and red until then.
3. **Conviction compression**: verdicts cluster at 60-66 — the judge needs a
   reason to spread the distribution before conviction-scaled anything can
   learn. Candidate: calibration feedback in the digest (measured, staged).

## Confirmed-and-fixed this session (from the 59 findings)

- [CRITICAL] Leveraged/inverse ETPs (SOXL/TQQQ/SQQQ +31) admitted to seat
  eligibility → name-based classifier, seats are 1x-only, §79 tests.
- [HIGH ×3] Premarket seat/parking buy legs deterministically killed at the
  9:30 flush battery → mechanical-leg bypass.
- [HIGH] In-app 8:30 cycle held no cross-process lock (A13 regression) → locked.
- [HIGH] Headless monitor/postmarket sweeps held no lock → locked.
- [HIGH] 8:30 deadline could run past the bell → capped at 50 min, backlog sections.
- [HIGH] 8:30 packets blind to overnight gaps → premarket quote + gap overlay.
- [HIGH] Momentum-lane lever unproposable → review prompt now offers it, bounded.
- [HIGH] Report-card retention halved by the 2-cycle day → history 50→120 rows.
- [HIGH] Street-research credibility mintable without evidence → ≥20-char
  concrete-evidence guard (fetched-observable grounding remains filed).
- Breaker-trip advisory tagging, verdict-review watermark, partial-fill
  journaling, options picks gated on Public options permissions.

## Deferred (filed in the fund's fix-brief loop)

Paper→live flip nuances for the new machinery (live sells settle async — the
flush and seat trades must respect pending settlement before the flip);
parking inclusion in firm-wide exposure caps; shared-momentum-engine
concentration; estimate-revision edge grading; fetched-observable grounding
for street-research reviews.

## Note on the spend limit

The verification fleet and synthesis died on the account's monthly spend
limit. The fund's research brain uses the same subscription: until the limit
is raised, committee workups will fail and be BACKLOGGED (never faked — the
degraded-verdict elimination shipped today guarantees no fabricated trading).
Mechanical layers (stops, rotation, seats, queued fills) run without Opus.
