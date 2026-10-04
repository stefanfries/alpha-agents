from datetime import date
from decimal import Decimal

from app.models.market import Position, Ticker
from app.models.signals import (
    MonitoringResult,
    PlannedPosition,
    PortfolioAccountSnapshot,
    PortfolioHoldingValue,
    PortfolioProposal,
    PositionReview,
    RollTrade,
    WarrantSelectionResult,
)
from app.portfolio_review import build_portfolio_action_tables


def _holding(symbol: str, isin: str, wkn: str, quantity: str, bid: str, value: str) -> PortfolioHoldingValue:
    return PortfolioHoldingValue(
        position=Position(
            ticker=Ticker(symbol=wkn, isin=isin),
            quantity=Decimal(quantity),
            avg_cost=Decimal("1.00"),
        ),
        underlying_symbol=symbol,
        underlying_name=f"{symbol} Incorporated",
        bid_price_eur=Decimal(bid),
        market_value_eur=Decimal(value),
    )


def test_portfolio_action_rows_follow_sell_roll_buy_keep_order():
    trend_holding = _holding("TREND", "TREND-ISIN", "TREND-WKN", "100", "10", "1000")
    failed_roll_holding = _holding("ROLLFAIL", "ROLLFAIL-ISIN", "ROLLFAIL-WKN", "50", "10", "500")
    roll_holding = _holding("ROLL", "ROLL-ISIN", "ROLL-WKN", "80", "10", "800")
    kept_holding = _holding("KEEP", "KEEP-ISIN", "KEEP-WKN", "40", "10", "400")
    entry = PlannedPosition(
        ticker=Ticker(symbol="ENTRY-WKN", isin="ENTRY-ISIN", name="Entry Incorporated"),
        notional_eur=Decimal("1000"),
        target_weight=0.1,
        buy_price_eur=Decimal("2.40"),
        reason="spread 0.5%, leverage 5.0x",
        underlying_symbol="ENTRY",
    )
    replacement = PlannedPosition(
        ticker=Ticker(symbol="ROLL-NEW-WKN", isin="ROLL-NEW-ISIN", name="Roll Incorporated"),
        notional_eur=Decimal("800"),
        target_weight=0.08,
        buy_price_eur=Decimal("4.00"),
        reason="spread 0.4%, leverage 4.8x",
        underlying_symbol="ROLL",
    )
    proposal = PortfolioProposal(
        positions=[entry],
        target_weights={"ENTRY-WKN": 0.1},
        new_positions=[entry],
        close_positions=[trend_holding.position, failed_roll_holding.position],
        roll_trades=[RollTrade(
            incumbent=roll_holding.position,
            replacement=replacement,
            target_weight=0.08,
        )],
        account_snapshot=PortfolioAccountSnapshot(
            source="virtual",
            available_cash_eur=Decimal("7300"),
            nav_eur=Decimal("10000"),
            holdings=[trend_holding, failed_roll_holding, roll_holding, kept_holding],
        ),
    )
    monitoring = MonitoringResult(
        positions_to_sell=[PositionReview(
            underlying_symbol="TREND",
            underlying_name="TREND Incorporated",
            warrant_isin="TREND-ISIN",
            warrant_wkn="TREND-WKN",
            held_since=date(2026, 1, 1),
            decision_reason="trend break",
            trend_status="trend degraded: Price below EMA50",
            warrant_health_status="healthy",
        )],
        positions_to_roll=[PositionReview(
            underlying_symbol="ROLL",
            underlying_name="ROLL Incorporated",
            warrant_isin="ROLL-ISIN",
            warrant_wkn="ROLL-WKN",
            trend_status="trend intact",
            warrant_health_status="degraded",
            warrant_health_reason="leverage too low: 2.6",
        ), PositionReview(
            underlying_symbol="ROLLFAIL",
            underlying_name="ROLLFAIL Incorporated",
            warrant_isin="ROLLFAIL-ISIN",
            warrant_wkn="ROLLFAIL-WKN",
            trend_status="trend intact",
            warrant_health_status="degraded",
            warrant_health_reason="spread too wide: 3.2%",
        )],
        positions_to_keep=[PositionReview(
            underlying_symbol="KEEP",
            underlying_name="KEEP Incorporated",
            warrant_isin="KEEP-ISIN",
            warrant_wkn="KEEP-WKN",
            decision_reason="warrant healthy, trend intact",
            trend_status="trend intact",
            warrant_health_status="healthy",
        )],
        entry_candidates=[],
        free_positions=2,
        excluded_symbols=["TREND", "ROLLFAIL", "ROLL", "KEEP"],
    )
    warrant_selection = WarrantSelectionResult(
        selected=[],
        skipped=[],
        sell_existing_isins=["ROLLFAIL-ISIN"],
        roll_sell_underlyings=["ROLLFAIL"],
    )

    tables = build_portfolio_action_tables(proposal, monitoring, warrant_selection)

    assert [row.action_type for row in tables.sells] == ["SELL", "ROLL/SELL", "ROLL/SELL"]
    assert [row.symbol for row in tables.sells] == ["TREND", "ROLLFAIL", "ROLL"]
    assert tables.sells[0].reason == "trend degraded: Price below EMA50; Warrant healthy"
    assert tables.sells[1].reason == "trend intact; Warrant degraded: spread too wide: 3.2%; No replacement met the roll criteria"
    assert tables.sells[2].reason == "trend intact; Warrant degraded: leverage too low: 2.6; Paired with replacement BUY"
    assert [row.action_type for row in tables.buys] == ["ROLL/BUY", "BUY"]
    assert [row.symbol for row in tables.buys] == ["ROLL", "ENTRY"]
    assert tables.buys[0].reason == "Replacement for degraded warrant; spread 0.4%, leverage 4.8x"
    assert tables.buys[1].reason == "Entry selected; spread 0.5%, leverage 5.0x"
    assert all(row.held_since == "Next business day (planned)" for row in tables.buys)
    assert tables.buys[-1].quantity == Decimal("416")
    assert tables.buys[-1].total_price_eur == Decimal("998.40")
    assert [(row.action_type, row.symbol) for row in tables.keeps] == [("KEEP", "KEEP")]
    assert tables.keeps[0].reason == "Trend intact; warrant healthy"