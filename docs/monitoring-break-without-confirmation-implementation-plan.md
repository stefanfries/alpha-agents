# Monitoring BREAK Without Confirmation - Implementation Plan

Status: Completed 2026-07-12
Date: 2026-07-07
Owner: Monitoring + Screening pipeline

## Goal

Reduce SELL latency by removing candle-confirmation waiting for BREAK actions, while preserving explainability through richer trend-status reasons in Monitoring UI.

Secondary goal: maximize code simplification by deleting confirmation-related logic and data-model fields instead of preserving legacy compatibility paths.

## Problem Statement

Current monitoring behavior delays SELL actions:

- BREAK on current candle does not sell immediately.
- Position is kept as "break signal, not confirmed yet".
- SELL only occurs after confirmation logic marks BREAK as confirmed.

This adds lag on top of already-lagging indicators and can increase drawdown before exit.

## Proposed Product Behavior

### 1) Exit timing

- SELL immediately when BREAK condition is active in current run.
- Remove dependency on candle-confirmation state for SELL decisions.

### 2) Trend status column semantics

Replace confirmation-centric statuses with degradation-reason statuses.

Target statuses:

- trend intact
- trend degraded: <primary_reason>
- trend degraded: <primary_reason> (+N)
- no screening signal (unchanged fallback)

Examples:

- trend degraded: SuperTrend bearish
- trend degraded: EMA20 falling (+1)
- trend degraded: Price below EMA50 (+2)

### 3) BREAK policy setup (user strategy)

Initial intended configuration:

- SuperTrend bearish = on
- EMA20 falling = on
- ADX below threshold = on
- ADX falling = on
- Price below EMA50 = on
- Minimum selected = 2

Note: keep this configurable through existing screening overrides and UI.

## Scope

In scope:

- Monitoring action logic
- Monitoring trend-status text generation
- Optional enrichment of screening output consumed by monitoring
- Monitoring UI labels/tooltips for degradation reasons
- Tests and docs updates
- Removal of confirmation-era code and model fields
- Clean-slate data strategy (drop run/execution collections when deploying this change)

Out of scope:

- Rework of entry (NEW) policy logic
- Changes to warrant health thresholds
- Portfolio sizing and execution sequencing

## Design Principles for This Change

1. No backward compatibility for confirmation-era data or contracts.
2. Prefer deletion over adaptation when code only exists for confirmation semantics.
3. Keep only fields and branches needed for immediate-BREAK behavior.
4. Accept DB reset as deployment prerequisite for simplified models.

## Technical Design

### A) Remove confirmation gating in Monitoring

Current code path:

- Uses has_exit_signal plus is_break_confirmed to decide sell/keep.
- Unconfirmed BREAK leads to KEEP with reason "break signal, not confirmed yet".

Planned change:

- has_exit_signal true implies immediate SELL.
- Delete is_break_confirmed usage entirely.
- Delete `trend_signal is None` / "confirmed earlier" semantics.
- Delete reason text and UI labels tied to pending/confirmed distinction.

Likely touchpoints:

- app/agents/monitoring.py
  - _decide_action
  - _trend_status
  - run decision branch
  - remove dead helper methods related to confirmation transitions

### B) Expose degradation reasons for trend status

Need an ordered reason list per symbol indicating which BREAK rules are currently true.

Preferred source:

- Screening stage should provide per-symbol BREAK rule booleans (already available in policy_results).

Implementation option:

- Add a small helper in Monitoring to derive active BREAK reasons from screening policy payload for mapped underlying symbol.
- Select primary reason by deterministic priority order.
- Build status string:
  - one reason: trend degraded: \<reason\>
  - multiple: trend degraded: \<reason\> (+N)

Reason priority (initial):

1. Price below EMA50
2. SuperTrend bearish
3. EMA20 falling
4. ADX falling
5. ADX below threshold

Likely touchpoints:

- app/agents/monitoring.py
- app/models/signals.py (only if additional field needed)
- app/templates/stages/monitoring.html (rendering consistency)

### C) Data contract decision

Choose one of these in session kickoff:

Option 1 (minimal data contract change):

- Derive degraded reasons in Monitoring from existing trend signal and available fields only.
- Lower implementation risk, less detailed reason accuracy.

Option 2 (recommended):

- Persist explicit BREAK reason flags from Screening and pass to Monitoring input.
- Higher transparency and more robust diagnostics.

Recommended: Option 2.

Final decision for implementation: Option 2 without compatibility shims.

## Detailed Step Plan

### Step 1 - Baseline and safety net

1. Create branch for change.
2. Capture target behavior tests only (immediate BREAK sell semantics).
3. Do not preserve tests that validate pending/confirmed confirmation behavior.

