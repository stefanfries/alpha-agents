# Momentum Replacement Trigger Policy (Advisory Draft)

Status: Draft (not implemented)
Owner: Strategy research
Scope: Screening -> Monitoring replacement logic

## Purpose

Define a controlled way for stronger-momentum newcomers to replace weaker held underlyings, while preserving current BREAK-based risk exits and limiting churn.

This document is advisory only and does not change runtime behavior.

## Current Baseline (for context)

- New entries come from screening-ranked candidates.
- Held underlyings are excluded from same-run entry candidates.
- Direct SELL decisions are driven by BREAK signals.
- Warrant degradation can produce ROLL recommendations.
- Confirmed SELL capacity is recycled within the same run; sold underlyings remain excluded from entries.

## Design Principles

1. Keep BREAK exits as primary risk control.
2. Allow replacement only when relative momentum edge is clear and persistent.
3. Require friction-aware guardrails to avoid overtrading.
4. Cap replacement frequency with churn controls.

## Momentum Definition (for this policy)

- Primary momentum metric: TQ-60 (already implemented in Screening).
- Secondary confirmation only: TQ-20, rank-change deterioration, and multi-bar persistence checks.
- Interpretation: TQ-60 drives the replacement edge; secondary signals only reduce false positives and churn.

## Proposed Rule Set

### 1. Replacement eligibility (all required)

1. Incumbent holding age >= 10 trading days.
2. Challenger stock is currently in screening top 5.
3. Challenger trend signal is NEW or HOLD (not BREAK).
4. Challenger remains above entry threshold for 2 consecutive daily bars.

### 2. Relative momentum trigger (all required)

1. Absolute score edge:

   DeltaS = S_challenger - S_incumbent >= 0.08

2. Relative score edge:

   S_challenger / max(S_incumbent, 0.01) >= 1.20

3. Regime tightening:

- Yellow regime: DeltaS >= 0.12, relative edge >= 1.30, persistence >= 3 bars.
- Red regime: disable momentum replacements (BREAK exits still active).

### 3. Incumbent weakness filter (at least 1 required)

1. Incumbent rank worsened by >= 5 places over 1 week.
2. Incumbent TQ-20 < 0.
3. Incumbent trend signal is not NEW.

### 4. Cost and execution guardrails (all required)

1. Estimate round-trip friction proxy:

   old_warrant_spread + new_warrant_spread + 0.30% slippage buffer

2. Require expected score edge >= 2x friction proxy.
3. Hard block if challenger warrant spread > 3.0%.

### 5. Churn controls

1. Max 1 momentum replacement per run.
2. Max 2 momentum replacements per 10 trading days.
3. Cooldown on same underlying after replacement: 15 trading days.

## Pilot Profile (simpler first step)

Use this to test quickly before enabling the full rule set:

1. Holding age >= 10 days.
2. Challenger in top 3 for 2 bars.
3. Absolute score edge DeltaS >= 0.10.
4. Max 1 replacement per run.
5. Disable replacements in red regime.

## Validation Plan Before Any Implementation

1. Backtest baseline vs replacement policy with realistic spread/slippage assumptions.
2. Compare: net return, max drawdown, turnover, win rate, and average holding time.
3. Confirm replacement uplift survives transaction friction and does not materially worsen drawdown.
4. Roll out as configurable feature flag with conservative defaults.

## Open Questions

1. Which score should drive replacement (TQ only vs composite score)?
2. Should the incumbent weakness filter be mandatory in all regimes?
3. Should replacement be blocked near warrant maturity thresholds?
4. Is weekly rank delta the right horizon for weakness detection?

## Non-goals

- No change to current production behavior.
- No execution wiring, no UI changes, no config schema changes in this draft.
