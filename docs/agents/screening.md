# Agent Spec: Stock Selection Agent

## Responsibility

Analyse OHLCV candle data for all stocks in the research universe and identify those with **established uptrends** or **newly confirmed (starting) uptrends**. This is the second pipeline stage.

## Input

`ResearchResult` (output of `ResearchAgent`)

## Output

```python
class StockSelectionResult(AgentOutput):
    selected: list[Ticker]
    trend_status: dict[str, TrendStatus]   # "established" | "starting" | "none"
    scores: dict[str, float]               # Higher = stronger trend signal
    rationale: dict[str, str]              # Human-readable reason per ticker
```

Where `TrendStatus` is:

```python
class TrendStatus(str, Enum):
    ESTABLISHED = "established"   # Long-running confirmed uptrend
    STARTING    = "starting"      # New uptrend recently confirmed
    NONE        = "none"          # No qualifying trend
```

## Tools used

None — operates purely on the OHLCV data provided by `ResearchAgent`.

## Behaviour

1. For each ticker, compute trend indicators from OHLCV bars:
   - **Moving averages**: SMA20, SMA50, SMA200 (price above all three = positive signal)
   - **MA alignment**: SMA20 > SMA50 > SMA200 = established uptrend
   - **Golden cross**: SMA50 recently crossed above SMA200 = starting uptrend signal
   - **ADX**: ADX > 25 confirms trend strength
   - **Higher highs / higher lows**: structural trend confirmation
2. Classify each ticker into `TrendStatus`
3. Score each ticker (weighted combination of indicator signals)
4. Filter: only `ESTABLISHED` and `STARTING` tickers pass through
5. Rank by score and select top N
6. Persist the `StockSelectionResult` to MongoDB Atlas for the current `run_id`

## Trend scoring weights (configurable)

| Indicator | Default weight | Description |
| --------- | -------------- | ----------- |
| MA alignment (SMA20 > 50 > 200) | 35% | Core trend structure |
| ADX strength (> 25) | 25% | Trend momentum confirmation |
| Price above SMA200 | 20% | Long-term trend direction |
| Higher highs / lows (last 3 swings) | 20% | Structural confirmation |

## Configuration (via `config.py`)

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `stock_selection_top_n` | `20` | Maximum stocks to pass to warrant stage |
| `stock_selection_min_adx` | `20` | Minimum ADX to consider a trend meaningful |
| `stock_selection_lookback_swing` | `20` | Bars to look back for swing high/low detection |
| `stock_selection_allow_starting_trends` | `True` | Include `STARTING` status alongside `ESTABLISHED` |

## Notes

- All filter and scoring decisions are recorded in `rationale` for auditability
- Scoring weights are defined in configuration, not hardcoded
- The downstream `WarrantSelectionAgent` works **only** on the `selected` list from this stage — it does not re-examine the full universe
