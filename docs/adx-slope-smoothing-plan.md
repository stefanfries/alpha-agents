# ADX Slope Smoothing Experiment Plan

**Status:** Completed -- retain the five-bar raw ADX slope; no behavior or
configuration change.
**Scope:** Trend detection only (`app/policies/trend_detection.py`).

## Hypothesis

The current `adx_rising` rule fits a linear slope over five ADX observations. A single
noisy bar can flip the rule and alter a NEW/BREAK transition. A longer or smoothed ADX
slope may reduce unstable transitions without materially delaying useful entries or
exits.

This is not an entry-timing change. It changes a component of trend-strength detection
and must remain separate from the deferred extension classification work.

## Baseline

`bar_indicator_values()` currently evaluates `adx_rising` from the slope of
`adx[idx - 4:idx + 1]`. The default NEW/BREAK policy configuration consumes the result
as a boolean policy.

## Experiment Design

Compare exactly three variants against the same resolved NASDAQ-100 and DAX universes
and date range:

1. **Baseline:** raw ADX, five-bar linear slope (current production behavior).
2. **Longer window:** raw ADX, nine-bar linear slope.
3. **Smoothed:** EMA-smoothed ADX followed by a five-bar linear slope.

For each variant, report per universe:

- NEW and BREAK event count.
- Percentage of NEW events followed by a BREAK within 5 and 10 bars.
- Median bars from a baseline NEW or BREAK event to the matching variant event.
- Number of signals that appear only in the variant or only in baseline.

The initial report is diagnostic only; it must not estimate strategy performance or
select thresholds from the same sample.

## Decision Gate

Proceed to an opt-in implementation only if a variant:

- Reduces 5-bar NEW-to-BREAK churn in both NASDAQ-100 and DAX.
- Does not delay the median matching BREAK by more than two bars.
- Does not reduce NEW events by more than 15% without a documented quality benefit.

Otherwise retain the five-bar baseline.

## Result (2026-09-20)

The diagnostic replay ran five years of history over the resolved NASDAQ-100 and DAX
universes using `scripts/analyze_adx_slope.py`.

| Universe | Variant | NEW change | 5-bar churn | 10-bar churn | Decision |
| --- | --- | ---: | ---: | ---: | --- |
| NASDAQ-100 (102 tickers) | Raw 9-bar slope | +0.4% | 15.1% | 35.2% | No improvement |
| NASDAQ-100 (102 tickers) | EMA(5) ADX + 5-bar slope | 0.0% | 14.6% | 34.8% | Marginal only |
| DAX (40 tickers) | Raw 9-bar slope | +3.1% | 17.1% | 36.8% | Worse churn |
| DAX (40 tickers) | EMA(5) ADX + 5-bar slope | +3.4% | 17.7% | 37.3% | Worse churn |

The smoothed variant's small NASDAQ-100 reduction does not replicate in DAX, and the
longer window worsens churn in both samples. Neither variant clears the pre-specified
cross-universe decision gate. Keep the current raw five-bar slope and do not add
`adx_slope_window` or smoothing configuration.

## Conditional Implementation

If the decision gate passes:

1. Add an explicit `adx_slope_window` configuration field, default `5`.
2. Keep the current default behavior exactly unchanged.
3. Do not add EMA smoothing unless the smoothed variant, rather than the longer window,
   is the validated winner.
4. Add targeted tests for the configured window and parity tests for default `5`.
5. Update chart-marker computation to consume the same configuration and rerun the full
   state-machine regression suite.

## Out of Scope

- Weighted policy votes.
- Relative strength, volume confirmation, and weekly confirmation.
- Extension-based entry gates.
- Any change to the `NEW`/`BREAK` state-machine contract.
