# AUDIT 2026-07-16 — FULL-FUND SYNTHESIS (Chief Auditor)

Read-only audit of the live system as of 2026-07-16 ~21:30 UTC, covering the state of the fund
AND the major changes shipped today (8:30 held-book cycle, 9:45 new-names split, queued-for-the-open
entries, slot cap 8→16 + parked-capacity, degraded-verdict ban, street research desk).
Every finding below survived adversarial verification against code and live state; two candidate
findings were refuted and are listed only in the appendix. Evidence discipline: file:line or a
concrete state/journal record on every claim.

---

## 1. VERDICT

**Is the fund positioned to beat SPY? No — not as deployed today.** Since inception (06-09) the
fund is +0.93% vs SPY +1.82% (-0.89pts) while holding ~54% of capital in benchmark(-like) beta.
The options desk (25% of capital) has never opened a real option in 5+ weeks. The measurement
stack is genuinely institutional; almost none of it moves capital yet.

**Is it positioned to self-improve? Yes in architecture, but today's shipments quietly poisoned
three of the measurement channels the improvement loop feeds on** (held-book context rows,
acceptance grading of queued entries, ungrounded street-research claim resolution) — and one
shipped bug (`_persist` NameError) means **the next 9:45 cycle will abort before any sleeve
trades at all** until fixed.

### Scorecard

| Dimension | Grade | One-line justification |
|---|---|---|
| **Alpha / strategy** | **D+** | 53.7% benchmark beta, 18.2% singles at 2.5-3.5% weights, options desk inert since inception, -0.89pts vs SPY; the fund converges to SPY-minus-costs by construction. |
| **Risk architecture** | **C** | The classic battery (breakers, kill switch, caps, drawdown diagnosis) is coherent and verified — but today's queued-entry path bypasses most of it at fill time, the queue is mode-blind, and the 9:45 split blinded the sleeve sector cap to the held book. |
| **Execution mechanics** | **D** | Tomorrow's 4 seeded intents will half-fail (parking pull silently no-ops), the flush is not idempotent on a box that keepalive-restarted 3x today, the premarket swap recreates the banned half-swap — and the `_persist` NameError aborts the next wide cycle entirely. |
| **Learning loop** | **B-** | Genuinely closed loop (lessons + digest verifiably reach prompts, degraded quarantine works, IG briefs filed AND closed 10/10) — but today's changes fabricate 'blind' evidence for the held book and grade the fund's own queued buys as passes. |
| **Info diet** | **B-** | Cross-vendor sanitization, trust-tagged news, live macro calendar, earnings actuals — institutional hygiene; missing forward-looking (estimate revisions) and positioning (short interest/insider) inputs a real desk consumes daily. |

**Overall: C-.** The bones are institutional. The capital is indexed. The newest code is the
riskiest code in the building.

---

## 2. TOP 10 RANKED ACTIONS (expected beat-SPY impact × confidence)

**#1 — CRITICAL: Fix the `_persist` NameError before the next cycle runs.**
`firm/cycle.py:1824, 1867, 1875` reference `_persist` in the research-history append; it is never
assigned anywhere in the file (only other occurrence is the comment at 1822). File mtime 20:10 UTC
is 9 minutes AFTER the last `cycle_done` (20:01 UTC) — this code has never executed. The next
`run_cycle` raises NameError at 1824, is caught by the outer handler (~1967), and aborts BEFORE
sleeves trade and before `state.save("research", ...)` at 1879: **zero trades and zero persisted
verdicts until fixed.** Fix: define `_persist` as this cycle's verdict set excluding reused held
names (the documented intent), i.e. `_persist = {k: v for k, v in verdicts.items()}` filtered per
the 1804-1809 comment. Then note that once it runs, finding #5 (sector-cap blindness) goes live
silently. *File: firm/cycle.py.*