Verification:

- Existing tests pass.
- Removed confirmation tests are replaced by immediate-sell tests.

### Step 2 - Flip action semantics to immediate sell

1. Update monitoring decision logic to sell on active BREAK without confirmation wait.
2. Remove obsolete reason strings tied to confirmation waiting.
3. Remove obsolete trend states: BREAK pending, BREAK confirmed, BREAK confirmed earlier.
4. Keep fallback handling for missing signal key unchanged.

Verification:

- Unit tests for monitoring:
  - BREAK active -> SELL
  - NEW/HOLD -> KEEP unless warrant degradation rules trigger ROLL
  - missing signal -> KEEP with explicit rationale

### Step 3 - Add degradation reason rendering

1. Implement reason extraction helper and reason-priority mapping.
2. Populate trend_status with degraded reason text.
3. Preserve status badge styles in monitoring template.

Verification:

- Unit tests for reason selection order.
- Snapshot-style assertion for trend_status strings:
  - single reason
  - multi-reason (+N)
  - intact

### Step 4 - Wire policy payload (if Option 2)

1. Ensure BREAK rule booleans are persisted and available to Monitoring input.
2. Add model fields as needed with lean required contracts (no legacy aliases/default shims).
3. Remove unused confirmation-era fields from models and pipeline payloads.

Verification:

- Pipeline tests for new contracts succeed after DB reset.

### Step 5 - Update docs

1. Update monitoring agent spec for immediate SELL semantics.
2. Update any monitoring enhancement docs that still mention confirmation gating.
3. Add a short runbook for tuning BREAK min-selected.

Verification:

- Docs reflect final behavior and no contradictory statements remain.

## Test Plan

Required automated tests:

- tests/test_monitoring.py
  - immediate sell on BREAK
  - no "break signal, not confirmed yet" branch remains
  - degradation reason text generation
- tests/test_pipeline.py
  - monitoring stage output contract unchanged or intentionally versioned
  - restart/approve flow unaffected

Optional integration checks:

- Manual UI pass on Monitoring page:
  - Trend status text appears as degraded reasons
  - SELL rows align with current BREAK state in same run

## Migration and Backward Compatibility

Backward compatibility is intentionally not required.

Deployment assumption:

1. Delete old execution/run data collections and restart from clean state.
2. Remove legacy model fields and compatibility branches in code.

Operational note:

- Keep a manual backup/export only if historical analytics are needed later.

## Risks and Mitigations

Risk: Increased false exits (whipsaws)
Mitigation: Use BREAK minimum selected = 2 and monitor sell/re-entry churn.

Risk: Ambiguous reason labels
Mitigation: deterministic priority order and explicit label mapping.

Risk: Contract mismatch between Screening and Monitoring
Mitigation: add tests on full stage payload path for the new strict contract.

Risk: Loss of historical run data after cleanup
Mitigation: explicitly accept this tradeoff; export snapshots before drop if needed.

## Rollout Strategy

1. Stop app and drop run-related collections (executions and dependent stage outputs).
2. Deploy simplified code and data models (no confirmation artifacts).
3. Run with intended BREAK config (all enabled, minimum selected 2).
4. Observe for 2 to 4 weeks:
   - average delay from first break condition to sell
   - sell frequency
   - quick re-entry rate
5. If whipsaw rises too much, adjust minimum selected or disable ADX-below rule first.

## Rollback Plan

If behavior is too aggressive:

1. Revert to pre-change commit/tag.
2. Restore DB from backup if historical runs are needed.
3. Re-enable previous confirmation-based semantics only via full rollback.

## Session Kickoff Checklist (next session)

1. Confirm strict simplification mode (no backward compatibility, DB reset accepted).
2. Confirm Option 2 (explicit BREAK reason payload) for reason diagnostics.
3. Confirm reason label text and priority order.
4. Confirm target BREAK defaults for your runs.
5. Implement Step 2 first, then Step 3, then Step 4.
6. Run tests and manual Monitoring UI validation before commit.

## Explicit Deletion Checklist (to avoid leaving dead complexity)

Delete confirmation-related items during implementation:

1. Confirmation state inputs/fields in Monitoring (`break_confirmed_symbols` and related plumbing) where no longer required.
2. Trend status labels and UI branches for pending/confirmed variants.
3. Reason strings and logs mentioning "not confirmed yet" or "confirmed earlier".
4. Orchestrator helpers and persistence paths used only for confirmation tracking (for example first-break date propagation if solely used for confirmation).
5. Tests that validate confirmation waiting behavior.

Keep only:

1. Active BREAK evaluation.
2. Reason extraction and status rendering for degraded trend.
3. Warrant-health logic and existing ROLL behavior.
