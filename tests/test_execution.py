from decimal import Decimal

import pytest

from app.agents.execution import TradeExecutionAgent
from app.agents.risk import RiskAgent
from app.models.market import Position, Ticker
from app.models.signals import PortfolioProposal


@pytest.mark.asyncio
async def test_sell_orders_are_emitted_before_buy_orders():
    sold = Position(
        ticker=Ticker(symbol="OLD", isin="OLD-ISIN"),
        quantity=Decimal("3"),
        avg_cost=Decimal("10"),
    )
    bought = Position(
        ticker=Ticker(symbol="NEW", isin="NEW-ISIN"),
        quantity=Decimal("1000"),
        avg_cost=Decimal("0"),
    )

    assessment = await RiskAgent().run(
        PortfolioProposal(
            positions=[bought],
            target_weights={"NEW": 0.1},
            new_positions=[bought],
            close_positions=[sold],
        )
    )
    plan = await TradeExecutionAgent(dry_run=True).run(assessment)

    assert [(order.side, order.ticker.symbol) for order in plan.orders] == [
        ("sell", "OLD"),
        ("buy", "NEW"),
    ]
    assert plan.orders[0].quantity == Decimal("3")
