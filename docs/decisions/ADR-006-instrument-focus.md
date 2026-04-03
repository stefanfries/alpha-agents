# ADR-006: Instrument Focus — Stocks and Call Warrants

## Status

Proposed

## Context

The investment strategy is trend-following. The most capital-efficient expression of a confirmed uptrend is a Call Warrant (Optionsschein) rather than the underlying stock directly, because:

- Warrants provide leverage (4–10×) on confirmed price moves
- Risk is limited to the premium paid (no margin calls)
- Comdirect offers a broad derivative marketplace suitable for German and European retail investors

This ADR documents the decision to focus the portfolio construction on **Call Warrants** rather than on direct stock purchases.

## Decision

### Two-stage instrument selection

1. **Stock selection** (StockSelectionAgent): Identify stocks with confirmed uptrends. These are the *underlyings* — not necessarily purchased directly.
2. **Warrant selection** (WarrantSelectionAgent): For each selected underlying, find and score all available Call Warrants. Only warrant characteristics are considered at this stage (not the stock's trend score, which was already the selection gate).

### One warrant per underlying

The portfolio holds **at most one Call Warrant per selected underlying stock**. This constraint:

- Avoids unintended doubling of exposure to a single underlying
- Simplifies portfolio weight calculation
- Makes position tracking straightforward

### Warrant scoring model

The `WarrantSelectionAgent` uses the multi-criteria scoring model documented in `docs/agents/warrant_selection.md`, derived from `optionsschein_scoring.md` in the `portfolio-trend-analyzer` sibling project. The model balances:

- Sensitivity to upward price moves (delta)
- Leverage (not excessive — prefer 4–10×)
- Cost efficiency (spread, premium p.a.)
- Time horizon match (at least 3–6 months remaining)

### Direct stock purchases

Not excluded by design — the `sizing_method` and `Position` model are instrument-agnostic. A future configuration option (`instrument_type: "warrant" | "stock" | "both"`) could enable direct stock positions alongside warrants. For now, the warrant path is the primary focus.

## Consequences

- A new `Warrant` domain model is required in `models/market.py`
- A new `WarrantSelectionAgent` and corresponding `docs/agents/warrant_selection.md` spec are required
- The `InstrumentApiTool` must support a warrant search endpoint (`/warrants?underlying_isin=...`) returning delta, leverage, bid/ask, strike, expiry, IV — to be implemented in the `fastapi-azure-container-app` sibling project
- `PortfolioProposal` must carry `Warrant` objects (not just `Ticker`) to communicate to the execution stage
- The `ExecutionAgent` produces orders for **manual placement** by the user; autonomous submission via Comdirect is not possible (2FA requirement)
