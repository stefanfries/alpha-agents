# Entry Timing / Trend Extension Filter — Improvement Plan

Status: **Not started** — idea captured for later implementation, no code changes yet.
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

### Phase 1 — single metric, informational only (recommended starting point)

1. Add `ema20_extension_atr` to `TrendIndicatorSeries` (or compute alongside it) using
   the existing `atr20` array — no new indicator library calls needed.
2. Surface it in `SelectionResult` as a new field, e.g.
   `extension_scores: dict[str, float]` (symbol → last-bar extension value), populated
   in `SecuritySelectionAgent`.
3. Display it in the Screening UI (`app/templates/stages/screening.html`) as an extra
   column or badge — no behavior change, no filtering.
4. **Do not** wire this into `entry_enabled_rules()` / the NEW policy chain yet.

### Phase 2 — empirical validation (prerequisite for any hard filter)

Before adding any threshold-based gating, backtest against the system's own historical
NEW signals:

> How does the stock perform over the next 5/10/20 trading days, conditioned on its
> `ema20_extension_atr` value at the time of the NEW signal?

Only if this shows a real, non-trivial performance difference should Phase 3 proceed.
This directly avoids picking arbitrary 1.5/2.5 ATR cutoffs "from the gut."

### Phase 3 — soft entry-timing state (only if Phase 2 justifies it)

Introduce a three-state classification, non-blocking by default:

```text
NORMAL          extension < threshold_low   (e.g. < 1.5 ATR)
EXTENDED        threshold_low <= extension < threshold_high
VERY_EXTENDED   extension >= threshold_high (e.g. >= 2.5 ATR)
```

Possible integration point: an additional optional policy in
`TrendDetectionPolicyConfig.entry_enabled_rules()` (config-gated, default `False` /
disabled) so it can be toggled on per quant system without affecting existing runs,
consistent with how other policies in that file are already individually toggleable.

### Explicitly deferred / not planned for now

- Signal age (`signal_age_days`) as a separate factor — plausible but a second,
  independent piece of work; revisit only after Phase 1–3 for extension are validated.
- Breakout-distance ("chase indicator") and 5d/10d momentum z-scores — same reasoning.
- Any weighted multi-factor "Entry Timing Score" (0–100) — reconsider only if a single
  ATR-extension metric proves insufficient after Phase 2 backtesting.
- Warrant-specific stricter thresholds (leveraged asymmetry: -5% stock ≈ -20% warrant)
  — worth revisiting once the stock-level filter itself is validated.

## Files likely touched (when implementation starts)

- `app/policies/trend_detection.py` — `TrendIndicatorSeries`, `build_trend_indicator_series`, optional new policy field
- `app/agents/screening.py` — populate `extension_scores` on `SelectionResult`
- `app/models/signals.py` — new `SelectionResult.extension_scores` field
- `app/templates/stages/screening.html` — display extension value/badge
- `app/config.py` — optional threshold settings (only once Phase 3 is justified)
- `tests/` — unit tests for the ATR-extension calculation; parity tests if added to the policy engine

## Success criteria (for whenever this is picked up)

- [ ] Phase 1: `ema20_extension_atr` computed and visible in Screening UI, zero change
      to entry/exit decisions.
- [ ] Phase 2: backtest report showing forward returns bucketed by extension at NEW
      signal time (documented, not necessarily code).
- [ ] Phase 3 (conditional): configurable, opt-in soft entry-timing classification with
      tests, default-disabled so existing quant systems are unaffected.
