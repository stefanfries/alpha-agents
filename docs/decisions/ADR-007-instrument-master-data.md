# ADR-007: Instrument Master Data — Identifier Mapping and Storage

## Status

Accepted

## Context

The pipeline uses two fundamentally different identifier systems:

- **yfinance** works with ticker symbols (e.g. `NVDA`, `SAP.DE`)
- **Comdirect / FastAPI Instrument API** works with ISINs, WKNs, and internal notation IDs

There is no reliable real-time translation between these systems. A ticker symbol lookup via yfinance can return multiple matches; Comdirect's `symbol` field (e.g. `"NVD"` for NVIDIA) is their own short name, not the yfinance ticker (`"NVDA"`). A persistent mapping store is required.

Additionally, when fetching live prices or warrant data from Comdirect via the FastAPI Instrument API, the correct **notation ID** (venue-specific internal ID) must be supplied — not only the ISIN or ticker. This requires a venue list per instrument.

## Decision

### 1. MongoDB Atlas collection `instrument_master`

Store one document per instrument in a dedicated `instrument_master` collection, separate from `pipeline_runs`. Instrument data is **reference data** (slowly changing, owned by the FastAPI Instrument API) and must not be mixed with pipeline artefacts.

**Document `_id`**: ISIN (globally unique under ISO 6166, stable across renames and exchanges).

**Indexes**:

- Unique sparse index on `global_identifiers.wkn` (German instruments only)
- Non-unique index on `global_identifiers.ticker_yfinance` (symbol lookup)

### 2. GlobalIdentifiers block

The `global_identifiers` sub-document consolidates all cross-system identifiers, including the instrument name returned by OpenFIGI (useful for bridging between identifier systems when the local `name` field is unavailable or ambiguous):

```json
{
  "isin":             "US67066G1040",   // Luhn-validated; null for instruments without ISIN
  "wkn":              "918422",         // required primary key
  "cusip":            "67066G104",      // derived from ISIN chars 3–11 for US securities
  "figi":             "BBG001S5N8V8",   // Composite FIGI from OpenFIGI; null until enriched
  "symbol_yfinance":  "NVDA",           // Yahoo Finance-compatible ticker
  "symbol_comdirect": "NVD",            // Comdirect short name (informational only; ≠ yfinance)
  "name_openfigi":    "NVIDIA CORP"     // Instrument name from OpenFIGI; null until enriched
}
```

> **`symbol_comdirect` ≠ `symbol_yfinance`**: Comdirect uses its own short names that frequently differ from exchange ticker symbols. `symbol_comdirect` must never be passed to yfinance. Only `symbol_yfinance` is used for OHLCV fetching. Both `symbol_yfinance` and `figi` are `null` for asset classes not supported by Yahoo Finance (Warrant, Certificate).

### 3. OpenFIGI as the `ticker_yfinance` enrichment source

OpenFIGI (v3 API, free with API key) supports batch ISIN → ticker+exchange lookups natively. This solves the `ticker_yfinance` population problem automatically:

**Supported mappings relevant to this project:**

| Input | idType | Returns |
| ----- | ------ | ------- |
| ISIN | `ID_ISIN` | `ticker`, `exchCode`, `figi`, `name` |
| WKN | `ID_WERTPAPIER` | `ticker`, `exchCode`, `figi`, `name` |

**Rate limits (with free API key):** 25 requests / 6 seconds, 100 jobs per request — a 200-instrument universe = 2 API calls.

**`ticker_yfinance` derivation rule:**

```python
EXCH_CODE_TO_YFINANCE_SUFFIX = {
    "US": "",       # Nasdaq, NYSE → "NVDA"
    "GR": ".DE",   # Xetra → "SAP.DE"
    "LN": ".L",    # London → "SHEL.L"
    "FP": ".PA",   # Euronext Paris
    "SM": ".MC",   # Madrid
    # ... extend as needed
}

def derive_yfinance_symbol(ticker: str, exch_code: str) -> str:
    suffix = EXCH_CODE_TO_YFINANCE_SUFFIX.get(exch_code, "")
    return f"{ticker}{suffix}"
```

For each ISIN: filter OpenFIGI results to `marketSector: "Equity"` and select the result matching the instrument's primary home exchange. This gives `ticker_yfinance` without manual curation for the vast majority of instruments.

**What OpenFIGI does NOT return:** WKN. WKN always comes from the Comdirect data sync.

**Enrichment chain (run at instrument upsert time, not per pipeline run):**

```text
Comdirect sync  →  ISIN + WKN + notation_ids  →  upsert to instrument_master
      ↓
OpenFIGI batch  →  ISIN  →  ticker + exchCode + figi
      ↓
Derive ticker_yfinance from (ticker, exchCode)
      ↓
Derive cusip from ISIN (US instruments: chars 3–11)
      ↓
instrument_master document fully enriched
```

All enrichment steps run in the FastAPI Instrument API as a background job triggered by instrument upsert, not during pipeline runs.

### 4. Venue data as `VenueInfo` objects

Trading venues are returned as two dicts keyed by venue name, one for exchange trading and one for live trading:

```json
"id_notations_exchange_trading": {
  "Tradegate":  { "id_notation": "9386126", "currency": "EUR" },
  "Nasdaq":     { "id_notation": "277381",  "currency": "USD" }
},
"id_notations_life_trading": {
  "LT Lang & Schwarz": { "id_notation": "3240497", "currency": "EUR" }
},
"preferred_id_notation_exchange_trading": "9386126",
"preferred_id_notation_life_trading":     "3240497",
"default_id_notation":                    "3240497"
```

The `id_notation` value is the Comdirect internal venue identifier required by the `/history` endpoint (see §5 below) and any live price fetch.

**Currency resolution**: The FastAPI Instrument API maintains a static `venue_name → currency` lookup. Unknown venues default to `null`.

Known venue → currency mappings (non-exhaustive):

| Venue | Currency |
| ----- | -------- |
| Xetra, Tradegate, Frankfurt, Stuttgart, München, Hamburg, Hannover, Düsseldorf, Quotrix, gettex | EUR |
| Nasdaq, NYSE | USD |
| SIX Swiss (CHF) | CHF |
| SIX Swiss (USD) | USD |
| London Stock Exchange | GBP |
| Wiener Börse | EUR |

### 5. Historical price data — `/history` endpoint

`GET /v1/history/{identifier}?id_notation={notation_id}` returns historical OHLCV data for any instrument type (stock, warrant, ETF, …) identified by WKN or ISIN. The `id_notation` parameter selects the venue; the currency of the returned series matches the venue's currency.

This endpoint is the only available source of historical warrant price data — yfinance does not carry warrant OHLCV. The `id_notation` for the desired venue is obtained from the `VenueInfo` objects in the instrument's master record.

### 5. Instrument master ownership

The `instrument_master` collection is **written by the FastAPI Instrument API** (via a sync/upsert endpoint), not by this pipeline. The pipeline only reads from it.

The FastAPI Instrument API exposes:

- `GET /v1/instruments/{wkn_or_isin}` — fetch one instrument by WKN or ISIN
- `GET /v1/instruments?symbol_yfinance={symbol}` — reverse lookup by yfinance symbol

### 6. Identifier resolution flow in the pipeline

```text
UniverseAgent resolves index  →  list[Ticker(isin=..., symbol=symbol_yfinance)]
         │  (ISIN from FastAPI /v1/indices or Wikipedia; symbol_yfinance from
         │   instrument_master, pre-populated via OpenFIGI enrichment)
         ↓
ResearchAgent fetches OHLCV via symbol_yfinance  (no ISIN needed for yfinance)
         ↓
StockSelectionAgent selects stocks  (ISIN-keyed)
         ↓
WarrantSelectionAgent  →  FastAPI GET /v1/warrants?underlying_isin={isin}
         ↓
PortfolioAgent  →  reads id_notation from instrument_master for venue price fetch
```

## Alternatives considered

### A flat lookup table (CSV / SQLite)

Simple but not queryable from the FastAPI API, doesn't support concurrent writers, and is harder to keep in sync with Comdirect data.

**Rejected** in favour of MongoDB, which is already in the infrastructure.

### OpenFIGI for real-time per-run lookups

OpenFIGI is fast enough for batch enrichment (100 ISINs per call) but should not be called during pipeline runs to avoid adding a hard external dependency to the hot path. All OpenFIGI lookups happen as background enrichment at instrument upsert time in the FastAPI Instrument API.

**Decision**: OpenFIGI is used as a background enrichment step, not a real-time pipeline dependency. Results are cached in `instrument_master`.

## Consequences

- A `models/instrument.py` module must be added implementing `Instrument`, `GlobalIdentifiers`, and `VenueInfo` Pydantic models
- The `InstrumentApiTool` must implement `get_by_wkn(wkn)`, `get_by_isin(isin)`, and `get_by_yfinance_symbol(symbol)` methods
- An `InstrumentCache` (simple dict, scoped to a pipeline run) avoids redundant API calls per run
- The FastAPI Instrument API (`fastapi-azure-container-app`) has implemented:
  - ✅ `GET /v1/instruments/{identifier}` returning `Instrument` with `global_identifiers` (`symbol_yfinance`, `symbol_comdirect`, `figi`, `cusip`, `name_openfigi`), venue dicts (`id_notations_exchange_trading`, `id_notations_life_trading`), and preferred/default notation IDs
  - ✅ OpenFIGI background enrichment on instrument upsert: batch ISIN → `symbol_yfinance` + `figi` + `name_openfigi`
  - ✅ `GET /v1/history/{identifier}?id_notation={notation_id}` returning historical OHLCV for any instrument type (stock, warrant, ETF, …)
- `symbol_yfinance` is automatically populated by OpenFIGI for most instruments; exceptions can be manually overridden in `instrument_master`
- `symbol_yfinance` and `figi` are `null` for Warrant and Certificate asset classes (not covered by Yahoo Finance)
- The pipeline must use `symbol_yfinance` (never `symbol_comdirect`) for all yfinance calls