**#2 — Fix the parking pull in `flush_pending_open_entries` (fires tomorrow 9:30).**
`firm/sleeves.py:1605` prices only the queued symbols; `sleeves.py:187-188` then gets
`prices.get("SPY")=0` and returns silently — no fallback, no journal — while both sibling paths
have the fallback (`_equitize_idle_cash` ~161-165, calls desk ~1013-1015). Walked against live
state (alpha cash $1,946.63, $16,508.69 SPY parking, MIN_CASH_BUFFER $100): DVA and ARMK fill,
**ALKS fills ~9.72 of 17sh (57%), FLXS bounces `open_queue_insufficient_cash`** — while $16.5k of
designated parking sits unsold, violating the flush's own docstring ("the cash clamp with the
parking pulled first"). Fix: include each sleeve's `CASH_EQUITIZATION` symbol in the price fetch,
or give `_free_cash_from_equitization` the sibling fallback + a `parking_pull_skipped_no_price`
journal event. *File: firm/sleeves.py.*

**#3 — Re-key the options-desk gate to the EMITTED distribution and make its watchdog fireable.**
Gate (`sleeves.py:879-904`, `config.py:705`) requires `suitable_for_calls` AND (STRONG_BUY≥70 or
BUY≥72). Measured against state/research.json: 0 STRONG_BUY in 575 non-degraded verdicts across
39 cycles; BUY mean 62.7, max 72 (n=4); `suitable_for_calls` emitted 0 times since the gate
shipped 07-02; `suitable_for_puts` 0 ever. The desk is a $24,087 SPY tracker (94.8% parked). The
07-16 health check (`health.py:655-680`) REDs only at ≥40 real BUY-family verdicts with 0
qualified in a 30-cycle window; observed emission ~32/30 cycles → **the watchdog cannot fire in
practice.** Fix: gate on rolling p90 of trailing-30-day BUY conviction (floor 65) + explicit
prompted suitability decision; lower the watchdog threshold to the observed emission rate; or cut
the sleeve to $5k until the gate fires once. *Files: firm/sleeves.py, config.py, health.py.*

**#4 — Re-run the full entry gate battery at flush time.**
`sleeves.py:1609-1653` re-checks only drift band + breaker/halt + cash. Skipped at fill:
max_positions/parked-capacity ceiling, `sector_allowed`, position/sector/correlation/firm caps,
ADV liquidity, risk_scale sizing. Compounding: market-closed `broker.execute` queues WITHOUT
booking (`broker.py:548-560`), so every cap sum stays flat while a loop queues successive BUYs —
live proof: 2 of the 4 queued intents are Healthcare into a book already 4/8 Healthcare, each
passed sector-cap excluding the other. Fix: re-run the battery per intent against the live book
(counting intents filled this flush), skip-if-already-held, route failures to `_requeue_research`.
Applies identically in live mode. *File: firm/sleeves.py.*

**#5 — Restore the sleeve sector cap's view of the held book.**
`risk.py:459` counts a held position only if the per-cycle verdict-built sectors map covers it
(`cycle.py:1911-1912`); the 9:45 split deliberately excludes held names with a fresh 8:30 verdict
(`cycle.py:1804-1809`, reuse gate at 1276-1280), so **every held position contributes zero to the
sleeve sector-cap sum, and the gap ratchets** (each 9:45 buy becomes tomorrow's invisible held
name). Live: 4-name Healthcare cluster CNC/OSCR/ASTH/CON ($3,361) invisible. One-line fix: fall
back to `pos.get("sector")` in check_sector_cap — plus backfill the sector=None positions
(CSCO/AMD/CAT, TXN) via a postmarket healer, which also repairs the firm cap and riskbrain
drawdown diagnosis (`riskbrain.py:57-62`). *Files: firm/risk.py, firm/health.py.*

