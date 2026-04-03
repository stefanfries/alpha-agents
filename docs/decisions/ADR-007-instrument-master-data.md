# ADR-007: Instrument Master Data — Identifier Mapping and Storage

## Status

Proposed

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

The `global_identifiers` sub-document consolidates all cross-system identifiers:

```json
{
  "isin":              "US67066G1040",   // required
  "wkn":               "918422",         // required for German instruments; null otherwise
  "cusip":             "67066G104",      // derived from ISIN for US securities (chars 3–11)
  "figi":              "BBG001S5N8V8",   // from OpenFIGI API; null until resolved
  "ticker_yfinance":   "NVDA",           // yfinance-compatible symbol
  "ticker_comdirect":  "NVD"            // Comdirect's internal short name (informational only)
}
```

> **`ticker_comdirect` ≠ `ticker_yfinance`**: Comdirect uses its own short names that frequently differ from exchange ticker symbols. `ticker_comdirect` must never be passed to yfinance. Only `ticker_yfinance` is used for OHLCV fetching.

### 3. OpenFIGI as the `ticker_yfinance` enrichment source

OpenFIGI (v3 API, free with API key) supports batch ISIN → ticker+exchange lookups natively. This solves the `ticker_yfinance` population problem automatically:

**Supported mappings relevant to this project:**

| Input | idType | Returns |
|-------|--------|---------|
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

```
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

### 4. Venue list as structured objects

```json
"trading_venues": [
  {
    "venue_name":                  "Xetra",
    "venue_id":                    "179322401",
    "venue_type":                  "exchange",
    "currency":                    "EUR",
    "is_default":                  false,
    "is_preferred_exchange":       false,
    "is_preferred_live_trading":   false
  }
]
```

**Currency resolution**: Comdirect does not return currency per venue. The FastAPI Instrument API maintains a static `venue_name → currency` lookup table. Known venues are mapped at startup; unknown venues default to `null` and are flagged for manual review.

Known venue → currency mappings (non-exhaustive):

| Venue | Currency |
|-------|----------|
| Xetra, Tradegate, Frankfurt, Stuttgart, München, Hamburg, Hannover, Düsseldorf, Quotrix, gettex | EUR |
| Nasdaq, NYSE | USD |
| SIX Swiss (CHF) | CHF |
| SIX Swiss (USD) | USD |
| London Stock Exchange | GBP |
| Wiener Börse | EUR |

### 5. Instrument master ownership

The `instrument_master` collection is **written by the FastAPI Instrument API** (via a sync/upsert endpoint), not by this pipeline. The pipeline only reads from it.

The FastAPI Instrument API should expose:

- `GET /instruments/{isin}` — fetch one instrument
- `PUT /instruments/{isin}` — upsert instrument master data (called when syncing from Comdirect)
- `GET /instruments?ticker_yfinance={symbol}` — reverse lookup by yfinance ticker

### 6. Identifier resolution flow in the pipeline

```text
UniverseAgent resolves index  →  list[Ticker(isin=..., symbol=yfinance_symbol)]
         │  (ISIN from Wikipedia/FastAPI; ticker_yfinance from instrument_master,
         │   pre-populated via OpenFIGI enrichment)
         ↓
ResearchAgent fetches OHLCV via ticker_yfinance  (no ISIN needed for yfinance)
         ↓
StockSelectionAgent selects stocks  (ISIN-keyed)
         ↓
WarrantSelectionAgent  →  FastAPI /warrants?underlying_isin={isin}
         ↓
PortfolioAgent  →  reads notation_id from instrument_master for venue price fetch
```

## Alternatives considered

### A flat lookup table (CSV / SQLite)

Simple but not queryable from the FastAPI API, doesn't support concurrent writers, and is harder to keep in sync with Comdirect data.

**Rejected** in favour of MongoDB, which is already in the infrastructure.

### OpenFIGI for real-time per-run lookups

OpenFIGI is fast enough for batch enrichment (100 ISINs per call) but should not be called during pipeline runs to avoid adding a hard external dependency to the hot path. All OpenFIGI lookups happen as background enrichment at instrument upsert time in the FastAPI Instrument API.

**Decision**: OpenFIGI is used as a background enrichment step, not a real-time pipeline dependency. Results are cached in `instrument_master`.

## Consequences

- A `models/instrument.py` module must be added implementing `Instrument`, `GlobalIdentifiers`, and `TradingVenue` Pydantic models
- The `InstrumentApiTool` must implement `get_by_isin(isin)` and `get_by_yfinance_symbol(symbol)` methods
- An `InstrumentCache` (simple dict, scoped to a pipeline run) avoids redundant Atlas reads per run
- The FastAPI Instrument API (`fastapi-azure-container-app`) must be extended to:
  - Return the new `global_identifiers` block including `ticker_yfinance`, `ticker_comdirect`, `figi`, `cusip`
  - Return `trading_venues` as a list of venue objects with `currency`
  - Persist instrument documents to MongoDB Atlas `instrument_master` on upsert
  - Run OpenFIGI enrichment as a background task on upsert: batch ISIN → `ticker_yfinance` + `figi` derivation
  - Require an `OPENFIGI_API_KEY` environment variable
- `ticker_yfinance` will be automatically populated by OpenFIGI for the vast majority of instruments; exceptions (e.g. dual-listed stocks where the preferred listing is ambiguous) can be manually overridden in `instrument_master`
