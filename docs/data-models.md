# Data Model Reference

All models are Pydantic V2. Shared domain types live in `models/`; they are imported by agents and tools — never defined inline.

---

## Market types (`models/market.py`)

### `Ticker`

A lightweight reference to a security, used as a key throughout the pipeline. The canonical identifier is the yfinance-compatible symbol. Rich instrument data (ISIN, WKN, venues) lives in the `Instrument` master document.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `symbol` | `str` | yfinance-compatible ticker (e.g. `"NVDA"`, `"SAP.DE"`) |
| `isin` | `str \| None` | ISIN — used to look up the instrument master record |

### `OHLCV`

One daily candlestick bar.

| Field | Type | Description |
| ----- | ---- | ----------- |
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
| ----- | ---- | ----------- |
| `ticker` | `Ticker` | The security |
| `quantity` | `Decimal` | Number of shares/units (negative = short) |
| `avg_cost` | `Decimal` | Average cost basis per unit |

### `Order`

A trade instruction produced by the Execution Agent.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `ticker` | `Ticker` | Security to trade |
| `side` | `Literal["buy", "sell"]` | Direction |
| `quantity` | `Decimal` | Units to trade |
| `order_type` | `Literal["market", "limit"]` | Execution type |
| `limit_price` | `Decimal \| None` | Required when `order_type="limit"` |

### `Warrant`

A Call Warrant (Optionsschein) with its derivative characteristics. Fields are populated from the FastAPI Instrument API `GET /v1/warrants/{identifier}` response (`WarrantDetailResponse`). All analytics fields are `Optional` — the scoring model must handle `None` gracefully.

#### Identifiers & reference data

| Field | Type | Description |
| ----- | ---- | ----------- |
| `isin` | `str` | Warrant ISIN |
| `wkn` | `str \| None` | German WKN (6 chars) |
| `underlying` | `Ticker` | The underlying stock |
| `issuer` | `str \| None` | Issuing bank (e.g. `"Deutsche Bank"`) |
| `warrant_type` | `str \| None` | e.g. `"Call (Amer.)"` |
| `strike` | `Decimal \| None` | Strike price (Basispreis) |
| `strike_currency` | `str \| None` | Currency of strike price |
| `expiry` | `date \| None` | Expiry / maturity date |
| `last_trading_day` | `date \| None` | Last day the warrant can be traded |
| `ratio` | `str \| None` | Bezugsverhältnis (e.g. `"10 : 1"`) |
| `currency` | `str \| None` | Settlement currency |

#### Market data

| Field | Type | Description |
| ----- | ---- | ----------- |
| `bid` | `Decimal \| None` | Bid (Geld) price |
| `ask` | `Decimal \| None` | Ask (Brief) price |
| `spread_percent` | `float \| None` | Bid-ask spread as % of ask |
| `venue` | `str \| None` | Trading venue |

#### Analytics (Greeks & derived metrics)

| Field | Type | Description |
| ----- | ---- | ----------- |
| `delta` | `float \| None` | Option delta |
| `leverage` | `float \| None` | Hebel (simple leverage ratio) |
| `omega` | `float \| None` | Omega — effective leverage (delta × leverage) |
| `iv` | `float \| None` | Implied volatility (%) |
| `premium_pa` | `float \| None` | Aufgeld p.a. (%) — annualised cost of time value |
| `premium` | `float \| None` | Aufgeld (%) — absolute time value premium |
| `intrinsic_value` | `float \| None` | Innerer Wert |
| `time_value` | `float \| None` | Zeitwert |
| `theoretical_value` | `float \| None` | Theoretical fair value |
| `break_even` | `float \| None` | Break-even price of the underlying |
| `moneyness` | `float \| None` | Moneyness |
| `theta` | `float \| None` | Theta — time decay per day |
| `vega` | `float \| None` | Vega — sensitivity to IV change |
| `gamma` | `float \| None` | Gamma — rate of change of delta |

### `WarrantScoreDetail`

The full scoring breakdown for a single warrant.

| Field | Type | Description |
| ----- | ---- | ----------- |
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

### `VenueInfo`

A single trading venue entry combining the Comdirect internal notation ID with the inferred currency.

```python
class VenueInfo(BaseModel):
    id_notation: str            # Comdirect internal ID_NOTATION for this venue
    currency: str | None        # ISO 4217 (e.g. "EUR", "USD"); None if venue not in lookup table
```

