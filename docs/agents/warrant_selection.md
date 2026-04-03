# Agent Spec: Warrant Selection Agent

## Responsibility

For each stock selected by the `StockSelectionAgent`, find available Call Warrants (Optionsscheine) via the FastAPI Instrument API, score them using a systematic multi-criteria model, and return a ranked shortlist of warrants per underlying. This is the third pipeline stage.

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

- `InstrumentApiTool` — queries the derivative search endpoint of the FastAPI Instrument API (`/warrants?underlying_isin=...`) to find Call Warrants for a given underlying

## Behaviour

1. For each selected underlying ticker, call the FastAPI Instrument API's derivative search endpoint to get all available Call Warrants
2. Pre-filter warrants by hard constraints (see configuration)
3. For each remaining warrant, compute the `WarrantScore` using the scoring model below
4. Select the highest-scoring warrant per underlying
5. Persist the `WarrantSelectionResult` to MongoDB Atlas for the current `run_id`

## Scoring model

The scoring model is adapted from `optionsschein_scoring.md` in the `portfolio-trend-analyzer` project. All weights are configurable.

### Criteria

| # | Criterion | Default weight | Description |
|---|-----------|---------------|-------------|
| 1 | **Delta** | 30% | Optimal range 0.5–0.7 for trend-following |
| 2 | **Leverage (Hebel)** | 20% | Target 4–10× (low leverage = less risk, still meaningful) |
| 3 | **Intrinsic value** | 15% | Prefer in-the-money warrants (innerer Wert > 0) |
| 4 | **Bid-ask spread** | 10% | Lower is better; < 2% = excellent |
| 5 | **Premium p.a. (Aufgeld)** | 10% | Cost of time value; < 20% p.a. = excellent |
| 6 | **Remaining time (Restlaufzeit)** | 10% | > 9 months preferred (avoid time decay pressure) |
| 7 | **Implied volatility** | 5% | Lower IV → cheaper premium; < 40% = excellent |

### Score tables

**Delta**

| Range | Points |
|-------|--------|
| 0.5 – 0.7 | 10 |
| 0.4–0.5 or 0.7–0.8 | 7 |
| 0.3 – 0.4 | 4 |
| < 0.3 | 0 |

**Leverage**

| Range | Points |
|-------|--------|
| 4 – 10× | 10 |
| 10 – 15× | 7 |
| 15 – 25× | 4 |
| > 25× | 0 |

**Intrinsic value**

| State | Points |
|-------|--------|
| > 0 (in the money) | 10 |
| = 0 (at/out of the money) | 0 |

**Spread**

| Range | Points |
|-------|--------|
| < 2% | 10 |
| 2 – 4% | 7 |
| 4 – 6% | 4 |
| > 6% | 0 |

**Premium p.a.**

| Range | Points |
|-------|--------|
| < 20% | 10 |
| 20 – 30% | 7 |
| 30 – 40% | 4 |
| > 40% | 0 |

**Remaining time**

| Range | Points |
|-------|--------|
| > 9 months | 10 |
| 6 – 9 months | 7 |
| 3 – 6 months | 4 |
| < 3 months | 0 |

**Implied volatility**

| Range | Points |
|-------|--------|
| < 40% | 10 |
| 40 – 60% | 7 |
| > 60% | 4 |

### Final score

$$\text{score} = \frac{\sum_i w_i \cdot p_i}{\sum_i w_i} \in [0, 10]$$

**Interpretation:**

| Score | Rating |
|-------|--------|
| 8 – 10 | Excellent (trend-following suitable) |
| 6 – 8 | Good |
| 4 – 6 | Mediocre |
| < 4 | Unsuitable |

## Hard-filter constraints (pre-scoring)

| Constraint | Default | Description |
|------------|---------|-------------|
| `warrant_min_remaining_days` | `90` | Exclude warrants expiring in < 3 months |
| `warrant_max_leverage` | `30` | Exclude extreme leverage |
| `warrant_max_spread_pct` | `8.0` | Exclude illiquid warrants |
| `warrant_min_score` | `4.0` | Exclude warrants below minimum score |
| `warrant_type` | `"call"` | Only Call Warrants (bullish trend-following strategy) |

## Configuration (via `config.py`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `warrant_scoring_weights` | See table above | Dict of criterion → weight (must sum to 1.0) |
| `warrant_candidates_per_stock` | `20` | How many warrants to fetch per underlying from Comdirect |
| `warrant_min_score` | `4.0` | Minimum score to include in output |

## Notes

- If no warrant passes the hard filters for a given underlying, that stock is listed in `no_warrant_found` — it will be excluded from portfolio construction
- The `InstrumentApiTool` warrant search endpoint must return: ISIN, WKN, delta, leverage, bid/ask, strike, expiry date, IV, issuer. These fields must be implemented in the `fastapi-azure-container-app` warrant endpoint
- Scoring weights are fully configurable — the warrant agent never hardcodes them
