# ADR-003: Data Sources — yfinance for Market Data, Comdirect for Execution

**Date:** 2026-03-29
**Status:** Accepted

---

## Context

The system needs two categories of external data:

1. **Market data** (OHLCV, fundamentals, news) for research and screening
2. **Broker connectivity** (account info, order placement) for execution

## Decision

- **Stock OHLCV data**: [yfinance](https://github.com/ranaroussi/yfinance) — a Python wrapper around Yahoo Finance, used via `symbol_yfinance` from the instrument master
- **Warrant and instrument data**: FastAPI Instrument API (`fastapi-azure-container-app` sibling project) — instrument master, warrant search, warrant detail, and historical OHLCV for all instrument types including warrants
- **Broker**: Comdirect REST API, via the `comdirect_api` sibling project in this repository

All external integrations are wrapped in `Tool` subclasses under `tools/` and injected into agents at construction time.

## Rationale

### yfinance

- Free, no API key required for basic OHLCV + fundamentals
- Supports a large global universe (US, EU, APAC exchanges)
- Well-maintained Python library with a simple interface
- **Limitation**: does not carry warrant or certificate price history — the FastAPI Instrument API `/history` endpoint fills this gap

### FastAPI Instrument API

- Provides instrument master data with OpenFIGI-enriched global identifiers (`symbol_yfinance`, FIGI, CUSIP, `name_openfigi`)
- Warrant search (`GET /v1/warrants`) and warrant detail (`GET /v1/warrants/{identifier}`) with full analytics
- Historical OHLCV for all instrument types (`GET /v1/history/{identifier}?id_notation=…`); currency follows the selected venue
- Index constituent lists (`GET /v1/indices/{index_name}`)
- Hosted on Azure Container Apps; uses Scale to Zero (allow up to 30 s cold start)

### Comdirect

- Primary broker account is at Comdirect (German bank)
- A fully-featured async Python client already exists in the `comdirect_api` sibling project
- Reusing it avoids duplicating OAuth2 + 2FA handling

## Consequences

- Stock OHLCV quality depends on Yahoo Finance; gaps or inaccuracies in yfinance data will affect research quality
- Warrant historical data depends on the FastAPI Instrument API and its Comdirect data feed
- The `Tool` abstraction means any data source can be swapped by implementing a new `Tool` subclass — agents are not coupled to yfinance or Comdirect directly
- Credentials for Comdirect are loaded from `.env` via `config.py` and never hardcoded
- The `OPENFIGI_API_KEY` environment variable is required by the FastAPI Instrument API for identifier enrichment
