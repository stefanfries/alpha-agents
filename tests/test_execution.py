from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.agents.execution import TradeExecutionAgent
from app.agents.risk import RiskAgent
from app.models.market import Position, Ticker
from app.models.signals import (
    PlannedPosition,
    PortfolioAccountSnapshot,
    PortfolioHoldingValue,
    PortfolioProposal,
    RollTrade,
)


def _account_snapshot(positions: list[Position] = ()) -> PortfolioAccountSnapshot:
    now = datetime.now(timezone.utc)
    return PortfolioAccountSnapshot(
        source="virtual",
        available_cash_eur=Decimal("100000"),
        nav_eur=Decimal("1000000"),
        holdings=[
            PortfolioHoldingValue(
                position=position,
                underlying_isin=f"UNDERLYING-{position.ticker.isin}",
                sector="Technology",
                bid_price_eur=Decimal("10"),
                market_value_eur=Decimal("1000"),
                quote_timestamp_utc=now,
            )
            for position in positions
        ],
        recorded_at_utc=now,
    )


@pytest.mark.asyncio
async def test_sell_orders_are_emitted_before_buy_orders():
    sold = Position(
        ticker=Ticker(symbol="OLD", isin="OLD-ISIN"),
        quantity=Decimal("3"),
        avg_cost=Decimal("10"),
    )
    bought = PlannedPosition(
        ticker=Ticker(symbol="NEW", isin="NEW-ISIN"),
        notional_eur=Decimal("1000"),
        target_weight=0.1,
        sector="Technology",
    )

    assessment = await RiskAgent().run(
        PortfolioProposal(
            positions=[bought],
            target_weights={"NEW": 0.1},
            new_positions=[bought],
            close_positions=[sold],
            account_snapshot=_account_snapshot(),
        )
    )
    plan = await TradeExecutionAgent(dry_run=True).run(assessment)

    assert [(order.side, order.ticker.symbol) for order in plan.orders] == [
        ("sell", "OLD"),
        ("buy", "NEW"),
    ]
    assert plan.orders[0].quantity == Decimal("3")
    assert plan.orders[1].notional_eur == Decimal("1000")


@pytest.mark.asyncio
async def test_confirmed_roll_emits_paired_sell_and_buy_orders():
    incumbent = Position(
        ticker=Ticker(symbol="OLD", isin="OLD-ISIN"),
        quantity=Decimal("3"),
        avg_cost=Decimal("100"),
    )
    replacement = PlannedPosition(
        ticker=Ticker(symbol="NEW", isin="NEW-ISIN"),
        notional_eur=Decimal("300"),
        target_weight=0.03,
        sector="Technology",
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
            account_snapshot=_account_snapshot([incumbent]),
        )
    )
    plan = await TradeExecutionAgent(dry_run=True).run(assessment)

    assert [
        (order.side, order.ticker.symbol, order.quantity if order.side == "sell" else order.notional_eur)
        for order in plan.orders
    ] == [
        ("sell", "OLD", Decimal("3")),
        ("buy", "NEW", Decimal("300")),
    ]


@pytest.mark.asyncio
async def test_execution_groups_all_roll_and_close_sells_before_any_buys():
    close_position = Position(
        ticker=Ticker(symbol="CLOSE", isin="CLOSE-ISIN"),
        quantity=Decimal("2"),
        avg_cost=Decimal("5"),
    )
    rolls = [
        RollTrade(
            incumbent=Position(
                ticker=Ticker(symbol=f"OLD{index}", isin=f"OLD{index}-ISIN"),
                quantity=Decimal("3"),
                avg_cost=Decimal("10"),
            ),
                replacement=PlannedPosition(
                ticker=Ticker(symbol=f"NEW{index}", isin=f"NEW{index}-ISIN"),
                    notional_eur=Decimal("300"),
                    target_weight=0.03,
                    sector="Technology",
            ),
            target_weight=0.03,
        )
        for index in (1, 2)
    ]
    new_position = PlannedPosition(
        ticker=Ticker(symbol="ENTRY", isin="ENTRY-ISIN"),
        notional_eur=Decimal("500"),
        target_weight=0.05,
        sector="Healthcare",
    )

    assessment = await RiskAgent().run(
        PortfolioProposal(
            positions=[new_position],
            target_weights={"ENTRY": 0.05},
            close_positions=[close_position],
            roll_trades=rolls,
            account_snapshot=_account_snapshot([
                close_position,
                rolls[0].incumbent,
                rolls[1].incumbent,
            ]),
        )
    )
    plan = await TradeExecutionAgent(dry_run=True).run(assessment)

    assert [(order.side, order.ticker.symbol) for order in plan.orders] == [
        ("sell", "CLOSE"),
        ("sell", "OLD1"),
        ("sell", "OLD2"),
        ("buy", "NEW1"),
        ("buy", "NEW2"),
        ("buy", "ENTRY"),
    ]


@pytest.mark.asyncio
async def test_roll_does_not_consume_new_position_risk_slot():
    new_position = PlannedPosition(
        ticker=Ticker(symbol="ENTRY", isin="ENTRY-ISIN"),
        notional_eur=Decimal("500"),
        target_weight=0.05,
        sector="Healthcare",
    )
    roll = RollTrade(
        incumbent=Position(
            ticker=Ticker(symbol="OLD", isin="OLD-ISIN"),
            quantity=Decimal("3"),
            avg_cost=Decimal("100"),
        ),
        replacement=PlannedPosition(
            ticker=Ticker(symbol="NEW", isin="NEW-ISIN"),
            notional_eur=Decimal("300"),
            target_weight=0.03,
            sector="Technology",
        ),
        target_weight=0.03,
    )

    assessment = await RiskAgent(max_positions=2).run(
        PortfolioProposal(
            positions=[new_position],
            target_weights={"ENTRY": 0.05},
            roll_trades=[roll],
            account_snapshot=_account_snapshot([roll.incumbent]),
        )
    )

    assert assessment.approved_positions == [new_position]
    assert assessment.approved_roll_trades == [roll]
