import logging

from agents.execution import TradeExecutionAgent
from agents.portfolio import PortfolioConstructionAgent
from agents.research import ResearchAgent, ResearchInput
from agents.risk import RiskAgent
from agents.screening import SecuritySelectionAgent
from config import settings
from models.market import Ticker
from models.signals import ExecutionPlan
from tools.yfinance import YFinanceTool

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self) -> None:
        tool = YFinanceTool()
        self._research = ResearchAgent(tool=tool)
        self._screening = SecuritySelectionAgent(
            top_n=settings.screening_top_n,
            min_market_cap_eur=settings.min_market_cap_eur,
        )
        self._portfolio = PortfolioConstructionAgent(
            capital_eur=settings.portfolio_capital_eur,
            sizing_method=settings.sizing_method,
            max_position_weight=settings.max_position_weight,
        )
        self._risk = RiskAgent(
            max_position_weight=settings.risk_max_position_weight,
            max_positions=settings.risk_max_positions,
        )
        self._execution = TradeExecutionAgent(
            dry_run=settings.execution_dry_run,
            min_trade_eur=settings.execution_min_trade_eur,
            order_type=settings.execution_order_type,
        )

    async def run(self, symbols: list[str]) -> ExecutionPlan:
        tickers = [Ticker(symbol=s) for s in symbols]
        logger.info("Pipeline starting for %d tickers", len(tickers))

        research_result = await self._research.run(
            ResearchInput(tickers=tickers, lookback_days=settings.research_lookback_days)
        )
        selection_result = await self._screening.run(research_result)
        portfolio_proposal = await self._portfolio.run(selection_result)
        risk_assessment = await self._risk.run(portfolio_proposal)
        execution_plan = await self._execution.run(risk_assessment)

        logger.info(
            "Pipeline complete: %d orders, %d skipped",
            len(execution_plan.orders),
            len(execution_plan.skipped),
        )
        return execution_plan
