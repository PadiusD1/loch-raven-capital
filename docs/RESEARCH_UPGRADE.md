# Source-linked research and integrity upgrade

## Delivered behavior

`firm/research_rag.py` reads persistent JSON/JSONL research from `data/research`
(or `LOCH_RAVEN_RESEARCH_DIR`). It filters availability, company identity and
desk before fitting BM25 and TF-IDF cosine rankers. Publication, observation,
effective dates, content hashes and source URLs remain attached to evidence.
Company-unknown records cannot silently cross ticker boundaries. Methodology
records are explicitly tagged; macro evidence can apply across companies.

`firm/analysts.py` injects the applicable evidence into each desk. The portfolio
manager receives the same source identifiers through `firm/debate.py`; source
identifiers are included in citation checks and audit records. Engine timestamps
(`YYYY-MM-DD HH:MM UTC`) work, and a date-only query means start of that day in
New York, not an assumed end-of-day. Undated packets get no external evidence.

The full-depth mode uses independent calls. Standard depth remains one model
call with partitioned evidence, NOT five independently isolated models.

## Data and refresh

The reviewed seed contains 11 concise, source-linked records from Apple,
Microsoft, NVIDIA, the Federal Reserve, FRED, AQR, Kenneth French's data library
and the original RAFT paper. The record URLs and observation timestamps are in
`data/research/reviewed_seed.jsonl`. Issuer opinions and forward guidance are
labeled separately from reported results. Technical/quant methodology is not
misrepresented as a current market price or a validated forecast.

`python -m scripts.refresh_research` fetches:

* SEC company-reported financial facts and filing metadata for seven companies.
* Six government-origin FRED economic series.
* Completed Yahoo adjusted-price observations for those companies, QQQ and SPY,
  calculating benchmark-relative momentum, volatility, beta and drawdowns.

The collected data is persisted in `data/research/snapshots.jsonl`, not merely
an expiring Actions artifact. A source error is reported without erasing earlier
snapshots. Exact duplicate content retains its original observation date.
SEC contact comes from `SEC_USER_AGENT` or a public author email in repository
history; no invented email or newly required paid key is used. Collection makes
no LLM calls and has no brokerage access. The scheduled GitHub workflow stages
research-data paths only and does not deploy to a local PC.

FRED observations are current-vintage downloads, NOT as-originally-released
ALFRED observations. They are eligible only from this system's observation time.
Today's collected material is never backdated into a June decision. Yahoo is a
vendor source, not an exchange-certified tape, and adjusted closes are not
executable quotes. Filing-calendar entries are metadata, not a reading of the
filing or a sentiment score. Stale evidence is flagged in prompts.

## Historical memory and learning

The institutional memory now rebuilds lexical index statistics on eligible
historical text. A bounded cache keeps repeated calls economical. The prior
LSA/SVD method was replaced by sparse TF-IDF cosine to make per-date validation
practical. The code does not claim semantic-embedding capabilities it lacks.

Playbook material and lessons have conservative availability dates; lessons
written in September are no longer inserted into June fine-tuning examples.
Training rows still require matured outcomes before the split. Profitable
historical calls are not automatically expert-quality reasoning. Model
promotion is disabled in the generated data card.

`python -m scripts.evaluate_research` writes a retrieval report and reviewable
source-grounding candidates, including distractors and source-absent abstention
examples. These candidates overlap the curated development probes and MUST NOT
be trained on then scored as held-out tests. No model weights are trained or
promoted by this upgrade. This is a functioning retrieval/data pipeline, not
proof of autonomous model improvement.

## Verification and interpretation

The pipeline runs compilation, targeted integrity tests, the existing 64-check
memory suite, the complete firm's regression suite and simulated trading week
in GitHub Actions. `data/research_evaluation/retrieval_evaluation.json` reports
actual corpus coverage, retrieval hit rate, reciprocal rank, temporal exclusion
and five-desk injection. The initial 11-record corpus retrieved all 10 expected
sources within the top three; this small development set is not a production
accuracy estimate. Re-evaluation after ingestion is required.

The historical memory evaluator reports retrospective selected-call statistics;
it is not a transaction-cost-aware portfolio backtest or significance test.
Nothing here proves the system beats SPY. Live trading mode, risk limits,
position sizing, broker credentials and order-placement code are unchanged.

## Inspect without trading

```
GET /api/memory/research/status
GET /api/memory/research/search?q=cash%20flow&desk=fundamental&ticker=NVDA&asof=2026-10-11
```

These read-only routes expose research status and evidence. Optional source
errors surface as warnings rather than silently pretending data was supplied.
