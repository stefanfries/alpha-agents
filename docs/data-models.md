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

A current held instrument position. `quantity` is always instrument units; planned EUR BUY
amounts use `PlannedPosition.notional_eur` instead.

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
| `quantity` | `Decimal \| None` | Instrument units for a SELL; absent for BUY |
| `notional_eur` | `Decimal \| None` | EUR amount for a BUY; absent for SELL |
| `order_type` | `Literal["market", "limit"]` | Execution type |
| `limit_price` | `Decimal \| None` | Required when `order_type="limit"` |

BUY orders require `notional_eur`; SELL orders require `quantity`.

### `Warrant`

A Call Warrant (Optionsschein) with its derivative characteristics. Fields are populated from the FinHub API `GET /v1/warrants/{identifier}` response (`WarrantDetailResponse`). All analytics fields are `Optional` — the scoring model must handle `None` gracefully.

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

**Currency sourcing**: Comdirect does not return currency per venue. The FinHub API maintains a static `venue_name → currency` lookup (e.g. Xetra/Tradegate/Frankfurt → EUR, Nasdaq/NYSE → USD, SIX Swiss CHF → CHF). Unknown venues default to `null`.

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

**OpenFIGI enrichment**: `symbol_yfinance`, `figi`, and `name_openfigi` are populated by a background job in the FinHub API that batches ISINs to the OpenFIGI v3 API (`idType: "ID_ISIN"`). The job derives `symbol_yfinance` from `ticker + exchCode` using a suffix map (e.g. `"GR"` → `".DE"`, `"US"` → `""`). All fields remain `null` until the job has run.

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

### FinHub API — instrument endpoints

- `GET /v1/instruments/{identifier}` — fetch one instrument by WKN or ISIN; returns `Instrument`
- `GET /v1/instruments?symbol_yfinance={symbol}` — reverse lookup by yfinance symbol

### FinHub API response example

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

### FinHub API — `/history` endpoint

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
| `adr_isins` | `list[str]` | ISINs flagged as ADRs (`security_type == "ADR"`); warrant availability is scanned only for these (see ADR-012) |

### `ResearchResult`

Output of `ResearchAgent`. Input of `StockSelectionAgent`.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `tickers` | `list[Ticker]` | Universe considered |
| `bars` | `dict[str, list[OHLCV]]` | Historical OHLCV candles keyed by symbol |
| `fundamentals` | `dict[str, dict]` | Raw fundamentals payload per symbol (yfinance `.info`) |
| `benchmark_symbol` | `str` | Yahoo index symbol selected from dominant universe source index (for example `^NDX`) |
| `benchmark_bars` | `list[OHLCV]` | Benchmark index OHLCV bars used for regime computation |
| `market_regime` | `MarketRegime \| None` | Partial regime context (display name, TQ-60, TQ-20, status, optional breadth fields) |

### `MarketRegime`

Regime snapshot for benchmark index context in Research/Screening.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `symbol` | `str` | Yahoo benchmark symbol (for example `^NDX`) |
| `display_name` | `str` | Human-friendly index name (for example `Nasdaq 100`) |
| `tq60` | `float` | Trend quality over 60 bars |
| `tq20` | `float` | Trend quality over 20 bars |
| `status` | `"green" \| "yellow" \| "red"` | Regime classification |
| `breadth_score` | `float \| None` | Optional breadth composite score |
| `breadth_components` | `dict[str, float]` | Optional breadth metric breakdown |

### `SelectionResult`

