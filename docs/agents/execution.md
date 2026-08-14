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

1. Convert `close_positions` into SELL orders first
2. Convert risk-approved positions into BUY orders
3. For positions where the allocation is below the minimum threshold → `skipped` (avoid unnecessary churn)
4. In dry-run mode (default): return the `ExecutionPlan` without any broker interaction
5. Display the `ExecutionPlan` in the HITL checkpoint; the user places SELL orders before BUY orders manually via Comdirect web or mobile app

## Configuration (via `config.py`)

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `execution_dry_run` | `True` | If True, orders are computed but not submitted |
| `execution_min_trade_eur` | `100` | Minimum trade size; smaller deltas are skipped |
| `execution_order_type` | `"limit"` | `"market"` or `"limit"` |

## Notes

- **Autonomous order submission is not supported**: Comdirect requires 2FA per session and cannot be called programmatically without manual authentication. The pipeline produces a complete, actionable order list but delegation to the user for placement is by design.
- **Default is dry-run** (`execution_dry_run=True`) — no broker interaction occurs
- All orders are logged and persisted to MongoDB Atlas
- The web UI (ADR-008) presents the `ExecutionPlan` for review before the user places orders; a future enhancement could add one-click order prefill into the Comdirect web interface
