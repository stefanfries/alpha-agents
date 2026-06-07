# ADR-009 — Screening Stage: Score/Select Separation

**Date:** 2026-06-05  
**Status:** Accepted

## Context

The previous screening algorithm used a two-gate voting approach:

- **Gate 1** (fast): SuperTrend + EMA20 slope + regression slope, 2-of-3 majority vote → direction
- **Gate 2** (slow): ADX + EMA50 → confirm strength
- **Classification**: ESTABLISHED_UP / STARTING_UP / SIDEWAYS / …
- **Score (TQ)**: computed only for tickers that cleared Gate 1

This had two problems:

1. Scoring and selection were entangled in a single algorithm, making it hard to experiment with either independently.
2. Bearish tickers received a positive TQ score (bearish regression slope / ATR > 0) and appeared in `scores` but not in `selected`, causing non-consecutive rank numbers in the UI.

## Decision

Separate screening into two independent phases, applied after the hard pre-filters (market cap, bar count):

### Phase 1 — Score every ticker

All tickers that pass the pre-filters receive three computed values:

| Column | Formula | Meaning |
| ------ | ------- | ------- |
| **TQ** | `R² × slope / ATR` (60-bar) | Primary ranking score: trend linearity × normalised steepness |
| **TQ-20** | `R² × slope / ATR` (20-bar) | Short-window variant; detects early breakouts |
| **TSI** | `100 × EMA_s(EMA_f(ΔClose)) / EMA_s(EMA_f(\|ΔClose\|))` (fast=13, slow=25) | Momentum context; informational only |

TQ remains the primary sort column. TQ-20 and TSI are displayed for reference.

### Phase 2 — Select by policies (AND logic)

Four independent boolean policies, each backed by a checkbox in the UI and persisted per-run:

| Policy | Condition | Default |
| ------ | --------- | ------- |
| `policy_supertrend` | SuperTrend bullish on last bar | on |
| `policy_ema20_rising` | EMA20[-1] > EMA20[-6] | on |
| `policy_adx` | ADX[-1] > threshold AND ADX[-1] > ADX[-6] | on |
| `policy_price_above_ema50` | Close[-1] > EMA50[-1] | on |

A ticker is a **candidate** if and only if all *enabled* policies pass.  
The top-N candidates by TQ are selected into `SelectionResult`.

If 0 candidates result, the user can uncheck a policy and re-run without creating a new run.

### Policy persistence

Policy settings are stored per-run in `config_overrides.screening` in the MongoDB run document. The orchestrator merges these into `ScreeningSettings` before constructing `SecuritySelectionAgent`. This ensures full reproducibility (each run records the exact policies used).

## Consequences

- `TrendStatus` enum and `trend_status` field are removed from `SelectionResult` (the concept is replaced by the explicit `policy_results` dict).
- `ScreeningSettings` gains: `lookback_regression_short`, `tsi_fast`, `tsi_slow`, four `policy_*` bools. Removes: `allow_starting_trends`, `lookback_swing`.
- The screening UI table gains TQ-20 and TSI columns and a policy pass/fail badge per row.
- A "Screening policies" panel with checkboxes appears above the action bar; submitting it updates `config_overrides` and re-runs screening.
