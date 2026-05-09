import logging
import traceback
from typing import Any

from app.agents.execution import TradeExecutionAgent
from app.agents.portfolio import PortfolioConstructionAgent
from app.agents.research import ResearchAgent, ResearchInput
from app.agents.risk import RiskAgent
from app.agents.screening import SecuritySelectionAgent
from app.agents.universe import UniverseAgent, UniverseInput
from app.config import settings
from app.db import runs_collection
from app.models.signals import (
    ExecutionPlan,
    PortfolioProposal,
    ResearchResult,
    RiskAssessment,
    SelectionResult,
    UniverseResult,
)
from app.tools.finhub import FinHubTool
from app.tools.wikipedia import WikipediaIndexTool
from app.tools.yfinance import YFinanceTool

logger = logging.getLogger(__name__)


class Pipeline:
    async def run_stage(self, run_id: str, stage: str) -> None:
        coll = runs_collection()
        run = await coll.find_one({"run_id": run_id})
        if run is None:
            logger.error("run_stage: run %r not found", run_id)
            return
        try:
            result = await self._dispatch(stage, run)
            await coll.update_one(
                {"run_id": run_id},
                {"$set": {
                    f"stages.{stage}.result": result.model_dump(mode="json"),
                    f"stages.{stage}.status": "awaiting_review",
                    "status": "awaiting_review",
                }},
            )
        except Exception:
            tb = traceback.format_exc()
            logger.exception("Stage %r failed for run %s", stage, run_id)
            await coll.update_one(
                {"run_id": run_id},
                {"$set": {
                    f"stages.{stage}.status": "error",
                    f"stages.{stage}.error": tb,
                    "status": "error",
                }},
            )

    async def _dispatch(self, stage: str, run: dict) -> Any:
        match stage:
            case "universe":
                return await self._run_universe(run)
            case "research":
                return await self._run_research(run)
            case "screening":
                return await self._run_screening(run)
            case "warrant_selection":
                return await self._run_warrant_selection(run)
            case "portfolio":
                return await self._run_portfolio(run)
            case "risk":
                return await self._run_risk(run)
            case "execution":
                return await self._run_execution(run)
            case _:
                raise ValueError(f"Unknown stage: {stage!r}")

    async def _run_universe(self, run: dict) -> UniverseResult:
        async with FinHubTool() as finhub, WikipediaIndexTool() as wikipedia:
            return await UniverseAgent(finhub=finhub, wikipedia=wikipedia).run(
                UniverseInput(indices=run.get("indices", []))
            )

    async def _run_research(self, run: dict) -> ResearchResult:
        universe = UniverseResult.model_validate(run["stages"]["universe"]["result"])
        async with YFinanceTool() as yf:
            result = await ResearchAgent(tool=yf).run(
                ResearchInput(tickers=universe.tickers, lookback_days=settings.research.lookback_days)
            )
        # OHLCV bars are excluded from the stored document — 500+ tickers × 365 bars
        # exceeds MongoDB's 16 MB BSON limit. Bars are re-fetched in the screening stage.
        return ResearchResult(tickers=result.tickers, bars={}, fundamentals=result.fundamentals)

    async def _run_screening(self, run: dict) -> SelectionResult:
        research = ResearchResult.model_validate(run["stages"]["research"]["result"])
        async with YFinanceTool() as yf:
            bars = await yf.fetch_ohlcv_batch(research.tickers, settings.research.lookback_days)
        research_with_bars = ResearchResult(
            tickers=research.tickers, bars=bars, fundamentals=research.fundamentals
        )
        return await SecuritySelectionAgent(
            top_n=settings.screening.top_n,
            min_market_cap_eur=settings.screening.min_market_cap_eur,
        ).run(research_with_bars)

    async def _run_warrant_selection(self, run: dict) -> SelectionResult:
        # Stub: pass screening result through until FinHub warrant lookup is implemented
        return SelectionResult.model_validate(run["stages"]["screening"]["result"])

    async def _run_portfolio(self, run: dict) -> PortfolioProposal:
        selection = SelectionResult.model_validate(run["stages"]["warrant_selection"]["result"])
        return await PortfolioConstructionAgent(
            capital_eur=settings.portfolio.capital_eur,
            sizing_method=settings.portfolio.sizing_method,
            max_position_weight=settings.portfolio.max_position_weight,
        ).run(selection)

    async def _run_risk(self, run: dict) -> RiskAssessment:
        proposal = PortfolioProposal.model_validate(run["stages"]["portfolio"]["result"])
        return await RiskAgent(
            max_position_weight=settings.risk.max_position_weight,
            max_positions=settings.risk.max_positions,
        ).run(proposal)

    async def _run_execution(self, run: dict) -> ExecutionPlan:
        assessment = RiskAssessment.model_validate(run["stages"]["risk"]["result"])
        return await TradeExecutionAgent(
            dry_run=settings.execution.dry_run,
            min_trade_eur=settings.execution.min_trade_eur,
            order_type=settings.execution.order_type,
        ).run(assessment)


_pipeline = Pipeline()


def get_pipeline() -> Pipeline:
    return _pipeline
