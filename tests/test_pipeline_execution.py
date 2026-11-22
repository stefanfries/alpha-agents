from decimal import Decimal

import pytest

from app.agents.execution import TradeExecutionAgent
from app.models.market import Ticker
from app.models.signals import (
    PlannedPosition,
)


@pytest.mark.asyncio
async def test_execution_dry_run_does_not_raise():
    from app.models.signals import RiskAssessment

    ticker = Ticker(symbol="AAPL")
    agent = TradeExecutionAgent(dry_run=True, min_trade_eur=100.0, order_type="limit")
    result = await agent.run(
        RiskAssessment(
            approved_positions=[
                PlannedPosition(
                    ticker=ticker,
                    notional_eur=Decimal("500"),
                    target_weight=0.05,
                )
            ],
            rejected_positions=[],
            risk_notes={},
        )
    )
    assert len(result.orders) == 1
    assert result.orders[0].side == "buy"
