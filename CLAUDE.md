# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Alpha Agents is a modular, multi-agent investment system that decomposes the investment lifecycle into a sequential pipeline: Research → Screening → Portfolio Construction → Risk → Execution. Each agent has clearly defined responsibilities and typed Pydantic input/output contracts for full traceability.

The parent repo at `../CLAUDE.md` defines shared conventions (uv, Pydantic V2, httpx, pytest, ruff) — follow those here too.

## Commands

```bash
# Install dependencies
uv sync

# Run the pipeline
uv run main.py

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

```
main.py → orchestrator.Pipeline → Research → Screening → Portfolio → Risk → Execution
```

- **`agents/base.py`** — abstract `Agent[I, O]` generic over typed Pydantic input/output
- **`agents/*.py`** — one file per agent; tools injected at construction (not imported directly)
- **`models/market.py`** — shared domain types: `Ticker`, `OHLCV`, `Position`, `Order`
- **`models/signals.py`** — inter-agent contracts: `ResearchResult` → `SelectionResult` → `PortfolioProposal` → `RiskAssessment` → `ExecutionPlan`
- **`tools/`** — external data sources wrapped as `Tool` subclasses (`YFinanceTool`, `ComdirectTool` stub)
- **`config.py`** — all settings via `pydantic-settings`; loaded from `.env`
- **`orchestrator.py`** — `Pipeline` chains agents sequentially; each stage result is logged

Agents never share mutable state. Adding a new agent means adding a file in `agents/` and inserting it into `Pipeline.__init__()`.

## Documentation

See `docs/` for architecture decisions and agent specs:
- `docs/system-design.md` — component overview and data flow
- `docs/decisions/` — ADRs for orchestration, communication, and data sources
- `docs/agents/` — one spec per agent (responsibilities, inputs, outputs, config)
- `docs/data-models.md` — full Pydantic model reference

## Key config defaults

| Setting | Default | Notes |
|---------|---------|-------|
| `execution_dry_run` | `True` | **Never submits live orders unless explicitly set to False** |
| `portfolio_capital_eur` | `10_000.0` | Set in `.env` |
| `screening_top_n` | `20` | Max tickers selected |

## Python version

Requires Python 3.13 (see `.python-version`). Use `uv run` for all execution.
