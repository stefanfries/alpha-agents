# ADR-004: Investment Universe — Index Membership Data Source

## Status

Proposed

## Context

The pipeline must resolve index names (e.g. `DAX`, `NASDAQ-100`) into lists of ticker symbols before any market data can be fetched. Several sources exist with different trade-offs.

### Options evaluated

#### Option A: yfinance

`yfinance` offers no constituent/member API. `yf.Ticker('^GDAXI')` returns only the index price series, not its components.

**Verdict: Not viable.**

#### Option B: Wikipedia via `pandas.read_html()`

Wikipedia maintains up-to-date constituent tables for all major indices. The `pandas.read_html(url)` function can scrape these tables without authentication.

```python
import pandas as pd

WIKIPEDIA_URLS = {
    "DAX":       "https://en.wikipedia.org/wiki/DAX",
    "MDAX":      "https://en.wikipedia.org/wiki/MDAX",
    "SDAX":      "https://en.wikipedia.org/wiki/SDAX",
    "NASDAQ-100":"https://en.wikipedia.org/wiki/Nasdaq-100",
    "SP500":     "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "FTSE100":   "https://en.wikipedia.org/wiki/FTSE_100_Index",
}

def get_dax_tickers() -> list[str]:
    tables = pd.read_html(WIKIPEDIA_URLS["DAX"])
    # First table on the DAX page lists constituents
    df = tables[3]  # table index can shift; identify by column "Ticker symbol"
    return df["Ticker symbol"].tolist()
```

Pros:
- Free, zero authentication
- Covers all major indices used in this project
- `pandas.read_html` is already a common dep via analysis workflows

Cons:
- Table index within the page can change when Wikipedia is edited
- German tickers sometimes lack `.DE` suffix — requires normalisation step
- Constituency changes may lag by hours/days
- Scraping public pages can break when page structure changes (best-effort, not contractual)

**Verdict: Good enough for development and early production use for major indices.**

#### Option C: FastAPI Instrument API `/indexes` endpoint (planned extension)

A new endpoint to be added to the `fastapi-azure-container-app` sibling project, hosted at `https://ca-fastapi.yellowwater-786ec0d0.germanywestcentral.azurecontainerapps.io/`. This service already provides various instrument data endpoints and is the natural home for index membership data.

> **Note:** This Azure Container App is configured with Scale to Zero — allow ~30 seconds for cold start on first request.

Pros:
- Authoritative Xetra ticker symbols and ISINs (can scrape Comdirect's index pages server-side)
- Same API as the planned warrant search endpoint — consistent data source
- Real-time constituency
- No scraping fragility in the client (logic centralised in the API)

Cons:
- Requires development effort (add endpoint to `fastapi-azure-container-app`)
- Cold-start latency on Scale-to-Zero container

**Verdict: Best long-term solution, especially for German market focus.**

> **Note on `comdirect_api`**: The `comdirect_api` sibling project is intentionally excluded as a data source here. It provides read access to Comdirect bank/portfolio data and syncs it to MongoDB Atlas, but it cannot be run autonomously (requires 2FA per session) and does not have an index membership endpoint.

#### Option D: Commercial data providers (e.g. Refinitiv, Bloomberg)

Authoritative but require paid subscriptions. Out of scope for this project.

**Verdict: Not applicable.**

## Decision

**Use Wikipedia as the primary source** for the initial implementation (covers all initially required indices: DAX, MDAX, SDAX, NASDAQ-100).

**Plan for FastAPI Instrument API `/indexes` endpoint** as the production-grade replacement. The `UniverseAgent` is designed with a pluggable `source_priority` configuration so the switch requires no code changes to the agent itself — only adding a new `InstrumentApiTool` implementation.

## Consequences

- `WikipediaIndexTool` must handle table index brittleness defensively (try multiple table indices, identify by column header)
- A `symbol_normaliser` step is required for German indices to append `.DE` exchange suffixes
- When the FastAPI `/indexes` endpoint is implemented, `universe_source_priority` configuration is changed from `["wikipedia"]` to `["fastapi", "wikipedia"]`
- Results should be cached for the current trading day to avoid repeated HTTP requests within one session
