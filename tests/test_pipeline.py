from decimal import Decimal

import pytest

from agents.execution import TradeExecutionAgent
from agents.portfolio import PortfolioConstructionAgent
from agents.risk import RiskAgent
from agents.screening import SecuritySelectionAgent
from models.market import Position, Ticker
from models.signals import ResearchResult, SelectionResult


@pytest.mark.asyncio
async def test_screening_filters_low_market_cap():
    agent = SecuritySelectionAgent(top_n=10, min_market_cap_eur=1_000_000_000)
    ticker = Ticker(symbol="SMALL")
    result = await agent.run(
        ResearchResult(
            tickers=[ticker],
            bars={"SMALL": []},
            fundamentals={"SMALL": {"marketCap": 100_000}},
        )
    )
    assert ticker not in result.selected
    assert "SMALL" in result.rationale


@pytest.mark.asyncio
async def test_portfolio_equal_weights():
    tickers = [Ticker(symbol=s) for s in ["A", "B", "C", "D"]]
    agent = PortfolioConstructionAgent(capital_eur=10_000, sizing_method="equal", max_position_weight=0.5)
    result = await agent.run(
        SelectionResult(
            selected=tickers,
            scores={"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0},
            rationale={},
        )
    )
    assert len(result.positions) == 4
    for w in result.target_weights.values():
        assert abs(w - 0.25) < 1e-9


@pytest.mark.asyncio
async def test_risk_rejects_oversized_position():
    from models.signals import PortfolioProposal

    ticker = Ticker(symbol="BIG")
    agent = RiskAgent(max_position_weight=0.10, max_positions=30)
    result = await agent.run(
        PortfolioProposal(
            positions=[Position(ticker=ticker, quantity=Decimal("5000"), avg_cost=Decimal("0"))],
            target_weights={"BIG": 0.50},
        )
    )
    assert ticker in [p.ticker for p in result.rejected_positions]
    assert "BIG" in result.risk_notes


@pytest.mark.asyncio
async def test_execution_dry_run_does_not_raise():
    from models.signals import RiskAssessment

    ticker = Ticker(symbol="AAPL")
    agent = TradeExecutionAgent(dry_run=True, min_trade_eur=100.0, order_type="limit")
    result = await agent.run(
        RiskAssessment(
            approved_positions=[
                Position(ticker=ticker, quantity=Decimal("500"), avg_cost=Decimal("0"))
            ],
            rejected_positions=[],
            risk_notes={},
        )
    )
    assert len(result.orders) == 1
    assert result.orders[0].side == "buy"
