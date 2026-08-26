# Roll-Candidate Warrant Selection — Implementation Plan

Status: **Implemented (2026-08-24/25)** — replacement search, guardrail, output wiring,
and UI are done. Roll **execution** (pairing SELL incumbent + BUY replacement) is the
remaining follow-up; see "Next session" at the bottom.

**Update (2026-08-26):** when no replacement clears the score margin, the stage now
recommends **SELL** for the incumbent instead of **KEEP** — every candidate reaching
this path is already known-degraded (that's why monitoring classified it as ROLL), so
keeping it silently was misleading. See `WarrantSelectionResult.roll_sell_underlyings`
/ `sell_existing_isins` (renamed from `roll_keep_underlyings` / `keep_existing_isins`).

Scope: Warrant Selection stage (replacement discovery for ROLL candidates) + UI
Owner: Strategy / pipeline

## Purpose

Monitoring classifies degraded-but-trend-intact positions as **ROLL** candidates and
emits `roll_underlyings`, but no replacement warrant is ever selected for them. The
warrant selection stage currently searches only over `entry_candidates`; roll
underlyings are held positions, so they sit in `excluded_symbols` and are never
searched. This plan implements same-underlying replacement discovery in the warrant
selection stage, per the authoritative design in
[ADR-011](decisions/ADR-011-portfolio-monitoring.md) and
[orchestrator.md](orchestrator.md) (monitoring is classification-only; warrant
selection owns replacement discovery).

This supersedes the `_find_roll_replacement`-in-monitoring sketch in
[monitoring-enhancement-plan.md](monitoring-enhancement-plan.md) §6, which predates the
classification-only decision.

## Existing scaffolding (already present, currently unpopulated)

> Historical note: this section described the pre-implementation state (2026-08-24,
> before R1–R5). Kept for context; see "Implementation status" below for what actually
> shipped.

- `app/models/signals.py`: `RollReplacement` model, `PositionReview.roll_replacement`,
  and `WarrantSelectionResult.{roll_underlyings, roll_keep_underlyings,
  keep_existing_isins}` — all defined, none populated by real logic.
- `app/orchestrator.py` `_run_warrant_selection`: assigns `roll_underlyings` /
  `roll_keep_underlyings` as pass-through metadata only (no search).
- `app/templates/stages/warrant_selection.html`: renders the `ENTRY` / `ROLL` /
  `ROLL/KEEP` Type badge, but roll rows never reach the table today.

## Design decisions (agreed)

1. **Guardrail: score-margin only.** Replace only if
   `best_replacement.score >= incumbent_score + roll_min_improvement`; otherwise
   recommend **SELL** for the incumbent (`ROLL/SELL`). No separate "replacement must be
   non-degraded" gate — the incumbent is already known-degraded and the four scoring
   components (spread, leverage, days, delta) already capture warrant health.
2. **`roll_min_improvement` default `0.10`** to avoid overtrading (same-underlying,
   lower-risk swap; deliberately conservative).
3. **Slot isolation.** Rolls are 1:1 replacements and must **not** consume
   `free_positions`. The roll search runs independently of the `max_selected` entry cap.
4. **Reuse existing search + scoring.** Roll replacement reuses `_pick_best()` and
   `compute_warrant_score` unchanged — no new search or scoring logic.

## Implementation status (as of 2026-08-25)

All of R1–R5 and UI items 1–3+5 are implemented and covered by tests
(`tests/test_pipeline.py::test_warrant_selection_rolls_when_replacement_better`,
`::test_warrant_selection_keeps_incumbent_when_replacement_not_better`,
`::test_warrant_selection_rolls_ignore_entry_slot_cap`). 157 tests pass, ruff clean.

### Implementation steps (as executed)

### R1 — Feed roll candidates into warrant selection (data flow)

- In `app/orchestrator.py` `_run_warrant_selection`, pass `monitoring.positions_to_roll`
  into `WarrantSelectionAgent` alongside `entry_candidates`. Each `PositionReview`
  already carries the incumbent `warrant_isin`, `warrant_wkn`, `spread_pct`, `leverage`,
  `delta`, `days_to_maturity` — enough to re-score the incumbent.
- Remove the current pass-through assignment of `roll_underlyings` /
  `roll_keep_underlyings` in the orchestrator (the agent will now populate them).
- **Verify:** roll underlyings reach the agent (orchestrator wiring test).

### R2 — Replacement search + score-margin guardrail (agent)

- Add a roll path in `WarrantSelectionAgent.run()` that, for each roll underlying, runs
  the existing `_pick_best()` search (same strike/maturity/spread/scoring machinery).
- Re-score the incumbent with `compute_warrant_score` from its snapshot metrics.
- Apply the guardrail: if `best_replacement.score >= incumbent_score +
  roll_min_improvement` → ROLL (record replacement); else → ROLL/KEEP.
- Run the roll search independently of the `max_selected` entry cap (slot isolation).
- **Verify:** strictly-better replacement rolls; marginal replacement downgrades to KEEP;
  no replacement found → KEEP incumbent.

### R3 — Output wiring

Roll replacements are stored in **dedicated** result fields, **not** in `selected`.
Rationale: `_run_portfolio` turns every `warrant_result.selected` entry into a BUY and
already keeps roll incumbents via `kept_warrant_isins`. Appending replacements to
`selected` would buy the replacement while keeping the incumbent (doubled position).
Executing the roll (pairing SELL(incumbent) + BUY(replacement)) is a separate
execution-stage change — deferred to the follow-up below.

- Add to `WarrantSelectionResult`:
  - `roll_selected: list[SelectedWarrant]` — chosen replacement warrants (better).
  - `roll_incumbents: dict[str, RollReplacement]` — incumbent snapshot per symbol
    (re-scored via `compute_warrant_score`) for the before→after UI card.
- Populate existing `roll_underlyings` (replacement found & better),
  `roll_keep_underlyings` (searched, not better enough), `keep_existing_isins`
  (incumbent ISINs kept).
- Remove the orchestrator pass-through assignment of `roll_underlyings` /
  `roll_keep_underlyings` (the agent now populates them).
- **Verify:** result schema populated; portfolio behavior unchanged (`selected`
  unchanged); no double-writes from the orchestrator.

### Follow-up (out of scope here) — execute the roll

Wiring the roll into portfolio/execution (SELL incumbent + BUY replacement, close the
incumbent only for confirmed rolls, keep it for `roll_keep`) is a separate increment.
Until then, roll replacements are selected and visualized but not traded, matching the
current behavior where monitoring keeps roll incumbents.

**This is the next planned increment — see "Next session" at the bottom of this doc.**

### R4 — Config

- Add `roll_min_improvement: float = 0.10` to `WarrantSelectionSettings`
  (`app/config.py`), tunable via `.env` (`WARRANT_SELECTION__ROLL_MIN_IMPROVEMENT`).
- No other new config.

### R5 — Tests

- Unit: incumbent re-scoring; better / marginal / no-replacement branches; slot
  isolation from entries.
- Pipeline: held+degraded position → ROLL row with replacement; marginal case →
  ROLL/KEEP. Reuse existing FinHub stub patterns in `tests/test_pipeline.py`.
- Run `uv run pytest tests/ -v` and `uv run ruff check .` — all green.

## UI visualization

Items 1–3 + 5 are implemented (revised twice from the original sketch based on visual
review); item 4 remains optional/deferred.

1. **Grouped Rolls table, placed above New Entries.** Uses the *same column set and
   widths* as the Entries table (shared `<colgroup>` + `table-layout:fixed` for strict
   alignment): `#` (— for roll rows, no screening rank applies), Type, Underlying,
   Analyzed, WKN, ISIN, Strike, Maturity, Spread, Lev, Delta, Score. Each roll candidate
   renders as **two stacked rows**: `ROLL` (incumbent, muted, not clickable) and `NEW`
   (replacement, clickable). `KEEP` rows (roll candidates where no replacement cleared
   the margin) render as a single clickable row. Badge text was shortened from
   `ROLL FROM`/`ROLL TO` to `ROLL`/`NEW` to save column width.
2. **Before → After comparison, reusing the existing detail-panel mechanism** — no
   separate comparison card was built. Clicking the `NEW` or `KEEP` row uses the exact
   same `loadWarrantDetailPanel()` JS call as an Entries row, showing the top-3
   alternatives analyzed for that underlying (via `top3`/`analyzed_count`, now merged
   from both the entry and roll search paths). For `KEEP` rows this makes it visible
   that *all* searched alternatives scored worse than the incumbent — this was an
   explicit ask during review. The incumbent (`ROLL` row) shows its own strike/maturity/
   spread/leverage/delta/score inline (from `roll_incumbents`) plus a green ▲ score-delta
   badge on the `NEW` row when a replacement was selected.
3. **`KEEP` inline explanation**: a muted italic row under each `KEEP` pair reads
   *"No replacement scored ≥ {margin} above incumbent — click to see the N
   alternative(s) analyzed, all worse than the incumbent."*
4. **Summary counts** at top: `"X new entries · Y rolls · Z kept (replacement worse) ·
   W skipped"`.

Data plumbing needed for this: `strike`/`maturity_date` were not previously captured on
the incumbent side, so `WarrantSnapshot` (`app/agents/monitoring.py`), `PositionReview`
and `RollCandidate` (`app/models/signals.py`) all gained `strike`/`maturity_date`
fields, populated in `Pipeline._fetch_warrant_snapshots` via a new
`_parse_maturity_date` helper (extracted from `_days_to_maturity`) and threaded through
to `RollReplacement` in `_select_rolls()`. This also improved roll scoring accuracy —
the incumbent's real `maturity_date` is now used instead of the days-based
approximation when available.

### Optional (deferred)

5. **Dual-strike chart overlay.** On a ROLL/NEW row, plot both strike lines (incumbent
   dashed/grey, replacement solid) and both maturity markers on the underlying chart.
   Requires a small change to the warrant_selection chart endpoint; defer to a follow-up.

## Success criteria

- [x] ROLL candidates produce a replacement warrant when a meaningfully better one
      exists (score margin ≥ 0.10), otherwise ROLL/KEEP.
- [x] Roll search does not consume entry slots.
- [x] Roll rows are visible and explained in the UI (items 1–3 + 5).
- [x] All tests and ruff pass (157 tests, clean lint, as of 2026-08-25).
- [ ] Roll is actually **executed** (SELL incumbent + BUY replacement) — not done yet,
      see "Next session" below.

## Next session (resume here)

The remaining, explicitly deferred piece is **executing** the roll — today, roll
replacements are selected, scored, and visualized, but never traded. To close the loop:

1. **Portfolio stage** (`app/agents/portfolio.py`, `Pipeline._run_portfolio`): decide how
   `WarrantSelectionResult.roll_selected` feeds into `PortfolioProposal`. Likely needs a
   new `roll_positions` (or similar) list distinct from `new_positions` so it can be
   paired with a close of the specific incumbent ISIN it replaces — a plain BUY (as done
   for `selected`) would double the position since `kept_warrant_isins` still protects
   the incumbent from closure.
2. **Incumbent closure**: for a confirmed roll, the incumbent's `warrant_isin` must move
   from "kept" to "closed" — currently `_run_portfolio` unconditionally adds
   `positions_to_roll` ISINs to `kept_warrant_isins`. Needs to become conditional on
   whether that underlying ended up in `roll_underlyings` (replaced) vs
   `roll_keep_underlyings` (still kept).
3. **Execution stage** (`app/agents/execution.py`): confirm SELL(incumbent) and
   BUY(replacement) can be emitted as a paired trade (or two independent orders) without
   breaking `execution_dry_run` semantics.
4. **Risk stage**: check whether `RiskAgent` needs awareness of roll pairs (e.g. netting
   exposure) or can treat them as independent approved/rejected positions.
5. Add tests mirroring the existing pipeline integration tests
   (`tests/test_pipeline.py`), asserting a roll produces exactly one SELL + one BUY (not
   a naked BUY) end-to-end.
6. Optionally revisit UI item 4 (dual-strike chart overlay) once execution wiring is
   confirmed useful in practice.

Relevant current constraints to keep in mind (do not regress):

- Portfolio must not double-buy: `roll_selected` is deliberately kept out of `selected`.
- Rolls must not consume `free_positions` / `max_selected` entry slots.
- Guardrail stays score-margin only (`roll_min_improvement`, default `0.10`).
