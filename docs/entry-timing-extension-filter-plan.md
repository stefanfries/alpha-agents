# Entry Timing / Trend Extension Filter — Improvement Plan

Status: **Phase 1 and Phase 2 complete; composite timing work deferred** — no entry
filter or standalone threshold has been introduced because the results do not support a
stable cutoff.
Scope: Screening stage (`SecuritySelectionAgent`, `app/policies/trend_detection.py`)
Owner: Strategy / pipeline

## Origin

Discussion (2026-08-26) about whether to chase a stock that just had a strong
breakout/earnings move (example: Regeneron/REGN at RSI ~74, well above EMA20/EMA50).
Conclusion: a strong trend is good, but a *stock that has run far away from its own
trend* is a worse NEW entry — not because the trend is broken, but because a pullback
hits a leveraged call warrant disproportionately harder than the underlying stock.

## Problem

The current NEW-entry policy chain (`TrendDetectionPolicyConfig.entry_enabled_rules()`
in `app/policies/trend_detection.py`) is purely binary: SuperTrend bullish, EMA20
rising, ADX > 20 (and rising), price > EMA50, TQ60/TQ20 above threshold, TSI above
threshold. It has no concept of "the trend is excellent, but the current price is far
above where the trend line actually is right now" (late entry / chasing).

A blunt `RSI > 70 → no entry` rule was explicitly rejected in the discussion — strong
momentum stocks can stay overbought for weeks, and that's exactly what a trend-follower
wants to catch. What is missing is a *volatility-normalized distance* measure, not a
hard oscillator cutoff.

## Core idea

Measure how far the current price has run from its own trend, normalized by ATR.
Use **ATR-20** (`timeperiod=20`) for consistency — every other ATR usage in the
codebase (TQ score in `app/agents/screening.py`/`app/agents/research.py`, and
`TrendIndicatorSeries.atr20` in `app/policies/trend_detection.py`) already uses
ATR-20, so this reuses the existing `atr20` array instead of introducing a second,
differently-parameterized ATR (e.g. the commonly-cited ATR-14) alongside it:

```python
ema20_extension_atr = (price - ema20) / atr20
```

