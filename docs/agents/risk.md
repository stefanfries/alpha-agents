# Agent Spec: Risk Agent

## Responsibility

Validate Portfolio's planned EUR BUY notionals and roll replacements against current NAV,
sector exposure, quote validity, and the shared position-count limit. Risk is a hard gate
before Execution; it does not submit orders or automatically close existing breaches.

## Input and output

Input: `PortfolioProposal`, including current `PortfolioAccountSnapshot`, planned BUY notionals,
held positions selected for SELL, and atomic roll pairs.

Output: `RiskAssessment` with approved/rejected `PlannedPosition`s, approved/rejected roll
pairs, unchanged close positions, rejection reasons, and warnings for existing over-limit
positions or sectors.

## Rules

- **Position notional cap:** `3 × NAV / max_positions` per warrant position.
- **Sector cap:** total marked EUR exposure per sector must not exceed one-third of NAV.
- **Position count:** use the same `portfolio.max_positions` value as Portfolio and Monitoring.
  A roll replaces an incumbent and does not consume a new-entry slot.
- Evaluate post-trade exposure: remove planned close positions; for a roll, replace the
  incumbent exposure with the replacement notional only if both legs pass.
- Reject only the BUY or paired ROLL that breaches a limit. Do not redistribute rejected
  amounts in this phase; they remain cash.
- Existing positions that have naturally grown above a limit generate warnings, not automatic
  SELLs. Do not add exposure to a sector already above its limit.
- Missing/stale/non-EUR quote data or missing NAV blocks all risk-increasing orders. A quote
  is stale after `portfolio.quote_max_age_hours` (72 hours by default). This does not suppress
  separately planned SELLs.
- A held position with unknown sector prevents exposure increases until its sector can be
  classified; candidate and held-sector joins use underlying ISIN.

## Configuration

| Setting | Default | Description |
| ------- | ------- | ----------- |
| `risk.max_position_multiple` | `3.0` | Multiple of the equal target slot size (`NAV / max_positions`) |
| `risk.max_sector_weight` | `0.333333...` | Maximum sector share of NAV |

`max_positions` is sourced from `portfolio.max_positions`; there is no separate Risk count.

## Atomic rolls and execution

Risk approves/rejects the incumbent SELL and replacement BUY as one `RollTrade`. Execution
groups all approved close and roll SELLs before all BUYs. Since fills are manual, the operator
must confirm sale proceeds and available cash before placing the planned BUYs.
