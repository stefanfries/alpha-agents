# Agent Spec: Warrant Selection Agent

## Responsibility

For each stock selected by the `SecuritySelectionAgent`, find available Call Warrants (Optionsscheine) via the FinHub API, score them using a systematic multi-criteria model, and return the best warrant plus a top-3 shortlist per underlying. This is the fourth pipeline stage.

## Input

`SelectionResult` (output of `SecuritySelectionAgent`) — the `selected` list is consumed as underlyings.

## Output

```python
class WarrantSelectionResult(BaseModel):
    selected: list[SelectedWarrant]           # Single best warrant per underlying (for portfolio)
    skipped: list[str]                        # Underlying symbols where no warrant was found
    top3: dict[str, list[SelectedWarrant]]    # Symbol -> up to 3 best warrants by score
    analyzed_count: dict[str, int]            # Symbol -> total candidates detail-fetched
```

### `SelectedWarrant`

| Field | Type | Description |
|-------|------|-------------|
| `underlying` | `Ticker` | The underlying stock |
| `warrant_isin` | `str` | Warrant ISIN |
| `warrant_wkn` | `str` | German WKN (6 chars) |
| `strike` | `float \| None` | Strike price (Basispreis) |
| `maturity_date` | `str \| None` | Maturity date as ISO-8601 string |
| `spread_pct` | `float \| None` | Bid-ask spread as % of ask |
| `leverage` | `float \| None` | Simple leverage ratio |
| `delta` | `float \| None` | Option delta |
| `bid` | `float \| None` | Bid price |
| `ask` | `float \| None` | Ask price |
| `score` | `float` | Composite score in [0, 1] |
| `rationale` | `str` | Human-readable score breakdown |

## Tools used

- `FinHubTool` — two FinHub API endpoints:
  - `GET /v1/warrants` — warrant search filtered by underlying ISIN, type, maturity range, and strike band
  - `GET /v1/warrants/{isin}` — full reference data, live market data, and analytics (Greeks)

## Behaviour

1. For each selected underlying, call `GET /v1/warrants` with `preselection=CALL`, the underlying's ISIN, and a strike range of `current_price × (1 ± atm_band)` (default ±2%)
2. If the narrow band returns no candidates, retry with the wider fallback band `current_price × (1 ± atm_band_fallback)` (default ±10%)
3. For each candidate, call `GET /v1/warrants/{isin}` to fetch full detail. Both steps use a one-retry pattern (2 s sleep) for transient API errors.
4. Score all successfully fetched details using the scoring model
5. Sort by score descending; record the best warrant as `selected`, the top-3 as `top3[symbol]`, and the total detail-fetch count as `analyzed_count[symbol]`
6. Underlyings with no candidates (neither band) are recorded in `skipped` and excluded from portfolio construction

Up to 5 underlyings are processed concurrently (`asyncio.Semaphore(5)`); detail fetches share a pool of 10 concurrent connections (`asyncio.Semaphore(10)`).

## Scoring model

Each warrant receives a continuous composite score in `[0, 1]` from four criteria:

| # | Criterion | Weight | Function |
|---|-----------|--------|----------|
| 1 | **Bid-ask spread** | 40% | Linear: 0% -> 1.0, 3% -> 0.0; clamped at 0 |
| 2 | **Leverage** | 25% | Gaussian peak at 5x, sigma=3 (sweet spot 3–8x) |
| 3 | **Days to expiry** | 20% | Gaussian peak at 315 days (midpoint of 9–12 month window), sigma=45 |
| 4 | **Delta** | 15% | Linear: peak at delta=0.5; penalty proportional to abs(delta - 0.5) |

Final score = weighted sum. The warrant with the highest score per underlying becomes `selected[i]`.

## Configuration (`WarrantSelectionSettings`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `min_days_to_expiry` | `270` | Minimum remaining life (9 months) for maturity filter |
| `max_days_to_expiry` | `365` | Maximum remaining life (12 months) for maturity filter |
| `atm_band` | `0.02` | Primary strike filter half-width (±2%) |
| `atm_band_fallback` | `0.10` | Fallback strike filter half-width (±10%) |

## Web UI

The warrant selection stage page shows:

- **Main table** (left, 55%): one row per underlying, ordered by screening TQ rank. Columns include: rank, underlying symbol, analyzed count, best warrant WKN/ISIN, strike, maturity, spread, leverage, delta, composite score. Clicking a row loads the top-3 detail panel.
- **Top-3 detail panel** (top-right): shows the top 3 warrants by score for the selected underlying. Clicking a warrant row triggers the stock chart.
- **Underlying stock chart** (bottom-right): candlestick chart with EMA20/50, SuperTrend, a horizontal price line at the strike price, and an arrow marker at the maturity date. Loaded via `GET /runs/{run_id}/charts/warrant_selection/{ticker}?strike={n}&maturity={date}`.
