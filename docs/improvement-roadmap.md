# Strategy Improvement Roadmap

**Status:** Canonical planning index as of 2026-09-20. This document consolidates
open improvement ideas and their ordering. Detailed documents remain the design and
implementation records for their individual initiatives.

## Decision Framework

The pipeline should keep three distinct concerns:

1. **Trend detection and signal semantics** answer whether an underlying trend is
   valid and emit the durable `NEW`, `HOLD`, and `BREAK` state-machine outputs.
2. **Entry decision quality** answers whether a valid `NEW` is an attractive entry
   now. It includes extension, entry class, and market-regime context.
3. **Warrant selection and lifecycle** chooses and maintains an instrument only after
   the underlying passes the first two concerns.

Do not split trend detection from signal generation into independent implementation
tracks. The signal is the output of the shared trend policy and state machine; separate
implementations would risk disagreement between Screening, chart markers, and
Monitoring. Entry-decision quality is a separate workstream that consumes those
signals without redefining them.

```text
Market regime
  -> Trend detection and NEW/HOLD/BREAK semantics
  -> Entry decision quality
  -> Warrant selection and lifecycle
  -> Portfolio, risk, and execution
```

## Current Baseline

- The shared trend-policy implementation, dual `NEW`/`BREAK` rule groups, and state
  machine are complete. See [screening-policy-refactor-plan.md](screening-policy-refactor-plan.md).
- Market regime and breadth are computed and displayed, but remain advisory. See
  [market-regime-filter-plan.md](market-regime-filter-plan.md).
- Monitoring classifies held positions; Warrant Selection searches and scores roll
  replacements. A roll is selected and displayed but is not yet executed. See
  [roll-warrant-selection-plan.md](roll-warrant-selection-plan.md).
- The baseline warrant score is refactored and covered by parity tests. See
  [warrant-scoring-refactor-plan.md](warrant-scoring-refactor-plan.md).

## Ordered Roadmap

### 0. Close the existing roll-execution gap

**Goal:** Execute a confirmed roll as one incumbent `SELL` and one replacement `BUY`.

1. Model a roll pair in Portfolio rather than treating a replacement as an ordinary
   new position.
2. Close the incumbent only when a replacement cleared `roll_min_improvement`; retain
   it nowhere else in the protected holdings path.
3. Have Risk validate the pair and Execution emit exactly one `SELL` plus one `BUY`
   under existing dry-run semantics.
4. Add end-to-end tests for no double exposure, slot isolation, and the no-replacement
   `SELL` branch.

**Why first:** the system currently makes roll recommendations that cannot reach
execution. This is an operational correctness gap, not a strategy experiment.

**Source:** [roll-warrant-selection-plan.md](roll-warrant-selection-plan.md).

### 1. Measure entry extension without changing decisions

**Goal:** expose whether a `NEW` signal is far from its own trend.

1. Compute $\mathrm{ema20\_extension\_atr} = (close - ema20) / atr20$.
2. Persist and display the metric on `SelectionResult`.
3. Do not add an entry gate or alter `NEW`/`BREAK`.
4. Produce a forward-return analysis for historical `NEW` signals in extension buckets
   at 5, 10, and 20 trading days.

**Decision gate:** only introduce an extension classification or threshold when the
analysis shows a material, repeatable difference after realistic warrant costs.

**Source:** [entry-timing-extension-filter-plan.md](entry-timing-extension-filter-plan.md).

### 2. Add advisory entry-decision quality

**Goal:** distinguish a valid trend from a timely leveraged entry.

1. If Step 1 validates it, add advisory `NORMAL`, `EXTENDED`, and `VERY_EXTENDED`
   states. Never use this state as an exit trigger.
2. Design Early and Confirmed entry labels as classifications that consume the existing
   `NEW` signal history; preserve the current state machine initially.
3. Test $+DI > -DI$ as direction-aware trend strength and define a reproducible
   swing-high breakout rule before enabling either as a policy.
4. Only after evidence exists, consider A-E quality classes or a weighted model for
   trend, momentum, trend strength, and timing. Do not replace flat rule voting with
   guessed weights.

**Source:** [entry-conditions-v2-improvement-analysis.md](entry-conditions-v2-improvement-analysis.md).

### 3. Run isolated trend-detection experiments

Each proposal below needs its own short plan, default-off configuration, and a targeted
test/backtest. Do not bundle them into Entry V2.

1. Smooth or lengthen the ADX-rising slope window.
2. Add relative strength versus the selected market benchmark.
3. Add relative-volume confirmation.
4. Add weekly confirmation from resampled daily bars.
5. Consider weighted policy votes only after sufficient execution history exists.

**Source:** [trend-detection-improvement-ideas.md](trend-detection-improvement-ideas.md).

### 4. Use market regime downstream, advisory first

1. Add focused automated coverage for current regime classification, breadth downgrade,
   and UI payloads.
2. Define advisory handling: stricter timing review in Yellow and no automatic Early
   entries in Red.
3. Validate the regime effect before it changes Monitoring, Warrant Selection, or
   Portfolio behavior. Any automatic entry block requires a separate approved plan.
4. Treat VIX and persisted regime history as later, independent additions.

**Source:** [market-regime-filter-plan.md](market-regime-filter-plan.md).

### 5. Improve position lifecycle only after its data paths are complete

1. Implement re-entry prevention from transaction history and test the exclusion
   window.
2. Add historical degradation tracking and alerts if operational monitoring needs it.
3. Consider incumbent-versus-challenger momentum replacement only with a
   friction-aware backtest, persistence/cooldowns, and a feature flag. It must not
   replace the normal `BREAK` exit path.

**Sources:** [monitoring-enhancement-plan.md](monitoring-enhancement-plan.md),
[momentum-replacement-trigger-policy.md](momentum-replacement-trigger-policy.md).

### 6. Refine warrant quality after stock-entry quality is validated

1. Add volatility-adjusted leverage using underlying ATR%, initially as a no-op/default
   scoring input until calibration is demonstrated.
2. Evaluate computed Black-Scholes gamma/convexity only after input quality and
   backtesting are sufficient.
3. Optimize scoring weights only with enough historical simulations or execution data
   to control overfitting.
4. Keep the dual-strike chart overlay as an optional review UX enhancement.

**Sources:** [underlying-volatility-leverage-plan.md](underlying-volatility-leverage-plan.md),
[warrant-convexity-scoring-plan.md](warrant-convexity-scoring-plan.md),
[warrant-scoring-refactor-plan.md](warrant-scoring-refactor-plan.md), and
[roll-warrant-selection-plan.md](roll-warrant-selection-plan.md).

## Planning Rules

- A completed step must have focused tests and a documentation status update before the
  next behavioral step begins.
- A metric precedes a hard filter; a backtest precedes a threshold; advisory behavior
  precedes automatic behavior.
- Preserve the existing `NEW`/`BREAK` contract and typed stage boundaries unless a
  dedicated plan explicitly changes them.
- A warrant-quality improvement may reject an otherwise valid entry, but it must not
  be used to compensate for a poor underlying entry.

## Document Authority

This roadmap is the authority for cross-cutting priority. The detailed plans remain
authoritative for their local contracts and implementation details. Where an older
historical section conflicts with current behavior, defer to the status/current-state
section in its document and to [roll-warrant-selection-plan.md](roll-warrant-selection-plan.md)
for roll ownership and execution status.
