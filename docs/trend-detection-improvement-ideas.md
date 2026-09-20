# Trend Detection Algorithm — Improvement Ideas

## Status: Ideas captured for discussion (not implemented, no plan yet)

Scope: Screening stage trend detection (`app/policies/trend_detection.py`,
`app/agents/screening.py`)

## Related plans (already captured elsewhere — not repeated here)

- `docs/entry-timing-extension-filter-plan.md` — ATR-normalized "distance from
  own trend" filter to avoid chasing extended breakouts.
- `docs/momentum-replacement-trigger-policy.md` — incumbent-vs-challenger
  momentum swaps.
- `docs/market-regime-filter-plan.md` — market regime is already computed but
  currently advisory-only, not enforced at entry.

## New ideas

### 1. Smooth the ADX-rising slope

`bar_indicator_values()` in `app/policies/trend_detection.py` fits a line over
just `adx[idx-4:idx+1]` (5 bars) to determine `adx_rising`. A single noisy bar
can flip the result from True to False. Smoothing the input (e.g. an EMA of
ADX before the slope-fit) or using a slightly longer window (8-10 bars)
would reduce false NEW/BREAK flips without changing the underlying logic or
output contract.

### 2. Weight rules instead of a flat `min_true` vote

All boolean rules (SuperTrend, EMA20-rising, ADX above/rising,
price>EMA50, TQ60/TQ20, TSI) currently count as one vote each toward
`new_min_true`/`break_min_true` (`TrendDetectionPolicyConfig`,
`passes_rule_group`). Once there is enough execution history to validate
against, each rule's individual hit-rate could be backtested and used to
move from an equal-weight threshold to a weighted vote. Bigger structural
change — only worth pursuing once enough trade history exists.

### 3. Add a relative-strength-vs-market component

Every current rule (SuperTrend, EMA, ADX, TSI, TQ) is computed on the
absolute price series. A stock trending up purely because the whole
market/index is up (beta-driven) looks identical to one with genuine
idiosyncratic strength. Research already fetches `benchmark_bars`/
`market_regime` per index. A relative TQ (`stock_TQ - benchmark_TQ`) could
be added as an optional additional NEW-policy, rewarding alpha over
beta-chasing.

### 4. Add volume confirmation

Every existing rule is price-derived only (EMA, ADX, ATR, SuperTrend, TSI).
Classic trend-following practice treats a breakout on low volume as weaker
than one on high volume. `OHLCV.volume` (`app/models/market.py`) is already
fetched via yfinance but unused in trend detection. A relative-volume filter
(e.g. volume vs its own N-day average) could be added as an optional
confirming policy, following the same pattern as the existing boolean
rules.

### 5. Multi-timeframe confirmation (weekly)

Trend detection currently uses daily bars only. A common trend-following
refinement is requiring the daily signal to align with a weekly-resampled
trend, reducing whipsaws in choppy/range-bound markets. Would use a weekly
resample of the existing daily bars — no new data source required.

## Next steps

These are discussion-stage ideas, not a committed plan. If one is prioritized,
write a dedicated plan doc (matching the style of the existing plan docs)
before implementation, including: exact formula, new config keys (matching
`TrendDetectionPolicyConfig`'s existing `policy_*` naming convention),
backward-compatibility/default-off behavior, and test coverage.