Output of `SecuritySelectionAgent`. Input of `WarrantSelectionAgent`.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `selected` | `list[Ticker]` | Top-N tickers that passed all enabled policies, sorted by TQ descending |
| `all_tickers` | `list[Ticker]` | Full scored universe including non-selected tickers (for HITL display) |
| `scores` | `dict[str, float]` | Primary TQ score ($R^2_{60} \times Slope_{60}/ATR_{20}$) per ticker |
| `rationale` | `dict[str, str]` | Human-readable summary per ticker |
| `tq_short` | `dict[str, float]` | TQ-20 short-window score per ticker |
| `tsi` | `dict[str, float]` | True Strength Index value per ticker |
| `extension_scores` | `dict[str, float]` | `(close - EMA20) / ATR20` per ticker; retained for future composite entry-timing analysis, not a standalone policy or table signal |
| `weekly_confirmed` | `dict[str, bool]` | Latest completed weekly close is above a rising weekly EMA20; advisory only, not a NEW/BREAK policy |
| `policy_results` | `dict[str, dict[str, bool]]` | Per-ticker indicator booleans used by NEW/BREAK policy groups: `supertrend`, `supertrend_bearish`, `ema20_rising`, `ema20_falling`, `adx_above`, `adx_below`, `adx_rising`, `adx_falling`, `price_above_ema50`, `price_below_ema50`, `tq60_above`, `tq20_above` |
| `rank_changes` | `dict[str, list[int \| None]]` | Rank delta vs 1W, 2W, and 4W ago |
| `history_labels` | `list[str]` | `["1W", "2W", "4W"]` |
| `trend_signals` | `dict[str, str \| None]` | Per-ticker trend signal: `"NEW"` \| `"HOLD"` \| `"BREAK"` \| `None` (see below). `BREAK` is emitted when the active BREAK group passes while the ticker is in trend; it does not require a new edge. |
| `last_break_age_bars` | `dict[str, int]` | Per-ticker bars since the most recent BREAK event (present even when `trend_signals[symbol]` is `None`) |
| `latest_candle_dates` | `dict[str, date]` | Date of the most recent OHLCV bar per ticker |
| `previous_candle_dates` | `dict[str, date]` | Date of bars[-2] per ticker (second-to-last closed candle) |
| `market_regime` | `MarketRegime \| None` | Forwarded market regime context for Screening UI |

### `WarrantSelectionResult`

Output of `WarrantSelectionAgent`. Input of `PortfolioConstructionAgent`.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `selected` | `list[SelectedWarrant]` | Single best-scoring warrant per underlying stock (capped to `max_selected` = free slots, with backfill) |
| `skipped` | `list[str]` | Underlying symbols for which no warrant was selected |
| `skipped_reasons` | `dict[str, str]` | Symbol → human-readable skip reason (e.g. `only capped call warrants available`, `all candidates above configured spread cap`) |
| `skipped_names` | `dict[str, str]` | Symbol → underlying display name (for the skipped list) |
| `top3` | `dict[str, list[SelectedWarrant]]` | Symbol → up to 3 best warrants by score (for HITL detail panel) |
| `analyzed_count` | `dict[str, int]` | Symbol → total warrant details fetched and scored |
| `sell_existing_isins` | `list[str]` | Incumbent warrant ISINs recommended for SELL (no replacement cleared the roll score margin) |
| `roll_underlyings` | `list[str]` | Roll underlyings where a better replacement was found |
| `roll_sell_underlyings` | `list[str]` | Roll underlyings recommended for SELL (replacement below `roll_min_improvement`) |
| `roll_selected` | `list[SelectedWarrant]` | Chosen replacement warrants for confirmed rolls (kept separate from `selected`; paired with the incumbent in Portfolio/Risk/Execution) |
| `roll_incumbents` | `dict[str, RollReplacement]` | Underlying symbol → incumbent snapshot (re-scored) for the before→after UI comparison |

### `PositionReview`

Represents a single depot position under review by the Monitoring Agent. Used in `positions_to_sell`, `positions_to_keep`, and `positions_to_roll` lists inside `MonitoringResult`.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `underlying_symbol` | `str` | yfinance symbol of the underlying stock; empty string if mapping is unavailable |
| `underlying_name` | `str \| None` | Display name for the underlying (preferred universe name; fallback cached warrant-derived name) |
| `warrant_isin` | `str` | ISIN of the held warrant |
| `warrant_wkn` | `str` | WKN of the held warrant (key in depot transactions) |
| `quantity` | `Decimal \| None` | Held warrant units from the depot snapshot |
| `held_since` | `date \| None` | Held-since date from latest snapshot position (`held_since_date`); for virtual depots may fall back to most recent BUY transaction; `None` if unavailable |
| `buy_price` | `float \| None` | Average buy price (`avg_cost`) of the held position |
| `current_price` | `float \| None` | Current warrant midprice from monitoring snapshot |
| `performance_pct` | `float \| None` | Percentage performance: `((current_price - buy_price) / buy_price) * 100` |
| `sell_reason` | `Literal["exit_signal", "warrant_degraded"] \| None` | Reason for SELL decision; `None` when position is KEEP or ROLL |
| `spread_pct` | `float \| None` | Bid-ask spread as percentage (from warrant snapshot) |
| `leverage` | `float \| None` | Current leverage ratio (from warrant snapshot) |
| `delta` | `float \| None` | Delta / directional sensitivity (from warrant snapshot) |
| `days_to_maturity` | `int \| None` | Days until warrant expiry |
| `strike` | `float \| None` | Strike price of the held warrant (from warrant snapshot) |
| `maturity_date` | `date \| None` | Maturity date of the held warrant (from warrant snapshot) |
| `monitoring_score` | `float \| None` | Health score 0-1 (weighted 4-component: spread, leverage, maturity, delta) |
| `screening_signal` | `str \| None` | Resolved screening signal for mapped underlying symbol (`NEW`/`HOLD`/`BREAK`/`None`) |
| `screening_signal_present` | `bool \| None` | Whether mapped underlying symbol exists as a key in `SelectionResult.trend_signals` |
| `trend_status` | `str \| None` | UI-ready trend state (`trend intact`, `trend degraded: <reason>`, `trend degraded: <reason> (+N)`, `no signal, last BREAK X bars ago`, `no screening signal`) |
| `warrant_health_status` | `str \| None` | UI-ready health state (`healthy`, `degraded`, `unknown`) |
| `warrant_health_reason` | `str \| None` | Degradation detail text when health is degraded |
| `decision_reason` | `str \| None` | Human-readable action rationale (non-redundant with warrant-health detail in UI) |
| `roll_replacement` | `RollReplacement \| None` | Optional replacement payload (not populated by monitoring stage) |

