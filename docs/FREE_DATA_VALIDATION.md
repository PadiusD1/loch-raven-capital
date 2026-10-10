# Free-source expansion: observed validation

## Reproducible source and execution

The tested source tree is `7820ead3e6f83495295849ba7d866177ed197156`, archived by GitHub run 38022884163. Its artifact includes the full source, module SHA-256 manifest, per-suite logs, first collection report and repeat-run report. Tests and live collection completed before the runner's publication step encountered a workflow-permission restriction. The native GitHub connection publishes the inspected source tree separately; the temporary installer and source-snapshot workflows are removed. Ordinary CI is read-only and tests the actual checked-out source without installing patches.

## Regression checks observed passing

| Suite | Passed |
| --- | ---: |
| Research, automation, provenance and API | 64 |
| Directional memory | 5 |
| Separate desk integration | 1 |
| Institutional memory | 64 |
| Full fund engine | 1104 |
| Simulated trading week | 53 |
| Total unique checks | 1291 |

Research tests were repeated after collection; those repeats are not counted twice. The final cleaned source must pass normal CI before merging.

## Actual collected data

Collection began 2026-10-10T04:08:32.260526+00:00 and completed at 2026-10-10T04:09:38.943188+00:00. It collected 620 records, added 611 after deduplication, and reported zero source errors or skipped sources. The persisted corpus contains 693 records: fundamental 464, sentiment 46, macro 43, technical 71, quant 69. These are records, not independent observations or training examples.

Actual acquisition used 138 HTTP requests: SEC directory 1, SEC facts/filings 48, FRED 20, BLS 3, Federal Reserve RSS 2, Yahoo 64. The same-run SPY reuse was a cache hit. A subsequent identical refresh made zero HTTP requests and reused 139 cached responses. This proves cache reuse within one environment; a new GitHub runner has an ephemeral cache unless separately persisted.

Coverage in this run: 24 SEC issuers, 64 price symbols, 20 FRED series, 3 BLS series, 2 Federal Reserve feeds. The configured exploratory stock universe rotates while actual holdings, option underlyings and candidates are prioritized only on the local machine. Whole-market research coverage is not claimed.

The three BLS/FRED reconciliation checks agreed for unemployment, payrolls and CPI at their last common observation. They share original data lineage, so agreement is a consistency check, not two independent confirmations. Latest-vintage snapshots are not historical ALFRED reconstructions.

## Freshness and retrieval

The health report distinguishes current evidence from old context. It flagged 39 fundamental topics, 3 macro topics and 1 sentiment topic as stale; SPGI lacked fresh fundamental evidence. Zero fetch errors does not mean zero missing or stale evidence. Stale records remain labelled historical context rather than being rewritten as current facts.

Development retrieval probes found the expected source in the top three for 10/10 cases, with MRR@5 0.95. All five desk prompt probes received eligible evidence, and pre-availability records were excluded. These probes are not an untouched held-out benchmark, generated-answer accuracy or investment performance.

## Cost, operation and limits

The public standard-runner workflow is scheduled at 09:15 and 17:15 America/New_York on weekdays. Private-repository automatic collection is disabled to avoid silently assuming paid-runner entitlement. Artifact retention is three days; no larger runners, GPU jobs, paid APIs or purchased storage are configured. Local HTTP caches are capped and remain untracked. GitHub scheduling is best-effort.

Local full research and held-name re-underwriting refresh their evidence before analyst workers start, within 100-request/90-second collection budgets and a 105-second subprocess timeout. Failures retain prior good snapshots and explicitly report degraded status. Deploying the repository updates is still necessary on a previously running user machine; these tests do not prove that machine redeployed.

No brokerage calls, orders, live-mode changes, risk-limit changes or model-weight training occur in acquisition. Yahoo-derived features are descriptive, not executable quotes; free access does not imply unrestricted commercial redistribution. The fund has not demonstrated improved investment returns from these retrieval tests.
