# Momentum Replacement Trigger Policy (Advisory Draft)

Status: Draft (not implemented)
Owner: Strategy research
Scope: Screening -> Monitoring replacement logic

## Implementation Status (as of 2026-11-16)

Overall: Core momentum-replacement behavior is not implemented.

Current runtime behavior remains:

- Entries come from screening-ranked candidates.
- Held underlyings are excluded from same-run entries unless a position is sold.
- SELL decisions are driven by BREAK signals.
- Replacement logic exists only for warrant-roll (same underlying when warrant quality degrades), not incumbent-vs-challenger momentum swaps.

### What is implemented (building blocks only)

1. TSI, TQ-60, and TQ-20 screening metrics are implemented; TSI is the default ranking metric.
2. Trend signal state machine is implemented (NEW/HOLD/BREAK/None).
3. Rank-change diagnostics are implemented (1W/2W/4W deltas).
4. Market regime classification is implemented (green/yellow/red with breadth enrichment).
5. Warrant-health degradation checks are implemented for roll classification.
6. Advisory warrant discovery searches eligible challengers within the existing
   screened top-N entry pool, even without free slots, when their configured
   TSI/TQ-20/TQ-60 Monitoring rank is strictly better than the weakest ranked
   KEEP/ROLL underlying. Results are separate from actionable entries; no momentum
   replacement or minimum metric delta is enabled. Missing valid incumbent ranks
   are reported explicitly, not inferred from absence in Screening's entry shortlist.
   See [Warrant Selection](agents/warrant_selection.md#advisory-momentum-challengers).

### Advisory UI Update (2026-10-23)

Monitoring now shows one comparable universe-wide screening rank in SELL, ROLL,
KEEP, and ENTRY tables, using the execution's configured TSI, TQ-60, or TQ-20 metric.
Hovering or focusing a rank cell reveals the metric value. Held tables display
strongest-first ranks; ENTRY retains screening order. Invalid or unavailable metrics
remain unranked. This supports manual incumbent-versus-challenger review only:
rank gaps do not measure economic advantage, and none of the replacement triggers,
cost guardrails, or churn controls below are enabled by this UI change.

See [Web UI Spec](web-ui.md#44-monitoring--stagesmonitoring) for exact rank semantics.

### Rule coverage status against this policy

1. Replacement eligibility (holding age, top-5 challenger, NEW/HOLD challenger, 2-bar persistence): Not implemented.
2. Relative momentum trigger (persistent TSI score difference): Not implemented.
3. Regime-tightened thresholds for yellow and replacement-disable in red: Not implemented for momentum replacement.
4. Incumbent weakness filter (rank deterioration, TQ-20, trend condition): Not implemented.
5. Cost and execution guardrails (friction proxy, 2x edge, spread hard block): Not implemented for momentum replacement.
6. Churn controls (per-run/per-10-day caps, same-underlying cooldown): Not implemented.
7. Pilot profile and feature-flagged rollout: Not implemented.
8. Backtest validation workflow for this policy: Not implemented.

### Evidence snapshot (code-level)

- Screening computes momentum score and rank deltas, but no incumbent-challenger replacement decision path exists.
- Monitoring computes SELL/KEEP/ROLL decisions, where ROLL is tied to warrant degradation and not to challenger momentum.
- Orchestrator explicitly keeps monitoring classification-only and delegates replacement lookup to warrant selection for roll underlyings.
- No tests currently assert incumbent-vs-challenger momentum replacement rules from this policy.

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

- Primary momentum metric: TSI, which is bounded from -100 to +100 in the current screening implementation.
- Compare the challenger and incumbent with an absolute score difference, not a ratio:

   DeltaTSI = TSI_challenger - TSI_incumbent

- A positive difference means the challenger has stronger relative TSI momentum, including when both TSI values are negative. It does not by itself mean the challenger is eligible for entry; normal screening eligibility still applies.
- TQ-60 and TQ-20 remain possible secondary trend-quality context, but are not part of the first TSI-threshold pilot. Test them as separate variants to avoid conflating the primary metric with confirmation signals.
- Require the TSI edge to persist across consecutive daily closes to reduce one-bar noise.

The [minimum metric delta plan](#minimum-metric-delta-calibration-and-rollout-plan)
below covers discovery thresholds for all three configurable ranking metrics.
It does not change the TSI-first replacement pilot or enable replacement trading.

## Proposed Rule Set

### 1. Replacement eligibility (all required)

1. Incumbent holding age >= 10 trading days.
2. Challenger stock is currently in the TSI-ranked screening top 5 among entry-eligible candidates.
3. Challenger trend signal is NEW or HOLD (not BREAK).
4. Challenger satisfies the configured normal entry rules on 2 consecutive daily closes.

### 2. Relative momentum trigger (all required)

1. Absolute TSI score edge, on the raw -100 to +100 scale:

   DeltaTSI = TSI_challenger - TSI_incumbent >= 20 points

   Treat 20 points as the initial pilot hypothesis, not a validated cutoff. Evaluate sensitivity at 10, 15, 20, and 25 points.

2. Regime tightening:

- Yellow regime: test a 25-point TSI edge and persistence >= 3 bars.
- Red regime: disable momentum replacements (BREAK exits still active).

### 3. Incumbent weakness filter (at least 1 required)

1. Incumbent rank worsened by >= 5 places over 1 week.
2. Incumbent TSI slope over the latest five valid daily values is <= 0.
3. Incumbent trend signal is not NEW.

### 4. Cost and execution guardrails (all required)

1. Estimate round-trip friction proxy:

   old_warrant_spread + new_warrant_spread + 0.30% slippage buffer

2. Do not compare a raw TSI score difference directly with a percentage cost estimate. Require the replacement strategy to show positive net results after realistic friction in the validation backtest.
3. Hard block if challenger warrant spread > 3.0%.

### 5. Churn controls

1. Max 1 momentum replacement per run.
2. Max 2 momentum replacements per 10 trading days.
3. Cooldown on same underlying after replacement: 15 trading days.

## Pilot Profile (simpler first step)

Use this to test quickly before enabling the full rule set:

1. Holding age >= 10 days.
2. Challenger in top 3 for 2 bars.
3. Absolute TSI score edge DeltaTSI >= 20 points (initial pilot hypothesis; test the sensitivity range above).
4. Max 1 replacement per run.
5. Disable replacements in red regime.

## Validation Plan Before Enabling Momentum Replacements

1. Backtest the no-replacement baseline against the TSI policy with realistic spread/slippage assumptions and a walk-forward or held-out evaluation.
2. Compare the pre-specified 10, 15, 20, and 25 point TSI thresholds; do not select a cutoff solely because it wins on the calibration period.
3. Compare net return after costs, max drawdown, turnover, win rate, and average holding time across more than one market/universe where data permits.
4. Confirm any uplift survives transaction friction and does not materially worsen drawdown.
5. Roll out as a configurable feature flag with conservative defaults only after validation.

## Minimum Metric Delta Calibration and Rollout Plan

Status: Planned; no minimum metric delta is currently applied.

### Goal and scope

Define separate, evidence-based thresholds for TSI, TQ-20, and TQ-60 that remove
negligible challenger advantages without hiding useful replacement opportunities.
Start with fixed thresholds in each metric's native units. Adaptive thresholds
are a later research variant, not a prerequisite.

Keep two purposes distinct:

| Purpose | Comparison incumbent | Success criterion |
| ------- | -------------------- | ----------------- |
| Challenger discovery | Weakest ranked KEEP/ROLL underlying | Reduce warrant-search cost while retaining opportunities admitted by the tested replacement policy |
| Actual replacement | The specific holding proposed for replacement | Improve held-out net strategy results after friction without unacceptable drawdown or turnover |

Discovery remains restricted to normal entry-eligible names within the current
screened top-N pool. Use the execution's configured TSI/TQ-20/TQ-60 metric and
Monitoring's universe-wide ranking, not Screening shortlist row numbers. SELL
holdings do not define the discovery benchmark. Existing missing-rank diagnostics
remain in force; absence from the entry shortlist is not a missing Monitoring rank.

### 1. Define the threshold contract

For the same metric M, measured at the same completed daily close:

```text
Delta_M = M(challenger) - M(incumbent)

Qualifies when:
Delta_M > 0
and Delta_M >= Minimum_Delta_M
```

The explicit positive-edge requirement preserves strict rank improvement when the
minimum is zero. Compare raw scores, not rounded display values, rank gaps,
percentage improvements, or ratios. Zero and negative metric values are valid;
missing or non-finite values cannot establish an advantage.

| Metric | Native delta unit | Interpretation |
| ------ | ----------------- | -------------- |
| TSI | Index points on the -100 to +100 scale | TSI 35 versus 18 gives a 17-point advantage |
| TQ-20 | Trend-quality score units | Difference in R-squared times regression slope divided by ATR-20, using the short regression window |
| TQ-60 | Trend-quality score units | Same formula using the longer regression window; calibrate independently of TQ-20 |

For example, TQ-60 of 0.14 versus 0.08 gives a delta of 0.06 score units, not a
6% expected-return advantage. TQ has no fixed theoretical bounds like TSI, and
identical numerical thresholds need not have comparable effects across its two
windows. A metric advantage is not an expected-return estimate and must never be
compared directly with a warrant spread percentage.

Keep discovery and replacement thresholds separately identifiable in any future
configuration. For the same metric and operating conditions, discovery must be no
stricter than replacement, so its prefilter cannot exclude an otherwise qualifying
pair solely because of the delta threshold. Warrant quality, entry eligibility,
holding age, regime, and churn controls remain separate conditions.

### 2. Establish the zero-threshold baseline and collect diagnostics

1. Retain the current discovery baseline: strictly better rank, equivalent to a
   positive raw metric difference with a zero minimum.
2. Record execution/date, metric and indicator parameters, top-N/eligibility
   settings, market regime, benchmark identity, both raw metric values, and delta
   for each eligible challenger-benchmark comparison. Keep actual replacement-pair
   comparisons distinct from discovery-benchmark comparisons.
3. Record attempted lookups, warrant-detail requests, elapsed lookup time, failures,
   suitable-warrant availability, and resulting shortlist size. Candidate counts
   alone are not a sufficient proxy for API cost.
4. Use zero-threshold observations or equivalent historical replay to assess names
   that a positive threshold would exclude; filtered runs alone cannot reveal their
   warrant availability.
5. Use point-in-time universes, holdings, eligibility, and completed candles in
   historical evaluation. Do not use future constituents or future observations
   when selecting thresholds.

### 3. Calibrate a small fixed-threshold candidate set

1. For TSI, retain the predeclared 10, 15, 20, and 25 point replacement hypotheses,
   with zero as the discovery baseline. Treat the existing 20-point pilot as a
   hypothesis, not a validated optimum. Choose discovery candidates no stricter
   than the replacement thresholds being evaluated.
2. For each metric/date, measure cross-sectional dispersion across the evaluated
   universe with valid metric values, not just the truncated top-N entry pool:

   ```text
   IQR_M = percentile_75(M) - percentile_25(M)
   Normalized advantage = Delta_M / IQR_M
   ```

3. Use calibration-period distributions of raw deltas and IQRs to propose a small
   set of separate fixed TQ-20 and TQ-60 thresholds. Fractions such as 0.25, 0.5,
   or 1.0 of a representative historical IQR are research starting points, not
   validated cutoffs. Define the historical aggregation in advance, translate the
   candidates into native score units, and freeze them before held-out evaluation.
4. Do not report a normalized advantage when the IQR is zero or unavailable.
   Record that limitation explicitly; do not silently manufacture a scale.
5. Test each primary metric separately. Do not simultaneously add multiple
   confirmation indicators or optimize many regime-specific thresholds, since
   that obscures the source of any improvement and increases overfitting risk.

Illustration only, not observed data or recommended settings:

| Metric | Hypothetical universe IQR | Half-IQR raw advantage |
| ------ | ------------------------ | ---------------------- |
| TSI | 30 points | 15 points |
| TQ-20 | 0.16 | 0.08 |
| TQ-60 | 0.10 | 0.05 |

### 4. Validate discovery efficiency and replacement economics separately

1. Before held-out testing, specify acceptable discovery API cost/latency and the
   tolerated loss of opportunities admitted by the fixed replacement policy.
   Compare positive discovery thresholds against the zero-threshold baseline with
   the same downstream policy, eligibility, and warrant-selection rules.
2. Evaluate replacement thresholds using walk-forward or held-out periods and
   realistic warrant spreads, slippage, and fees. Compare net return, drawdown,
   turnover, holding time, and replacement frequency against no replacement.
   Underlying-only results cannot establish warrant-level profitability; disclose
   limitations where historical executable warrant quotes are unavailable.
3. For actual replacements, require the same challenger-incumbent pair to maintain
   the required edge over consecutive completed daily closes. Do not count
   persistence by switching comparison incumbents between days. Discovery itself
   need not add this persistence requirement in the first version.
4. Prefer stable results across neighboring thresholds and evaluation periods over
   a single best calibration result. Keep the TSI-first pilot separate from
   alternative TQ-20/TQ-60 replacement experiments.

### 5. Roll out in stages

1. First persist diagnostics and expose raw advantages for advisory review, without
   changing current selections or enabling momentum trades.
2. After discovery validation, introduce explicit per-metric discovery minimums
   with zero defaults. Verify strict ties remain excluded, negative scores work,
   missing data is reported, and normal entry backfill/ROLL behavior is unchanged.
   Discovery filtering must not remove ordinary entries needed to fill free slots.
3. Only after separate strategy validation, introduce feature-flagged replacement
   thresholds, pair persistence, execution guardrails, and churn controls.
   Recheck discovery-versus-replacement threshold compatibility when settings change.
4. Consider a daily adaptive rule such as `Delta_M >= k * IQR_M` only as a later
   separately validated variant. It changes with market dispersion and can become
   too permissive when dispersion contracts; it would need an explicit native-unit
   floor and defined handling of unavailable/zero dispersion.

Deliverables before activation: a calibration report with frozen candidate
thresholds, a held-out discovery efficiency comparison, a separate net-of-costs
replacement evaluation, and an explicit rollout decision. No production threshold
is selected by this plan alone.

## Open Questions

1. Does TSI outperform TQ-60 or TQ-20 as the primary replacement metric out of sample?
2. Should the incumbent weakness filter be mandatory in all regimes?
3. Should replacement be blocked near warrant maturity thresholds?
4. Is weekly rank delta the right horizon for weakness detection?

## Non-goals

- No change to current production behavior.
- No execution wiring, no UI changes, no config schema changes in this draft.
