# Volume Confirmation Experiment Plan

**Status:** Completed -- no trend-policy, field, or configuration change.
**Scope:** Screening trend context using existing `OHLCV.volume`.

## Hypothesis

A NEW transition on unusually high volume may have stronger subsequent performance than
a similarly shaped price signal on routine or weak volume. Volume may therefore confirm
price-based trend evidence without duplicating the existing trend rules.

## Exact Metric

For a NEW event on bar $t$, calculate relative volume using only preceding completed
bars:

$$
relative\_volume_t = volume_t / mean(volume_{t-20}, \ldots, volume_{t-1})
$$

Events require 20 prior positive-volume bars. This avoids comparing the event bar to a
moving average that includes itself and excludes symbols with unusable volume data.

## Experiment Design

1. Resolve the live `NASDAQ100` universe through `UniverseAgent`; repeat on `DAX`.
2. Replay the unchanged NEW/BREAK state machine over five years of OHLCV history.
3. Record relative volume and raw 5/10/20-bar underlying returns at each NEW event.
4. Bucket events into data-driven relative-volume quintiles without imposing a cutoff.
5. Report event count, mean return, median return, and win rate per horizon.
6. Compare potential threshold candidates only after the quintile relationship is known.

## Decision Gate

Consider an advisory-only volume field only if both NASDAQ-100 and DAX show:

- A broadly monotonic or otherwise pre-specified relationship between relative-volume
  quintile and 10/20-bar returns.
- A material high-versus-low volume difference supported by medians and sufficient
  observations, not a few outliers.
- No result driven by zero/invalid volume data or a small number of symbols.

Do not add a NEW-policy gate from this experiment. A later opt-in filter requires a
separate threshold decision and out-of-sample validation.

## Result (2026-09-20)

`scripts/analyze_volume_confirmation.py` replayed five years of current NEW events.
NASDAQ-100 produced 747 valid-volume observations; DAX produced 290.

NASDAQ-100 had its strongest 20-bar mean returns in the middle volume quintiles
(Q2: 2.59%, Q3: 2.89%), while the highest-volume quintile was lower at 1.58%. DAX had
mixed results: Q5 reached 2.02% at 20 days but had negative 5-day mean return, and Q4
had -1.73% at 20 days. The medians also do not establish a monotonic relationship.

Neither universe supports a robust high-volume confirmation rule. The decision gate
fails: do not add `relative_volume20` to Screening and do not add a volume NEW policy.
Revisit only with a pre-specified alternative volume metric and out-of-sample protocol.

## Conditional Implementation

If the decision gate passes:

1. Add an informational `relative_volume20` mapping to `SelectionResult`.
2. Display it in Screening while preserving all current selection and NEW/BREAK behavior.
3. Retest on a held-out date range before proposing `policy_relative_volume` as a
   default-off rule.

## Out of Scope

- Intraday volume or volume-profile analysis.
- Relative volume against index volume.
- Relative-strength, extension, ADX, or weekly-confirmation changes.
- Any warrant selection or portfolio behavior.
