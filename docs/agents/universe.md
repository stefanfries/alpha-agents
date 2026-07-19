# Agent Spec: Universe Agent

## Responsibility

Resolve one or more index names into a flat, deduplicated list of ticker symbols. This is the **zeroth pipeline stage** — it runs before any market data is fetched.

## Input

```python
class UniverseSpec(AgentInput):
    indices: list[str]          # e.g. ["DAX", "MDAX", "SDAX"]
    extra_tickers: list[Ticker] = []   # Optional manual additions
    exclude_tickers: list[Ticker] = [] # Optional manual exclusions
```

## Output

```python
class UniverseResult(AgentOutput):
    tickers: list[Ticker]          # Deduplicated on ISIN (primary), symbol (fallback)
    source: dict[str, str]         # Maps ISIN → originating index name
    missing_isin: list[str]        # Symbols for which no ISIN could be resolved (warning)
    unresolved_indices: list[str]  # Indices that could not be resolved
    adr_isins: list[str]           # ISINs flagged as ADRs (warrant availability is checked only for these)
```

## Tools used

- `FinHubTool` — queries `GET /v1/indices/{index_name}` and `GET /v1/instruments/{isin}` from the FinHub API; primary source for all supported indices
- `WikipediaIndexTool` — scrapes index constituent tables via `pandas.read_html()`; fallback when the FastAPI endpoint is unavailable or the index is not yet covered

## Behaviour

1. For each index name, attempt resolution via configured source priority (FastAPI first, Wikipedia fallback)
2. For FinHub members, resolve each member ISIN via `GET /v1/instruments/{isin}` and require `global_identifiers.symbol_yfinance`
3. Skip entries where no ISIN is present or `symbol_yfinance` is missing; these are logged as warnings
4. Merge all constituent lists; **deduplicate on ISIN** — if two entries from different indices share the same ISIN, keep one and record the first source in the `source` map
5. Apply `extra_tickers` additions and `exclude_tickers` removals
6. Return the final universe with provenance (`source` map keyed by ISIN, or symbol when ISIN is absent)
7. Persist the `UniverseResult` to MongoDB Atlas for the current `execution_id`

## Supported indices

### Via Wikipedia (`pandas.read_html`)

| Index | Coverage | Wikipedia URL pattern |
| ----- | -------- | --------------------- |
| DAX | 40 German large-caps | `DAX` |
| MDAX | 50 German mid-caps | `MDAX` |
| SDAX | 70 German small-caps | `SDAX` |
| TecDAX | 30 German tech | `TecDAX` |
| EuroStoxx 50 | 50 EU large-cap | `EURO STOXX 50` |
| NASDAQ-100 | 100 US tech | `Nasdaq-100` |
| S&P 500 | 500 US large-cap | `List_of_S%26P_500_companies` |
| FTSE 100 | 100 UK large-cap | `FTSE_100_Index` |

### Via FinHub API `/indices` (primary)

`GET /v1/indices/{index_name}` on the `fastapi-azure-container-app` service (`https://ca-fastapi.yellowwater-786ec0d0.germanywestcentral.azurecontainerapps.io`). Returns authoritative Xetra ticker symbols with correct ISIN mapping sourced from Comdirect. This is the primary source for all supported indices.

## Ticker symbol handling

- FinHub path: the symbol is taken from `global_identifiers.symbol_yfinance` as returned by FinHub.
- Wikipedia fallback path: symbol formatting follows `WikipediaIndexTool` index-specific parsing rules.
- No local fallback to `symbol_comdirect` is performed for yfinance symbols.

## Configuration (via `config.py`)

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `finhub.instrument_lookup_concurrency` | `8` | Max concurrent `GET /v1/instruments/{isin}` requests during FinHub universe resolution |

## Notes

- **ISIN availability by source**: FinHub index payloads are expected to include ISINs. Wikipedia fallback may provide symbols without ISIN.
- **Cold start**: The FastAPI container uses Scale to Zero. On the first request of the day, allow up to 30 seconds for the container to start. `FinHubTool` timeout should remain high enough to tolerate cold start.
- **No local ISIN backfill**: The agent does not query `instrument_master` for missing ISINs.
- **ADR detection**: Members whose FinHub instrument `details.security_type == "ADR"` are kept in the universe (no longer skipped) and their ISINs collected in `adr_isins`. The orchestrator uses this list to run the ADR-only warrant-availability scan (see ADR-012). The ADR's yfinance `symbol` and price candles are unchanged.
- **Deduplication rule**: ISIN is the primary key. A stock listed in both DAX and TecDAX is included once; the first source wins in the `source` map.
- Resolution failures (unknown index names) are reported in `unresolved_indices`; the pipeline continues with whatever could be resolved
- This agent is intentionally stateless — it produces the same output for the same inputs; results can be cached per `(indices, date)` key
