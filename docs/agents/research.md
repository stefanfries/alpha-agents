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
    bars: dict[str, list[OHLCV]]        # Keyed by ticker symbol; full OHLCV history
    fundamentals: dict[str, dict]       # Keyed by ticker symbol; raw yfinance .info dict
```

## Tools used

- `YFinanceTool` — fetches OHLCV daily bars (batch) and fundamentals (per-ticker) via yfinance

## Behaviour

1. Fetch `lookback_days` of daily OHLCV candles for all tickers in one batch call
2. Concurrently fetch fundamentals (yfinance `.info`) for each ticker (semaphore: 10 concurrent)
3. Tickers with no OHLCV data are excluded from output and logged as a warning
4. Tickers with no fundamentals are kept in output with an empty dict

## Error handling

- If a ticker returns no OHLCV data (delisted, invalid symbol), it is excluded from output
- Fundamentals fetch retries once (1 s delay) before falling back to `{}`; yfinance can return a near-empty stub dict without raising — this is treated as a failure and triggers the retry
- Partial failures do not abort the pipeline; only tickers with complete OHLCV data proceed

## Notes

- OHLCV is fetched in a single `yf.download()` batch call for efficiency
- Fundamentals are fetched concurrently with a semaphore of 10 to avoid rate-limiting
- A sufficient `lookback_days` value (≥ 200) is required to compute long-period moving averages downstream
