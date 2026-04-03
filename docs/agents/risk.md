# Agent Spec: Risk Agent

## Responsibility

Validate the proposed portfolio against risk limits. Reject or adjust positions that breach configured thresholds. This is the fourth pipeline stage and the last gate before execution.

## Input

`PortfolioProposal` (output of `PortfolioConstructionAgent`)

## Output

```python
class RiskAssessment(AgentOutput):
    approved_positions: list[Position]
    rejected_positions: list[Position]
    risk_notes: dict[str, str]          # Reason for each rejection
```

## Tools used

None — all risk checks are rule-based against configured limits.

## Behaviour

1. Check each proposed position against all configured risk rules
2. Positions that pass all checks → `approved_positions`
3. Positions that breach any rule → `rejected_positions` with the violated rule recorded in `risk_notes`
4. Re-normalise weights across approved positions if any were rejected

## Risk rules (configurable)

| Rule | Parameter | Default |
|------|-----------|---------|
| Max single-position weight | `risk_max_position_weight` | `0.10` (10%) |
| Max sector concentration | `risk_max_sector_weight` | `0.30` (30%) |
| Max number of positions | `risk_max_positions` | `30` |

## Notes

- Risk rules are the hardest constraints in the system; the Execution Agent must never bypass them
- All rejections are logged with the specific rule that was violated
- If all positions are rejected, the pipeline returns an empty execution plan (no trades)