**Currency sourcing**: Comdirect does not return currency per venue. The FastAPI Instrument API maintains a static `venue_name → currency` lookup (e.g. Xetra/Tradegate/Frankfurt → EUR, Nasdaq/NYSE → USD, SIX Swiss CHF → CHF). Unknown venues default to `null`.

### `GlobalIdentifiers`

Consolidated cross-system identifiers for an instrument, populated via OpenFIGI enrichment.

```python
class GlobalIdentifiers(BaseModel):
    isin: str | None            # 12-char ISO 6166 ISIN; validated via Luhn checksum
    wkn: str                    # German WKN (6 chars); required primary key
    cusip: str | None           # US CUSIP (9 chars); derived from ISIN chars 3–11 for US securities
    figi: str | None            # Composite FIGI from OpenFIGI (e.g. "BBG001S5N8V8")
    symbol_comdirect: str | None  # Ticker as displayed on comdirect.de (e.g. "NVD")
    symbol_yfinance: str | None   # Yahoo Finance-compatible ticker (e.g. "NVDA", "SIE.DE")
    name_openfigi: str | None     # Instrument name returned by OpenFIGI (e.g. "NVIDIA CORP")
```

> **`symbol_comdirect` ≠ `symbol_yfinance`**: Comdirect uses its own short names that frequently differ from exchange ticker symbols. Always use `symbol_yfinance` for yfinance calls; `symbol_comdirect` is informational only. `symbol_yfinance` is `None` for asset classes not supported by Yahoo Finance (Warrant, Certificate).

**OpenFIGI enrichment**: `symbol_yfinance`, `figi`, and `name_openfigi` are populated by a background job in the FastAPI Instrument API that batches ISINs to the OpenFIGI v3 API (`idType: "ID_ISIN"`). The job derives `symbol_yfinance` from `ticker + exchCode` using a suffix map (e.g. `"GR"` → `".DE"`, `"US"` → `""`). All fields remain `null` until the job has run.

### `Instrument`

The full master record for one security, as returned by `GET /v1/instruments/{wkn_or_isin}`.

```python
class Instrument(BaseModel):
    name: str                                               # e.g. "NVIDIA Corporation"
    wkn: str                                                # WKN — primary key
    isin: str | None                                        # ISIN (Luhn-validated)
    asset_class: AssetClass                                 # Stock, Warrant, ETF, Bond, …
    global_identifiers: GlobalIdentifiers | None            # OpenFIGI-enriched identifiers
    id_notations_exchange_trading: dict[str, VenueInfo] | None   # venue_name → VenueInfo
    id_notations_life_trading: dict[str, VenueInfo] | None       # venue_name → VenueInfo
    preferred_id_notation_exchange_trading: str | None      # preferred notation ID for exchange orders
    preferred_id_notation_life_trading: str | None          # preferred notation ID for live trading
    default_id_notation: str | None                         # Comdirect default notation ID
```

**MongoDB collection**: `instrument_master`
**Primary key**: WKN (required on all instruments). ISIN is present for most instruments but not all.
**Indexes**: unique sparse on `global_identifiers.symbol_yfinance`; unique sparse on `isin`.

### FastAPI Instrument API — instrument endpoints

- `GET /v1/instruments/{identifier}` — fetch one instrument by WKN or ISIN; returns `Instrument`
- `GET /v1/instruments?symbol_yfinance={symbol}` — reverse lookup by yfinance symbol

### FastAPI Instrument API response example

```json
{
  "name": "NVIDIA Corporation",
  "wkn": "918422",
  "isin": "US67066G1040",
  "asset_class": "Stock",
  "global_identifiers": {
    "isin": "US67066G1040",
    "wkn": "918422",
    "cusip": "67066G104",
    "figi": "BBG001S5N8V8",
    "symbol_comdirect": "NVD",
    "symbol_yfinance": "NVDA",
    "name_openfigi": "NVIDIA CORP"
  },
  "id_notations_exchange_trading": {
    "Tradegate":  { "id_notation": "9386126", "currency": "EUR" },
    "Nasdaq":     { "id_notation": "277381",  "currency": "USD" }
  },
  "id_notations_life_trading": {
    "LT Lang & Schwarz": { "id_notation": "3240497", "currency": "EUR" }
  },
  "preferred_id_notation_exchange_trading": "9386126",
  "preferred_id_notation_life_trading": "3240497",
  "default_id_notation": "3240497"
}
```

### FastAPI Instrument API — `/history` endpoint

