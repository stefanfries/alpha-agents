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
    # Monitoring metadata (consumed by UI/portfolio flows):
    keep_existing_isins: list[str]            # Optional: incumbents explicitly marked to keep
    roll_underlyings: list[str]               # Underlyings requested for replacement search
    roll_keep_underlyings: list[str]          # Optional: roll underlyings downgraded to KEEP
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

1. For each selected underlying, call `GET /v1/warrants` with `preselection=CALL`, the underlying's ISIN, and a strike range of `current_price × strike_min_factor .. current_price × strike_max_factor` (default `0.95 .. 1.00`)
2. For ADR overrides, the `current_price` comes from the FinHub `/quotes` endpoint for the override ISIN; if the quote has no explicit last/current field, the bid/ask midprice is used instead. Otherwise it comes from the research-stage current price map.
3. Adapt strike interval width by candidate count (up to two adjustments each direction): if `<5` candidates, widen interval by doubling width; if `>50`, narrow interval by halving width.
4. If no candidates remain after adaptation, retry once with the wider fallback band `current_price × (1 ± atm_band_fallback)` (default ±10%)
5. For each candidate, call `GET /v1/warrants/{isin}` to fetch full detail. Both steps use the shared `retry_call()` helper (`app/tools/retry.py`: up to 3 attempts with exponential backoff, roughly 2 s then 4 s) for transient API errors.
6. Score all successfully fetched details using the scoring model and keep only candidates with `score > min_score`.
7. Sort by score descending; record the best warrant as `selected`, the top-3 as `top3[symbol]`, and the total detail-fetch count as `analyzed_count[symbol]`
8. Underlyings with no suitable candidate are recorded in `skipped` with a reason in `skipped_reasons` and excluded from portfolio construction

Up to 5 underlyings are processed concurrently (`asyncio.Semaphore(5)`); detail fetches share a pool of 5 concurrent connections (`asyncio.Semaphore(5)`). The detail concurrency was reduced from 10 to 5 to avoid triggering Comdirect rate limiting on the FinHub backend when processing large candidate pools (e.g. AMD with 100+ candidates).

## Scoring model

Each warrant receives a continuous composite score in `[0, 1]` from four criteria:

| # | Criterion | Weight | Function |
| - | --------- | ------ | -------- |
| 1 | **Bid-ask spread** | 40% | Linear: 0% -> 1.0, 3% -> 0.0; clamped at 0 |
| 2 | **Leverage** | 25% | Gaussian peak at 5x, sigma=3 (sweet spot 3–8x) |
| 3 | **Days to expiry** | 20% | Gaussian peak at midpoint of active maturity window (default 360 days for 9–15 months), sigma adapts with range width |
| 4 | **Delta** | 15% | Linear: peak at delta=0.5; penalty proportional to abs(delta - 0.5) |

Final score = weighted sum. The warrant with the highest score per underlying becomes `selected[i]`.

**Implementation:** Scoring logic is extracted to [app/policies/warrant_scoring.py](../../app/policies/warrant_scoring.py) with pure helper functions (`score_spread`, `score_leverage`, `score_days_to_expiry`, `score_delta`, `compute_warrant_score`, `build_warrant_rationale`) and a `WarrantScoringConfig` dataclass for centralized, reusable evaluation. See [Warrant Scoring Refactor Plan](../warrant-scoring-refactor-plan.md) for architecture and testing details.

## Configuration

**WarrantSelectionSettings** (selection filters):

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `min_days_to_expiry` | `270` | Minimum remaining life (9 months) for maturity filter |
| `max_days_to_expiry` | `450` | Maximum remaining life (15 months) for maturity filter |
| `strike_min_factor` | `0.95` | Primary strike lower bound factor (`strike_min = current_price × factor`) |
| `strike_max_factor` | `1.00` | Primary strike upper bound factor (`strike_max = current_price × factor`) |
| `min_score` | `0.0` | Minimum accepted score; only warrants with `score > min_score` are eligible |
| `atm_band_fallback` | `0.10` | Fallback strike filter half-width (±10%) |

**WarrantScoringSettings** (scoring component weights & thresholds, runtime-tunable via `.env`):

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `spread_weight` | `0.40` | Weight for spread component in composite score |
| `spread_cutoff_pct` | `3.0` | Spread % at which linear falloff reaches zero |
| `leverage_weight` | `0.25` | Weight for leverage component |
| `leverage_mean` | `5.0` | Gaussian peak leverage (sweet spot) |
| `leverage_sigma` | `3.0` | Gaussian standard deviation (range ~3–8×) |
| `days_weight` | `0.20` | Weight for days-to-expiry component |
| `days_mean` | `360` | Base/fallback Gaussian peak days; warrant selection runtime aligns target to midpoint of selected min/max maturity |
| `days_sigma` | `45.0` | Base/fallback Gaussian sigma; warrant selection runtime widens sigma for wider maturity windows |
| `delta_weight` | `0.15` | Weight for delta component |
| `delta_peak` | `0.5` | Linear peak delta (ATM calls) |
| `delta_half_width` | `0.5` | Linear falloff half-width (range ~0–1) |

**To tune scoring parameters without redeployment:** Edit `.env` with any of the above params prefixed with `WARRANT_SCORING__`, e.g.:

```bash
WARRANT_SCORING__SPREAD_WEIGHT=0.35
WARRANT_SCORING__LEVERAGE_MEAN=6.0
WARRANT_SCORING__DAYS_MEAN=360
```

Note: in warrant selection, the effective maturity target is derived from the selected maturity window shown in the UI (`target = (min + max) / 2`) and therefore adjusts automatically when the user changes min/max months.

## Web UI

The warrant selection stage page shows:

- **Status summary** (top): count of selected warrants and skipped underlyings. If `keep_existing_isins` is populated by upstream/downstream enrichment, an info box displays those incumbent ISINs.
- **Main table** (left, 55%): one row per underlying, ordered by screening TQ rank. Columns include: rank, underlying symbol, analyzed count, best warrant WKN/ISIN, strike, maturity, spread, leverage, delta, composite score, **Type** badge showing `ENTRY` (new) or `ROLL` (replacement). Optional `ROLL/KEEP` is supported when `roll_keep_underlyings` is populated.
- **Top-3 detail panel** (top-right): shows the top 3 warrants by score for the selected underlying. Clicking a warrant row triggers the stock chart.
- **Maturity controls** (below table): configurable min/max maturity in months plus a read-only target maturity field showing the scoring midpoint used for days-to-expiry.
- **Strike controls** (below table): configurable strike min/max factors plus a read-only target strike factor field showing the midpoint of the selected strike range.
- **Underlying stock chart** (bottom-right): candlestick chart with EMA20/50, SuperTrend, a horizontal price line at the strike price, and an arrow marker at the maturity date. Loaded via `GET /quant-systems/{qs_id}/executions/{execution_id}/charts/warrant_selection/{ticker}?strike={n}&maturity={date}&chart_symbol={sym}`. For ISIN-override underlyings, `chart_symbol` plots the override underlying (native currency) so candles and the strike line share one currency; otherwise the ADR/underlying symbol is charted.

Monitoring integration note:

- Monitoring classifies roll candidates and emits `roll_underlyings`.
- Replacement warrant discovery and replacement quality guardrails are applied in this stage.