### `MonitoringResult`

Output of `MonitoringAgent`. Consumed by `WarrantSelectionAgent` (entry candidates) and `PortfolioConstructionAgent` (kept warrant ISINs). See ADR-011.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `positions_to_sell` | `list[PositionReview]` | Positions with a confirmed trend exit (active `BREAK`) or an aged-out BREAK (`trend_signal is None` with a known `last_break_age_bars` for the mapped symbol) |
| `positions_to_keep` | `list[PositionReview]` | Incumbent positions with no exit trigger and/or degraded-but-not-roll-eligible positions |
| `positions_to_roll` | `list[PositionReview]` | Degraded warrants classified as roll candidates (replacement selection occurs downstream) |
| `entry_candidates` | `list[Ticker]` | All eligible screening candidates (rank order) for new entry this run; **not** capped to `free_positions` — warrant selection enforces the slot cap (`max_selected`) with backfill |
| `free_positions` | `int` | `max_positions − len(current_holdings) + len(positions_to_sell)` (`Free now`, including confirmed sells) |
| `excluded_symbols` | `list[str]` | Held or recently sold underlying symbols blocked from entry in this run |
| `reentry_blocked_symbols` | `set[str]` | Recent virtual-depot SELL underlyings blocked for the configured prevention window |
| `nav_eur` | `Decimal \| None` | Total NAV from current EUR cash plus fresh held-warrant bid values; absent if incomplete |
| `available_cash_eur` | `Decimal \| None` | Current free EUR cash |
| `valuation_errors` | `list[str]` | Missing or invalid cash/quote valuation details shown in Monitoring |

### `PortfolioProposal`

Output of `PortfolioConstructionAgent`. Input of `RiskAgent`.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `positions` | `list[PlannedPosition]` | Proposed warrant EUR notionals |
| `target_weights` | `dict[str, float]` | Proposed notional as a share of NAV, keyed by warrant symbol |
| `new_positions` | `list[PlannedPosition]` | Proposed BUYs not currently held |
| `existing_positions` | `list[PlannedPosition]` | Selected positions already held |
| `close_positions` | `list[Position]` | Current holdings to close (not in shortlist) |
| `roll_trades` | `list[RollTrade]` | Confirmed incumbent/replacement pairs |
| `account_snapshot` | `PortfolioAccountSnapshot \| None` | Current NAV, cash, held-position values, and quote validity |
| `standard_buy_amount_eur` | `Decimal \| None` | Shared equal-size amount for each BUY |
| `expected_net_sell_proceeds_eur` | `Decimal \| None` | Expected proceeds from planned SELLs after modeled sell costs |
| `cost_reserve_eur` | `Decimal \| None` | Estimated transaction fees and slippage |
| `sizing_blocked_reason` | `str \| None` | Why no risk-increasing notional was produced |

### `PlannedPosition`

A proposed BUY amount, distinct from a held position's instrument-unit quantity.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `ticker` | `Ticker` | Warrant to buy |
| `notional_eur` | `Decimal` | Planned BUY amount in EUR |
| `target_weight` | `float` | Notional as a share of current NAV |
| `underlying_isin` | `str \| None` | Canonical underlying identity |
| `underlying_symbol` | `str \| None` | Supporting/display symbol |
| `sector` | `str \| None` | Sector used for concentration checks |
| `issuer_action` | `bool` | Comdirect issuer-action fee exception |
| `issuer_no_fee_action` | `bool` | Comdirect no-fee issuer action; takes precedence |

### `PortfolioAccountSnapshot`

