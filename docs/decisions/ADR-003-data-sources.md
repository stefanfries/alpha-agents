# ADR-003: Data Sources — yfinance for Market Data, Comdirect for Execution

**Date:** 2026-03-29
**Status:** Accepted

---

## Context

The system needs two categories of external data:

1. **Market data** (OHLCV, fundamentals, news) for research and screening
2. **Broker connectivity** (account info, order placement) for execution

## Decision

- **Market data**: [yfinance](https://github.com/ranaroussi/yfinance) — a Python wrapper around Yahoo Finance
- **Broker**: Comdirect REST API, via the `comdirect_api` sibling project in this repository

All external integrations are wrapped in `Tool` subclasses under `tools/` and injected into agents at construction time.

## Rationale

### yfinance

- Free, no API key required for basic OHLCV + fundamentals
- Supports a large global universe (US, EU, APAC exchanges)
- Well-maintained Python library with a simple interface
- Sufficient for research and screening; can be replaced with a paid provider later without changing agent code (swap the tool implementation, not the agent)

### Comdirect

- Primary broker account is at Comdirect (German bank)
- A fully-featured async Python client already exists in the `comdirect_api` sibling project
- Reusing it avoids duplicating OAuth2 + 2FA handling

## Consequences

- Data quality and availability depends on Yahoo Finance; gaps or inaccuracies in yfinance data will affect research quality
- The `Tool` abstraction means any data source can be swapped by implementing a new `Tool` subclass — agents are not coupled to yfinance or Comdirect directly
- Credentials for Comdirect are loaded from `.env` via `config.py` and never hardcoded
