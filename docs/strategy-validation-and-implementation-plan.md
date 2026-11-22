# Investment Strategy Validation and Implementation Plan

**Status:** Proposed; this plan does not change trading behavior.
**Purpose:** Sequence strategy research and define the evidence required before a
proposal can affect entries, exits, position replacement, or warrant selection.

**Current market-gate baseline:** The configurable benchmark SuperTrend gate already
blocks new entries by default; the optional red-regime gate is off by default. The
gate does not change SELL or ROLL decisions. Implementation is not evidence of
trading efficacy; evaluate each gate against the ungated baseline.

## Current Momentum-Replacement Status (2026-11-16)

- Advisory challenger warrant discovery is implemented, including when there are no
  free entry slots. Candidates stay within the current screened top-N eligible entry
  pool and must beat the weakest ranked KEEP/ROLL underlying using Monitoring's
  universe-wide rank for the configured TSI, TQ-20, or TQ-60 metric.
- Suitable challenger warrants are retained separately from actionable entries.
  Entry capacity, ordinary backfill, same-underlying rolls, and `BREAK` exits are
  unchanged. Missing valid incumbent ranks are reported explicitly.
- Minimum metric deltas, persistent incumbent-versus-challenger replacement triggers,
  and automatic momentum trades are not implemented. Existing advisory discovery is
  research preparation, not evidence that replacement improves returns.