This is a much more robust measure than a fixed percentage distance (e.g. "price >
EMA20 + 8%"), because it accounts for the stock's own typical daily volatility instead
of an absolute threshold that means different things for a low-vol pharma stock vs. a
high-vol small cap.

### Extension ≠ trend break (important constraint)

This must be implemented as a **separate informational/entry-timing signal**, not a
new exit trigger:

- It only ever gates **new entries** (screening → warrant selection), never causes a
  SELL/BREAK for an existing position. Existing BREAK/exit logic in
  `TrendDetectionPolicyConfig.exit_enabled_rules()` stays untouched.
- A high extension score with a still-excellent trend should not silently disqualify a
  stock from the watchlist — it should be visible, not blocking, in Phase 1.

## Phased approach (deliberately minimal — do not build the full multi-factor score up front)

The original discussion proposed a full weighted "Entry Timing Score" (EMA20/EMA50
extension, ATR extension, 5d/10d momentum, breakout distance, signal age — 6 weighted
components). That is over-engineered for a first step: the thresholds would be guessed,
not validated, and CLAUDE.md's simplicity-first guidance argues against building
speculative configurability before it's justified by data.

### Phase 1 — single metric, informational only (implemented 2026-09-20)

1. `SecuritySelectionAgent` computes `(close - EMA20) / ATR20` with the existing
  20-bar periods and emits finite last-bar values only.
2. `SelectionResult.extension_scores` stores the symbol → extension mapping.
3. The score remains persisted for future composite entry-timing work, but is not shown
  as a standalone Screening table column because it is not a validated confirmation.
4. The metric is not wired into `entry_enabled_rules()` or the NEW/BREAK state machine.

### Phase 2 — empirical validation (prerequisite for any hard filter)

Before adding any threshold-based gating, backtest against the system's own historical
NEW signals:

> How does the stock perform over the next 5/10/20 trading days, conditioned on its
> `ema20_extension_atr` value at the time of the NEW signal?

Only if this shows a real, non-trivial performance difference should Phase 3 proceed.
This directly avoids picking arbitrary 1.5/2.5 ATR cutoffs "from the gut."

#### Replay utility and validation result (2026-09-20)

Run the reproducible underlying-only replay with:

```powershell
uv run python scripts/analyze_entry_extension.py
uv run python scripts/analyze_entry_extension.py --index DAX
```

The default command resolves the live `NASDAQ100` universe through the same Universe
Agent used by the pipeline. The script downloads five years of OHLCV history, replays
the current NEW/BREAK policy state machine, records extension at each NEW event, and
reports 5/10/20-bar returns by data-driven extension quintile. It deliberately does not
impose extension thresholds.

The NASDAQ-100 calibration sample produced 747 NEW observations. Its lowest-extension
quintile had mean 5/10/20-day returns of 0.24% / 0.39% / 0.36%; its highest-extension
quintile had 1.23% / 3.15% / 3.35%. The DAX robustness sample produced 290 observations:
its lowest quintile had 0.08% / 0.23% / -0.78%, while its highest quintile had
-0.01% / 0.38% / -0.93%.

These results are non-monotonic and disagree across universes. They reject a simple
"high extension means no entry" rule. Do not introduce a standalone extension threshold
or state from these results. Retain the metric as a future composite timing input, where
it can be combined with a fresh trigger, trend context, and market regime. A future
retry needs a pre-specified hypothesis, market-regime split, and warrant-friction model
rather than additional threshold mining.

### Phase 3 — composite entry-timing classification (deferred)

Introduce an advisory classification only after defining and validating independent
dimensions: trend state, trend strength, trigger freshness, market regime, and extension.
Extension remains contextual evidence, not a direct gate. Possible labels include:

```text
EARLY       trend emerging, momentum improving, extension acceptable
IMMEDIATE   trend confirmed with a fresh trigger and acceptable extension
LATE        trend intact but extended without a fresh trigger or pullback
```

This classification must initially remain advisory and must not alter
`TrendDetectionPolicyConfig.entry_enabled_rules()` or the NEW/BREAK state machine.

### Explicitly deferred / not planned for now

- Signal age (`signal_age_days`) as a separate factor — plausible but a second,
  independent piece of work; revisit only after Phase 1–3 for extension are validated.
- Breakout-distance ("chase indicator") and 5d/10d momentum z-scores — same reasoning.
- Any weighted multi-factor "Entry Timing Score" (0–100) — reconsider only if a single
  ATR-extension metric proves insufficient after Phase 2 backtesting.
- Warrant-specific stricter thresholds (leveraged asymmetry: -5% stock ≈ -20% warrant)
  — worth revisiting once the stock-level filter itself is validated.

## Files likely touched (when implementation starts)

- `app/agents/screening.py` — populates `extension_scores` on `SelectionResult`
- `app/models/signals.py` — `SelectionResult.extension_scores` field
- `scripts/analyze_entry_extension.py` — replays NEW events and reports quintile outcomes
- `app/config.py` — optional threshold settings (only once Phase 3 is justified)
- `tests/` — unit tests for the ATR-extension calculation; parity tests if added to the policy engine

## Success criteria (for whenever this is picked up)

- [x] Phase 1: `ema20_extension_atr` computed and persisted with zero change to
  entry/exit decisions; removed from the standalone Screening table after validation.
- [x] Phase 2: NASDAQ-100 calibration plus DAX robustness replay completed; no stable
  extension relationship supports a threshold.
- [ ] Phase 3 (conditional): deferred. Revisit only as a composite, advisory timing
  model with a pre-specified regime-aware, warrant-friction-aware hypothesis.
