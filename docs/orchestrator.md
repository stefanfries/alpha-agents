# Orchestrator Spec — Pipeline State Machine

## Responsibility

`Pipeline` manages the full lifecycle of a pipeline run: stage sequencing, per-stage
result persistence to MongoDB Atlas, HITL checkpoint pausing, config override application,
and restart-from-stage logic. It is the sole entry point for starting and advancing runs.

---

## Stage sequence

```text
universe → research → screening → warrant_selection → portfolio → risk → execution
```

Stage names are the canonical identifiers used in MongoDB documents, HTTP routes, and
log messages throughout the system.

---

## Execution model

Each stage is triggered by an explicit HTTP call — either from the web UI (user approves)
or from the orchestrator itself (auto-approve in non-HITL mode). The pipeline is **not**
a long-running async process; it executes one stage per HTTP request and persists its
result before returning. This makes the system resilient to process restarts (Azure
Container Apps scale-to-zero) between HITL reviews.

```text
POST /runs                         → create run document, trigger stage "universe"
stage completes                    → persist result, set status "awaiting_review", return
POST …/stages/universe/approve     → trigger stage "research"
...
POST …/stages/execution/approve    → mark run "complete"
```

In non-HITL mode, the orchestrator calls `_advance()` immediately after each stage
completes, without waiting for an HTTP approve.

---

## MongoDB run document

Each pipeline run is stored as a single document in collection `pipeline_runs`.

```json
{
  "run_id":          "a3f9c1",
  "created_at":      "2026-05-09T08:00:00Z",
  "indices":         ["DAX", "MDAX"],
  "hitl_mode":       true,
  "config_overrides": {},
  "current_stage":   "screening",
  "status":          "awaiting_review",
  "stages": {
    "universe":   { "status": "approved",         "completed_at": "...", "result": { ... } },
    "research":   { "status": "approved",         "completed_at": "...", "result": { ... } },
    "screening":  { "status": "awaiting_review",  "completed_at": "...", "result": { ... } },
    "warrant_selection": { "status": "pending" },
    "portfolio":  { "status": "pending" },
    "risk":       { "status": "pending" },
    "execution":  { "status": "pending" }
  }
}
```

`run_id` is a 6-character hex string derived from a UUID4. Stage `result` fields contain
the serialised Pydantic model (`.model_dump()`). Results are written atomically using
MongoDB `$set` on `stages.{stage_name}`.

### Stage statuses

| Status | Meaning |
| ------ | ------- |
| `pending` | Not yet run in this run |
| `running` | Currently executing |
| `awaiting_review` | Complete; waiting for user approval (HITL mode only) |
| `approved` | User approved (or auto-approved); result locked |
| `error` | Stage raised an exception; run is halted |

### Run statuses

| Status | Meaning |
| ------ | ------- |
| `running` | A stage is currently executing |
| `awaiting_review` | A stage completed and is waiting for user approval |
| `complete` | Execution stage was approved; run is finished |
| `error` | A stage failed; run is halted |

---

## Pipeline class interface

```python
class Pipeline:
    async def create_run(
        self,
        indices: list[str],
        capital_eur: float,
        hitl_mode: bool = True,
    ) -> str:
        """Create a run document in MongoDB and trigger the first stage. Returns run_id."""

    async def approve(
        self,
        run_id: str,
        stage: str,
        selection_override: list[str] | None = None,
    ) -> None:
        """
        Mark stage as approved and trigger the next stage.
        selection_override: tickers or ISINs the user kept checked (screening / warrant stages).
        """

    async def restart(
        self,
        run_id: str,
        from_stage: str,
        config_overrides: dict,
    ) -> None:
        """
        Reset all stages from from_stage onward to "pending", merge config_overrides
        into the run document, and trigger from_stage.
        """

    async def get_run(self, run_id: str) -> dict:
        """Return the full run document from MongoDB."""
```

`Pipeline` is instantiated once at application startup and held as a FastAPI dependency.

---

## Stage execution internals