- The detailed threshold contract and calibration sequence are maintained in the
  [minimum metric delta plan](momentum-replacement-trigger-policy.md#minimum-metric-delta-calibration-and-rollout-plan).
  This document owns cross-workstream validation gates, not a second threshold specification.

## Principles

1. Keep the existing `NEW` / `HOLD` / `BREAK` state machine as the baseline. Entry
   quality and market context may consume its signals, but must not silently redefine
   them.
2. Test one hypothesis at a time. Do not combine indicators or tune thresholds after
   inspecting the holdout results.
3. Use a chronological holdout or walk-forward evaluation. Resolve point-in-time
   constituents where possible; if unavailable, disclose survivorship bias and limit
   the strength of the conclusion.
4. Evaluate decision-changing policies on net outcomes after spread, fees, and
   slippage. Underlying-only results can reject a hypothesis, but cannot by themselves
   prove a leveraged-warrant policy profitable.
5. Before each test, record the hypothesis, data window, universes, parameter variants,
   metrics, minimum practical benefit, and stop conditions. Preserve the baseline
   report so a later result is comparable.
6. A passing backtest permits a conservative, default-off pilot; it does not justify
   immediate automatic behavior. Keep human approval and dry-run safeguards.

## Evaluation Contract

### Baseline and data

- Replay the current production `NEW` / `HOLD` / `BREAK` behavior without modifying
  its rule configuration.
- For momentum research, freeze the configured ranking metric, indicator parameters,
  screened top-N pool, re-entry exclusions, and capacity rules. Use current
  zero-minimum advisory discovery as the search baseline and no momentum replacement
  as the trading baseline; they answer different questions.
- Use at least two distinct universes (initially NASDAQ-100 and DAX, where data
  coverage permits), multiple chronological periods, and a final untouched holdout.
- Prevent look-ahead: use only information available at the signal timestamp, including
  completed weekly bars and then-current regime values.
- Report data coverage, missing values, event counts, and exclusions. Do not silently
  drop periods, symbols, or unavailable warrant quotes.
- Start underlying-only analysis when that is all the hypothesis requires, but use
  historical warrant prices and conservative transaction costs before making a claim
  about leveraged-warrant outcomes. Replace modeled costs with observed fill evidence
  when sufficient fill data is available.

### Required metrics

Report the metrics appropriate to each hypothesis, including:

- Forward 5-, 10-, and 20-trading-day returns; mean and median.
- NEW-to-BREAK churn within 5 and 10 bars, and signal-to-exit delay where relevant.
- Net portfolio return, maximum drawdown, turnover, holding duration, and cost drag for
  portfolio-level tests.
- Signal coverage and number of symbols/events affected, to identify gains from small
  samples or policies that suppress most opportunities.
- Per-universe and per-period results, not only pooled averages.
- For discovery-only filters: warrant-search/detail request counts, latency, failures,
  suitable-warrant coverage, and opportunities excluded relative to zero-minimum
  discovery under the same fixed downstream replacement policy.

Before running a holdout, define what constitutes a material improvement and what
drawdown, turnover, coverage, or cost regression is unacceptable. A result that appears
only in one universe or one selected period does not pass.

## Ordered Workstreams

### 0. Establish the baseline and research protocol

Create a reproducible baseline report and document the evaluation contract above.
Confirm the availability and limitations of historical market, warrant, universe, and
cost data. Capture actual fill prices, quantities, timestamps, and explicit fees when
available; do not infer missing fees from price differences.

**Deliverable:** versioned baseline results plus a fixed, reviewable test protocol.

**Gate:** do not tune a policy against the same sample that will be used to claim
success.

### 1. Validate existing weekly and market-regime context separately

These inputs are already computed and displayed. Test their predictive value separately;
the existing market gate can already affect entry decisions.

1. **Weekly confirmation:** rerun the documented weekly-confirmed vs. unconfirmed
   comparison on a held-out period. The earlier five-year replay passed its advisory
   gate, but the DAX unconfirmed group was small. Report event coverage, 5/10/20-bar
   returns, and 5/10-bar NEW-to-BREAK churn.
2. **Market regime:** test outcomes for NEW signals and held positions by Green,
   Yellow, and Red regime, including breadth state where available. Check whether
   the observed difference is stable across universes and periods. Regime history
   persistence may be added as research data plumbing.

**Backtest required to assess:** the existing market-gate variants against an ungated
baseline. Backtest before adding a weekly NEW-policy gate, further regime-based entry
rules, regime-specific position sizing, or regime-aware Monitoring actions.

**Gate:** keep new proposals advisory if their effect is unstable, too sparse, or not
material after costs; review the existing gate settings against the same evidence.
Do not add VIX until the existing regime hypothesis is evaluated.

### 2. Research composite entry quality

Only proceed after Workstream 1 identifies context features worth investigating.
Define an advisory classification that consumes existing signals; do not start with a
weighted 0–100 score or guessed A–E grades.

Candidate dimensions to test independently before combining:

- Trigger freshness and signal age.
- Trend context/strength, including direction-aware `+DI > -DI`.
- Weekly confirmation and market regime, only if their separate tests support use.
- ATR-normalized EMA20 extension as context, not a standalone veto.
- A precisely defined swing-high breakout or pullback/recovery event, if separately
  specified before testing.

Compare each feature against the same baseline, then test a pre-specified composite.
Initially report classifications as advisory and preserve the `NEW` / `BREAK` output.

**Backtest required before:** using a class to rank, delay, reject, or size an entry.
Any threshold must be selected on training data and evaluated unchanged on holdout.

**Gate:** require repeatable net improvement without unacceptable drawdown, turnover,
or loss of valid opportunities. If no composite passes, retain the current policy.

### 3. Test regime-aware downstream behavior

Do not treat “regime awareness” as one combined change. Select one decision and one
hypothesis at a time:

- Monitoring: whether a Yellow/Red regime should change review urgency or a defined
  exit/stop policy.
- Warrant Selection: whether regime should change candidate eligibility or score.
- Portfolio: whether regime should change exposure or cash allocation.

Use the historical regime labels from Workstream 1 and evaluate the proposed action
against the unchanged baseline, including false exits, missed rebounds, costs, and
drawdown. VIX and persisted regime history are separate extensions, not implicit
requirements for every test.

**Backtest required before:** any additional regime-driven exit, entry block, instrument
filter, or exposure change. The existing entry-gate variants also require evaluation.

**Gate:** test each downstream effect independently. A passing result for entries does
not validate a Monitoring or Portfolio rule.

### 4. Evaluate incumbent-versus-challenger momentum replacement

Split this workstream into discovery preparation, discovery filtering, and actual
replacement. Follow the
[detailed minimum metric delta plan](momentum-replacement-trigger-policy.md#minimum-metric-delta-calibration-and-rollout-plan)
for formulas, native metric units, candidate thresholds, and edge cases.

1. **Advisory discovery — implemented:** retain suitable warrants for eligible top-N
   challengers even without free slots. The discovery benchmark is the weakest ranked
   KEEP/ROLL underlying, excluding SELL positions. Screening shortlist row numbers
   must not be substituted for Monitoring's universe-wide ranks.
2. **Diagnostics and calibration — planned:** collect raw metric advantages,
   benchmark identities, universe dispersion, and actual lookup cost/latency.
   Establish point-in-time zero-minimum observations before filtering names out.
   Data collection may proceed alongside Workstream 0; it does not depend on a
   composite entry-quality model or justify activating a trading policy.
3. **Discovery minimums — planned:** calibrate separate fixed native-unit thresholds
   for TSI, TQ-20, and TQ-60; keep zero as the default/baseline. Use historical
   dispersion as calibration context, not an automatic adaptive filter. Predeclare
   the acceptable search-cost reduction and opportunity-loss tradeoff, then evaluate
   frozen candidates on held-out data or replay. Do not remove ordinary entries
   needed for free-slot backfill. This gate establishes discovery efficiency, not
   replacement profitability.
4. **Replacement policy — not implemented or validated:** compare each challenger
   with the specific holding to be replaced, not merely the weakest discovery
   benchmark. Test the proposed holding-age, eligibility, pair persistence, TSI-edge,
   incumbent weakness, regime, spread/friction, and churn controls against no
   replacement. Keep the TSI-first pilot and pre-specified 10, 15, 20, and 25 point
   variants; test TQ-20/TQ-60 primary metrics separately rather than mixing them into
   the pilot. Do not choose the winner on holdout data.

Measure replacement net return, maximum drawdown, turnover, transaction costs, and
holding time. Keep ordinary `BREAK` exits unchanged. If warrant transaction-cost or
historical quote evidence is insufficient, disclose the limitation and label the
result provisional; underlying-only results cannot establish warrant profitability.

**Validation required before:** positive discovery minimums need an efficiency and
coverage evaluation against zero-minimum discovery. Any replacement recommendation
that changes a holding or entry additionally needs a walk-forward/held-out strategy
backtest with friction and churn. A passing replacement result should first run as a
default-off, human-reviewed pilot with pair and cost diagnostics.

**Gate:** keep discovery and replacement settings distinct. Discovery must be no
stricter than replacement for the same metric and conditions, and actual replacement
requires the same pair's edge to persist across completed daily closes. Advisory
data plumbing needs correctness tests, not a profitability claim. Adaptive
dispersion-based minimums remain a later, separately validated experiment.

### 5. Evaluate warrant-quality changes, one at a time

1. **Volatility-adjusted leverage:** expose ATR% per underlying and evaluate a
   pre-specified adjustment against the unchanged warrant score. Start with a no-op
   default; test sensitivity and a bounded leverage target on historical warrant
   outcomes.
2. **Gamma/convexity:** first validate the Black–Scholes inputs and calculation
   against known values and observed warrant data. Resolve whether the score is
   additive or replaces part of delta preference and define its shape before testing.
   Compare the result against the current delta-based score.
3. **Score-weight optimization:** defer until there are enough independent historical
   simulations or actual executions. Tune on training data only, then evaluate one
   frozen configuration on holdout.

**Backtest required before:** any nonzero volatility or convexity scoring impact, any
change in delta preference, and any score-weight change.

**Gate:** no warrant-score improvement should compensate for a weak underlying entry.
Preserve current score parity when new factors are disabled.

### 6. Revisit deferred trend-rule proposals only with new hypotheses

The completed ADX-slope smoothing, standalone relative-strength, and volume
confirmation experiments did not pass their documented decision gates. Do not
re-run them as parameter searches. A new experiment needs a materially different,
pre-specified hypothesis and a fresh validation design.

Weighted trend votes remain last: first collect sufficient execution history, then
test against equal voting with the same held-out protocol. No vote weights should be
derived only from in-sample indicator correlations or hit rates.

## Backtest Requirements at a Glance

| Proposal | Backtest before decision-changing implementation? | Current evidence |
| --- | --- | --- |
| Weekly confirmation as a NEW-policy gate | Yes — held-out validation | Initial advisory result passed; DAX comparison group was small |
| Regime-aware entry, Monitoring, Warrant Selection, or Portfolio behavior | Yes — separately for each behavior | SuperTrend entry gate enabled by default; red-regime gate optional; trading efficacy not established |
| Composite entry-quality ranking/filter | Yes — out-of-sample, net of warrant costs | Extension is measured; no stable standalone threshold |
| `+DI`/`-DI`, swing-high, or new entry rule | Yes — test each pre-specified rule | Proposed, not validated |
| Advisory challenger discovery and diagnostic data plumbing | No trading-efficacy backtest while decisions remain unchanged; correctness and capacity-isolation tests required | Rank-based top-N challenger discovery implemented; expanded delta/lookup-cost diagnostics planned |
| Positive minimum metric deltas for discovery | Held-out/replay efficiency and opportunity-coverage evaluation before filtering | No minimum delta applied; zero baseline retained, separate metric calibration planned |
| Momentum replacement | Yes — walk-forward/held-out with friction and churn | No replacement-policy backtest yet |
| Volatility-adjusted leverage | Yes — before nonzero score impact | Proposed |
| Gamma/convexity scoring | Yes — before nonzero score impact | Proposed; design and input validation remain |
| Weighted trend votes or score-weight optimization | Yes — requires sufficient data and holdout | Deferred |
| UI-only advisory display with no effect on ranking/decisions | Not for trading efficacy; normal correctness tests still apply | Weekly indicator is advisory; regime is displayed and may optionally block entries |
| ADX smoothing, standalone relative strength, standalone volume confirmation | No immediate implementation; repeat only with a new hypothesis | Existing experiments failed their gates |

## Rollout and Decision Records

For each completed workstream, record the data window, code/version, configuration,
universe coverage, results, limitations, and explicit pass/fail decision in its
detailed plan. If a hypothesis passes:

1. Add the policy behind a default-off setting with current behavior as the default.
2. Add focused tests for the new behavior and parity tests for the disabled setting.
3. Run a shadow/advisory or human-reviewed pilot before enabling any automatic effect.
4. Monitor the same pre-registered outcome and stop conditions used in validation.
5. Update [the strategy roadmap](improvement-roadmap.md) only after evidence and
   implementation agree.
