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
| ----- | ---- | ----------- |
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
| `issuer_action` | `bool` | Issuer promo flag ("Aktion") |
| `issuer_no_fee_action` | `bool` | Issuer fee-free promo flag |
| `chart_symbol` | `str \| None` | yfinance symbol to chart for the strike currency; set only for ISIN-override underlyings (e.g. `ASML.AS`), otherwise `None` |

## Tools used

- `FinHubTool` — four FinHub API endpoints:
  - `GET /v1/quotes/{isin}` — live quote for override underlyings; used to anchor the strike band in the warrant's strike currency
  - `GET /v1/warrants` — warrant search filtered by underlying ISIN, type, maturity range, and strike band
  - `GET /v1/warrants/{isin}` — full reference data, live market data, and analytics (Greeks)
  - `GET /v1/instruments/{isin}` — resolves an override underlying's yfinance symbol (for the chart)

## ISIN overrides (ADRs)

For an ADR whose warrants are written on a different underlying (e.g. the ASML ADR
`USN070592100`, whose EUR-listed stock `NL0010273215` carries the warrants), a persisted
manual override (see ADR-012) redirects **warrant lookup only**. When an override is active
for a ticker:

- `get_warrants` / `get_warrant_detail` use the **override ISIN**, not the ADR's.
- The strike band is derived from the **override underlying's live native-currency quote**
  fetched from the FinHub `/quotes` endpoint — **no FX conversion**. The agent first uses a
  direct last/current-price field (`currentPrice`, `price`, `lastPrice`, `last`, `close`), and
  falls back to the bid/ask midprice when the quote payload only exposes `bid` and `ask`.
  Using the ADR's `currentPrice` would be the wrong currency and magnitude (USD vs EUR).
- `chart_symbol` is set to the override underlying's yfinance symbol (e.g. `ASML.AS`) so the
  warrant-selection chart plots candles in the same currency as the strike line.

The ADR remains the analyzed instrument everywhere else (research, screening, fundamentals,
name); only warrant sourcing and the strike chart follow the override.

## Behaviour

1. For each selected underlying, call `GET /v1/warrants` with `preselection=CALL`, the underlying's ISIN, and a strike range of `current_price × (1 ± atm_band)` (default ±2%)
2. For ADR overrides, the `current_price` comes from the FinHub `/quotes` endpoint for the override ISIN; if the quote has no explicit last/current field, the bid/ask midprice is used instead. Otherwise it comes from the research-stage current price map.
3. If the narrow band returns no candidates, retry with the wider fallback band `current_price × (1 ± atm_band_fallback)` (default ±10%)
4. For each candidate, call `GET /v1/warrants/{isin}` to fetch full detail. Both steps use the shared `retry_call()` helper (`app/tools/retry.py`: 1 retry, 2 s wait) for transient API errors.
5. Score all successfully fetched details using the scoring model
6. Sort by score descending; record the best warrant as `selected`, the top-3 as `top3[symbol]`, and the total detail-fetch count as `analyzed_count[symbol]`
7. Underlyings with no candidates (neither band) are recorded in `skipped` and excluded from portfolio construction

Up to 5 underlyings are processed concurrently (`asyncio.Semaphore(5)`); detail fetches share a pool of 10 concurrent connections (`asyncio.Semaphore(10)`).

## Scoring model

Each warrant receives a continuous composite score in `[0, 1]` from four criteria:

| # | Criterion | Weight | Function |
| - | --------- | ------ | -------- |
| 1 | **Bid-ask spread** | 40% | Linear: 0% -> 1.0, 3% -> 0.0; clamped at 0 |
| 2 | **Leverage** | 25% | Gaussian peak at 5x, sigma=3 (sweet spot 3–8x) |
| 3 | **Days to expiry** | 20% | Gaussian peak at 315 days (midpoint of 9–12 month window), sigma=45 |
| 4 | **Delta** | 15% | Linear: peak at delta=0.5; penalty proportional to abs(delta - 0.5) |

Final score = weighted sum. The warrant with the highest score per underlying becomes `selected[i]`.

## Configuration (`WarrantSelectionSettings`)

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `min_days_to_expiry` | `270` | Minimum remaining life (9 months) for maturity filter |
| `max_days_to_expiry` | `365` | Maximum remaining life (12 months) for maturity filter |
| `atm_band` | `0.02` | Primary strike filter half-width (±2%) |
| `atm_band_fallback` | `0.10` | Fallback strike filter half-width (±10%) |

## Web UI

The warrant selection stage page shows:

- **Main table** (left, 55%): one row per underlying, ordered by screening TQ rank. Columns include: rank, underlying symbol, analyzed count, best warrant WKN/ISIN, strike, maturity, spread, leverage, delta, composite score. Clicking a row loads the top-3 detail panel.
- **Top-3 detail panel** (top-right): shows the top 3 warrants by score for the selected underlying. Clicking a warrant row triggers the stock chart.
- **Underlying stock chart** (bottom-right): candlestick chart with EMA20/50, SuperTrend, a horizontal price line at the strike price, and an arrow marker at the maturity date. Loaded via `GET /runs/{run_id}/charts/warrant_selection/{ticker}?strike={n}&maturity={date}&chart_symbol={sym}`. For ISIN-override underlyings, `chart_symbol` plots the override underlying (native currency) so candles and the strike line share one currency; otherwise the ADR/underlying symbol is charted.
