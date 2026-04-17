# Alpha Agents

A modular, multi-agent investment pipeline that automates the full equity and warrant investment lifecycle — from index universe construction through trend analysis, warrant selection, portfolio construction, risk validation, and trade execution.

Inspired by [*Building a Multi-Agent Investment Platform: From Research to Trade Execution*](https://medium.com/@farhadmalik/building-a-multi-agent-investment-platform-from-research-to-trade-execution-7ca297595ce9) (Farhad Malik, Data Science Collective). This implementation adapts the conceptual agent architecture to a concrete German-market warrant strategy built on Python, Pydantic V2, and the Comdirect broker ecosystem — without a framework dependency (no Google ADK).

---

## Pipeline

```text
UniverseSpec (e.g. ["DAX", "MDAX"])
      │
      ▼
┌─────────────┐    ┌──────────────┐    ┌─────────────────┐
│  Universe   │───▶│   Research   │───▶│ Stock Selection │
│   Agent     │    │    Agent     │    │     Agent       │
└─────────────┘    └──────────────┘    └────────┬────────┘
                                                │
                   ┌────────────────────────────┘
                   ▼
        ┌──────────────────┐    ┌──────────────────┐
        │Warrant Selection │───▶│   Portfolio      │
        │     Agent        │    │ Construction     │
        └──────────────────┘    └────────┬─────────┘
                                         │
                   ┌─────────────────────┘
                   ▼
        ┌──────────────────┐    ┌──────────────────┐
        │   Risk Agent     │───▶│ Trade Execution  │
        │                  │    │     Agent        │
        └──────────────────┘    └──────────────────┘
```

Each agent has a single responsibility, consumes a typed Pydantic input contract, and produces a typed Pydantic output contract. No shared mutable state. Every intermediate result is persisted to MongoDB Atlas before the optional **man-in-the-loop (MITL) review checkpoint**.

## Agents

| Agent | Responsibility |
| ----- | -------------- |
| **Universe** | Resolves index names (DAX, MDAX, …) to a flat, deduplicated list of ticker symbols |
| **Research** | Fetches OHLCV candle data for the full universe via yfinance |
| **Stock Selection** | Identifies established and newly confirmed uptrends using MA alignment, ADX, and swing-point analysis |
| **Warrant Selection** | Finds and scores Call Warrants per selected underlying using a seven-criteria scoring model (delta, leverage, IV, spread, premium p.a., remaining time, intrinsic value) |
| **Portfolio Construction** | Allocates capital across the warrant shortlist; diffs against current Comdirect holdings to identify new trades |
| **Risk** | Validates proposed positions against configurable risk limits; rejects positions that violate them |
| **Trade Execution** | Produces a list of `Order` objects for broker submission (dry-run by default) |

## Tech stack

| Concern | Choice |
| ------- | ------ |
| Language | Python 3.13 |
| Data models & inter-agent contracts | Pydantic V2 |
| Config & secrets | pydantic-settings + `.env` |
| HTTP | httpx (async) |
| Market data | yfinance |
| Warrant & instrument data | FastAPI Instrument API (Azure Container App) |
| Persistence | MongoDB Atlas (`pipeline_runs`, `instrument_master`) |
| Current holdings | MongoDB Atlas (synced by `comdirect_api` sibling project) |
| Package manager | uv |
| Testing | pytest + pytest-asyncio |
| Linting | ruff |

## Quick start

```bash
# Install dependencies
uv sync

# Copy and fill in secrets
cp .env.example .env

# Run the pipeline (MITL mode, dry-run by default)
uv run main.py

# Run tests
uv run pytest tests/ -v

# Lint
uv run ruff check .
```

Key `.env` settings:

```dotenv
MONGODB_URI=mongodb+srv://...
PORTFOLIO_CAPITAL_EUR=10000.0
EXECUTION_DRY_RUN=true      # set to false only when ready to trade
MITL_MODE=true
```

## Documentation

| Document | Contents |
| -------- | -------- |
| [docs/system-design.md](docs/system-design.md) | Component overview, data flow, technology choices |
| [docs/data-models.md](docs/data-models.md) | Full Pydantic model reference (market types, signals, persistence) |
| [docs/agents/universe.md](docs/agents/universe.md) | Universe Agent spec |
| [docs/agents/research.md](docs/agents/research.md) | Research Agent spec |
| [docs/agents/screening.md](docs/agents/screening.md) | Stock Selection Agent spec |
| [docs/agents/warrant_selection.md](docs/agents/warrant_selection.md) | Warrant Selection Agent spec |
| [docs/agents/portfolio.md](docs/agents/portfolio.md) | Portfolio Construction Agent spec |
| [docs/agents/risk.md](docs/agents/risk.md) | Risk Agent spec |
| [docs/agents/execution.md](docs/agents/execution.md) | Trade Execution Agent spec |
| [docs/decisions/](docs/decisions/) | Architecture Decision Records (ADR-001 through ADR-008) |

## Safety defaults

- `EXECUTION_DRY_RUN=true` — no live orders are submitted unless explicitly disabled
- `MITL_MODE=true` — the pipeline pauses after every stage for human review
- Comdirect requires 2FA — autonomous order submission is not supported; orders are reviewed and placed manually
