# Data Model Reference

All models are Pydantic V2. Shared domain types live in `models/`; they are imported by agents and tools — never defined inline.

---

## Market types (`models/market.py`)

### `Ticker`

A lightweight reference to a security, used as a key throughout the pipeline. The canonical identifier is the yfinance-compatible symbol. Rich instrument data (ISIN, WKN, venues) lives in the `Instrument` master document.

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | `str` | yfinance-compatible ticker (e.g. `"NVDA"`, `"SAP.DE"`) |
| `isin` | `str \| None` | ISIN — used to look up the instrument master record |

### `OHLCV`
One daily candlestick bar.

| Field | Type | Description |
|-------|------|-------------|
| `ticker` | `Ticker` | The security this bar belongs to |
| `date` | `date` | Trading date |
| `open` | `Decimal` | Opening price |
| `high` | `Decimal` | Intraday high |
| `low` | `Decimal` | Intraday low |
| `close` | `Decimal` | Closing price |
| `volume` | `int` | Share volume |

### `Position`

A current or proposed holding (stocks or warrants).

| Field | Type | Description |
|-------|------|-------------|
| `ticker` | `Ticker` | The security |
| `quantity` | `Decimal` | Number of shares/units (negative = short) |
| `avg_cost` | `Decimal` | Average cost basis per unit |

### `Order`

A trade instruction produced by the Execution Agent.

| Field | Type | Description |
|-------|------|-------------|
| `ticker` | `Ticker` | Security to trade |
| `side` | `Literal["buy", "sell"]` | Direction |
| `quantity` | `Decimal` | Units to trade |
| `order_type` | `Literal["market", "limit"]` | Execution type |
| `limit_price` | `Decimal \| None` | Required when `order_type="limit"` |

### `Warrant`
A Call Warrant (Optionsschein) with its derivative characteristics.

| Field | Type | Description |
|-------|------|-------------|
| `isin` | `str` | Warrant ISIN |
| `wkn` | `str \| None` | German security identifier |
| `underlying` | `Ticker` | The underlying stock |
| `issuer` | `str` | Issuing bank (e.g. `"Deutsche Bank"`) |
| `strike` | `Decimal` | Strike price (Basispreis) |
| `expiry` | `date` | Expiry date |
| `ratio` | `Decimal` | Bezugsverhältnis (e.g. 0.1 = 1 warrant per 0.1 shares) |
| `delta` | `float \| None` | Option delta |
| `leverage` | `float \| None` | Hebel (effective leverage) |
| `bid` | `Decimal \| None` | Current bid price |
| `ask` | `Decimal \| None` | Current ask price |
| `iv` | `float \| None` | Implied volatility (%) |
| `intrinsic_value` | `Decimal \| None` | Innerer Wert |
| `premium_pa` | `float \| None` | Aufgeld p.a. (%) |

### `WarrantScoreDetail`
The full scoring breakdown for a single warrant.

| Field | Type | Description |
|-------|------|-------------|
| `isin` | `str` | Warrant identifier |
| `total_score` | `float` | Weighted total score in [0, 10] |
| `delta_score` | `float` | Delta component score |
| `leverage_score` | `float` | Leverage component score |
| `intrinsic_score` | `float` | Intrinsic value component score |
| `spread_score` | `float` | Bid-ask spread component score |
| `premium_score` | `float` | Premium p.a. component score |
| `time_score` | `float` | Remaining time component score |
| `iv_score` | `float` | IV component score |

---

## Instrument master (`models/instrument.py`)

Instrument master data is **reference data** — slowly changing, shared across pipeline runs. It is stored in the MongoDB Atlas collection `instrument_master` and is distinct from pipeline artefacts. It provides the identifier bridge between yfinance (symbol-based) and Comdirect (ISIN/WKN/notation-ID-based).

### `TradingVenue`
One entry in an instrument's list of trading venues, as returned by the FastAPI Instrument API.

```python
class TradingVenue(BaseModel):
    venue_name: str          # e.g. "Xetra", "Tradegate", "Nasdaq"
    venue_id: str            # Comdirect internal notation ID
    venue_type: Literal["exchange", "live_trading"]
    currency: str            # ISO 4217, e.g. "EUR", "USD", "CHF"
    is_default: bool         # Comdirect's default venue for this instrument
    is_preferred_exchange: bool
    is_preferred_live_trading: bool
```

