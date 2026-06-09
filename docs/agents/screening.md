# Agent Spec: Stock Selection (Screening) Agent

## Responsibility

Score every ticker in the research universe using three quantitative metrics, then apply a set of configurable boolean policies to select the top-N candidates for warrant selection. This is the third pipeline stage.

## Input

`ResearchResult` (output of `ResearchAgent`)

## Output

```python
class SelectionResult(BaseModel):
    selected: list[Ticker]                         # Top-N tickers that passed all policies
    all_tickers: list[Ticker]                      # Full scored universe (for HITL display)
    scores: dict[str, float]                       # Primary TQ score per ticker
    rationale: dict[str, str]                      # Human-readable summary per ticker
    tq_short: dict[str, float]                     # TQ-20 (short window) per ticker
    tsi: dict[str, float]                          # TSI (True Strength Index) per ticker
    policy_results: dict[str, dict[str, bool]]     # policy_name -> pass/fail per ticker
    rank_changes: dict[str, list[int | None]]      # sym -> [delta_1W, delta_2W, delta_4W]
    history_labels: list[str]                      # ["1W", "2W", "4W"]
    trend_signals: dict[str, str | None]           # sym -> "NEW" | "HOLD" | "BREAK" | None
```

## Tools used

None — operates purely on the OHLCV data provided by `ResearchAgent`.

## Behaviour

See [ADR-009](../decisions/ADR-009-screening-redesign.md) for the full rationale.

Processing is split into two independent phases after a hard pre-filter:

### Pre-filter (hard gates)

| Filter | Config key | Default |
| ------ | ---------- | ------- |
| Minimum market cap | `min_market_cap_eur` | 500 M EUR |
| Minimum bar count | >= 60 OHLCV bars | n/a |

Tickers failing either filter are dropped silently; they do not appear in `scores` or `all_tickers`.

---

### Phase 1 — Score all tickers

Every ticker that passes the pre-filter receives three computed values:

#### TQ — Trend Quality (primary, 60-bar)

TQ = R^2_60 * Slope_60 / ATR_20

Where:

- R^2_60 = coefficient of determination of a linear regression over the last 60 closing prices — rewards **smoothness** (0 = random walk, 1 = perfect line)
- Slope_60 = regression slope in price units per bar — rewards **upward direction and steepness**
- ATR_20 = 20-period Average True Range — **normalises** slope for per-ticker volatility

TQ can be negative (bearish trends), zero (flat), or positive. It is the primary sort column for ranking.

#### TQ-20 — Trend Quality (short window, 20-bar)

Same formula as TQ but computed over the last 20 bars instead of 60. Detects early breakouts and short-term momentum that may not yet show up in the 60-bar window. Displayed for reference alongside TQ.

#### TSI — True Strength Index

TSI = 100 * EMA_slow(EMA_fast(dClose)) / EMA_slow(EMA_fast(|dClose|))

With `fast = 13`, `slow = 25` (configurable). TSI is a bounded momentum oscillator in `[-100, 100]`. Displayed as a context signal; not used in selection.

---

### Phase 2 — Select by policies (AND logic)

Four boolean policies are evaluated independently. A ticker is a **candidate** only if all *enabled* policies pass. The top-N candidates by TQ are then selected into `SelectionResult.selected`.

| Policy key | Condition | Default |
| ---------- | --------- | ------- |
| `policy_supertrend` | SuperTrend bullish on the last bar (SuperTrend period=10, multiplier=3) | on |
| `policy_ema20_rising` | EMA20[-1] > EMA20[-6] (5-bar slope > 0) | on |
| `policy_adx` | ADX[-1] > `min_adx` AND regression slope of ADX[-5:] > 0 (rising trend) | on |
| `policy_price_above_ema50` | Close[-1] > EMA50[-1] | on |

> **ADX rising**: uses `np.polyfit` over the last 5 ADX bars rather than a simple point-to-point comparison, so a single-day dip in an otherwise rising ADX does not fail the policy.

If all policies pass for a ticker -> it is a candidate. Candidates are sorted by TQ descending; the top `top_n` (default 20) are selected.

If zero candidates result (all tickers fail at least one policy), the user can uncheck a policy in the HITL UI and re-run screening without creating a new run.

### Trend signal

For every ticker with sufficient bar history (> `_MIN_BARS + 5`), the agent also evaluates whether all enabled policies passed **5 trading days ago** (by re-running `_evaluate_policies` on `bars[:-5]`). The result is stored in `trend_signals`:

| Signal | Condition |
| ------ | --------- |
| `"NEW"` | Passes now, did **not** pass 5 days ago — trend freshly established |
| `"HOLD"` | Passes now, also passed 5 days ago — trend already established |
| `"BREAK"` | Does **not** pass now, but passed 5 days ago — trend recently broken |
| `None` | Failed both now and 5 days ago — no meaningful signal |

This signal is purely observational. It informs entry timing at the screening stage; it does not trigger orders.

### Policy persistence

Policy states are stored per-run in `config_overrides.screening` in the MongoDB run document. The orchestrator merges these into `ScreeningSettings` before constructing `SecuritySelectionAgent`. This ensures full reproducibility — each run records the exact policies used.

### Rank change tracking

The agent compares the current TQ ranking against prior rankings stored in the database (offsets: 5 bars = 1W, 10 bars = 2W, 20 bars = 4W). The result is stored in `rank_changes` as `[delta_1W, delta_2W, delta_4W]` per ticker.

---

## Configuration (`ScreeningSettings`)

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `top_n` | `20` | Maximum tickers to select |
| `min_market_cap_eur` | `500_000_000` | Hard cap filter |
| `min_adx` | `20` | Minimum ADX threshold for policy |
| `lookback_regression` | `60` | Bars for primary TQ computation |
| `lookback_regression_short` | `20` | Bars for TQ-20 computation |
| `supertrend_period` | `10` | SuperTrend ATR period |
| `supertrend_multiplier` | `3.0` | SuperTrend multiplier |
| `tsi_fast` | `13` | TSI fast EMA period |
| `tsi_slow` | `25` | TSI slow EMA period |
| `policy_supertrend` | `True` | Enable SuperTrend policy |
| `policy_ema20_rising` | `True` | Enable EMA20 rising policy |
| `policy_adx` | `True` | Enable ADX rising policy |
| `policy_price_above_ema50` | `True` | Enable price-above-EMA50 policy |
