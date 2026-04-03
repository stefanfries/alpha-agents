# Agent Spec: Portfolio Construction Agent

## Responsibility

Allocate capital across the warrant shortlist and determine which positions require a trade by comparing the proposed portfolio against current holdings. This is the fourth pipeline stage.

## Input

```python
class PortfolioInput(AgentInput):
    warrant_selection: WarrantSelectionResult      # Scored warrant shortlist
    current_holdings: list[Position]               # Current broker positions (read from MongoDB Atlas)
```

## Output

```python
class PortfolioProposal(AgentOutput):
    positions: list[Position]                      # Target warrant positions
    target_weights: dict[str, float]               # ISIN → weight, sums to 1.0
    new_positions: list[Warrant]                   # Warrants not currently held
    existing_positions: list[Warrant]              # Warrants already in portfolio (no change needed)
    close_positions: list[Position]                # Current holdings not in new shortlist → sell
```

## Tools used

- `MongoDBTool` — reads current portfolio holdings from the MongoDB Atlas collection that the `comdirect_api` sibling project continuously syncs from the Comdirect account

## Behaviour

1. Receive the scored, ranked warrant shortlist from the `WarrantSelectionAgent`
2. Read current portfolio holdings from MongoDB Atlas (populated by `comdirect_api`)
3. **Compare**: identify which of the selected warrants are already held (no new trade needed), which are new, and which current warrant holdings are no longer on the shortlist (candidates for closing)
4. Allocate portfolio weights across the **new** warrant positions according to the configured `sizing_method`
5. Convert weights to quantities / nominal amounts based on available capital
6. Return the full proposal including the new/existing/close classification
7. Persist the `PortfolioProposal` to MongoDB Atlas for the current `run_id`

## Sizing methods

| Method | Description |
|--------|-------------|
| `equal` | Equal weight across all selected warrants |
| `score_weighted` | Weight proportional to `WarrantScore` |
| `trend_weighted` | Weight proportional to underlying stock trend score |

## Configuration (via `config.py`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `portfolio_capital_eur` | required | Total capital to deploy |
| `portfolio_sizing_method` | `"equal"` | `"equal"`, `"score_weighted"`, or `"trend_weighted"` |
| `portfolio_max_position_weight` | `0.10` | Maximum single-position weight (10%) |
| `portfolio_max_positions` | `20` | Maximum number of warrant positions |

## Notes

- The constraint of **one warrant per underlying stock** is enforced here: if two warrants for the same underlying somehow pass the warrant stage, only the higher-scoring one is retained
- `close_positions` are included in the proposal for the user's information at the MITL checkpoint — the `RiskAgent` and `ExecutionAgent` decide whether to actually close them
- Does not interact with the broker for order submission — position sizing is purely computational at this stage
