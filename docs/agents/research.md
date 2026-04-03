# Agent Spec: Research Agent

## Responsibility

Gather OHLCV candle data for all stocks in the investment universe. This is the **first pipeline stage** — it receives the universe resolved by the `UniverseAgent`.

## Input

```python
class ResearchInput(AgentInput):
    tickers: list[Ticker]       # From UniverseResult.tickers
    lookback_days: int = 365    # How many days of daily OHLCV history to fetch
```

## Output

```python
class ResearchResult(AgentOutput):
    tickers: list[Ticker]
    bars: dict[str, list[OHLCV]]   # Keyed by ticker symbol; full OHLCV history
```

## Tools used

- `YFinanceTool` — fetches OHLCV daily bars for each symbol

## Behaviour

1. For each ticker, fetch `lookback_days` of daily OHLCV candles
2. Return all bars; no filtering, scoring, or fundamental data at this stage
3. Persist the `ResearchResult` to MongoDB Atlas for the current `run_id`

## Error handling

- If a ticker returns no data (delisted, invalid symbol), it is excluded from output and logged as a warning
- Partial failures do not abort the pipeline; only tickers with complete data proceed

## Notes

- Data is fetched concurrently using `asyncio.gather()` across tickers
- This agent does not make investment decisions — it only collects OHLCV candles
- Fundamental metrics are not required here; the trend-detection focus of the downstream `StockSelectionAgent` operates entirely on price and volume data
- A sufficient `lookback_days` value (≥ 200) is required to compute long-period moving averages downstream
