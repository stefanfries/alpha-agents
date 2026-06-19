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
│   stage result to MongoDB Atlas for HITL review     │
└──┬──────┬───────┬────────┬───────┬──────┬───────────┘
   │      │       │        │       │      │       │       │
   ▼      ▼       ▼        ▼       ▼      ▼       ▼       ▼
Universe Res.  Screen. Monitor. Warrant Portfolio Risk  Execution
 Agent  Agent   Agent   Agent   Select.  Agent  Agent   Agent
   │      │       │        │       │       │      │       │
   └──────┴───────┴────────┴───────┴───────┴──────┴───────┘
                              │
               ┌──────────────┴───────────────┐
               ▼                              ▼
            Tools                          Models
  (yfinance, Wikipedia,            (market.py, signals.py,
   FinHub API)          warrants.py)
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
4. **Stock Selection Agent**: Scores every pre-filtered ticker with three metrics (Trend Quality TQ, short-window TQ-20, and TSI), evaluates configurable boolean policies (SuperTrend, EMA20 rising, ADX rising, price > EMA50), and selects the top-N candidates by TQ; emits `trend_signals` (NEW / HOLD / BREAK) for every scored ticker
5. **Monitoring Agent**: Reconciles the screening results with the current depot. For each open position, checks whether the underlying has a BREAK trend signal and whether the minimum holding period has elapsed; marks positions for SELL or KEEP. Derives the number of free slots and filters the entry candidate list to exclude already-held underlyings. Downstream stages operate only on `entry_candidates`, not the full screening shortlist. (See ADR-011.)
6. **Warrant Selection Agent**: For each entry candidate, fetches available Call Warrants from the **FinHub API**; scores each warrant using the optionsschein scoring model (spread 40%, leverage 25%, days-to-expiry 20%, delta 15%); returns the best warrant plus a top-3 shortlist per underlying
7. **Portfolio Construction Agent**: Allocates weights across the warrant shortlist (one warrant per underlying); compares proposed positions against **current holdings read from MongoDB Atlas** (synced there by the `comdirect_api` sibling project) to identify new trades. Incumbent positions marked KEEP by Monitoring are excluded from `close_positions`.
8. **Risk Agent**: Validates the proposed portfolio against risk limits; may reject positions
9. **Trade Execution Agent**: Produces a list of `Order` objects for submission to the broker

Each stage result is persisted to MongoDB Atlas before the **human-in-the-loop (HITL) checkpoint**. The user reviews the output and either approves (continuing to the next stage) or rejects (returning to the previous stage with adjusted parameters).

## Human-in-the-loop (HITL) checkpoints

```text
Universe → [✓ review] → Research → [✓ review] → Stock Selection → [✓ review]
        → Monitoring → [✓ review] → Warrant Selection → [✓ review]
        → Portfolio → [✓ review] → Risk → [✓ review] → Execution
```

At each `[✓ review]` point:

- The stage output is written to MongoDB Atlas collection `pipeline_runs` under the current `run_id` and `stage` name
- The **web UI** (FastAPI + Jinja2 + HTMX, see ADR-008) renders a stage-summary page with charts
- The user responds: **continue** → advance to next stage; **restart** → return to a named stage (optionally with new config parameters)

The pipeline is designed for **autonomous operation** in production (all checkpoints auto-approved), but HITL mode is the default during development.

## Technology choices

| Concern | Choice | Reason |
| ------- | ------ | ------ |
| Language | Python 3.13 | Latest stable; matches project convention |
| Validation | Pydantic V2 | Fast, type-safe data models for all inter-agent contracts |
| Config | pydantic-settings | Loads secrets from `.env`; never hardcoded |
| HTTP | httpx (async) | Used for FinHub API calls |
| Transient-error retries | tenacity | Shared policy in `app/tools/retry.py` (3 attempts, exponential backoff ~2 s then ~4 s); wraps all external-API calls (FinHub, yfinance) |
| Instrument master | MongoDB Atlas `instrument_master` | ISIN/WKN/notation-ID ↔ yfinance symbol bridge (see ADR-007) |
| OHLCV data (stocks) | yfinance | Symbol-based via `symbol_yfinance`; up to 4 years for chart warmup |
| Technical indicators | TA-Lib (numpy) | EMA, SMA, ADX, PLUS_DI, MINUS_DI, ATR; used for screening charts and SuperTrend |
| OHLCV data (warrants) | FinHub API `/history` | yfinance does not carry warrant price history; venue selected via `id_notation` |
| Index membership | FastAPI `/v1/indices/{index_name}` → Wikipedia fallback | See ADR-004 |
| Instrument master / identifiers | FinHub API `/v1/instruments/{identifier}` | WKN, ISIN, CUSIP, FIGI, `symbol_yfinance`, `name_openfigi`; OpenFIGI-enriched (see ADR-007) |
| Warrant search | FinHub API `/v1/warrants` | Finder by underlying WKN/ISIN with type and maturity filters |
| Warrant detail | FinHub API `/v1/warrants/{identifier}` | Full reference data, market data, and analytics (Greeks) |
| Current holdings | MongoDB Atlas `finance` DB | Read-only; synced from Comdirect by `comdirect_api` sibling project (`finance.depot_snapshots`, `finance.account_balances`) |
| Persistence | MongoDB Atlas `alpha_agents` DB | `quant_systems`, `executions`, `virtual_depots`, `virtual_depot_snapshots`, `warrant_availability` (see ADR-010, ADR-012) |
| Order placement | Manual (Comdirect web/app) | Comdirect requires 2FA — autonomous submission not supported |
| Package manager | uv | Project-wide convention |
| Testing | pytest + pytest-asyncio | Async-first test runner |
| Linting | ruff | Fast, zero-config linter + formatter |

## User interface

The pipeline exposes a **web UI** built with FastAPI + Jinja2 + HTMX (see [ADR-008](decisions/ADR-008-web-ui.md)). The user reviews each stage's output in a browser, then clicks to approve or restart. The screening stage features interactive **Lightweight Charts v4** candlestick charts loaded on demand when the user clicks a ticker row. Charts include EMA 20/50, SMA 200, SuperTrend, and a synchronized ADX sub-pane; all indicators are computed server-side with TA-Lib. The warrant selection stage shows a split-panel layout: a main table ordered by screening rank, a top-3 warrant detail panel (shown on row click), and an underlying stock chart with strike and maturity markers.

## Deployment

Deployed as a FastAPI web application to Azure Container Apps (consistent with sibling projects). Can also be hosted on AWS (App Runner, ECS) or GCP (Cloud Run) — all three support containerised Python services.

## Key constraints

- **No shared mutable state**: Agents communicate only via immutable Pydantic models
- **Secrets via `.env`**: No credentials in code or git history
- **Auditability**: Every agent logs its input and output; all intermediate results are persisted to MongoDB Atlas
- **Dry-run default**: `execution_dry_run=True` prevents accidental live orders
- **One warrant per underlying**: The portfolio holds at most one Call Warrant per selected stock
