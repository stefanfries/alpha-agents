# Plan: Underlying-Volatility-Adjusted Leverage Target in Warrant Scoring

## Status: Proposed (not yet implemented)

## Motivation

`score_leverage()` in `app/policies/warrant_scoring.py` scores every warrant's
leverage against a single, global Gaussian (`leverage_mean=5.0`, `leverage_sigma=3.0`
from `WarrantScoringSettings`), regardless of the underlying's volatility. A 6x
warrant on a low-volatility stock (e.g. GILD, AMGN) and a 6x warrant on a
high-beta stock (e.g. DASH, beta ~2) currently score identically on the
leverage component, even though the effective risk is very different.

Observed case: DASH warrant FG3FUS scored 0.844 (good) but was the worst
performing position (-28.8%). This does not mean the score was "wrong" — it
means warrant quality and underlying risk are orthogonal, and the scoring
model has no mechanism to penalize high leverage on high-volatility
underlyings.

## Goal

Make the leverage-scoring target (and optionally leverage_sigma) a function of
the underlying's volatility, so that high-volatility underlyings are scored
favorably at lower leverage, while low-volatility underlyings keep the current
5-6x target range.

## Non-goals

- Not removing high-volatility underlyings from the investment universe.
- Not adding a new external data source — reuse data already computed in the
  pipeline (ATR20).
- Not changing spread/days-to-expiry/delta scoring components.
- Not changing FinHub fetching, retry policy, or concurrency settings.

## Current state (relevant code)

- `app/policies/warrant_scoring.py`: `WarrantScoringConfig.leverage_mean/leverage_sigma`,
  `score_leverage()` — single global config used for every underlying in a run.
- `app/agents/warrant_selection.py`: `_range_adjusted_scoring_config()` already
  derives a per-run (not per-underlying) `WarrantScoringConfig` by adjusting
  `days_mean`/`days_sigma`/`delta_peak` based on maturity window and strike
  band — this is the precedent pattern to extend per-underlying.
- `app/agents/warrant_selection.py`: `_pick_best(ticker, ...)` calls
  `self._score(detail, today)` which delegates to `compute_warrant_score(...,
  self._scoring_config)` — one shared config object for all tickers.
- `app/agents/screening.py` / `app/policies/trend_detection.py`: ATR20 is
  already computed per underlying via TA-Lib for the trend-quality formula
  (`R² × slope / ATR20`), but is not exposed outside the screening stage.
- `app/models/signals.py`: `SelectionResult` carries `scores`, `policy_results`,
  `trend_signals` per symbol — no volatility field yet.

## Data source decision

Use **ATR20 / current close price** ("ATR%") as the volatility proxy:

- Already computed in screening (no new API calls, no added latency).
- Simple, well-understood normalization of ATR to a percentage.
- Alternative considered: historical daily-return stdev or beta vs benchmark
  — more statistically standard, but requires additional computation or a new
  data source. Can be a future refinement; ATR% is the pragmatic first step
  since the data already exists.

## Design

1. **Expose ATR% from screening.**
   In `app/agents/screening.py` (or wherever ATR20 is available at the last
   bar per ticker), compute `atr_pct = atr20[-1] / close[-1]` per selected
   ticker.

2. **Add `volatility: dict[str, float]` to `SelectionResult`.**
   `app/models/signals.py` — new field `volatility: dict[str, float] = {}`
   mapping `symbol -> atr_pct`, populated alongside `scores`/`trend_signals`.

3. **Pass volatility into `WarrantSelectionAgent`.**
   `app/orchestrator.py._run_warrant_selection` passes
   `screening.volatility` into `WarrantSelectionAgent(...)`.

4. **Per-underlying scoring config.**
   In `app/agents/warrant_selection.py`, extend the existing adjustment
   pattern: add a method (parallel to `_range_adjusted_scoring_config`) that,
   given the per-run base config and a ticker's `atr_pct`, derives a
   per-ticker `leverage_mean` (and optionally `leverage_sigma`), e.g.:

   ```python
   leverage_mean = base_config.leverage_mean / (1.0 + k * atr_pct)
   ```

   with `k` as a new tunable weight (default TBD, e.g. 5.0) and the result
   clamped to a sane range (e.g. `[2.0, base_config.leverage_mean]`).
   Call this once per ticker inside `_pick_best`, producing a per-ticker
   `WarrantScoringConfig` used only for that ticker's `_score()` calls (top3
   included), instead of the single shared `self._scoring_config`.

5. **New tunable settings.**
   `app/config.py` `WarrantScoringSettings`: add
   `leverage_volatility_sensitivity: float` (the `k` factor above) and
   `leverage_mean_floor: float` (minimum allowed leverage target), both with
   safe defaults that reduce to current behavior when `atr_pct=0` or the
   feature is disabled (e.g. `k=0.0` as default to preserve behavior until
   explicitly tuned).

6. **Rationale/UI (optional, follow-up).**
   `build_warrant_rationale()` and `warrant_selection.html` could surface the
   effective per-ticker leverage target for transparency, so users can see
   why a high-vol underlying's warrant scored differently. Deferred unless
   requested.

## Backward compatibility

- Default `k=0.0` (or equivalent no-op default) must reproduce identical
  scores/ranking to today — verified by existing parity tests in
  `tests/test_warrant_scoring.py`.
- `SelectionResult.volatility` defaults to `{}`; missing entries (e.g. ADR
  overrides where ATR wasn't computed for the override symbol) fall back to
  the base `leverage_mean` — matches existing "handle None gracefully"
  convention used throughout warrant scoring.

## Plan of work

1. Compute and expose `atr_pct` per ticker from screening into
   `SelectionResult.volatility`.
   → verify: unit test confirms `volatility` dict populated for tickers with
   sufficient bar history, empty/omitted for tickers lacking data.
2. Add `leverage_volatility_sensitivity` / `leverage_mean_floor` to
   `WarrantScoringSettings`, defaulting to no-op.
   → verify: existing `test_warrant_scoring.py` parity tests still pass
   unchanged.
3. Add per-ticker scoring-config derivation in `WarrantSelectionAgent`,
   wired into `_pick_best` (and `_select_rolls` for roll candidates using the
   roll's underlying volatility).
   → verify: new unit tests — same warrant scores lower with a higher
   `atr_pct` input and identical raw leverage/spread/delta/days values.
4. Wire `screening.volatility` through `app/orchestrator.py` into
   `WarrantSelectionAgent(...)`.
   → verify: `tests/test_pipeline.py` integration test that a high-volatility
   underlying's selected warrant differs from a low-volatility case under a
   non-zero sensitivity setting.
5. Update docs: `docs/agents/warrant_selection.md`, `docs/data-models.md`
   (new `SelectionResult.volatility` field), `docs/warrant-scoring-refactor-plan.md`
   (append a note that this is a follow-on to the completed W1-W4 refactor).
6. Run full suite (`uv run pytest tests/ -v`, `uv run ruff check .`) before
   marking complete.

## Open questions (need user input before implementation)

- Preferred default value for `k` (`leverage_volatility_sensitivity`) — needs
  a starting point that can be tuned via `.env`, not guessed silently.
- Whether to also scale `leverage_sigma` with volatility (narrower band for
  high-vol names) or leave it as `base_config.leverage_sigma` for simplicity.
- Whether roll-candidate evaluation (`_select_rolls`) should also use the
  volatility-adjusted config for the incumbent's score, for consistency.
