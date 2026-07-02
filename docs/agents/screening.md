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

### Phase 2 — Select by grouped NEW/BREAK policies

Policies are evaluated as indicator booleans per ticker, then grouped into two rule sets:

1. **NEW group (entry detection)** controls whether a ticker becomes a candidate for `SelectionResult.selected`.
2. **BREAK group (exit detection)** is used by the trend-signal state machine to emit `BREAK` transitions.

Rules are evaluated with k-of-n semantics via `passes_rule_group`:

- Active rules = enabled rules in the group
- If `min_true` is set, at least `min_true` active rules must be true
- If `min_true` is empty, all active rules must be true

If the NEW group passes on the latest bar, the ticker is a candidate. Candidates are sorted by TQ descending; top `top_n` are selected.

NEW group rules:

| Policy key | Condition | Default |
| ---------- | --------- | ------- |
| `policy_supertrend` | SuperTrend bullish on last bar | on |
| `policy_ema20_rising` | EMA20[-1] > EMA20[-6] | on |
| `policy_adx_above` | ADX[-1] > `min_adx` | on |
| `policy_adx_rising` | Slope of ADX[-5:] > 0 | on |
| `policy_price_above_ema50` | Close[-1] > EMA50[-1] | on |
| `policy_tq60_above` | TQ-60 > `policy_tq60_min` | off |
| `policy_tq20_above` | TQ-20 > `policy_tq20_min` | off |
| `new_min_true` | Required true count among active NEW rules (`None` = all) | `None` |

BREAK group rules:

| Policy key | Condition | Default |
| ---------- | --------- | ------- |
| `policy_supertrend_break` | SuperTrend bearish on last bar | on |
| `policy_ema20_falling_break` | EMA20 not rising | on |
| `policy_adx_below_break` | ADX[-1] <= `min_adx` | on |
| `policy_adx_falling_break` | ADX[-5:] slope <= 0 | on |
| `policy_price_below_ema50_break` | Close[-1] <= EMA50[-1] | on |
| `break_min_true` | Required true count among active BREAK rules (`None` = all) | `None` |

If zero candidates result, the user can relax NEW rules in HITL and re-run screening without creating a new run.

### Trend signal

For every ticker with sufficient bar history, the agent runs a state machine over the full bar series:

- `OUT -[NEW]-> IN_TREND -[BREAK]-> OUT`
- A `NEW` event is emitted only when the NEW rule group changes from failing to passing.
- A `BREAK` event is emitted only when the BREAK rule group changes from failing to passing while already `IN_TREND`.
- Consecutive same-direction events are impossible by construction.

The stored `trend_signals` value is then derived from the most recent event and the current state:

| Signal | Condition |
| ------ | --------- |
| `"NEW"` | The most recent signal was `NEW`, it occurred on the current bar or within the last 5 trading bars, and the ticker still passes the NEW rule group |
| `"HOLD"` | The state machine is currently `IN_TREND`, but the latest `NEW` event is older than 5 trading bars or the current bar no longer qualifies for `NEW` stickiness |
| `"BREAK"` | The most recent signal was `BREAK` and it occurred on the current bar or the immediately following trading bar |
| `None` | The state machine is `OUT` and there is no recent `BREAK` signal to expose |

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
| `policy_adx_above` | `True` | Enable ADX-above-threshold NEW rule |
| `policy_adx_rising` | `True` | Enable ADX-rising NEW rule |
| `policy_price_above_ema50` | `True` | Enable price-above-EMA50 policy |
| `policy_tq60_above` | `False` | Enable TQ-60 threshold NEW rule |
| `policy_tq20_above` | `False` | Enable TQ-20 threshold NEW rule |
| `policy_tq60_min` | `0.05` | TQ-60 threshold when `policy_tq60_above` is enabled |
| `policy_tq20_min` | `0.0` | TQ-20 threshold when `policy_tq20_above` is enabled |
| `new_min_true` | `None` | Required true NEW rules (`None` = all active NEW rules) |
| `policy_supertrend_break` | `True` | Enable SuperTrend bearish BREAK rule |
| `policy_ema20_falling_break` | `True` | Enable EMA20 falling BREAK rule |
| `policy_adx_below_break` | `True` | Enable ADX-below-threshold BREAK rule |
| `policy_adx_falling_break` | `True` | Enable ADX-falling BREAK rule |
| `policy_price_below_ema50_break` | `True` | Enable price-below-EMA50 BREAK rule |
| `break_min_true` | `None` | Required true BREAK rules (`None` = all active BREAK rules) |