**Currency sourcing**: Comdirect does not return currency per venue. Currency is resolved via a static `venue_name → currency` mapping maintained in the FastAPI Instrument API (e.g. Xetra/Tradegate/Frankfurt → EUR, Nasdaq/NYSE → USD, SIX Swiss CHF → CHF). New venues default to `null` until mapped.

### `GlobalIdentifiers`
The set of cross-system identifiers for an instrument.

```python
class GlobalIdentifiers(BaseModel):
    isin: str                       # required; 12-char ISO 6166
    wkn: str | None                 # German WKN (6 chars); None for non-German instruments
    cusip: str | None               # US CUSIP (9 chars); derivable from ISIN for US securities
    figi: str | None                # OpenFIGI (12 chars); populated via OpenFIGI API
    ticker_yfinance: str | None     # yfinance-compatible symbol (e.g. "NVDA", "SAP.DE")
    ticker_comdirect: str | None    # Comdirect's internal short symbol (e.g. "NVD") — may differ from yfinance
```

> **Important**: `ticker_comdirect` (e.g. `"NVD"`) and `ticker_yfinance` (e.g. `"NVDA"`) are **different fields**. Comdirect uses its own short names which often differ from standard exchange symbols. Always use `ticker_yfinance` when calling yfinance; `ticker_comdirect` is informational only.

**CUSIP sourcing**: For US ISINs (`US...`), the CUSIP is characters 3–11 of the ISIN. Computed at import time, no external call needed.

