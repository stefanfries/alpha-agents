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
```

## Tools used

- `InstrumentApiTool` — queries the `GET /v1/indices/{index_name}` endpoint of the FastAPI Instrument API; primary source for all supported indices
- `WikipediaIndexTool` — scrapes index constituent tables via `pandas.read_html()`; fallback when the FastAPI endpoint is unavailable or the index is not yet covered

## Behaviour

1. For each index name, attempt resolution via configured source priority (FastAPI first, Wikipedia fallback)
2. Merge all constituent lists; **deduplicate on ISIN** — if two entries from different indices share the same ISIN, keep one and record the first source in the `source` map
3. For tickers where no ISIN is available from the source (see notes), attempt ISIN lookup from the `instrument_master` MongoDB collection via `ticker_yfinance` symbol; if still unresolved, record in `missing_isin` and proceed with symbol-only entry
4. Apply `extra_tickers` additions and `exclude_tickers` removals
5. Return the final universe with provenance (`source` map keyed by ISIN)
6. Persist the `UniverseResult` to MongoDB Atlas for the current `run_id`

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

### Via FastAPI Instrument API `/indices` (primary)

`GET /v1/indices/{index_name}` on the `fastapi-azure-container-app` service (`https://ca-fastapi.yellowwater-786ec0d0.germanywestcentral.azurecontainerapps.io`). Returns authoritative Xetra ticker symbols with correct ISIN mapping sourced from Comdirect. This is the primary source for all supported indices.

## Ticker symbol normalisation

Wikipedia sometimes provides Frankfurt (Xetra) suffixes inconsistently. The agent normalises symbols:

- German indices: append `.DE` if no exchange suffix present (e.g. `SAP` → `SAP.DE`)
- US indices: no suffix
- Custom logic per index can be injected via a `symbol_normaliser` callable in config

## Configuration (via `config.py`)

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `universe_source_priority` | `["fastapi", "wikipedia"]` | Ordered list of sources to try |
| `universe_wikipedia_lang` | `"en"` | Wikipedia language version |
| `universe_symbol_suffix_map` | `{"DAX": ".DE", "MDAX": ".DE", "SDAX": ".DE", "TecDAX": ".DE"}` | Auto-suffix by index |

## Notes

- **ISIN availability by source**: The FastAPI `/v1/indices/{index_name}` endpoint always returns ISINs. Wikipedia includes ISINs in constituent tables for DAX, MDAX, SDAX, TecDAX, NASDAQ-100, and S&P 500; ISIN coverage from Wikipedia is high for the initially supported indices.
- **Cold start**: The FastAPI container uses Scale to Zero. On the first request of the day, allow up to 30 seconds for the container to start. The `InstrumentApiTool` should configure an HTTP timeout of at least 35 seconds.
- **Fallback for missing ISINs**: If the source does not provide an ISIN for an entry, the agent queries the `instrument_master` collection by `ticker_yfinance` symbol. Tickers with no ISIN after this fallback are included in `missing_isin` and proceed with `isin=None`; they will be excluded from any downstream step that requires ISIN (warrant search, Comdirect data fetch).
- **Deduplication rule**: ISIN is the primary key. A stock listed in both DAX and TecDAX is included once; the first source wins in the `source` map.
- Resolution failures (unknown index names) are reported in `unresolved_indices`; the pipeline continues with whatever could be resolved
- This agent is intentionally stateless — it produces the same output for the same inputs; results can be cached per `(indices, date)` key
