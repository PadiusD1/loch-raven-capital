# Free-data automation: executed validation

## Tested artifact

GitHub run 38022884163 successfully executed compilation, research, memory, full-fund, simulated-week, live-collection, cache-reuse and populated-retrieval steps. Its publication step failed with a protected-branch error. The tested tree was recovered from its artifact for submission through a normal pull request, without disabling branch protection. Tested tree: 7820ead3e6f83495295849ba7d866177ed197156. Nested tested-source.zip SHA-256: 1599ade86d73923cffe6d66e53a0ea73e43f9da2771a74ea6d7aeb63b07e1186. Temporary installers are excluded from the final tree. The final cleaned PR is tested by normal CI before merge.

## Passing checks

| Suite | Checks |
| --- | ---: |
| Research, transport, provenance, automation and API | 64 |
| Directional memory | 5 |
| Separate desk integration | 1 |
| Institutional memory | 64 |
| Full fund regression | 1,104 |
| Simulated trading week | 53 |
| Total unique checks | 1,291 |

The 64 research checks also passed after ingestion and are not counted twice.

## Actual persisted dataset

The artifact's report starts at 2026-10-10T04:08:32.260526+00:00. It collected 620 eligible records, adding 611 new versions to 82 existing records: **693 persisted records**. These include historical versions and methodology, not 693 independent investment signals. There were 137 successful source groups, zero source errors and zero skipped requests. The collector used 138 HTTP requests and one cache hit.

| Desk | Persisted records, including history |
| --- | ---: |
| Fundamental | 464 |
| Technical | 71 |
| Sentiment | 46 |
| Macro | 43 |
| Quant | 69 |

Each cloud run selects up to 24 SEC issuers and 64 price symbols, with 20 FRED series, three BLS series and two Federal Reserve feeds. Actual symbols are in data/research_refresh_report.json. The public equity tail rotates daily; this is not complete whole-market coverage.

An immediate repeat in the same runner reused **139 cached responses, made zero new HTTP requests and added zero records**. Source-observation timestamps were preserved. This does not claim that hosted-runner caches persist between scheduled jobs. All three overlapping BLS/FRED observations matched; these are mirrors of the same original data, not independent corroboration. Health reporting still flagged stale fundamental evidence for SPGI. Healthy acquisition does not mean every observation is current; stale-data and quality warnings accompany retrieval.

## Quality, cost and connectivity

Collection is bounded by time, request counts, per-host rate limits and response size, with validated cache hashes, immutable revisions, per-source failures and explicit partial/degraded status. It connects to all five analyst desks and the PM, prioritizes locally held names, option underlyings and candidates, and leaves broker quotes and risk limits separate. It makes zero LLM calls and zero brokerage calls. No paid data service, model API, GPU or larger runner was configured. Provider terms and GitHub storage limits still apply.

The normal schedule is 09:15 and 17:15 America/New_York on weekdays. It validates before publishing research data and reports only. Local cycle collection has a 90-second budget and 105-second hard timeout, a four-hour success gate and bounded retry cooldown. GitHub updates do not remotely redeploy an already-running local fund.

## Retrieval and financial limits

The ten curated development probes found expected sources in the top three (10/10), with MRR@5 of 0.95. All five desk prompts received eligible evidence, and future data was excluded. These are development probes, not a held-out financial benchmark or proof of improved investment returns. Twenty grounding candidates require review; no model weights were trained or promoted.

SEC filing events are metadata, not full-text sentiment or insider-buy judgments. FRED/BLS data is collection-time vintage, not reconstructed historical ALFRED data. Yahoo-derived features are best-effort research, not executable quotes or a guaranteed commercial data license. Raw price histories are not redistributed.
