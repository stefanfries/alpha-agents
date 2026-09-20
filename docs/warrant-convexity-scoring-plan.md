# Plan: Delta/Gamma Convexity Scoring for Warrant Selection

## Status: Proposed (not yet implemented)

## Motivation

The current entry-delta preference in `app/policies/warrant_scoring.py`
(`score_delta()`) is a symmetric triangle centered at `delta_peak=0.5`
(ATM): Delta 0.3 and Delta 0.7 are penalized identically. This treats
"sensitivity today" as the only criterion and ignores how that sensitivity
is expected to *change* as the underlying moves — which is what Gamma
describes.

For a trend-following system that enters on NEW and exits on BREAK (not
held to maturity), a warrant with a lower entry Delta (more OTM) but high
positive Gamma has an attractive asymmetric payoff shape:

- Underlying moves against the trade → Delta shrinks → further losses are
  dampened.
- Underlying moves with the trade → Delta grows → gains accelerate.

This is a distinct, complementary idea to
`docs/underlying-volatility-leverage-plan.md` (which addresses the
*leverage* target based on underlying volatility). This plan addresses the
*shape* of the payoff curve of the warrant itself via Delta/Gamma.

## Non-goals

- Not changing the leverage scoring component (see the volatility-leverage
  plan for that).
- Not changing spread/days-to-expiry scoring.
- Not adding new FinHub endpoints or API calls — all required inputs are
  already present in the existing `get_warrant_detail()` response.
- Not blindly trusting the provider's `analytics.gamma` field, since it has
  been observed to be `0.0` (likely rounded/imprecise) for some warrants in
  live data (e.g. FE58U7 / DE000FE58U71 on 2026-09-04), while
  `implied_volatility`, `delta`, `strike`, `underlying_price`, and
  `maturity_date` were populated and usable.

## Current state (relevant code)

- `app/policies/warrant_scoring.py`: `score_delta()` — symmetric triangle,
  peak at `delta_peak` (0.5 by default, or shifted by
  `WarrantSelectionAgent._range_adjusted_scoring_config()` based on the
  active strike-factor band).
- `app/agents/warrant_selection.py` `_score()`: reads `analytics.delta`,
  `analytics.leverage`, `market_data.spread_percent`,
  `reference_data.maturity_date` from the warrant detail response — does
  **not** read `analytics.gamma` or `analytics.implied_volatility` today.
- FinHub warrant detail response (`GET /v1/warrants/{identifier}`) already
  includes, per warrant: `reference_data.strike`, `reference_data.maturity_date`,
  `reference_data.underlying_price`, `analytics.delta`,
  `analytics.implied_volatility`, `analytics.gamma` (unreliable),
  `analytics.moneyness`.

## Design approach

Compute Delta and Gamma **ourselves** via Black-Scholes, using the already-
available `underlying_price`, `strike`, `days_to_maturity`,
`implied_volatility` fields, rather than trusting the provider's `gamma`
field. This avoids a new data source and avoids the precision problem
observed in live data.

1. **Add a pure Black-Scholes helper module.**
   New file `app/policies/black_scholes.py` (or a section in
   `warrant_scoring.py` if kept small) with stateless functions:
   - `bs_delta(spot, strike, days_to_expiry, iv, r=0.0) -> float | None`
   - `bs_gamma(spot, strike, days_to_expiry, iv, r=0.0) -> float | None`
   Both return `None` on invalid/missing inputs (spot/strike/iv <= 0, or
   days_to_expiry <= 0) — following the existing "handle None gracefully"
   convention in `warrant_scoring.py`. Risk-free rate `r` defaults to 0 to
   keep the model simple (consistent with the existing scoring module's
   preference for simplicity over precision).