`GET /v1/history/{identifier}` returns historical OHLCV data for any instrument type (stocks, warrants, ETFs, etc.) identified by WKN or ISIN.

| Query parameter | Type | Description |
| --------------- | ---- | ----------- |
| `id_notation` | `str` | Comdirect notation ID specifying the venue; obtain from `VenueInfo.id_notation` |

The currency of the returned price series matches the venue's currency (e.g. EUR for Tradegate, USD for Nasdaq). This is the only source of historical price data for warrants — yfinance does not carry warrant price history.

---

## Signal types (`models/signals.py`)

These are the typed inter-agent contracts — the "messages" passed between agents in the pipeline.

### `UniverseResult`

Output of `UniverseAgent`. Input of `ResearchAgent`.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `tickers` | `list[Ticker]` | Universe deduplicated on ISIN (primary key); symbol-only fallback for entries without ISIN |
| `source` | `dict[str, str]` | ISIN → originating index name |
| `missing_isin` | `list[str]` | yfinance symbols for which no ISIN could be resolved (warning; these tickers cannot use warrant search or Comdirect data) |
| `unresolved_indices` | `list[str]` | Indices that could not be resolved |

### `ResearchResult`

Output of `ResearchAgent`. Input of `StockSelectionAgent`.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `tickers` | `list[Ticker]` | Universe considered |
| `bars` | `dict[str, list[OHLCV]]` | Historical OHLCV candles keyed by symbol |

### `StockSelectionResult`

Output of `StockSelectionAgent`. Input of `WarrantSelectionAgent`.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `selected` | `list[Ticker]` | Stocks with qualifying uptrends |
| `trend_status` | `dict[str, TrendStatus]` | `"established"` or `"starting"` per ticker |
| `scores` | `dict[str, float]` | Trend score per ticker (higher = stronger) |
| `rationale` | `dict[str, str]` | Human-readable reason per ticker |

### `WarrantSelectionResult`

Output of `WarrantSelectionAgent`. Input of `PortfolioConstructionAgent`.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `selected_warrants` | `list[Warrant]` | Best warrant per underlying stock |
| `scores` | `dict[str, WarrantScoreDetail]` | Full score breakdown keyed by ISIN |
| `rationale` | `dict[str, str]` | Human-readable reason per ISIN |
| `no_warrant_found` | `list[Ticker]` | Stocks excluded due to no suitable warrant |

### `PortfolioProposal`

Output of `PortfolioConstructionAgent`. Input of `RiskAgent`.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `positions` | `list[Position]` | Proposed warrant position sizes |
| `target_weights` | `dict[str, float]` | Target weight per ISIN |
| `new_positions` | `list[Warrant]` | Warrants not currently held (new trades) |
| `existing_positions` | `list[Warrant]` | Already held — no action needed |
| `close_positions` | `list[Position]` | Current holdings to close (not in shortlist) |

### `RiskAssessment`

Output of `RiskAgent`. Input of `TradeExecutionAgent`.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `approved_positions` | `list[Position]` | Positions that passed risk checks |
| `rejected_positions` | `list[Position]` | Positions blocked by risk limits |
| `risk_notes` | `dict[str, str]` | Reason for each rejection |

### `ExecutionPlan`

Output of `TradeExecutionAgent`. Final pipeline output.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `orders` | `list[Order]` | Orders ready for broker submission |
| `skipped` | `list[Position]` | Positions with no action needed |

---

## MongoDB persistence (`models/persistence.py`)

### `PipelineRun`

Top-level document in Atlas collection `pipeline_runs`. One document per pipeline invocation.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `run_id` | `str` | UUID v4 |
| `started_at` | `datetime` | UTC timestamp |
| `universe_spec` | `dict` | Serialised `UniverseSpec` input |
| `config_snapshot` | `dict` | All non-secret config values at time of run |
| `stages` | `dict[str, StageRecord]` | Keyed by stage name |
| `status` | `Literal["running", "paused", "completed", "failed"]` | Current run status |

### `StageRecord`

Embedded in `PipelineRun.stages`. One record per completed stage.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `stage` | `str` | Stage name (e.g. `"stock_selection"`) |
| `completed_at` | `datetime` | UTC timestamp |
| `output` | `dict` | Serialised agent output (Pydantic `.model_dump()`) |
| `mitl_status` | `Literal["pending", "approved", "rejected"]` | User review status |
| `mitl_note` | `str \| None` | Optional user comment |
