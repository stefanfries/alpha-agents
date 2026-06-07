# ADR-010 — Quant System Redesign: from Pipeline Runs to Named Quant Systems

**Date:** 2026-06-07
**Status:** Accepted
**Supersedes:** ADR-001 (Orchestration), ADR-005 (Human-in-the-loop), ADR-008 (Web UI) — all remain valid but are extended by this ADR

---

## Context

The original design exposed a single concept: a *run*, which bundled both the pipeline configuration (which indices, how much capital, which screening policies) and the execution state (stage results, approvals, errors) into one MongoDB document in `pipeline_runs`. This worked for a prototype but has two problems at scale:

1. **No reusability.** Every run requires re-entering all configuration. There is no way to define a named strategy and execute it repeatedly.
2. **No benchmarking.** Because configuration and execution are fused, comparing the performance of two different strategies over time is not possible.

The user additionally wants to:

- Create, edit, test, and delete named investment strategies
- Associate each strategy with a depot (real Comdirect depot or a virtual paper-trading depot)
- Execute each strategy many times (manually during development; automatically daily/weekly in production)
- Benchmark strategies against each other using historical execution results and virtual depot performance

---

## Decision

### 1. Terminology rename — "run" → "Quant System" + "Execution"

| Old term | New term | Scope |
| -------- | -------- | ----- |
| run / pipeline run | **execution** | One pipeline pass (universe → research → … → trade) |
| (no concept) | **Quant System** | Named, saved investment strategy configuration |
| `pipeline_runs` collection | `executions` collection | MongoDB |
| `/runs/...` URL prefix | `/quant-systems/{qs_id}/executions/...` | HTTP routes |
| `run_id` variable | `execution_id` variable | Python + templates |

The rename is applied consistently across all layers: Python code, MongoDB collection names, HTTP URLs, and Jinja2 templates. No partial renames.

### 2. Data model — 1:N (Quant System → Executions)

A `QuantSystem` is a durable configuration document. An `Execution` is an immutable run-result document linked to a `QuantSystem` by foreign key. One Quant System has many Executions.

**`quant_systems`** (MongoDB collection, `alpha_agents` database)

```text
quant_system_id   str          short hex (6 chars), unique
name              str          user-supplied, unique
depot_id          str          FK → real depot (finance.depot_snapshots) or virtual_depots
depot_type        "real"|"virtual"
indices           list[str]    e.g. ["DAX", "MDAX", "SDAX"]
status            "draft"|"active"|"paused"|"archived"
config_overrides  dict         per-stage parameter overrides (same structure as before)
created_at        datetime (UTC)
updated_at        datetime (UTC)
```

**`executions`** (replaces `pipeline_runs`, `alpha_agents` database)

```text
execution_id      str          short hex (6 chars), unique
quant_system_id   str          FK → quant_systems
created_at        datetime (UTC)
status            "running"|"awaiting_review"|"complete"|"error"
current_stage     str
hitl_mode         bool
capital_eur       float        snapshot from QS at execution start
indices           list[str]    snapshot from QS at execution start
config_overrides  dict         snapshot from QS at execution start
stages            dict[str, stage_doc]
```

Snapshots of `capital_eur`, `indices`, and `config_overrides` are taken at execution start so that editing a Quant System does not retroactively alter historical execution records.

### 3. Depot association

Each Quant System is associated with exactly one depot at creation time. The depot determines:

- **Starting capital** (virtual depot) or **current cash** (real depot) fed into the Portfolio Construction Agent
- **Current positions** read at execution start to identify required BUY/SELL operations

Two depot types are supported:

**Real depots** — read-only cross-database queries against the `finance` database (maintained by the `comdirect_api` sibling project, same Atlas cluster):

- `finance.depot_snapshots` → latest positions (`find_one` sorted by `recorded_at DESC`)
- `finance.account_balances` → latest cash balance

**Virtual (paper-trading) depots** — stored in the `alpha_agents` database using an insert-only pattern identical to real depots:

**`virtual_depots`** — metadata, rarely mutated

```text
depot_id          str          short hex, unique
name              str          user-supplied, unique
starting_capital  float        EUR, default 100 000
created_at        datetime (UTC)
updated_at        datetime (UTC)
```

**`virtual_depot_snapshots`** — insert-only; one document per state change

```text
depot_id          str          FK → virtual_depots
current_cash      float        EUR after the triggering operation
positions         list[dict]   [{wkn, isin, instrument_name, quantity,
                                 purchase_price, current_value}]
recorded_at       datetime (UTC)
triggered_by      str          execution_id that caused this snapshot
```