Current account valuation input to Portfolio and Risk. Held-warrant values use fresh FinHub
bid quotes in EUR; invalid or stale values are represented as valuation errors and must not be
treated as zero.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `source` | `Literal["real", "virtual"]` | Depot source |
| `available_cash_eur` | `Decimal \| None` | Current available cash in EUR |
| `holdings` | `list[PortfolioHoldingValue]` | Current held positions with marks and underlying metadata |
| `nav_eur` | `Decimal \| None` | Cash plus held-position bid values; absent if incomplete |
| `recorded_at_utc` | `datetime \| None` | Snapshot assembly time |
| `valuation_errors` | `list[str]` | Missing/invalid account valuation data |

`PortfolioHoldingValue` includes the held `Position`, canonical `underlying_isin`, optional
`underlying_symbol` and `sector`, `bid_price_eur`, `market_value_eur`, `quote_timestamp_utc`,
`issuer_action`, `issuer_no_fee_action`, and `quote_error`.

### `RollTrade`

Paired replacement of a held warrant. The incumbent is a held `Position`; the replacement is
a `PlannedPosition` sized to the standard BUY amount. Risk and Execution keep the pair atomic.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `incumbent` | `Position` | Held warrant to sell |
| `replacement` | `PlannedPosition` | Selected warrant and EUR notional to buy |
| `target_weight` | `float` | Replacement notional as a share of NAV |

### `RiskAssessment`

Output of `RiskAgent`. Input of `TradeExecutionAgent`.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `approved_positions` | `list[PlannedPosition]` | EUR notionals that passed risk checks |
| `rejected_positions` | `list[PlannedPosition]` | EUR notionals blocked by risk limits |
| `risk_notes` | `dict[str, str]` | Reason for each rejection |
| `close_positions` | `list[Position]` | Positions carried through for SELL order generation |
| `approved_roll_trades` | `list[RollTrade]` | Roll pairs approved independently of new-entry slots |
| `rejected_roll_trades` | `list[RollTrade]` | Roll pairs rejected by risk checks |
| `portfolio_warnings` | `list[str]` | Existing over-limit position/sector warnings; no automatic SELL |

### `ExecutionPlan`

Output of `TradeExecutionAgent`. Final pipeline output.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `orders` | `list[Order]` | Orders ready for broker submission |
| `skipped` | `list[PlannedPosition]` | Planned BUY notionals below the minimum trade size |

---

## MongoDB persistence

### Finance snapshot integration assumptions (2026-07-20 schema)

- Canonical position fields are `average_purchase_price`, `purchase_price_at_entry`, and `held_since_date`.
- Legacy fields `purchase_price` and `buy_price_at_entry` are treated as schema violations and should fail fast in runtime parsing.
- Numeric amount values are stored as strings in nested amount objects (for example `{"value": "123.45", "unit": "EUR"}`) and are explicitly converted to `Decimal` before calculations.
- `held_since_date` and `purchase_price_at_entry` can be `null`; consumers must handle missing values without crashing.
- Current holdings are always read from the latest snapshot per depot (`max(recorded_at)`).
- Real-depot free cash is the sum of the newest EUR `account_balances` record for each of
  `Girokonto`, `Tagesgeld PLUS-Konto`, and `Verrechnungskonto`, selected by `account_type`.
  Unchanged balances may have older `recorded_at` values and remain valid until superseded.

### `Execution` document (`executions` collection)

Top-level document in Atlas collection `executions`. One document per Quant System execution.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `execution_id` | `str` | Short execution identifier (6-char hex) |
| `quant_system_id` | `str` | Parent Quant System identifier |
| `created_at` | `datetime` | UTC timestamp |
| `indices` | `list[str]` | Snapshot from Quant System at execution start |
| `capital_eur` | `float` | Snapshot from Quant System at execution start |
| `hitl_mode` | `bool` | Whether stage approvals are required |
| `config_overrides` | `dict` | Per-stage runtime override values |
| `current_stage` | `str` | Active stage name |
| `status` | `Literal["running", "awaiting_review", "complete", "error"]` | Execution status |
| `stages` | `dict[str, StageState]` | Stage state map keyed by stage name |

### `StageState`

Embedded in `Execution.stages`. One record per stage in the stage sequence.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `status` | `Literal["pending", "running", "awaiting_review", "approved", "error"]` | Stage lifecycle status |
| `result` | `dict \| None` | Serialized stage output (`model_dump(mode="json")`) |
| `error` | `str \| None` | Traceback text when status is `error` |
| `progress` | `dict \| None` | Optional live progress payload for long-running stages |
