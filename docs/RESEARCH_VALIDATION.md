# Research RAG validation — October 9, 2026 (US Eastern)

## Reproducible execution

GitHub Actions run [38017585731](https://github.com/PadiusD1/loch-raven-capital/actions/runs/38017585731) completed successfully. It applied a SHA-256-verified source patch, ran regressions with bash pipefail, collected live data, evaluated the populated corpus, and committed source plus persistent data as `b51f90ebcb64fb2e79782f7bddd16f2f16939222`. The final cleaned PR head is additionally tested by the ordinary Research integrity checks workflow. No brokerage execution, risk-limit edits, or model-weight training was performed.

## Tests observed passing

| Suite | Checks passed |
| --- | ---: |
| Research, provenance, API and desk integration | 30 |
| Directional memory | 5 |
| Separate desk-isolation integration | 1 |
| Existing institutional memory | 64 |
| Full firm regression | 1,104 |
| Simulated trading week | 53 |
| **Unique regression checks** | **1,257** |

The 30 research checks were also rerun after live collection. They are not counted twice above. Logs are retained in the run's `verified-research-upgrade` artifact, and ordinary CI retains per-suite logs going forward.

## Actual persisted source data

Collection observed at `2026-10-10T02:39:17.319611+00:00` added 71 live records to 11 reviewed source passages: **82 records total**. The 29 source groups all completed; the collector reported zero source errors.

| Desk | Records |
| --- | ---: |
| Fundamental | 43 |
| Technical | 10 |
| Sentiment | 10 |
| Macro | 8 |
| Quant | 11 |

The structured records include 240 SEC financial facts, 35 SEC filing events, 144 FRED observations, and 18 derived technical/quant feature records. SEC coverage: AAPL, MSFT, NVDA, AMD, MU, CSCO, AMZN. Price features also cover QQQ and SPY. Macro series: DGS2, DGS10, FEDFUNDS, UNRATE, CPIAUCSL, T10Y2Y.

This is a bounded initial research universe, not whole-market coverage. Filing events are metadata, not full filings or sentiment judgments. FRED snapshots contain latest-vintage observations, not reconstructed historical ALFRED vintages. Yahoo adjusted-price features are descriptive research, not executable quotes. Source hashes establish integrity, not that every statement is true. Newly collected records remain unavailable to decisions before their observed-at timestamp.

### Numerical reconciliation spot checks

The downloaded SEC facts reconcile with three reviewed issuer releases: Apple's quarter ended June 27, 2026 revenue is USD 109,417 million, consistent with the release rounded to USD 109.4 billion. NVIDIA's quarter ended July 26, 2026 revenue is USD 96,221 million, matching its release. Microsoft's FY2026 revenue of USD 331,839 million less its nine-month USD 241,832 million equals Q4 USD 90,007 million, matching its release. These are spot checks, not exhaustive fact verification.

## Retrieval results and limits

The populated-corpus evaluation produced 10/10 expected-source matches within the first three results and MRR@5 of 0.95. All five AAPL desk prompts received their eligible source evidence; a pre-observation query excluded the new corpus. The ten questions are curated development probes, **not an untouched held-out benchmark**. These metrics measure retrieval, not generated-answer correctness or investment accuracy.

Twenty reviewable grounding candidates were exported with relevant evidence, distractors and abstention cases. These candidates overlap the development probes and must not be trained on and then presented as a held-out evaluation. No model was trained or promoted.

## Historical investment replay

A separate local replay of the unchanged 1,794-memo archive evaluated 264 graded directional calls using date-specific BM25/TF-IDF statistics and only mature, same-direction precedents. Archive SHA-256: `d6fbda24d3f13d6e6aa7b0439e90553df2812d5858e9a258ec1c14131789bca9`.

With k=6 and at least three eligible precedents, the historical committee alone had a 41.29% hit rate and mean direction-adjusted excess return of -3.13 percentage points over 20 trading days. Memory supported 21 calls (38.10% hit rate; -6.99 points), warned against 33 (48.48%; -1.15 points), and lacked sufficient evidence for 210. These retrospective selections do **not** demonstrate an investment edge and are not a portfolio backtest. The upgraded source corpus was unavailable at those old decision dates and was correctly excluded.

## Operational boundary

The weekday refresh is scheduled for 12:20 UTC and commits only validated research data and evaluation files after its checks. It uses an optional SEC_USER_AGENT secret or an existing public author contact from repository history; a new secret is not required for this repository. Collection errors are reported without deleting prior good snapshots. No model is automatically promoted and no order is placed by these workflows.

Merging updates GitHub. A previously running copy on a user's own computer still needs to receive the repository changes through its deployment or update mechanism; these tests do not verify that machine's running process or Claude CLI authentication.
