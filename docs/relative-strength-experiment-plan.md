# Relative Strength Versus Benchmark Experiment Plan

**Status:** Completed -- no trend-policy, field, or configuration change.
**Scope:** Research and Screening trend context.

## Hypothesis

A stock can satisfy every current trend rule because the whole market rises. At a NEW
event, positive relative trend quality versus the universe's benchmark may identify
idiosyncratic strength and improve subsequent **excess** returns.

## Exact Metric

For each stock NEW event on date $t$, align the benchmark's latest bar on or before $t$.
Compute both values with the existing 60-bar TQ definition and then calculate:

$$
relative\_tq60_t = stock\_tq60_t - benchmark\_tq60_t
$$

Evaluate future excess return, not raw return:

$$
excess\_return_{t,h} = stock\_return_{t,h} - benchmark\_return_{t,h}
$$

where $h \in \{5, 10, 20\}$ trading bars. Events without a date-aligned benchmark bar
or a complete forward benchmark horizon are excluded.

## Experiment Design

1. Resolve the live `NASDAQ100` universe through `UniverseAgent` and use `^NDX` as its
   benchmark; repeat on `DAX` with `^GDAXI`.
2. Replay the unchanged current NEW/BREAK state machine over five years of OHLCV.
3. Bucket NEW events into relative-TQ quintiles without imposing a threshold.
4. Report event count, mean and median excess return, and win rate for each 5/10/20-bar
   horizon.
5. Check whether the lowest and highest quintile ordering is directionally consistent
   across NASDAQ-100 and DAX.

## Decision Gate

Consider an advisory-only `relative_tq60` field only if both universes show:

- A monotonic or near-monotonic improvement in 10 and 20-bar excess returns across
  relative-TQ quintiles.
- A meaningful highest-versus-lowest quintile difference after event counts and medians
  are considered.
- No result driven solely by a small number of symbols or one market regime.

Do not add a NEW-policy gate from this experiment. A later policy proposal needs
out-of-sample validation and an explicit, default-off configuration.

## Result (2026-09-20)

`scripts/analyze_relative_strength.py` replayed five years of current NEW/BREAK events
using date-aligned benchmark bars. NASDAQ-100 produced 747 observations against `^NDX`;
DAX produced 289 observations against `^GDAXI`.

NASDAQ-100 showed positive 20-bar mean excess returns in the middle relative-TQ
quintiles (Q3: 2.13%, Q4: 2.90%), but the highest quintile fell to 1.30%. DAX did not
replicate that ordering: Q2 and Q4 were positive at 0.96% and 0.71%, while the highest
quintile was -0.94%.

The results are non-monotonic and inconsistent between universes. They do not establish
that higher relative TQ produces better excess returns, so the decision gate fails. Do
not add `relative_tq60` to Screening or add a relative-strength NEW policy. Revisit only
with a pre-specified alternative metric and an out-of-sample protocol.

## Conditional Implementation

If the decision gate passes:

1. Extend `ResearchResult`/`SelectionResult` with an informational per-symbol
   `relative_tq60` mapping.
2. Pass benchmark bars through existing stage contracts; do not fetch a second benchmark
   in Screening.
3. Display the field in Screening without changing selection.
4. Re-run the analysis on a held-out date range before considering an opt-in policy.

## Out of Scope

- Market-regime blocking.
- Weighted votes.
- Relative volume or weekly confirmation.
- Warrant scoring or selection changes.
