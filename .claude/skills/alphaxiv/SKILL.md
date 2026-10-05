---
name: alphaxiv
description: Search and analyze quantitative finance, ML, and portfolio optimization papers and codebases on alphaXiv/arXiv
---

Research academic papers and linked codebases for: {{topic_or_query}}

## Purpose

Query the **alphaXiv MCP server** (or arXiv/alphaXiv APIs) to discover, inspect, and synthesize mathematical and empirical literature in quantitative finance, portfolio optimization, regime detection, asset pricing, and machine learning.

## Workflows

### 1. Paper Search & Discovery
- Search papers by query (e.g. `"Black-Litterman robust optimization"`, `"HMM market regime switching"`, `"Sortino ratio downside risk estimation"`).
- Retrieve metadata: paper title, authors, publication date, arXiv ID, abstract, and citation links.

### 2. PDF & Methodology Analysis
- Fetch full-text content or section summaries for target papers.
- Extract mathematical formulations, estimators, optimization loss functions, and parameter calibration guidelines.
- Cross-check methodology against existing Quantfolio implementations (e.g., `app/foundation/quant.py`, `app/foundation/quant_metrics.py`, `app/lab/regime/`).

### 3. Linked Codebase Inspection
- Inspect GitHub repositories linked to papers on alphaXiv.
- Review reference implementations, numerical stability handling, and vectorization patterns for adaptation.

### 4. Quantfolio Architecture Guidelines
When synthesizing research for integration into Quantfolio:
- **No live trading**: Research is for discovery, risk metrics, estimation, and backtesting only.
- **Convention alignment**: Ensure alignment with `docs/adr/0003-quant-metrics-conventions.md`.
- **Bounded contexts**: Place domain logic in the appropriate `app/<layer>/<context>` package (`layer` ∈ foundation/lab/decision/interface), preserving module boundaries and import contracts.
