# Agent Spec: Warrant Selection Agent

## Responsibility

For each stock selected by the `StockSelectionAgent`, find available Call Warrants (Optionsscheine) via the FinHub API, score them using a systematic multi-criteria model, and return a ranked shortlist of warrants per underlying. This is the third pipeline stage.

## Input

```python
class WarrantSelectionInput(AgentInput):
    selected_stocks: list[Ticker]                  # From StockSelectionResult
    trend_scores: dict[str, float]                 # Stock trend score per ticker
```

## Output

```python
class WarrantSelectionResult(AgentOutput):
    # One best-scoring warrant per underlying stock
    selected_warrants: list[Warrant]
    scores: dict[str, WarrantScoreDetail]          # Keyed by warrant ISIN
    rationale: dict[str, str]                      # Human-readable reason per warrant
    no_warrant_found: list[Ticker]                 # Stocks for which no suitable warrant was found
```

## Tools used

- `InstrumentApiTool` — two endpoints of the FinHub API (`fastapi-azure-container-app`):
  - `GET /v1/warrants` — warrant search/finder; returns a list of warrants for a given underlying (specified by `wkn` or `isin`) filtered by type, maturity range, and other query parameters
  - `GET /v1/warrants/{identifier}` — warrant detail by WKN or ISIN; returns full reference data, live market data, and analytics (Greeks + derived metrics)

## Behaviour

1. For each selected underlying, call `GET /v1/warrants` with `preselection=CALL`, the underlying's ISIN, and a strike range of `current_price × (1 ± 0.02)` (ATM band ±2%)
2. If the narrow band returns no candidates, retry with a wider band of `current_price × (1 ± 0.10)` (fallback ±10%)
3. For each candidate, call `GET /v1/warrants/{isin}` to fetch full reference data, market data, and analytics. Both steps use a one-retry pattern (2 s sleep) to handle transient API errors
4. Score each warrant using the scoring model below
5. Select the highest-scoring warrant per underlying
6. Underlyings with no passing warrant (neither band) are recorded in `skipped` and excluded from portfolio construction
7. Persist the `WarrantSelectionResult` to MongoDB Atlas for the current `run_id`

Up to 5 underlyings are processed concurrently (`asyncio.Semaphore(5)`); detail fetches share a pool of 10 concurrent connections (`asyncio.Semaphore(10)`).

## Scoring model

Each warrant receives a continuous score in `[0, 1]` from four criteria using Gaussian or linear penalty functions (no discrete point brackets).

| # | Criterion | Weight | Function |
| - | --------- | ------ | -------- |
| 1 | **Bid-ask spread** | 40% | Linear: 0 % → 1.0, 3 % → 0.0; clamped at 0 |
| 2 | **Leverage** | 25% | Gaussian peak at 5×, σ = 3 (sweet spot 3–8×) |
| 3 | **Days to expiry** | 20% | Gaussian peak at 315 days (midpoint of 9–12 month window), σ = 45 |
| 4 | **Delta** | 15% | Linear: peak at δ = 0.5; penalty proportional to abs(δ − 0.5) |

The final score is the weighted sum (range `[0, 1]`). The warrant with the highest score per underlying is selected.

## Hard-filter constraints (pre-scoring)

| Constraint | Default | Description |
| ---------- | ------- | ----------- |
| `warrant_type` | `"call"` | Only Call Warrants (bullish trend-following strategy) |
| `min_days_to_expiry` | `270` | Exclude warrants expiring in < 9 months |
| `max_days_to_expiry` | `365` | Exclude warrants expiring in > 12 months |
| `atm_band` | `0.02` | Strike filter: current_price × (1 ± 2%) — narrow ATM band |
| `atm_band_fallback` | `0.10` | Widened band (± 10%) retried automatically when narrow band returns nothing |

## Configuration (via `config.py`)

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `min_days_to_expiry` | `270` | Minimum remaining life (9 months) |
| `max_days_to_expiry` | `365` | Maximum remaining life (12 months) |
| `atm_band` | `0.02` | Primary strike filter half-width |
| `atm_band_fallback` | `0.10` | Fallback strike filter half-width |

## FinHub API — warrant endpoints

### `GET /v1/warrants` (search)

Returns `WarrantFinderResponse` — a list of `Warrant` objects. Key query parameters:

| Parameter | Type | Description |
| --------- | ---- | ----------- |
| `underlying_isin` / `underlying_wkn` | `str` | Underlying identifier (one required) |
| `preselection` | `WarrantPreselection` | `CALL`, `PUT`, `OTHER`, or `ALL` |
| `maturity_range` | `WarrantMaturityRange` | `Range_6M`, `Range_1Y`, `Range_2Y`, … (see enum) |

Each `Warrant` in the result contains: `isin`, `wkn`, `strike`, `strike_currency`, `ratio`, `maturity_date`, `last_trading_day`, `issuer`.

### `GET /v1/warrants/{identifier}` (detail)

Returns `WarrantDetailResponse` — full data for one warrant by WKN or ISIN:

**Reference data** (`reference_data`): isin, wkn, strike, strike_currency, ratio, maturity_date, last_trading_day, underlying_name, underlying_price, warrant_type, issuer, currency, symbol, issuer_action flags

**Market data** (`market_data`): bid, ask, spread_percent, spread_homogenized, prev_close, open, high, low, quote timestamp, venue

**Analytics** (`analytics`): delta, leverage, omega (effective leverage), implied_volatility, premium_per_annum, premium (Aufgeld %), time_value, theoretical_value, intrinsic_value, break_even, moneyness, theta, vega, gamma

All analytics fields are `Optional[float]` — the scoring model must handle `None` values gracefully (treat as the lowest score bracket).

## Notes

- If no warrant passes the hard filters for a given underlying, that stock is listed in `no_warrant_found` — it will be excluded from portfolio construction
- Scoring weights are fully configurable — the warrant agent never hardcodes them
- The detail endpoint provides the full Greek set (theta, vega, gamma, omega) — these can be used to extend the scoring model in a future iteration