**#6 — Stamp mode on queued intents; refuse the paper→live flip while the queue is non-empty.**
`broker.py:499-502` stores no mode; `app.py:272-282` flip guard checks positions + pending_orders
only; `sleeves.py:1600` + `monitor.py:475-481` flush at CURRENT mode. Four paper intents are on
disk right now — a flat-books flip tonight would route them as REAL orders at 9:30, violating
app.py:282's "paper and live never mix." Fix: stamp mode at queue time, drop+requeue on mismatch
at flush, and count the queue in the flip guard. *Files: firm/broker.py, app.py, firm/sleeves.py.*

**#7 — Make the flush idempotent.**
Queue loaded before `SLEEVE_LOCK` (`sleeves.py:1597` vs 1603), rewritten once after the lock
(1661), fills persisted per-entry (1653), no already-held guard, a network price fetch INSIDE the
lock (1605-1608). `state.py:206-207` raises on the recurring WinError 5; journal shows 199
keepalive_restart events (3 today); the equity monitor (60s, `exclude_own` at app.py:611-617) and
end-of-cycle sweeps (cycle.py:1029, 1941) can race. Either path re-executes already-filled intents
and `_book_buy` (broker.py:388-391) silently averages the double. Fix: load AND rewrite inside the
lock, pop-then-save per intent, skip+journal already-held symbols. *File: firm/sleeves.py.*

**#8 — Give `underwrite_held` the main cycle's full context row (protects the learning loop).**
`cycle.py:976-985` persists only px/earnings/owned; the main cycle row (1828-1866) carries
had_news/news_tier/factors/macro_news. `learning.py:417-421`: missing fields grade as
'blind'/'quiet_period'/factorless. From the first 8:30 run, **the entire owned book grades as
news-blind — fabricated reinforcement for the already-live "IGNORE the news tape" directive**
(trusted-vs-blind edge already -12.3pts n=41) and all held-name factor observations vanish. Fix:
extract the 1828-1866 context builder into one shared helper both writers call. *Files:
firm/cycle.py, firm/learning.py.*

**#9 — Close the measured-edge → sizing loop (the actual beat-SPY lever).**
Momentum edge +18.2pts (edge_health.json n=61), BUYs grade +5.0% forward (n=11), yet every BUY at
conviction 60-66 gets the same 2.5-3.5% (committee following debate.py:145-152 calibration; the
code cap is 18% at sleeves.py:616-617). And MOMENTUM_LANE_BOUNDS (`config.py:526-530`) has ZERO
consumers — `_apply_cap_tuning` (learning.py:937-945) clamps only CAP_BOUNDS keys, so the largest
blocked cohort (228 momentum_lane_skip samples) feeds a lever that cannot move. Fix: one bounded
lever per month with a counterfactual ledger — start with momentum-scaled target weights (±2pts)
and wire MOMENTUM_LANE_BOUNDS into the review's allowed keys. *Files: firm/sleeves.py,
firm/learning.py, config.py.*

**#10 — Make the premarket swap atomic (sell-then-buy at the open, or defer the sell).**
`sleeves.py:675-677` (alpha) and 829-833 (core) commit the eviction SELL immediately before
`broker.execute(buy)` — at 8:30 the buy queues (market closed) AFTER the eviction booked, and the
queued buy can still die on the 3.0% drift band at 9:30 (requeued to research, not to the book).
Core satellites are 5/5 full today, so `_plan_swap` is armed every 8:30 cycle. This recreates the
half-swap the 2026-07-01 fix (documented at sleeves.py:512-517) was written to prevent. Fix: when
the market is closed, queue the buy tagged for atomic sell-then-buy at flush, or have
`_commit_swap_sell` treat 'queued' like 'pending' (slot not freed, swap deferred). *File:
firm/sleeves.py.*

