import asyncio
import logging
import traceback
from datetime import datetime, timezone
from typing import Any

from app.agents.execution import TradeExecutionAgent
from app.agents.portfolio import PortfolioConstructionAgent
from app.agents.research import ResearchAgent, ResearchInput
from app.agents.risk import RiskAgent
from app.agents.screening import SecuritySelectionAgent
from app.agents.universe import UniverseAgent, UniverseInput
from app.agents.warrant_selection import WarrantSelectionAgent
from app.config import settings
from app.db import executions_collection, update_stage_progress
from app.models.market import Ticker
from app.models.signals import (
    ExecutionPlan,
    PortfolioProposal,
    ResearchResult,
    RiskAssessment,
    SelectionResult,
    UniverseResult,
    WarrantSelectionResult,
)
from app.tools.finhub import FinHubTool
from app.tools.wikipedia import WikipediaIndexTool
from app.tools.yfinance import YFinanceTool

logger = logging.getLogger(__name__)


class Pipeline:
    async def run_stage(self, execution_id: str, stage: str) -> None:
        coll = executions_collection()
        run = await coll.find_one({"execution_id": execution_id})
        if run is None:
            logger.error("run_stage: execution %r not found", execution_id)
            return
        try:
            result = await self._dispatch(stage, run)
            await coll.update_one(
                {"execution_id": execution_id},
                {"$set": {
                    f"stages.{stage}.result": result.model_dump(mode="json"),
                    f"stages.{stage}.status": "awaiting_review",
                    "status": "awaiting_review",
                }},
            )
        except Exception:
            tb = traceback.format_exc()
            logger.exception("Stage %r failed for execution %s", stage, execution_id)
            await coll.update_one(
                {"execution_id": execution_id},
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
        execution_id = run["execution_id"]
        async with FinHubTool() as finhub, WikipediaIndexTool() as wikipedia:
            await self._wake_finhub(execution_id, finhub, "universe")
            return await UniverseAgent(finhub=finhub, wikipedia=wikipedia).run(
                UniverseInput(indices=run.get("indices", []))
            )

    async def _wake_finhub(self, execution_id: str, finhub: FinHubTool, stage: str) -> None:
        """Ping FinHub; if cold-start takes >3 s, surface a progress message."""
        ping_task = asyncio.create_task(finhub.ping())
        await asyncio.sleep(3)
        if not ping_task.done():
            await update_stage_progress(execution_id, stage, {
                "message": "Waking up FinHub API (may take 30–60 s)…",
                "waking_up_since": datetime.now(timezone.utc).isoformat(),
            })
            try:
                await ping_task
            except Exception:
                pass
            await update_stage_progress(execution_id, stage, None)

    async def _run_research(self, run: dict) -> ResearchResult:
        execution_id = run["execution_id"]
        universe = UniverseResult.model_validate(run["stages"]["universe"]["result"])
        total = len(universe.tickers)
        _last: list[int] = [0]
        batch = max(1, total // 20)

        async def on_progress(step: str, done: int, total: int) -> None:
            if step == "ohlcv" or done - _last[0] >= batch or done == total:
                _last[0] = done
                await update_stage_progress(execution_id, "research", {"step": step, "done": done, "total": total})

        async with YFinanceTool() as yf:
            result = await ResearchAgent(tool=yf, on_progress=on_progress).run(
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
        overrides = run.get("config_overrides", {}).get("screening", {})
        screening_cfg = settings.screening.model_copy(update=overrides)
        return await SecuritySelectionAgent(
            settings=screening_cfg,
        ).run(research_with_bars)

    async def _run_warrant_selection(self, run: dict) -> WarrantSelectionResult:
        execution_id = run["execution_id"]
        screening = SelectionResult.model_validate(run["stages"]["screening"]["result"])
        research = ResearchResult.model_validate(run["stages"]["research"]["result"])
        prices: dict[str, float] = {
            sym: float(fund["currentPrice"])
            for sym, fund in research.fundamentals.items()
            if fund.get("currentPrice")
        }

        async def on_progress(done: int, total: int, active: list[str]) -> None:
            await update_stage_progress(execution_id, "warrant_selection", {"done": done, "total": total, "active": active})

        async with FinHubTool() as finhub:
            await self._wake_finhub(execution_id, finhub, "warrant_selection")
            return await WarrantSelectionAgent(
                finhub=finhub,
                prices=prices,
                min_days_to_expiry=settings.warrant_selection.min_days_to_expiry,
                max_days_to_expiry=settings.warrant_selection.max_days_to_expiry,
                atm_band=settings.warrant_selection.atm_band,
                atm_band_fallback=settings.warrant_selection.atm_band_fallback,
                on_progress=on_progress,
            ).run(screening)

    async def _run_portfolio(self, run: dict) -> PortfolioProposal:
        warrant_result = WarrantSelectionResult.model_validate(
            run["stages"]["warrant_selection"]["result"]
        )
        # Build SelectionResult for PortfolioAgent — each position is the warrant instrument
        warrant_tickers = [
            Ticker(symbol=w.warrant_wkn or w.warrant_isin, isin=w.warrant_isin, name=w.underlying.name)
            for w in warrant_result.selected
        ]
        scores = {(w.warrant_wkn or w.warrant_isin): w.score for w in warrant_result.selected}
        selection = SelectionResult(selected=warrant_tickers, scores=scores, rationale={})
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
