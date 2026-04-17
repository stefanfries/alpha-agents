# System Design — Alpha Agents

## Purpose

Alpha Agents automates the end-to-end investment process by decomposing it into specialized, independently testable agents. Each agent has a single responsibility, consumes typed inputs, and produces typed outputs, enabling full traceability of every investment decision.

The primary investment instruments are **stocks** and **Call Warrants** (Optionsscheine). The system identifies stocks in confirmed uptrends, then finds and scores corresponding Call Warrants to build a trend-following warrant portfolio.

## Components

```text
┌─────────────────────────────────────────────────────┐
│                    main.py (entry)                  │
└────────────────────────┬────────────────────────────┘
                         │
                         ▼
            ┌────────────────────────┐
            │  UniverseSpec (input)  │
            │  e.g. [DAX, MDAX]      │
            └────────────┬───────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│              orchestrator.Pipeline                  │
│   Chains agents sequentially; persists each         │
│   stage result to MongoDB Atlas for MITL review     │
└──┬────────┬─────────┬────────┬────────┬─────────────┘
   │        │         │        │        │        │
   ▼        ▼         ▼        ▼        ▼        ▼
Universe Research  Stock   Warrant  Portfolio  Risk   Execution
 Agent    Agent  Selection Selection  Agent   Agent    Agent
   │        │         │        │        │        │        │
   └────────┴─────────┴────────┴────────┴────────┴────────┘
                              │
               ┌──────────────┴───────────────┐
               ▼                              ▼
            Tools                          Models
  (yfinance, Wikipedia,            (market.py, signals.py,
   FastAPI Instrument API)          warrants.py)
                              │
                              ▼
                       MongoDB Atlas
                  (intermediate results per
                   pipeline run + stage)
```

## Data flow

1. **Input**: An `UniverseSpec` — one or more index names (e.g. `["DAX", "MDAX", "SDAX"]`) — is passed to `Pipeline.run()`
2. **Universe Agent**: Resolves index names to a flat list of ticker symbols; stores the universe document in MongoDB Atlas
3. **Research Agent**: Fetches OHLCV candles for every ticker in the universe; resolves yfinance symbols via the **instrument master** (see ADR-007)
4. **Stock Selection Agent**: Detects established or newly confirmed uptrends; scores and ranks stocks
5. **Warrant Selection Agent**: For each selected stock, fetches available Call Warrants from the **FastAPI Instrument API**; scores each warrant using the optionsschein scoring model (delta, leverage, intrinsic value, spread, premium p.a., remaining time, IV); returns a ranked shortlist
6. **Portfolio Construction Agent**: Allocates weights across the warrant shortlist (one warrant per underlying); compares proposed positions against **current holdings read from MongoDB Atlas** (synced there by the `comdirect_api` sibling project) to identify new trades
7. **Risk Agent**: Validates the proposed portfolio against risk limits; may reject positions
8. **Trade Execution Agent**: Produces a list of `Order` objects for submission to the broker

Each stage result is persisted to MongoDB Atlas before the **man-in-the-loop (MITL) checkpoint**. The user reviews the output and either approves (continuing to the next stage) or rejects (returning to the previous stage with adjusted parameters).

## Man-in-the-loop (MITL) checkpoints

```text
Universe → [✓ review] → Research → [✓ review] → Stock Selection → [✓ review]
        → Warrant Selection → [✓ review] → Portfolio → [✓ review]
        → Risk → [✓ review] → Execution
```

At each `[✓ review]` point:

- The stage output is written to MongoDB Atlas collection `pipeline_runs` under the current `run_id` and `stage` name
- The **web UI** (FastAPI + Jinja2 + HTMX, see ADR-008) renders a stage-summary page with charts
- The user responds: **continue** → advance to next stage; **restart** → return to a named stage (optionally with new config parameters)

The pipeline is designed for **autonomous operation** in production (all checkpoints auto-approved), but MITL mode is the default during development.

## Technology choices

| Concern | Choice | Reason |
| ------- | ------ | ------ |
| Language | Python 3.13 | Latest stable; matches project convention |
| Validation | Pydantic V2 | Fast, type-safe data models for all inter-agent contracts |
| Config | pydantic-settings | Loads secrets from `.env`; never hardcoded |
| HTTP | httpx (async) | Used for FastAPI Instrument API calls |
| Instrument master | MongoDB Atlas `instrument_master` | ISIN/WKN/notation-ID ↔ yfinance symbol bridge (see ADR-007) |
| OHLCV data (stocks) | yfinance | Symbol-based via `symbol_yfinance` |
| OHLCV data (warrants) | FastAPI Instrument API `/history` | yfinance does not carry warrant price history; venue selected via `id_notation` |
| Index membership | FastAPI `/v1/indices/{index_name}` → Wikipedia fallback | See ADR-004 |
| Instrument master / identifiers | FastAPI Instrument API `/v1/instruments/{identifier}` | WKN, ISIN, CUSIP, FIGI, `symbol_yfinance`, `name_openfigi`; OpenFIGI-enriched (see ADR-007) |
| Warrant search | FastAPI Instrument API `/v1/warrants` | Finder by underlying WKN/ISIN with type and maturity filters |
| Warrant detail | FastAPI Instrument API `/v1/warrants/{identifier}` | Full reference data, market data, and analytics (Greeks) |
| Current holdings | MongoDB Atlas | Synced from Comdirect by `comdirect_api`; read directly from Atlas |
| Persistence | MongoDB Atlas | Intermediate results + portfolio state (see ADR-005) |
| Order placement | Manual (Comdirect web/app) | Comdirect requires 2FA — autonomous submission not supported |
| Package manager | uv | Project-wide convention |
| Testing | pytest + pytest-asyncio | Async-first test runner |
| Linting | ruff | Fast, zero-config linter + formatter |

## User interface

The pipeline exposes a **web UI** built with FastAPI + Jinja2 + HTMX (see [ADR-008](decisions/ADR-008-web-ui.md)). This replaces the CLI-based MITL pattern (ADR-005). The user reviews each stage's output in a browser, then clicks to approve or restart. Financial charts (candlestick, trend indicators) are rendered server-side by Plotly and swapped into the page by HTMX without a full reload.

## Deployment

Deployed as a FastAPI web application to Azure Container Apps (consistent with sibling projects). Can also be hosted on AWS (App Runner, ECS) or GCP (Cloud Run) — all three support containerised Python services.

## Key constraints

- **No shared mutable state**: Agents communicate only via immutable Pydantic models
- **Secrets via `.env`**: No credentials in code or git history
- **Auditability**: Every agent logs its input and output; all intermediate results are persisted to MongoDB Atlas
- **Dry-run default**: `execution_dry_run=True` prevents accidental live orders
- **One warrant per underlying**: The portfolio holds at most one Call Warrant per selected stock
