# Orchestrator Spec — Pipeline State Machine

## Responsibility

`Pipeline` manages the full lifecycle of a pipeline run: stage sequencing, per-stage
result persistence to MongoDB Atlas, HITL checkpoint pausing, config override application,
and restart-from-stage logic. It is the sole entry point for starting and advancing runs.

---

## Stage sequence

```text
universe → research → screening → monitoring → warrant_selection → portfolio → risk → execution
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
  "current_stage":   "monitoring",
  "status":          "awaiting_review",
  "stages": {
    "universe":          { "status": "approved",        "completed_at": "...", "result": { ... } },
    "research":          { "status": "approved",        "completed_at": "...", "result": { ... } },
    "screening":         { "status": "approved",        "completed_at": "...", "result": { ... } },
    "monitoring":        { "status": "awaiting_review", "completed_at": "...", "result": { ... } },
    "warrant_selection": { "status": "pending" },
    "portfolio":         { "status": "pending" },
    "risk":              { "status": "pending" },
    "execution":         { "status": "pending" }
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

## Warrant availability and ISIN overrides (ADRs)

Two stage runners integrate the global `warrant_availability` collection (see ADR-012):

- **`_run_universe`** — after resolving tickers, scans **only the ADR members**
  (`UniverseResult.adr_isins`) for an uncapped CALL warrant via
  `warrant_availability.scan(...)`, persisting results and surfacing progress. Regular
  stocks are not scanned.
- **`_run_warrant_selection`** — loads `warrant_availability.overrides_map()` and passes it
  to `WarrantSelectionAgent(isin_overrides=...)`. An override redirects warrant lookup to the
  override ISIN, derives the strike band from that underlying's live native-currency quote
  (falling back to bid/ask midprice when `/quotes` omits a last price, no FX), and sets each
  warrant's `chart_symbol`; the ADR remains the analyzed instrument.

---

## Monitoring stage internals

`_run_monitoring(run)` performs depot reconciliation between Screening and Warrant Selection:

1. Calls `_portfolio_max_positions(run)` — resolves the target position limit from execution `config_overrides.portfolio.max_positions`, or falls back to global `settings.portfolio.max_positions`.
2. Calls `_fetch_holdings(run)` — reads the latest depot snapshot for the linked QuantSystem, **excluding zero-quantity or negative-quantity positions** (e.g. pending settlement or correction entries).
3. If no holdings, returns all `SelectionResult.selected` tickers as entry candidates (full pass-through), with `free_positions = max_positions`.
4. Builds initial underlying names from screening universe (`SelectionResult.all_tickers` fallback `selected`): `{symbol -> name}`.
5. Calls `_fetch_warrant_underlying_map(run, holdings)` — layered resolver:
  last approved warrant-selection map; persisted `warrant_underlying_map` cache;
  FinHub `/v1/instruments/{identifier}` fallback (`isin` first, then `wkn`).
  The result can contain both key types (`warrant_isin` and `warrant_wkn`) mapped to `underlying_symbol`.
6. Normalizes mapped underlying symbols to screening symbols before monitoring decisions.
  Example: `ASML.AS` is normalized to `ASML` when `ASML` exists in `screening.trend_signals`.
7. Resolves held-warrant underlying ISIN via FinHub `/instruments` and prefers **universe names by ISIN** for monitoring display labels.
8. Fills remaining name gaps from cached fallback names only when universe names are unavailable.
9. Calls `_fetch_held_since(run)` — queries `virtual_depot_transactions` for the most recent BUY per WKN; returns `{wkn -> date}`.
10. Instantiates `MonitoringAgent` with the merged `MonitoringSettings` (global defaults overridden by `config_overrides.monitoring`) and delegates to it.
11. `MonitoringAgent.run()` evaluates each held position with trend-first priority (confirmed BREAK or aged-out BREAK-to-`None` sells, unconfirmed BREAK keeps, warrant-health checks only when trend is intact), then populates `trend_status`, `warrant_health_status`, `warrant_health_reason`, `decision_reason`, `screening_signal_present`, and `screening_signal`.
12. Monitoring is classification-only: no replacement lookup in `_run_monitoring`; `positions_to_roll` contains roll candidates and metadata exports `roll_underlyings`.
13. Calculates `free_positions = max(0, max_positions − len(current_holdings))` (`Free now`) and filters entry candidates to capped list. Positions whose underlying cannot be mapped are always kept (safe default).
14. `entry_candidates` = top `free_positions` screening candidates not in `excluded_symbols` (all held underlyings)

The `MonitoringResult` is stored as `stages.monitoring.result`. Downstream consumers:

- **`_run_warrant_selection`**: reads `monitoring.entry_candidates` (falls back to `screening.selected` if monitoring was skipped).
- **`_run_portfolio`**: reads `monitoring.positions_to_keep` → builds `kept_warrant_isins` set → passes to `PortfolioConstructionAgent`, which excludes kept warrants from `close_positions`.

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

---

## Bug fixes and corrections (2026-06-22)

### Monitoring free-capacity calculation (Issue: incorrect free positions reported)

Root causes:

1. No-holdings path computed `free_positions = min(candidates, max_positions)` instead of capacity
2. Holdings loader counted positions with quantity ≤ 0, inflating held count and reducing `Free now`
3. Max positions was hardcoded to global settings, ignoring execution-level config overrides

Fixes implemented:

- Added `_portfolio_max_positions(run)` resolver to honor execution `config_overrides.portfolio.max_positions`
- Modified `_fetch_holdings()` to skip all positions with quantity ≤ 0 (zero/negative quantities)
- Fixed no-holdings monitoring path to return `free_positions = max_positions` (full capacity)

Validation:

- 3 new integration tests: one for each bug fix
- Full test suite: 80 tests passing
- Example: With max_positions=20, no holdings, zero-qty depot entries → now correctly reports free_positions=20 (before: varied incorrectly)
