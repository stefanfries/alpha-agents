import asyncio
import logging

from agents.base import Agent
from models.market import Ticker
from models.signals import ResearchResult
from pydantic import BaseModel
from tools.yfinance import YFinanceTool

logger = logging.getLogger(__name__)


class ResearchInput(BaseModel):
    tickers: list[Ticker]
    lookback_days: int = 365


class ResearchAgent(Agent[ResearchInput, ResearchResult]):
    name = "research"

    def __init__(self, tool: YFinanceTool) -> None:
        self._tool = tool

    async def run(self, input: ResearchInput) -> ResearchResult:
        async def fetch_one(ticker: Ticker) -> tuple[Ticker, list, dict] | None:
            try:
                bars, fundamentals = await asyncio.gather(
                    self._tool.fetch_ohlcv(ticker, input.lookback_days),
                    self._tool.fetch_fundamentals(ticker),
                )
                if not bars:
                    logger.warning("No OHLCV data for %s — skipping", ticker.symbol)
                    return None
                return ticker, bars, fundamentals
            except Exception:
                logger.warning("Failed to fetch data for %s — skipping", ticker.symbol, exc_info=True)
                return None

        results = await asyncio.gather(*[fetch_one(t) for t in input.tickers])

        valid_tickers: list[Ticker] = []
        bars: dict[str, list] = {}
        fundamentals: dict[str, dict] = {}

        for result in results:
            if result is None:
                continue
            ticker, ticker_bars, ticker_fundamentals = result
            valid_tickers.append(ticker)
            bars[ticker.symbol] = ticker_bars
            fundamentals[ticker.symbol] = ticker_fundamentals

        logger.info("Research complete: %d/%d tickers fetched", len(valid_tickers), len(input.tickers))
        return ResearchResult(tickers=valid_tickers, bars=bars, fundamentals=fundamentals)
