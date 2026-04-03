# ADR-005: Man-in-the-Loop (MITL) Review Pattern

## Status

Proposed

## Context

During development the system should present the output of each pipeline stage to the user for review before proceeding. The user must be able to:

1. Inspect the output of any stage
2. Approve it (continue to the next stage)
3. Reject it and restart from a specific earlier stage, optionally with different parameters

In production, the pipeline should be able to run fully autonomously (all checkpoints auto-approved).

## Decision

### Persistence: MongoDB Atlas

All intermediate stage outputs are persisted to a MongoDB Atlas collection `pipeline_runs` as documents of type [`PipelineRun`](../data-models.md#pipelinerun). MongoDB is already available in the project's infrastructure (see `comdirect_api` sibling project) and is ideal here because:

- Stage outputs are naturally document-shaped (nested Pydantic models → JSON)
- No schema migration needed when data models evolve
- Built-in querying for historical run comparison
- The user can inspect results using MongoDB Atlas UI or Compass without additional tooling

### MITL checkpoint protocol

After each agent produces its output, the orchestrator:

```text
1. Serialise output → call output.model_dump()
2. Upsert StageRecord into PipelineRun.stages[stage_name] in Atlas
3. If mitl_mode=True:
   a. Print a summary table of the stage output to the CLI
   b. Prompt: "Continue → [Enter] | Restart from <stage> → type stage name | Quit → q"
   c. On continue: proceed to next agent
   d. On restart: update PipelineRun.status = "paused"; re-enter pipeline at named stage
   e. On quit: update PipelineRun.status = "paused"; save run_id for resumption
4. If mitl_mode=False (autonomous): auto-approve and continue
```

### Stage names (restart targets)

| Stage name | Agent |
|------------|-------|
| `universe` | UniverseAgent |
| `research` | ResearchAgent |
| `stock_selection` | StockSelectionAgent |
| `warrant_selection` | WarrantSelectionAgent |
| `portfolio` | PortfolioConstructionAgent |
| `risk` | RiskAgent |
| `execution` | TradeExecutionAgent |

### Resumption

A previous run can be resumed (from any stage) by passing its `run_id` to `Pipeline.run()`. The orchestrator loads the persisted stage outputs from Atlas and replays from the target stage.

### Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `mitl_mode` | `True` | Enable man-in-the-loop checkpoints |
| `mongodb_uri` | required | MongoDB Atlas connection URI (from `.env`) |
| `mongodb_db` | `"alpha_agents"` | Database name |

## Alternatives considered

### Flat file persistence (JSON / SQLite)

Simpler to set up locally but lacks querying capability and is harder to inspect visually. MongoDB Atlas provides a hosted UI (Atlas / Compass) usable by non-technical users. **Rejected** in favour of Atlas.

### Interactive web UI

A full web UI (e.g. FastAPI + React) would be ideal in the long run but adds significant frontend development effort. The CLI MITL approach is sufficient for development-phase usage. **Deferred** to a future ADR.

## Consequences

- `config.py` must add `mongodb_uri`, `mongodb_db`, and `mitl_mode` settings
- The `orchestrator.Pipeline` must be refactored to call a `_checkpoint()` method after each stage
- A `models/persistence.py` module must be added for `PipelineRun` and `StageRecord` models
- A `tools/mongodb.py` tool must be added wrapping `motor` (async MongoDB driver) or `pymongo`
- All agent output models must remain fully serialisable via `model_dump()` (no non-JSON-safe types)
