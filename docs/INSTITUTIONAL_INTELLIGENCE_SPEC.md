# Loch Raven Capital — Institutional Intelligence and Controlled Learning (design specification)
Status: DESIGN ONLY. No new publisher connectors, model training, self-modifying code, or automated strategy promotion are implemented by this document.

## Source registry and rights
Use explicit per-publisher configuration: publisher, canonical domain, official feed or API, discovery URL, access rights, permitted excerpt length, retention policy, cadence, robots/terms review date, and source-family lineage. Initial candidates: WSJ, CNN Business, Reuters, Bloomberg, FT, CNBC; Goldman Sachs Insights, JPMorgan Research, Morgan Stanley Insights, BlackRock Investment Institute, Vanguard research, Fidelity research; SEC, Federal Reserve, BEA, BLS, FRED and issuer IR. Do not imply that a paid research product is publicly accessible. Collect public metadata, titles, timestamps, canonical URLs and permitted abstracts; licensed full text only with explicit entitlement. Do not scrape around authentication, CAPTCHAs or paywalls.

## Source acquisition pipeline
Discover -> rights check -> canonicalize -> identify article/report version -> validate publication/observation dates -> extract claims and forecasts -> entity/ticker/industry linking -> reconcile against original filings and datasets -> record disagreement -> quality score -> deduplicate by canonical document and originating data lineage -> desk-specific evidence packet -> retrieval evaluation. Preserve raw-to-normalized lineage, publication and observed times separately, parser version, confidence reasons, and retraction/correction history. Treat publisher opinion, sell-side estimates and company disclosures as distinct claim types. Never turn forecasts into audited financial facts. Do not count syndication or FRED mirrors as independent confirmations. Quarantine unknown or expired rights, mismatched entities, stale evidence, and impossible timestamps.

## Five desk operating contracts
Each desk must output: timestamp, instrument, horizon, hypothesis, method, citations with page/section where available, assumptions, quantified scenarios, contradictory evidence, uncertainty, failure conditions, and abstention reason.
* Fundamental: reconcile SEC 10-K/10-Q financial statements, revenue segmentation, cash conversion, ROIC, invested capital, debt/leases, dilution; build explicit 3-statement forecast, DCF and peer comps; reconcile price target to current price and sensitivity to WACC/terminal growth. Earnings releases are provisional until reconciled to filings.
* Technical: adjusted corporate-action-safe daily bars, regime/trend, volume, volatility and liquidity, transaction costs, benchmark comparison; no lookahead from incomplete sessions; technicals never replace executable broker quotes.
* Sentiment: entity-resolved article/event clustering, primary-versus-secondary lineage, surprise versus consensus where legally available, corrections, earnings-guidance revisions, opposing coverage, publisher biases; abstain when only duplicated headlines exist.
* Macro: release calendars and vintage timestamps, inflation/jobs/growth, yield curves, credit spreads, liquidity, monetary policy, scenario transmission to each industry; FRED observations must not masquerade as historical real-time vintages.
* Quant: factor exposures, covariance shrinkage, costs/slippage, turnover, walk-forward splits, embargoed purged validation for overlapping labels, multiple-testing adjustment, drawdowns, calibration and capacity; report uncertainty intervals and reject unstable edges.
Portfolio manager: adjudicate desk disagreements, source conflicts and regime risk; enforce hard risk limits and no-trade gates independent of model text. No live order or model promotion without explicit authorization.

## RAFT and continuous improvement
Retrieval-augmented fine-tuning is a separate training process, not an automatic property of RAG. Build training examples only from licensed or internally authored evidence with precise citation spans, distractors, correct abstentions, verified calculations and contrary evidence. Split by publication time, entity and report family to prevent leakage. Maintain immutable decision journal: features available at decision time, forecast, sizing rationale, ex-ante confidence, outcome and risk-adjusted attribution. Generate error taxonomies: missing evidence, stale data, arithmetic, hallucinated citation, source disagreement, bad calibration, strategy error, execution slippage.
Nightly self-healing may repair ingestion/indexes, retry safe sources and rollback broken parsers. Weekly evaluation may propose prompt/retrieval changes; candidate changes run in shadow mode against locked baselines. Model fine-tuning requires dataset-rights review, held-out temporal testing, and manual approval for promotion. Never allow a model to rewrite its own tests, erase failures, loosen risk controls, or self-approve promotion.

## Promotion gates and metrics
Track permitted-source coverage, median discovery lag, freshness by desk, provenance completeness, correction/retraction propagation, entity resolution precision, citation faithfulness, unsupported-claim rate, abstention accuracy, retrieval recall@k and MRR, source-family diversity, data leakage checks, forecast Brier score/calibration, directional accuracy, benchmark-relative returns net of costs, drawdown, Sharpe uncertainty and turnover. Pre-register acceptance thresholds versus a frozen baseline. Require no regression on security, privacy, no-trade gates and data rights; promote only on statistically meaningful out-of-sample evidence. Keep full rollback, canary/shadow mode and immutable audit log.

## Delivery phases
1. Publisher rights/registry and official RSS/API discovery, metadata-only ingestion.
2. Claim-level provenance, entity/industry routing, report corrections, freshness dashboards and source audits.
3. Desk operating contracts and contradiction-aware research memos with deterministic math verification.
4. Locked temporal retrieval, grounding and financial backtests.
5. RAFT candidate creation and shadow evaluation; manual model promotion after measured improvements.

## Explicit limits
No performance or profit guarantee. No licensed Goldman Sachs/WSJ/Bloomberg full-text entitlement is assumed. Free public research varies by publisher. This specification alone does not activate feeds, train models, or change trading behavior.