2. **Add a convexity scoring component.**
   In `app/policies/warrant_scoring.py`, add
   `score_convexity(gamma: float | None, config) -> float`, following the
   same pattern as `score_leverage`/`score_delta` (weighted, Gaussian or
   linear falloff around a target Gamma range — exact shape TBD, see open
   questions). Wire it into `compute_warrant_score()` as an additional
   weighted term, and into `WarrantScoringConfig` as new fields
   (`convexity_weight`, plus whatever parameters the chosen shape needs).

3. **Compute Gamma at scoring time.**
   In `WarrantSelectionAgent._score()` ([warrant_selection.py](app/agents/warrant_selection.py#L539)),
   pull `reference_data.underlying_price`, `reference_data.strike`,
   `analytics.implied_volatility` from `detail` (alongside the existing
   `delta`/`leverage`/`spread_pct`/`maturity_date` reads), compute
   `gamma = bs_gamma(...)`, and pass it into `compute_warrant_score(...)`.

4. **New tunable settings.**
   `app/config.py` `WarrantScoringSettings`: add `convexity_weight` (default
   `0.0` — no-op until explicitly enabled/tuned) and the shape parameters
   for `score_convexity`. Re-normalize weights only if the user decides
   convexity should replace part of the existing delta weight rather than
   being additive (see open questions).

## Backward compatibility

- Default `convexity_weight=0.0` must reproduce identical scores/ranking to
  today — verified by existing parity tests in `tests/test_warrant_scoring.py`.
- Missing/invalid BS inputs (e.g. `implied_volatility` absent) → `gamma=None`
  → `score_convexity` returns `0.0`, matching the existing "None-safe"
  convention.

## Plan of work

1. Implement `bs_delta`/`bs_gamma` as pure functions with unit tests
   (known reference values for ATM/OTM/ITM cases, edge cases for
   zero/negative inputs).
   → verify: unit tests match textbook Black-Scholes values within
   floating-point tolerance.
2. Add `score_convexity()` + `WarrantScoringConfig` fields, default
   `convexity_weight=0.0`.
   → verify: existing `test_warrant_scoring.py` parity tests unchanged.
3. Wire Gamma computation into `WarrantSelectionAgent._score()`.
   → verify: new unit test — a warrant with higher computed Gamma scores
   higher than an otherwise-identical warrant with lower Gamma, once
   `convexity_weight > 0`.
4. Decide (see open questions) whether `delta_peak` should also shift lower
   by default now that convexity is scored separately, and implement if
   agreed.
5. Update docs: `docs/agents/warrant_selection.md`, `docs/data-models.md`
   (note the new internal Gamma computation), and
   `docs/warrant-scoring-refactor-plan.md` (cross-reference as a follow-on).
6. Run full suite (`uv run pytest tests/ -v`, `uv run ruff check .`) before
   marking complete.

## Open questions (need user input before implementation)

- **Additive vs. replacement:** should convexity be an additional weighted
  score term (existing `delta_weight` stays centered at ATM), or should the
  team also shift `delta_peak` lower (e.g. to 0.35–0.40) so entries are
  deliberately more OTM, with convexity purely as a tiebreaker among
  similar-Delta candidates?
- **Target Gamma shape:** peak-at-a-value (Gaussian, like leverage) or
  "higher is always better up to a cap" (monotonic, since positive Gamma is
  unambiguously good for a long call)? A monotonic-with-cap shape may be
  more appropriate here than the peaked shapes used for
  leverage/days/delta.
- **Risk-free rate `r`:** hardcode `0.0` (simplicity, consistent with the
  rest of the scoring model) or source a real short-term rate? Recommend
  `0.0` unless the user disagrees, given the model's existing preference
  for simple approximations (see `_STRIKE_DELTA_SENSITIVITY` comment in
  `warrant_selection.py` for a precedent of using a simplified constant).
- **Validation before rollout:** should this be backtested against
  historical executions (available in the `executions` MongoDB collection)
  before enabling with non-zero weight, to confirm OTM + high-Gamma entries
  would have outperformed the current Delta 0.5–0.7 practice?
