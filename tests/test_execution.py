from decimal import Decimal

import pytest

from app.agents.execution import TradeExecutionAgent
from app.agents.risk import RiskAgent
from app.models.market import Position, Ticker
from app.models.signals import PortfolioProposal, RollTrade


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


@pytest.mark.asyncio
async def test_confirmed_roll_emits_paired_sell_and_buy_orders():
    incumbent = Position(
        ticker=Ticker(symbol="OLD", isin="OLD-ISIN"),
        quantity=Decimal("3"),
        avg_cost=Decimal("100"),
    )
    replacement = Position(
        ticker=Ticker(symbol="NEW", isin="NEW-ISIN"),
        quantity=Decimal("300"),
        avg_cost=Decimal("0"),
    )

    assessment = await RiskAgent().run(
        PortfolioProposal(
            positions=[],
            target_weights={},
            roll_trades=[RollTrade(
                incumbent=incumbent,
                replacement=replacement,
                target_weight=0.03,
            )],
        )
    )
    plan = await TradeExecutionAgent(dry_run=True).run(assessment)

    assert [(order.side, order.ticker.symbol, order.quantity) for order in plan.orders] == [
        ("sell", "OLD", Decimal("3")),
        ("buy", "NEW", Decimal("300")),
    ]


@pytest.mark.asyncio
async def test_roll_does_not_consume_new_position_risk_slot():
    new_position = Position(
        ticker=Ticker(symbol="ENTRY", isin="ENTRY-ISIN"),
        quantity=Decimal("500"),
        avg_cost=Decimal("0"),
    )
    roll = RollTrade(
        incumbent=Position(
            ticker=Ticker(symbol="OLD", isin="OLD-ISIN"),
            quantity=Decimal("3"),
            avg_cost=Decimal("100"),
        ),
        replacement=Position(
            ticker=Ticker(symbol="NEW", isin="NEW-ISIN"),
            quantity=Decimal("300"),
            avg_cost=Decimal("0"),
        ),
        target_weight=0.03,
    )

    assessment = await RiskAgent(max_position_weight=0.10, max_positions=1).run(
        PortfolioProposal(
            positions=[new_position],
            target_weights={"ENTRY": 0.05},
            roll_trades=[roll],
        )
    )

    assert assessment.approved_positions == [new_position]
    assert assessment.approved_roll_trades == [roll]
