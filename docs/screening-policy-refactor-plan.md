# Trend Detection Policy Refactor Plan

## Goal

Improve readability and extensibility of trend-detection policy evaluation without changing user-visible behavior, config keys, or pipeline contracts.

## Why this approach

- The current complexity mostly comes from repeated policy plumbing and long function signatures.
- A class-per-policy design would add boilerplate and spread logic further.
- A small config object + reusable boolean evaluator gives the same flexibility with less code.

## Scope boundaries

In scope now:

- Trend-marker path used by the screening chart route.
- Shared policy configuration object.
- Shared boolean group evaluator.

Out of scope now:

- Warrant scoring/ranking refactor (handled in a later phase).
- Any behavior changes to NEW/BREAK signal semantics.
- UI changes and config key renames.

## Phase plan

### Phase 1 (implemented)

1. Add a shared trend-detection policy config object.
2. Add a reusable boolean group evaluator.
3. Refit marker computation to consume one config object instead of many separate policy parameters.

Implemented artifacts:

- `app/policies/trend_detection.py`
  - `TrendDetectionPolicyConfig`
  - `passes_rule_group(...)`
  - `TrendDetectionPolicyConfig.from_mapping(...)`
- `app/agents/screening_policy.py` (compatibility shim)
  - aliases for legacy imports
- `app/routes/pipeline.py`
  - `_compute_signal_markers(bars, policy_cfg)` now takes one policy config object.
  - `chart_screening(...)` now builds policy config via `TrendDetectionPolicyConfig.from_mapping(scr_cfg)`.

Behavior guarantees preserved in this phase:

- Same NEW/BREAK state machine semantics.
- Same policy names and defaults.
- Same marker output schema and chart payload fields.

### Phase 2 (in progress)

1. Reuse the same trend-detection config object and boolean evaluator inside `SecuritySelectionAgent`. ✅
2. Remove duplicated policy-group threshold code from screening agent internals. ✅
3. Keep all output contracts unchanged (`SelectionResult`, `policy_results`, `trend_signals`). ✅

Phase 2 note:

- Shared trend-indicator snapshot and per-bar boolean indicator evaluation are now reused by both
  `SecuritySelectionAgent` and `_compute_signal_markers(...)` in the screening chart route.

### Phase 3 (optional hardening)

1. Add targeted parity tests for policy evaluation and marker transitions.
2. Add fixture-based tests that compare old/new behavior on representative bar series.

### Phase 4 (later, separate track)

1. Introduce a separate scoring-helper set for warrant ranking.
2. Keep float scoring independent from boolean policy evaluation.

## Package structure guidance

- Keep reusable policy logic under `app/policies/`.
- Keep `app/agents/` focused on orchestration and stage flow.
- Start with flat files:
  - `app/policies/trend_detection.py`
  - `app/policies/warrant_scoring.py` (later)
- Add subfolders only when each domain accumulates enough files to justify it.

## Non-negotiable compatibility rules

- Do not rename persisted screening config keys.
- Do not change route payload structure used by templates.
- Do not alter NEW/BREAK/HOLD/None signal meaning.
- Do not introduce policy class explosion.

## Validation checklist per phase

- `uv run ruff check .`
- `uv run pytest tests/ -v`
- Manual chart sanity check on screening stage:
  - marker count is plausible
  - NEW/BREAK marker placement unchanged for same input config
