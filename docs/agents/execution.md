# Agent Spec: Trade Execution Agent

## Responsibility

Translate the risk-approved portfolio proposal into broker orders. This is the final pipeline stage.

## Input

`RiskAssessment` (output of `RiskAgent`)

## Output

```python
class ExecutionPlan(AgentOutput):
    orders: list[Order]
    skipped: list[Position]     # Positions already at target; no trade needed
```

## Tools used

None — order submission to Comdirect requires interactive 2FA authentication per session, which cannot be automated. The Execution Agent produces a fully specified `ExecutionPlan` for manual placement by the user.

## Behaviour

1. Compare approved positions against current holdings (read from MongoDB Atlas)
2. Compute the delta (required trade) for each position
3. For positions where delta is below a minimum threshold → `skipped` (avoid unnecessary churn)
4. For positions requiring a trade → construct an `Order` with all details needed for manual placement and add to `orders`
5. In dry-run mode (default): return the `ExecutionPlan` without any broker interaction
6. Display the `ExecutionPlan` in the MITL checkpoint; the user places orders manually via Comdirect web or mobile app

## Configuration (via `config.py`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `execution_dry_run` | `True` | If True, orders are computed but not submitted |
| `execution_min_trade_eur` | `100` | Minimum trade size; smaller deltas are skipped |
| `execution_order_type` | `"limit"` | `"market"` or `"limit"` |

## Notes

- **Autonomous order submission is not supported**: Comdirect requires 2FA per session and cannot be called programmatically without manual authentication. The pipeline produces a complete, actionable order list but delegation to the user for placement is by design.
- **Default is dry-run** (`execution_dry_run=True`) — no broker interaction occurs
- All orders are logged and persisted to MongoDB Atlas
- The web UI (ADR-008) presents the `ExecutionPlan` for review before the user places orders; a future enhancement could add one-click order prefill into the Comdirect web interface
