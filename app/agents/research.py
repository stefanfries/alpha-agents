import asyncio
import logging

from pydantic import BaseModel

from app.agents.base import Agent
from app.models.market import Ticker
from app.models.signals import ResearchResult
from app.tools.yfinance import YFinanceTool

logger = logging.getLogger(__name__)


class ResearchInput(BaseModel):
    tickers: list[Ticker]
    lookback_days: int = 365


class ResearchAgent(Agent[ResearchInput, ResearchResult]):
    name = "research"

    def __init__(self, tool: YFinanceTool) -> None:
        self._tool = tool

    async def run(self, input: ResearchInput) -> ResearchResult:
        all_bars = await self._tool.fetch_ohlcv_batch(input.tickers, input.lookback_days)

        sem = asyncio.Semaphore(10)

        async def fetch_fundamentals_safe(ticker: Ticker) -> tuple[str, dict]:
            async with sem:
                try:
                    return ticker.symbol, await self._tool.fetch_fundamentals(ticker)
                except Exception:
                    logger.warning("Failed to fetch fundamentals for %s", ticker.symbol, exc_info=True)
                    return ticker.symbol, {}

        fund_results = await asyncio.gather(*[fetch_fundamentals_safe(t) for t in input.tickers])
        fundamentals: dict[str, dict] = dict(fund_results)

        valid_tickers: list[Ticker] = []
        bars: dict[str, list] = {}

        for ticker in input.tickers:
            ticker_bars = all_bars.get(ticker.symbol)
            if not ticker_bars:
                logger.warning("No OHLCV data for %s — skipping", ticker.symbol)
                continue
            valid_tickers.append(ticker)
            bars[ticker.symbol] = ticker_bars

        logger.info("Research complete: %d/%d tickers fetched", len(valid_tickers), len(input.tickers))
        return ResearchResult(tickers=valid_tickers, bars=bars, fundamentals=fundamentals)