**Honorable mentions (do with the above):** ground street-research claim reviews with fetched
observables or restrict auto-transitions to intact/expired (`street_research.py:69-90, 279-356` —
an evidence-free Opus call currently mints "confirmed since publication" credibility); fix
`broker.execute` returning `queued:True` for option intents `_queue_open_entry` silently drops
(broker.py:490-491 vs 548-560); journal `open_queue_partial_fill` when filled < intended by >30%
(sleeves.py:1640-1646); implement the promised merge of `_reused_held` into the 9:45 pool
(cycle.py:1249-1250 comment is stale — without it, held-name option entries are structurally
unreachable and adds-on-strength accounting is bypassed); tag etf_rotation's advisory breaker
trips (5 of 7 rows in breaker_trips.json gate nothing, `config.py:899`); stop the money review
wiping `verdict_reviewed_asof` (learning.py:1753-1761 vs 1470-1477).

---

## 3. THE DEPLOYMENT-MATH TRUTH

Live book, 2026-07-16 (firm equity $100,931):

| Bucket | $ | % | Behaves like |
|---|---|---|---|
| SPY parking (alpha $16,509 + calls $24,087) | $40,596 | 40.2% | SPY |
| Core mechanical VOO/QQQ/GLD | $13,592 | 13.5% | SPY-adjacent |
| **Benchmark(-like) beta total** | **$54,188** | **53.7%** | **the index** |
| Single names (alpha 8 names + core satellites) | $18,396 | 18.2% | the only stock-picking |
| etf_rotation (SMH/XLK/IWM) | $24,397 | 24.2% | mechanical momentum (-2.5pts vs SPY to date) |
| Cash | ~$3,950 | 3.9% | zero |

The arithmetic:

```
fund excess ≈ 0.182 × excess(singles) + 0.242 × excess(rotation) + 0 × (parks/core)

To beat SPY by 3pts/yr with rotation at zero excess:
    excess(singles) ≥ 3 / 0.182 ≈ +16.5pts/yr   — top-decile institutional stock-picking
Measured excess(singles), placed names: +0.0%  (n=6, 21 sessions — noise, but not evidence FOR)
Measured excess(rotation) to date:      -2.5pts

Even a FULL 16-name alpha book at 3% average weight deploys ~48% of the sleeve;
today it holds 8 names = 27.6% real / 64.7% parked (config.py:726-728 concedes this).
At the shipped 65% real-deployment design floor (routing.parked_capacity, config.py:418),
required singles excess falls to ≈ +11pts/yr — better, still heroic at 3% position weights.
```