When `_run_stage(run_id, stage_name)` is called:

1. Set `stages.{stage_name}.status = "running"` and `run.status = "running"` in MongoDB.
2. Build the per-run config: merge global `settings` with `run.config_overrides`.
3. Instantiate the agent for this stage with the merged config.
4. Read the previous stage's result from `stages.{prev_stage}.result` in the run document.
   Deserialise from dict to the appropriate Pydantic input model.
5. Run the agent: `result = await agent.run(input_model)`.
6. Write `stages.{stage_name}.result = result.model_dump()` and
   `stages.{stage_name}.completed_at = now()` to MongoDB.
7. If HITL mode: set `stages.{stage_name}.status = "awaiting_review"`,
   `run.status = "awaiting_review"`, `run.current_stage = stage_name`. Return.
8. If non-HITL mode: set `stages.{stage_name}.status = "approved"`. Call `_advance(run_id)`.

`_advance(run_id)` finds the next `pending` stage in the sequence and calls
`_run_stage(run_id, next_stage)`, or marks the run `complete` if no stages remain.

---

## Selection overrides (screening and warrant stages)

At the screening and warrant selection HITL checkpoints, the user can deselect tickers
or warrants via checkboxes before approving. The `approve` endpoint accepts a
`selection_override` list of identifiers (ticker symbols for screening; ISINs for warrants).

The orchestrator rewrites the stage result in MongoDB before advancing:

- **Screening**: replaces `result.selected` with the user's kept subset.
- **Warrant selection**: replaces `result.selected_warrants` with the user's kept subset
  and moves removed warrants' underlyings to `result.no_warrant_found`.

The modified result is what downstream stages read as their input.

---

## Config override handling

Config overrides are stored in `run.config_overrides` (a flat dict) and applied at
stage execution time. When `restart()` is called with new overrides, they are merged
into the existing `config_overrides` using `dict | new_overrides` (later values win).

The per-run config is built as:

```python
merged = settings.model_copy(update=run["config_overrides"])
```

Overrides are scoped to the stage they were entered for (see web-ui.md), but the
implementation stores them all in one dict — key names must not collide across stages.

---

## Restart semantics

When `restart(run_id, from_stage, config_overrides)` is called:

1. Merge `config_overrides` into `run.config_overrides` in MongoDB.
2. Set all stages from `from_stage` onward to `status = "pending"` and clear their
   `result` and `completed_at` fields.
3. Set `run.current_stage = from_stage`, `run.status = "running"`.
4. Call `_run_stage(run_id, from_stage)`.

Stages before `from_stage` are untouched — their approved results remain as inputs.

---

## Error handling

If `agent.run()` raises an exception:

- Log the full traceback.
- Write `stages.{stage_name}.status = "error"` and `stages.{stage_name}.error = str(exc)`
  to MongoDB.
- Set `run.status = "error"`.
- Do not advance the pipeline.

The web UI surfaces the error message on the stage review page. The user's only option
is to restart from the failed stage (or any earlier stage).

---

## FastAPI integration

The orchestrator is exposed via a FastAPI router at prefix `/runs`, mounted in the main
`app/main.py`. The router holds a single `Pipeline` instance as a module-level dependency.

```text
app/
  main.py          ← FastAPI app; mounts /runs router and /static
  routes/
    pipeline.py    ← /runs router; calls Pipeline methods
  templates/       ← Jinja2 templates (see web-ui.md)
orchestrator.py    ← Pipeline class
```

The current `orchestrator.py` (a plain async function runner) is replaced by the
`Pipeline` class above. `main.py` becomes the FastAPI application entry point instead
of a CLI script.

---

## Autonomous mode

When `hitl_mode=False`, the pipeline runs all stages without pausing. Each stage is
auto-approved immediately after completion. This is intended for scheduled/unattended
runs. All stage results are still persisted to MongoDB for post-run review.

The pipeline logs a warning at startup if `execution_dry_run=False` and
`hitl_mode=False` simultaneously — this combination would result in live orders
without human review.
