# Weekly Confirmation Experiment Plan

**Status:** Advisory field implemented; no trend-policy or configuration change.
**Scope:** Screening trend context derived from existing daily OHLCV bars.

## Hypothesis

A daily NEW signal aligned with an established weekly uptrend may have fewer short-lived
failures and better subsequent returns than a daily NEW against a weak or falling weekly
trend.

## Exact Weekly State

Resample daily bars into calendar weeks ending Friday using:

- Open: first daily open.
- High: weekly maximum high.
- Low: weekly minimum low.
- Close: final daily close.
- Volume: sum of daily volume.

At each daily NEW event, use only the latest **completed** weekly bar. Define weekly
confirmation as both:

$$
weekly\_close > weekly\_EMA20
$$

and

$$
weekly\_EMA20 > weekly\_EMA20[-5]
$$

A partial current week must not confirm an entry, avoiding look-ahead bias.

## Experiment Design

1. Resolve NASDAQ-100 and DAX through `UniverseAgent` and fetch five years of daily
   OHLCV history.
2. Replay the unchanged daily NEW/BREAK state machine.
3. Split daily NEW events into weekly-confirmed and not-confirmed groups.
4. Report count, mean and median 5/10/20-bar returns, and NEW-to-BREAK churn within
   5 and 10 daily bars for each group.
5. Confirm that any benefit appears in both universes and is not explained by a small
   number of symbols.

## Decision Gate

Consider an advisory weekly state only if weekly-confirmed NEW events in both universes:

- Have lower 5-bar NEW-to-BREAK churn.
- Have higher median 10- and 20-bar returns than non-confirmed events.
- Retain sufficient event coverage; do not eliminate most current NEW signals.

A result in only one universe, or a return gain paired with worse churn, fails the gate.

## Result (2026-09-20)

The five-year replay passed the advisory decision gate in both universes. NASDAQ-100
weekly-confirmed events (710) had mean 5/10/20-bar returns of 0.46% / 0.97% / 2.01% and
5/10-bar churn of 14.8% / 33.7%. Not-confirmed events (37) had -1.84% / -2.19% / -2.73%
and 21.6% / 54.1% churn.

DAX confirmed events (275) had -0.00% / 0.57% / 0.26% and 16.0% / 34.2% churn; its
not-confirmed sample is small (15), but was weaker at -0.67% / -0.54% / -2.05% and
20.0% / 66.7% churn. The directional result replicates, with the small DAX comparison
group recorded as a limitation.

`SelectionResult.weekly_confirmed` now exposes the completed-week state in Screening.
The compact `Week` indicator is advisory only: it does not affect selection, NEW/BREAK,
or execution. A default-off weekly NEW policy still requires held-out validation.

## Conditional Implementation

Implemented after the gate passed:

1. Add an informational weekly-confirmation mapping to `SelectionResult`.
2. Display it as advisory context, preserving current selection and state-machine output.
3. Validate a default-off weekly NEW policy on a held-out date range before enabling it.

## Out of Scope

- Weekly SuperTrend, ADX, or a full duplicate weekly policy stack.
- Partial-week confirmation.
- Entry blocking, warrant changes, or portfolio changes.