The forward-graded signal exists (BUY verdicts +5.0% fwd excess n=11; momentum factor +18.2pts
n=61). The fund's problem is not signal, it is **expression**: weights pinned at 2.5-3.5%, the
convexity desk unreachable, and half of every dollar indexed. Today's slot-cap/parked-capacity
changes attack the right seam; the sizing loop (#9) and the options gate (#3) are the two levers
that change the arithmetic itself.

---

## 4. WATCH-LIST FOR TOMORROW (2026-07-17)

**~8:30 ET — held-book cycle (`underwrite_held`):**
- Should RUN (it writes its own history row at cycle.py:971-975 and does not touch `_persist`).
- Watch: its history rows will carry the impoverished context (#8) — first day of 'blind' grading
  of the owned book. Every held-name verdict enters the report card news-blind/factorless.
- Watch: if it ranks a swap (core satellites 5/5 full), the eviction SELL books premarket while
  the buy only queues (#10). A >3% overnight gap on the queued buy = half-swap: sold a holding,
  bought nothing. Look for `_commit_swap_sell` followed by `entry_queued_for_open`.
- Any 8:30 options pick dies silently: execute returns `queued:True` but `_queue_open_entry`
  drops non-equity (`open_queue_skipped_option` in the journal is the tell).

**~9:30-9:31 ET — first in-hours monitor sweep flushes state/pending_open_entries.json
(DVA 2sh @229.45, ARMK 16sh @55.11, ALKS 17sh @51.975, FLXS 6sh @93.085, FIFO):**
- Expected with the CURRENT code (#2): DVA fills, ARMK fills, **ALKS partial ~9.7/17sh, FLXS
  bounces `open_queue_insufficient_cash`** — and NO SPY parking sale despite $16.5k available.
  Journal will read "insufficient cash" with no hint the parking pull was skipped.
- Drift band: any of the four gapping >3.0% from thesis price gets requeued to research
  (correct behavior — verify the `buy_skipped_price_drift` reason is journaled).
- Idempotency (#7): if the app keepalive-restarts or a cycle sweep overlaps the flush window,
  check for DOUBLE fills of the same symbol (position qty 2x intent, averaged entry price).
- Sector (#4): the two Healthcare intents fill into a 4/8 Healthcare alpha book with no sector
  check at fill.
- Mode: confirm settings.mode is still paper before 9:30 — the queue is mode-blind (#6).

**~9:45 ET — new-names wide cycle (`run_cycle`):**
- **Will ABORT at cycle.py:1824 with NameError (`_persist` undefined) — caught ~1967, no sleeve
  trades, research history row NOT persisted (#1).** The journal will show research completing
  and then a cycle error; `cycle_done` will not stamp clean. This is the first thing to fix
  tonight — until then the 9:45 cycle produces research cost and zero trades.
- Once #1 is fixed: the sector-cap blindness (#5) and the placed=[] acceptance-grading of queued
  entries go live silently — the fund's own buys graded as counterfactual passes.

**End of day:** verify `open_queue_filled` events reconcile 1:1 against positions created;
verify no orphaned intents remain in pending_open_entries.json; check whether the options-gate
health check evaluated (it cannot RED at current emission rates — #3).

---

## 5. FINDINGS REGISTER (post-verification severities)

| # | Severity | Finding | Dim |
|---|---|---|---|
| F0 | **CRITICAL** (new, found in verification) | `_persist` NameError aborts the next run_cycle before sleeves (cycle.py:1824/1867/1875, never assigned) | execution |
| F1 | HIGH (downgraded from critical) | Options desk gate empirically unreachable vs emitted distribution; watchdog cannot fire (sleeves.py:879-904, health.py:655-680) | alpha |
| F2 | MEDIUM (downgraded from high) | Deployment math: 53.7% benchmark beta, 2.5-3.5% weights, no edge→sizing loop; partially remediated same-day by slot cap + parked_capacity | alpha |
| F3 | HIGH (downgraded from critical) | pending_open_entries mode-blind; paper intents survive a paper→live flip (broker.py:499, app.py:272-282) | risk |
| F4 | HIGH | Flush skips the entry gate battery; multi-intent queueing validated against a book that never grows (sleeves.py:1609-1653, broker.py:548-560) | risk |
| F5 | HIGH | 9:45 split blinds the sleeve sector cap to the held book; gap ratchets daily (risk.py:459, cycle.py:1276-1280, 1911-1912) | risk |
| F6 | HIGH | Parking pull silently no-ops in the flush; tomorrow ALKS ~57% fill, FLXS dropped, $16.5k idle (sleeves.py:1605, 187-188) | execution |
| F7 | HIGH | Flush not idempotent: queue load/rewrite outside lock, per-entry saves, no already-held guard; WinError 5 + 199 keepalive restarts + concurrent sweeps (sleeves.py:1597-1661, state.py:206-207) | execution |
| F8 | HIGH | 8:30 held cycle writes impoverished history context — owned book grades 'blind/factorless', corrupting news-edge and factor channels (cycle.py:976-985 vs 1828-1866, learning.py:417-421) | learning |

Medium/low items (unverified at the adversarial tier, evidence cited in §2 honorable mentions and
the dimension audits): research latency pushing entries past the close (today's 20:01 order storm,
recovered only by operator hand-seeding); thin/lumpy breadth (5 of last 12 cycles zero BUYs, 17%
degraded verdicts historically); etf_rotation drag and its advisory breaker-trip pollution;
sector=None positions; correlation-gate dilution at 16(+4) names; half-swap (#10); reused-held
merge never implemented; queued:True lie for options; silent partial fills; ungrounded
street-research claim grading; MOMENTUM_LANE_BOUNDS dead lever; verdict-review watermark wipe;
lessons hard-clipped at 400 chars.

---

## 6. APPENDIX

### A. Refuted findings (do not re-file)

1. **"Conviction is inverted (BUY closures -23.2% vs HOLD +7.2%)"** — REFUTED: the HOLD cohort is
   a verdict-attribution artifact on pre-06-10 legacy positions (newest-verdict fallback +
   survivorship); the larger forward sample shows correct ordering (BUY +5.0% n=11 vs HOLD -6.7%
   n=72, 52 correctly-avoided losers), and the bench/promotion machinery demonstrably re-judged
   the named tickers (ATEX 5x) — the committee chose HOLD, the mechanism did not fail.
2. **"Exit machinery destroys the entry edge (all exits stop-driven, 5.6-13.4pts gap-through)"** —
   REFUTED: 3 of 7 closures were de-dup satellite unwinds not stops, 4 of 6 stop exits were
   profit-harvesting trailers, and the anchor slippage case used the config-base stop instead of
   the actual vol-widened 16.1% stop stamped on the position (sleeves.py:40-52).

### B. Absences per dimension (what an institutional desk has that this fund lacks)

**Alpha:** distribution-calibrated gates and conviction/edge-scaled sizing (percentile bars,
fractional-Kelly); persisted daily fund-and-sleeve vs-SPY TWR series with tracking error / IR /
contribution decomposition (vs_spy exists only as a live API computation); transaction-cost and
gap-risk measurement (verdict-price vs fill, stop gap-through model, open-queue slippage) — the
seam where the +5.0% graded signal becomes +0.0% placed return is unowned.

**Risk:** execution-time pre-trade compliance re-check for deferred orders (an OMS re-validates at
ROUTE time); firm-level gross/net or beta-adjusted exposure limit (sleeves can stack ~100%
correlated long-equity beta with options delta un-aggregated); scenario/stress engine with
pre-committed de-risk levers (all risk response is reactive to realized drawdown).

**Execution:** opening-auction execution (MOO/LOC or participation algos with limit protection,
slippage vs the official open print — not a 60s-tick mid +5bps); a transactional order-management
layer (per-intent state machine decided→queued→routed→filled with IDs and fill-vs-intent
reconciliation); execution-time gate re-validation and auto-cancel of stale intents on regime or
mode changes.

**Learning:** lesson-efficacy attribution (no before/after or shadow-book test of whether a
directive like "IGNORE the news tape" actually helps); regime/config versioning in learning stats
(today's structural break straddles every evidence series with no segmentation flag; the money
review will tune on mixed-regime evidence); a grounded independent verification layer (verdict
review, IG audit, and claim reviews all rely on the same ungrounded Opus judgment with no fetched
data channel and no deterministic fallback).

**Info diet:** forward-looking inputs (estimate revisions / analyst consensus — several fields
already downloaded and discarded); positioning data (short interest, insider, institutional flows
— named in the sentiment desk's own mandate and absent).

### C. Same-day remediations verified live (credit where due)

Slot cap 8→16 + `routing.parked_capacity` (routing.py:183-228) with the 65% real-deployment floor;
queued-for-the-open mechanism exists and survived restart (queue is durable state, seeding
idempotent per sleeve+symbol); degraded fallback verdicts never recorded (the 07-14 "21 qualifying
conv 90" storm was fabricated `suitable_for_calls` correctly rejected by the guard);
degraded_health snapshot persists after 9 failures; toxic legacy cohort tombstoned with armed
share/flood legs; IG audit ran clean and its 10 fix briefs are closed in FIX_BRIEFS.md;
book-full BUYs benched (watchlist.add_book_full); route-coverage telemetry measuring the options
gate (even though its RED threshold is unreachable — #3).

---

*Chief Auditor synthesis, 2026-07-16. Read-only audit; no state, code, or app process was
modified. Sources: firm/*.py at mtimes noted, state/*.json live, journal.jsonl, /api/overview
(GET), per-dimension audits with adversarial verification passes.*
