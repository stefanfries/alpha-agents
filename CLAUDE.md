# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.
**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

Copied from <https://github.com/forrestchang/andrej-karpathy-skills/blame/main/CLAUDE.md>

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.
Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:

- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**
Transform tasks into verifiable goals:

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:

```text
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---
**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

## Project overview

Alpha Agents is a modular, multi-agent investment system that decomposes the investment lifecycle into a sequential pipeline: Research → Screening → Portfolio Construction → Risk → Execution. Each agent has clearly defined responsibilities and typed Pydantic input/output contracts for full traceability.

The parent repo at `../CLAUDE.md` defines shared conventions (uv, Pydantic V2, httpx, pytest, ruff) — follow those here too.

## Commands

```bash
# Install dependencies
uv sync

# Run the web UI (development, localhost only)
uv run uvicorn app.main:app --reload

# Run the web UI accessible from other devices on the LAN (e.g. phone/tablet)
# Requires inbound TCP 8000 allowed in Windows Firewall
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Add a dependency
uv add <package>

# Run tests
uv run pytest tests/ -v

# Run a single test
uv run pytest tests/test_pipeline.py::test_screening_filters_low_market_cap

# Lint / format
uv run ruff check .
uv run ruff format .
```

## Architecture

```text
app/main.py → app/orchestrator.Pipeline → agents → models → tools
tests/                                                            (peers with app/)
```

- **`app/agents/base.py`** — abstract `Agent[I, O]` generic over typed Pydantic input/output
- **`app/agents/*.py`** — one file per agent; tools injected at construction (not imported directly)
- **`app/models/market.py`** — shared domain types: `Ticker`, `OHLCV`, `Position`, `Order`
- **`app/models/signals.py`** — inter-agent contracts: `ResearchResult` → `SelectionResult` → `PortfolioProposal` → `RiskAssessment` → `ExecutionPlan`
- **`app/tools/`** — external data sources wrapped as `Tool` subclasses (`YFinanceTool`, `ComdirectTool` stub)
- **`app/config.py`** — all settings via `pydantic-settings`; loaded from `.env`
- **`app/orchestrator.py`** — `Pipeline` chains agents sequentially; each stage result is logged
- **`app/routes/`** — FastAPI routers (web UI and pipeline API)
- **`app/templates/`** — Jinja2 HTML templates

Agents never share mutable state. Adding a new agent means adding a file in `app/agents/` and inserting it into `Pipeline.__init__()`.

## Documentation

See `docs/` for architecture decisions and agent specs:

- `docs/system-design.md` — component overview and data flow
- `docs/decisions/` — ADRs for orchestration, communication, and data sources
- `docs/agents/` — one spec per agent (responsibilities, inputs, outputs, config)
- `docs/data-models.md` — full Pydantic model reference

## Key config defaults

| Setting | Default | Notes |
| ------- | ------- | ----- |
| `execution_dry_run` | `True` | **Never submits live orders unless explicitly set to False** |
| `portfolio_capital_eur` | `10_000.0` | Set in `.env` |
| `screening_top_n` | `20` | Max tickers selected |

## Python version

Requires Python 3.13 (see `.python-version`). Use `uv run` for all execution.
