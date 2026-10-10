# Free-data automation and fund-wide quality controls

Implemented October 9, 2026 (US Eastern). Builds on the tested source RAG, not a new trading strategy.

## What runs automatically

The GitHub refresh workflow runs at **09:15 and 17:15 America/New_York on weekdays**, using the documented timezone-aware schedule. It commits research data and reports only, after validation. Standard public-repository Linux runners are used; no larger runners, paid data subscription, cloud database, AI API, GPU job, or purchased storage is configured. Provider availability and platform policies can change. GitHub schedules may be delayed or disabled for repository inactivity; the collector is not a market-data SLA.

The local fund's full research cycle and held-name re-underwriting also call a bounded collector **before** starting analyst workers. It prioritizes holdings (including option underlyings), current candidates and watchlist names. It reads no demo book as live state, sends no holdings quantities/costs/credentials to external providers, never pushes private local state, and never calls the broker. Failure leaves the previous good corpus in place and journals a degraded status. The feature takes effect once the local installation receives this code; updating GitHub does not remotely deploy a running computer.

`LOCH_RAVEN_AUTO_RESEARCH=0` disables local acquisition. The collector does not run inside a quote loop, exit monitor, or analyst prompt. A local subprocess is limited to 90 seconds of collection, 100 HTTP requests, and a 105-second hard timeout. Repeated cycle calls use a four-hour successful-attempt gate and a five-minute failure/new-symbol cooldown. An operating-system lock prevents concurrent writers.

## Coverage instead of indiscriminate scraping

Each cloud run selects up to **24 SEC issuer symbols and 64 price symbols**. Held/candidate symbols take precedence locally. The public workflow covers the configured equity universe on a rotating daily schedule and includes core, parking, cash and rotation ETFs. Symbols outside the current budget, missing issuer mappings and failed providers are reported rather than silently treated as covered. Config hard-bounds allow up to 40 SEC/100 price symbols, but the total HTTP budget remains at most 300.

- SEC: official symbol-to-CIK lookup, 20 financial metric families, up to 12 distinct periods per metric, duration/instant tags, accession numbers, amendments, precise USD/shares/USD-per-share units. Up to 12 filing events include Form 4 and 8-K item metadata. Metadata alone is not an insider-buy or sentiment signal.
- FRED: 20 government-origin macro series including the existing rates/CPI/employment feeds plus core PCE, real GDP, payrolls, claims, industrial production, retail sales, housing starts, Fed assets, money supply and broad dollar conditions. Up to 120 observations per series. These are collection-time vintages, not ALFRED historical reconstructions.
- BLS: original unemployment, payroll and CPI series through the keyless v1 API. Monthly periods only; annual M13 aggregates are excluded and preliminary footnotes retained. The last overlapping observations are compared with their FRED mirrors. A mirror is not an independent source. Differences produce warnings, not an invented resolution.
- Federal Reserve: official monetary-policy and speech RSS feeds, dated and routed as macro/catalyst context. No recursive web crawler or arbitrary links. The same announcement reaching two desks retains the same lineage.
- Prices: existing Yahoo source, extended to the fund universe with aligned SPY benchmarks. Only completed US daily sessions are eligible; a conservative 16:15 Eastern cutoff excludes intraday bars. Gross adjusted-price dislocations are quarantined. Derived features are research, not executable quotes or evidence of a return forecast. Yahoo is best-effort third-party access, not a guaranteed free commercial market-data service. Raw price histories are not committed.

Unavailable premium fundamentals, unrestricted transcripts, analyst revision histories, historical options chains, licensed research, and unconfirmed campus/GPU/API entitlements are not fabricated or silently substituted. Existing Claude CLI research remains separate and subject to the user's subscription limits.

## Acquisition integrity

`SourceClient` uses an explicit HTTPS host allowlist, at most two sequential requests per second per host, three bounded attempts, Retry-After handling, no redirects, a 15 MB response limit, a 180-request / 480-second default job budget, and a 256 MB local response-cache cap. Cache headers and body hashes are retained without credentials. Local cache files are gitignored. A fresh TTL or HTTP 304 avoids downloading identical bytes; failed requests never renew the cache timestamp. GitHub-hosted caches are ephemeral unless the operator explicitly provisions persistence; no extra billed cache allowance is enabled.

There are separate timestamps for source-byte observation, rechecking, first ingestion and the economic period. New derived/parsed records receive an ingestion timestamp only after parsing completes. Consecutive identical content keeps its original availability; an A-to-B-to-A revision gets a new snapshot instead of incorrectly resurrecting an old date. Source hashes check integrity, not economic truth.

Bad rows are individually rejected during retrieval. Invalid ticker strings, negative/nonfinite freshness limits, NaN/infinite financial values, wrong entities, unknown BLS series, future dates and unsafe XML declarations fail validation. Newly collected data cannot enter older decisions. The packet timestamp now preserves seconds and timezone, avoiding a minute-rounding error that could hide just-collected research.

## How the fund consumes it

The five desks and PM continue to use the same source store with entity, desk and time filters. Global policy announcements may reach sentiment and macro, while company-specific filings cannot cross into another company's facts. Price features do not replace broker quotes or risk checks. Quality warnings and common-source lineage accompany evidence. `/api/memory/research/status` adds freshness by desk, rejected rows and coverage information. Runtime refresh results are also journaled.

No stop, position, portfolio, live-trading mode, order permission, or model-promotion gate is loosened. Research acquisition makes zero LLM calls and zero broker calls. Regressions measure correctness and connectivity; retrieval probes are development tests, not proof of improved returns. More observations do not guarantee a better investment strategy.

## Validation commands

```sh
python -m compileall -q firm scripts tests
python -m unittest discover -s tests -p 'test_research*.py'
python tests/test_memory.py
python tests/test_firm.py
python tests/test_week_sim.py
python -m scripts.refresh_research
python -m scripts.evaluate_research
```

Read `data/research_refresh_report.json` for actual source successes, failures, deferred symbols, request counts, BLS/FRED reconciliation and per-desk freshness. A green workflow means integrity checks passed; check the report's `status` and `errors` before assuming every provider succeeded.

## Primary-source documentation checked

- SEC API and access rules: https://www.sec.gov/search-filings/edgar-application-programming-interfaces and https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
- BLS v1 request and monthly-period schema: https://www.bls.gov/developers/api_signature.htm
- Federal Reserve feed directory: https://www.federalreserve.gov/feeds/feeds.htm
- FRED usage/copyright restrictions: https://fred.stlouisfed.org/docs/api/terms_of_use.html
- Representative government-origin series: https://fred.stlouisfed.org/series/PCEPILFE , https://fred.stlouisfed.org/series/ICSA , https://fred.stlouisfed.org/series/WALCL
- GitHub public standard-runner billing: https://docs.github.com/en/actions/concepts/billing-and-usage
- GitHub schedule timezone and inactivity limitations: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
