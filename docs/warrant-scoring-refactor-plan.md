# Warrant Scoring Refactor Plan

## Purpose

Capture an implementation-ready plan for warrant-side refactoring so work can continue later without losing context from this session.

## Session decisions to preserve

- Use lightweight, data-driven structure.
- Avoid class-per-policy or class-per-component explosion.
- Keep agent classes focused on orchestration and external calls.
- Place reusable evaluation logic under app/policies, not under app/agents.
- Keep persisted config keys, route contracts, and output schemas stable during refactor.
- Keep trend-detection and warrant-scoring abstractions separate:
  - Trend detection returns boolean rule outcomes and NEW/BREAK transitions.
  - Warrant evaluation returns float score contributions and final ranking.

## Current state snapshot

- Warrant scoring logic is concentrated in app/agents/warrant_selection.py in '_score(...)'.
- Scoring is currently a weighted sum of four factors:
  - spread
  - leverage
  - days to expiry
  - delta
- Output contract is stable and already consumed by downstream stages and templates:
  - WarrantSelectionResult.selected
  - WarrantSelectionResult.top3
  - WarrantSelectionResult.analyzed_count

## Refactor goals

- Improve readability and testability of scoring logic.
- Make it easy to add, remove, or tune score components.
- Keep behavioral parity by default.
- Keep score math centralized and reusable.

## Out of scope for this track

- No changes to FinHub fetching, retry policy, or concurrency settings.
- No changes to ADR override semantics.
- No changes to output schema fields or stage routing.
- No optimization changes unless explicitly requested later.

## Target structure

- app/policies/warrant_scoring.py
  - WarrantScoringConfig
  - score_spread(...)
  - score_leverage(...)
  - score_days_to_expiry(...)
  - score_delta(...)
  - compute_warrant_score(...)
  - build_score_explanation(...) optional helper for rationale strings

Keep app/agents/warrant_selection.py as the orchestrator that:

- fetches candidates
- fetches details
- calls compute_warrant_score(...)
- builds SelectedWarrant objects

## Proposed phases

### Phase W1: Extract scoring helpers with behavior parity

1. Create app/policies/warrant_scoring.py.
2. Move current formulas from _score(...) into small pure helper functions.
3. Add a WarrantScoringConfig dataclass with defaults matching current behavior.
4. Keep WarrantSelectionAgent._score as a thin compatibility wrapper that delegates to the new helper.

Success criteria:

- Same ranking order for unchanged input data.
- Same score values within floating-point tolerance.
- No changes in API or template payload contracts.

### Phase W2: Integrate config object cleanly

1. Construct and store WarrantScoringConfig once in WarrantSelectionAgent.__init__.
2. Pass config into scoring helper calls.
3. Keep constructor defaults and external behavior unchanged.

Success criteria:

- No route, orchestrator, or template changes required.
- Existing tests continue to pass.

### Phase W3: Rationale builder standardization

1. Extract rationale text formatting from _build(...) into a policy helper.
2. Keep the same default human-readable style unless explicitly changed.
3. Ensure handling of missing fields remains unchanged.

Success criteria:

- SelectedWarrant.rationale remains present and meaningful.
- No regression in None handling for spread, leverage, delta, maturity.

### Phase W4: Tests for scoring parity and edge cases

- Add focused tests for each scoring component:
  - spread edges
  - leverage peak behavior
  - expiry Gaussian behavior
  - delta peak behavior
- Add ranking-parity test for a small synthetic warrant set.
- Add missing-data test coverage for None values.

Success criteria:

- Stable ranking in deterministic fixtures.
- Missing fields never crash scoring.

### Phase W5: Optional extension points (later)

Only if requested after parity is stable:

- configurable weights from settings
- optional additional components (issuer flags, liquidity proxies, etc.)
- normalization strategy changes

## Behavioral guardrails

- Preserve formulas and constants in first extraction pass:
  - spread weight 0.40 and 3 percent linear cutoff
  - leverage weight 0.25, mean 5, sigma 3
  - expiry weight 0.20, mean 315, sigma 45
  - delta weight 0.15, peak at 0.5
- Preserve clamping and None handling semantics.
- Preserve capped warrant filtering behavior outside the scoring helper.

## Risks and mitigations

Risk: subtle ranking drift from accidental formula change.
Mitigation: add parity tests before and after extraction.

Risk: rationale text divergence.
Mitigation: keep formatting unchanged in W1 and defer formatting cleanup to W3.

Risk: over-abstraction.
Mitigation: keep helper functions simple and functional; no class-per-component pattern.

## Resume checklist for next session

- Start with Phase W1 only.
- Implement app/policies/warrant_scoring.py with parity formulas.
- Wire WarrantSelectionAgent._score to delegate.
- Run:
  - uv run ruff check .
  - uv run pytest tests/ -v
- If green, proceed to W2.

## Files expected to change in W1

- app/policies/warrant_scoring.py (new)
- app/agents/warrant_selection.py
- tests/test_pipeline.py and or new warrant-focused tests
- docs/agents/warrant_selection.md if needed for naming or helper references

## Naming conventions agreed

- Use domain naming: warrant_scoring, not generic policy names.
- Prefer explicit names like score_spread over vague names like component_a.
- Use config and helper function names that encode meaning clearly.
