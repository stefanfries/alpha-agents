# Warrant Selection — Zero Free-Slot Search Plan

**Status:** Implemented
**Scope:** Warrant Selection entry-search behavior when Monitoring reports no free positions

## Goal

Avoid FinHub warrant searches for ordinary entry candidates when there are no free entry
slots, without changing candidate backfill when slots are available or the independent roll
replacement search.

## Problem

Monitoring intentionally returns all eligible screening candidates in
`MonitoringResult.entry_candidates`; it reports available capacity separately as
`free_positions` (`app/agents/monitoring.py`). The orchestrator passes the full candidate
list and sets `WarrantSelectionAgent.max_selected` to `free_positions`
(`app/orchestrator.py::_run_warrant_selection`).

`WarrantSelectionAgent.run()` currently starts `_pick_best()` for every entry candidate,
then applies `max_selected` while collecting completed results. Consequently, with 15 held
positions, no planned closes, and 22 eligible candidates, all 22 underlyings can trigger
warrant-list and warrant-detail requests even though the entry cap is zero. The progress
counter reports the input candidate count, not the number of possible selections.

Roll replacements are separate: `_select_rolls()` runs after entry processing and does not
consume entry slots. That behavior must remain intact.

## Proposed Behavior

- If `max_selected` is zero or negative, do not call `_pick_best()` for any ordinary entry
  candidate.
- Continue processing `roll_candidates` normally, even when there are no entry slots.
- If `max_selected` is positive, preserve the current screening-order selection and
  backfill behavior. Candidates after the slot cap may still need to be searched when
  earlier candidates have no suitable warrant.
- Do not classify unsearched candidates as `skipped` or as having no suitable warrant.
- Make the running/completed stage UI explicit: entry searches were skipped because there
  were no free slots, including the number of candidates not searched. If roll searches are
  still running, say that they continue independently. Do not display a stale or misleading
  `done / total` entry-search count.

## Implementation Steps

### 1. Guard entry work in the agent

In `WarrantSelectionAgent.run()`, derive the entry work list from `max_selected` before
creating `select_one()` tasks. Use an empty entry work list when the cap is zero; calculate
entry progress totals and gather tasks from that same list. Leave the later
`_select_rolls()` call untouched so roll replacements still run.

Keep `MonitoringResult.entry_candidates` uncapped. It is the candidate pool needed for
backfill whenever capacity is positive, and changing Monitoring would blur eligibility and
capacity responsibilities.

### 2. Report the no-search state clearly

Use the persisted Monitoring result (`free_positions` and `entry_candidates`) to show a
specific no-free-slots message on the Warrant Selection page. Distinguish candidates that
were not searched from candidates searched and rejected for warrant-quality reasons. Ensure
stage progress is initialized or cleared on rerun so an earlier progress count cannot remain
visible.

No new result-model field is expected unless deriving this message from the stored
Monitoring result proves insufficient.

### 3. Add focused regression coverage

- With `max_selected=0` and multiple entry candidates, assert that no entry-underlying
  `get_warrants` or detail calls occur, no entries are selected, and unsearched candidates
  are not marked as failed/no-warrant.
- With `max_selected=0` plus a roll candidate, assert the roll replacement search still runs
  and its existing scoring/selection outcome is unchanged.
- Keep the existing positive-cap backfill test passing (a candidate with no suitable warrant
  must not prevent a later candidate from filling an open slot).
- Verify the zero-slot UI message reports the candidate count without presenting it as
  active entry lookup progress.

### 4. Update documentation

Update the Warrant Selection agent spec to describe the zero-slot short circuit and clarify
that progress counts actual entry lookups, while roll searches are independent.

## Acceptance Criteria

- Zero free entry slots produce zero ordinary entry warrant searches.
- Roll replacement searches remain independent and functional at zero entry capacity.
- Positive-slot backfill semantics and screening rank order do not change.
- Unsearched candidates are not mislabeled as candidates with no suitable warrant.
- Progress/status text distinguishes eligible candidates from candidates actually searched.
- Focused tests and the full test suite pass; Ruff is clean.

## Implementation Status

Implemented in `WarrantSelectionAgent.run()`, the Warrant Selection running page, and
the restart progress reset. Regression coverage verifies zero and negative entry caps,
no entry warrant-list/detail calls, independent roll search, unchanged positive-cap
backfill, accurate unsearched-candidate messaging, and clearing stale progress.

## Out of Scope

- Capping or otherwise changing Monitoring's eligible candidate pool.
- Reducing searches when one or more slots are free; doing so safely requires preserving
  backfill and rank-order semantics and is a separate optimization.
- Changing warrant filters, scoring, roll guardrails, or portfolio sizing.
