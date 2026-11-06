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
    RiskAssessment,
    RollTrade,
    SelectedWarrant,
    WarrantSelectionResult,
)
from app.risk_visualization import build_risk_view


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


def test_risk_view_projects_approved_trades_and_cash_only():
    now = datetime.now(timezone.utc)
    keep = Position(ticker=Ticker(symbol="KEEPW", isin="KEEP-I"), quantity=Decimal("1"), avg_cost=Decimal("10"))
    close = Position(ticker=Ticker(symbol="CLOSEW", isin="CLOSE-I"), quantity=Decimal("1"), avg_cost=Decimal("10"))
    approved_incumbent = Position(ticker=Ticker(symbol="OLDROLL", isin="OLDROLL-I"), quantity=Decimal("1"), avg_cost=Decimal("10"))
    rejected_incumbent = Position(ticker=Ticker(symbol="OLDFAIL", isin="OLDFAIL-I"), quantity=Decimal("1"), avg_cost=Decimal("10"))
    snapshot = PortfolioAccountSnapshot(
        source="virtual",
        available_cash_eur=Decimal("5000"),
        nav_eur=Decimal("10000"),
        holdings=[
            PortfolioHoldingValue(position=keep, underlying_symbol="KEEP", underlying_name="Keep Co", sector="Technology", market_value_eur=Decimal("2000"), bid_price_eur=Decimal("20"), quote_timestamp_utc=now),
            PortfolioHoldingValue(position=close, underlying_symbol="CLOSE", sector="Energy", market_value_eur=Decimal("1000"), bid_price_eur=Decimal("10"), quote_timestamp_utc=now),
            PortfolioHoldingValue(position=approved_incumbent, underlying_symbol="ROLL", sector="Health Care", market_value_eur=Decimal("1500"), bid_price_eur=Decimal("15"), quote_timestamp_utc=now),
            PortfolioHoldingValue(position=rejected_incumbent, underlying_symbol="FAIL", sector="Financials", market_value_eur=Decimal("700"), bid_price_eur=Decimal("7"), quote_timestamp_utc=now),
        ],
    )
    entry = PlannedPosition(
        ticker=Ticker(symbol="ENTRYW", isin="ENTRY-I", name="Entry Co"),
        notional_eur=Decimal("1000"), target_weight=0.1,
        underlying_symbol="ENTRY", sector="Industrials",
    )
    rejected_entry = PlannedPosition(
        ticker=Ticker(symbol="REJECTW", isin="REJECT-I", name="Rejected Co"),
        notional_eur=Decimal("800"), target_weight=0.08,
        underlying_symbol="REJECT", sector="Energy",
    )
    approved_replacement = PlannedPosition(
        ticker=Ticker(symbol="ROLLW", isin="ROLL-I", name="Roll Co"),
        notional_eur=Decimal("1200"), target_weight=0.12,
        underlying_symbol="ROLL", sector="Technology",
    )
    rejected_replacement = PlannedPosition(
        ticker=Ticker(symbol="FAILW", isin="FAIL-I", name="Fail Co"),
        notional_eur=Decimal("900"), target_weight=0.09,
        underlying_symbol="FAIL", sector="Financials",
    )
    approved_roll = RollTrade(incumbent=approved_incumbent, replacement=approved_replacement, target_weight=0.12)
    rejected_roll = RollTrade(incumbent=rejected_incumbent, replacement=rejected_replacement, target_weight=0.09)
    proposal = PortfolioProposal(
        positions=[entry, rejected_entry], target_weights={}, close_positions=[close],
        roll_trades=[approved_roll, rejected_roll], account_snapshot=snapshot,
    )
    assessment = RiskAssessment(
        approved_positions=[entry], rejected_positions=[rejected_entry], risk_notes={},
        close_positions=[close], approved_roll_trades=[approved_roll], rejected_roll_trades=[rejected_roll],
    )
    warrants = WarrantSelectionResult(
        selected=[], skipped=[],
        roll_selected=[
            SelectedWarrant(underlying=Ticker(symbol="ENTRY", name="Entry Co"), warrant_isin="ENTRY-I", warrant_wkn="WENTRY", score=0.5, rationale=""),
            SelectedWarrant(underlying=Ticker(symbol="REJECT", name="Rejected Co"), warrant_isin="REJECT-I", warrant_wkn="WREJ", score=0.5, rationale=""),
            SelectedWarrant(underlying=Ticker(symbol="ROLL", name="Roll Co"), warrant_isin="ROLL-I", warrant_wkn="WROLL", score=0.5, rationale=""),
            SelectedWarrant(underlying=Ticker(symbol="FAIL", name="Fail Co"), warrant_isin="FAIL-I", warrant_wkn="WFAIL", score=0.5, rationale=""),
        ],
    )

    view = build_risk_view(
        proposal, assessment, warrants, max_positions=10, max_position_multiple=3.0,
    )

    assert view["available"] is True
    assert view["cash_shortfall_eur"] == 0
    assert view["position_limit_pct"] == 30
    assert [
        (row["sector"], row["value_eur"], row["weight_pct"])
        for row in view["sector_rows"]
    ] == [
        ("Cash", 5100.0, 51.0),
        ("Technology", 3200.0, 32.0),
        ("Industrials", 1000.0, 10.0),
        ("Financials", 700.0, 7.0),
    ]
    technology = next(row for row in view["sector_rows"] if row["sector"] == "Technology")
    assert technology["tooltip"].splitlines() == [
        "Technology",
        "Keep Co (KEEP) · KEEPW",
        "Roll Co (ROLL) · WROLL",
    ]
    assert "Energy" not in {row["sector"] for row in view["sector_rows"]}
    assert "OLDROLL-I" not in {row["warrant_wkn"] for row in view["position_rows"]}
    assert "OLDFAIL" in {row["warrant_wkn"] for row in view["position_rows"]}
    assert "WREJ" not in {row["warrant_wkn"] for row in view["position_rows"]}
    assert len(view["review_rows"]) == 4
    assert next(row for row in view["review_rows"] if row["warrant_wkn"] == "WENTRY")["underlying_name"] == "Entry Co"


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