**`ticker_yfinance` + FIGI sourcing**: Both are populated by a background enrichment job in the FastAPI Instrument API using the [OpenFIGI API](https://www.openfigi.com/api). The job POSTs ISINs in batches of 100 (`idType: "ID_ISIN"`), receives `ticker` + `exchCode` + `figi` per result, then derives `ticker_yfinance` via the `exchCode → suffix` map (e.g. `"GR"` → `".DE"`, `"US"` → `""`). WKN lookups use `idType: "ID_WERTPAPIER"`. All fields remain `null` until the enrichment job has run.

### `Instrument`
The full master record for one security. Stored as a MongoDB document in `instrument_master`.

```python
class Instrument(BaseModel):
    # MongoDB _id = isin (set at write time)
    global_identifiers: GlobalIdentifiers    # required
    name: str                                # Full name, e.g. "NVIDIA Corporation"
    asset_class: str                         # "Aktie", "Optionsschein", "ETF", ...
    trading_venues: list[TradingVenue]       # All venues where the instrument is tradeable
    last_updated: datetime                   # UTC; set by the FastAPI Instrument API on each sync
```

**MongoDB collection**: `instrument_master`
**`_id`**: ISIN (globally unique, stable)
**Indexes**: unique on `global_identifiers.wkn` (sparse — not all instruments have a WKN); non-unique on `global_identifiers.ticker_yfinance`

### Proposed FastAPI Instrument API response format

```json
{
  "name": "NVIDIA Corporation",
  "asset_class": "Aktie",
  "global_identifiers": {
    "isin": "US67066G1040",
    "wkn": "918422",
    "cusip": "67066G104",
    "figi": "BBG001S5N8V8",
    "ticker_yfinance": "NVDA",
    "ticker_comdirect": "NVD"
  },
  "trading_venues": [
    {
      "venue_name": "Tradegate",
      "venue_id": "9386126",
      "venue_type": "exchange",
      "currency": "EUR",
      "is_default": true,
      "is_preferred_exchange": true,
      "is_preferred_live_trading": false
    },
    {
      "venue_name": "Nasdaq",
      "venue_id": "277381",
      "venue_type": "exchange",
      "currency": "USD",
      "is_default": false,
      "is_preferred_exchange": false,
      "is_preferred_live_trading": false
    },
    {
      "venue_name": "LT Lang & Schwarz",
      "venue_id": "3240497",
      "venue_type": "live_trading",
      "currency": "EUR",
      "is_default": false,
      "is_preferred_exchange": false,
      "is_preferred_live_trading": true
    }
  ],
  "last_updated": "2026-04-03T08:00:00Z"
}
```

---

## Signal types (`models/signals.py`)

These are the typed inter-agent contracts — the "messages" passed between agents in the pipeline.

### `UniverseResult`
Output of `UniverseAgent`. Input of `ResearchAgent`.

| Field | Type | Description |
|-------|------|-------------|
| `tickers` | `list[Ticker]` | Universe deduplicated on ISIN (primary key); symbol-only fallback for entries without ISIN |
| `source` | `dict[str, str]` | ISIN → originating index name |
| `missing_isin` | `list[str]` | yfinance symbols for which no ISIN could be resolved (warning; these tickers cannot use warrant search or Comdirect data) |
| `unresolved_indices` | `list[str]` | Indices that could not be resolved |

### `ResearchResult`
Output of `ResearchAgent`. Input of `StockSelectionAgent`.

| Field | Type | Description |
|-------|------|-------------|
| `tickers` | `list[Ticker]` | Universe considered |
| `bars` | `dict[str, list[OHLCV]]` | Historical OHLCV candles keyed by symbol |

### `StockSelectionResult`
Output of `StockSelectionAgent`. Input of `WarrantSelectionAgent`.

| Field | Type | Description |
|-------|------|-------------|
| `selected` | `list[Ticker]` | Stocks with qualifying uptrends |
| `trend_status` | `dict[str, TrendStatus]` | `"established"` or `"starting"` per ticker |
| `scores` | `dict[str, float]` | Trend score per ticker (higher = stronger) |
| `rationale` | `dict[str, str]` | Human-readable reason per ticker |

### `WarrantSelectionResult`
Output of `WarrantSelectionAgent`. Input of `PortfolioConstructionAgent`.

| Field | Type | Description |
|-------|------|-------------|
| `selected_warrants` | `list[Warrant]` | Best warrant per underlying stock |
| `scores` | `dict[str, WarrantScoreDetail]` | Full score breakdown keyed by ISIN |
| `rationale` | `dict[str, str]` | Human-readable reason per ISIN |
| `no_warrant_found` | `list[Ticker]` | Stocks excluded due to no suitable warrant |

### `PortfolioProposal`
Output of `PortfolioConstructionAgent`. Input of `RiskAgent`.

| Field | Type | Description |
|-------|------|-------------|
| `positions` | `list[Position]` | Proposed warrant position sizes |
| `target_weights` | `dict[str, float]` | Target weight per ISIN |
| `new_positions` | `list[Warrant]` | Warrants not currently held (new trades) |
| `existing_positions` | `list[Warrant]` | Already held — no action needed |
| `close_positions` | `list[Position]` | Current holdings to close (not in shortlist) |

### `RiskAssessment`
Output of `RiskAgent`. Input of `TradeExecutionAgent`.

| Field | Type | Description |
|-------|------|-------------|
| `approved_positions` | `list[Position]` | Positions that passed risk checks |
| `rejected_positions` | `list[Position]` | Positions blocked by risk limits |
| `risk_notes` | `dict[str, str]` | Reason for each rejection |

### `ExecutionPlan`
Output of `TradeExecutionAgent`. Final pipeline output.

| Field | Type | Description |
|-------|------|-------------|
| `orders` | `list[Order]` | Orders ready for broker submission |
| `skipped` | `list[Position]` | Positions with no action needed |

---

## MongoDB persistence (`models/persistence.py`)

### `PipelineRun`
Top-level document in Atlas collection `pipeline_runs`. One document per pipeline invocation.

| Field | Type | Description |
|-------|------|-------------|
| `run_id` | `str` | UUID v4 |
| `started_at` | `datetime` | UTC timestamp |
| `universe_spec` | `dict` | Serialised `UniverseSpec` input |
| `config_snapshot` | `dict` | All non-secret config values at time of run |
| `stages` | `dict[str, StageRecord]` | Keyed by stage name |
| `status` | `Literal["running", "paused", "completed", "failed"]` | Current run status |

### `StageRecord`
Embedded in `PipelineRun.stages`. One record per completed stage.

| Field | Type | Description |
|-------|------|-------------|
| `stage` | `str` | Stage name (e.g. `"stock_selection"`) |
| `completed_at` | `datetime` | UTC timestamp |
| `output` | `dict` | Serialised agent output (Pydantic `.model_dump()`) |
| `mitl_status` | `Literal["pending", "approved", "rejected"]` | User review status |
| `mitl_note` | `str \| None` | Optional user comment |