**`virtual_depot_transactions`** — insert-only; one document per BUY/SELL

```text
transaction_id    str          UUID, unique
depot_id          str          FK → virtual_depots
execution_id      str          FK → executions
wkn               str
transaction_type  "BUY"|"SELL"
quantity          float
execution_price   float        EUR per unit
transaction_value float        quantity × execution_price
booking_date      datetime (UTC)
recorded_at       datetime (UTC)
```

This mirrors the `depot_snapshots` + `transactions` pattern used by `comdirect_api` for real depots, making cross-depot benchmarking queries uniform.

### 4. URL structure

```text
# Quant System management
GET    /quant-systems                                             list
GET    /quant-systems/new                                         creation wizard
POST   /quant-systems                                            save new
GET    /quant-systems/{qs_id}/edit                               edit config
POST   /quant-systems/{qs_id}                                    save edits
DELETE /quant-systems/{qs_id}                                    delete (guarded)

# Execution management (pipeline execution, formerly /runs/...)
GET    /quant-systems/{qs_id}/executions                         history list
POST   /quant-systems/{qs_id}/executions                         start new execution
GET    /quant-systems/{qs_id}/executions/{exec_id}               → redirect to current stage
GET    /quant-systems/{qs_id}/executions/{exec_id}/stages/{stage}
POST   /quant-systems/{qs_id}/executions/{exec_id}/stages/{stage}/approve
POST   /quant-systems/{qs_id}/executions/{exec_id}/stages/{stage}/restart
GET    /quant-systems/{qs_id}/executions/{exec_id}/charts/...    chart fragments

# Depot management
GET    /depots                                                    list real + virtual
POST   /depots/virtual                                           create virtual depot
DELETE /depots/virtual/{depot_id}                                delete virtual depot
```

### 5. MongoDB database layout

| Database | Collections | Writer |
| -------- | ----------- | ------ |
| `alpha_agents` | `quant_systems`, `executions`, `virtual_depots`, `virtual_depot_snapshots`, `virtual_depot_transactions` | alpha-agents |
| `finance` | `depot_snapshots`, `account_balances`, `transactions` | comdirect_api |

Both databases live on the same Atlas cluster. `alpha-agents` holds a single `AsyncIOMotorClient` and accesses both databases via `_client["alpha_agents"]` and `_client["finance"]`. No second connection string is needed. `alpha-agents` is **read-only** against the `finance` database.

### 6. Configuration change

`DBSettings` in `app/config.py` gains one field:

```python
finance_db_name: str = "finance"
```

---

## Implementation phases

| Phase | Scope | Status |
| ----- | ----- | ------ |
| 1 | **Rename** — `pipeline_runs` → `executions`, all `run_id`/`run`/`/runs` in Python + templates | ✅ Complete |
| 2 | **Data models + DB layer** — `app/models/quant_system.py`, `app/db.py` additions | ✅ Complete |
| 3 | **Quant System CRUD** — `app/routes/quant_systems.py`, creation wizard (name → depot picker → config), new templates | ✅ Complete |
| 4 | **Wire executions to QuantSystem** — `app/routes/executions.py`, execution starts from QS config | ✅ Complete |
| 5 | **Depot read integration** — Portfolio agent reads real/virtual depot positions at execution start; virtual depot updated on Execution approval | ✅ Complete |

### Enhancement: Depot capital auto-calculation (2026-06-07)

Added `GET /quant-systems/depot-capital/{depot_id}` — a lightweight read-only endpoint that calculates the available capital for a real depot at form-load time:

1. Fetches the latest `finance.depot_snapshots` document for the depot → sums all position `current_value` fields
2. Joins to `finance.account_balances` via `account_name` (the shared key between the two collections) → reads latest `balance`
3. Returns `{"capital_eur": positions_total + cash_balance}`

The QS creation (`new.html`) and edit (`edit.html`) forms call this endpoint via `fetch()` whenever a real depot is selected and pre-fill the Capital field with the result. The value remains editable. A "(auto-calculated from depot)" hint is shown next to the field. Virtual depot selections clear the hint; the user enters capital manually.

---

## Consequences

- All existing `pipeline_runs` documents in MongoDB Atlas need a one-off migration: rename the collection to `executions` and add a synthetic `quant_system_id` field (can be a placeholder value like `"legacy"`) so they remain queryable.
- The `comdirect_api` sync service is unaffected — it writes to `finance.*` only.
- Bookmarked URLs under `/runs/...` will break; no redirect is provided (development environment only).
- Future work: scheduled execution trigger (Azure Container App scheduled jobs or cron) is explicitly out of scope for this ADR.
