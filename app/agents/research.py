import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable

import numpy as np
import talib
from pydantic import BaseModel
from yfinance.exceptions import YFRateLimitError

from app.agents.base import Agent
from app.config import ResearchSettings, settings
from app.models.market import Ticker
from app.models.signals import MarketRegime, ResearchResult
from app.tools.retry import retry_call
from app.tools.yfinance import YFinanceTool

logger = logging.getLogger(__name__)


class ResearchInput(BaseModel):
    tickers: list[Ticker]
    lookback_days: int = 365
    universe_source: dict[str, str] = {}  # ISIN/symbol → index name (from UniverseResult.source)


class ResearchAgent(Agent[ResearchInput, ResearchResult]):
    name = "research"

    def __init__(
        self,
        tool: YFinanceTool,
        on_progress: Callable[[str, int, int], Awaitable[None]] | None = None,
        research_settings: ResearchSettings | None = None,
    ) -> None:
        self._tool = tool
        self._on_progress = on_progress
        self._cfg = research_settings or settings.research

    # ------------------------------------------------------------------
    # TQ helper (same formula as SecuritySelectionAgent._trend_quality)
    # ------------------------------------------------------------------
    def _trend_quality(self, bars: list, lookback: int) -> float:
        """R²_lb × slope_lb / ATR_20. Returns 0.0 on insufficient data."""
        close = np.array([float(b.close) for b in bars])
        high  = np.array([float(b.high)  for b in bars])
        low   = np.array([float(b.low)   for b in bars])
        if len(close) < lookback:
            return 0.0
        atr = talib.ATR(high, low, close, timeperiod=20)
        atr_val = float(atr[-1])
        if np.isnan(atr_val) or atr_val <= 0:
            return 0.0
        segment = close[-lookback:]
        x = np.arange(lookback, dtype=float)
        slope, intercept = np.polyfit(x, segment, 1)
        fitted = slope * x + intercept
        ss_res = float(np.sum((segment - fitted) ** 2))
        ss_tot = float(np.sum((segment - segment.mean()) ** 2))
        r2 = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0
        return r2 * (slope / atr_val)

    async def run(self, input: ResearchInput) -> ResearchResult:
        if self._on_progress:
            await self._on_progress("ohlcv", 0, len(input.tickers))

        all_bars = await self._tool.fetch_ohlcv_batch(input.tickers, input.lookback_days)

        # Keep fundamentals fan-out conservative to reduce Yahoo throttling.
        sem = asyncio.Semaphore(4)
        done: list[int] = [0]

        async def fetch_fundamentals_safe(ticker: Ticker) -> tuple[str, dict]:
            async with sem:
                info: dict = {}
                try:
                    info = await retry_call(
                        self._tool.fetch_fundamentals,
                        ticker,
                        non_retry_exceptions=(YFRateLimitError,),
                    )
                except YFRateLimitError:
                    logger.warning("Yahoo rate limit for fundamentals on %s", ticker.symbol)
                except Exception:
                    logger.warning("Failed to fetch fundamentals for %s", ticker.symbol, exc_info=True)
            done[0] += 1
            if self._on_progress:
                await self._on_progress("fundamentals", done[0], len(input.tickers))
            return ticker.symbol, info

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

        # --- Determine benchmark index and fetch its OHLCV ---
        benchmark_symbol = ""
        benchmark_bars: list = []
        market_regime: MarketRegime | None = None
        dominant_index = ""
        try:
            index_counts: Counter[str] = Counter(input.universe_source.values())
            if index_counts:
                dominant_index = index_counts.most_common(1)[0][0]
                benchmark_symbol = self._cfg.market_regime_symbols.get(dominant_index, "")
            if benchmark_symbol:
                bench_ticker = Ticker(symbol=benchmark_symbol, isin="", name=benchmark_symbol)
                bench_data = await self._tool.fetch_ohlcv_batch([bench_ticker], lookback_days=200)
                benchmark_bars = bench_data.get(benchmark_symbol, [])
        except Exception:
            logger.warning("Failed to fetch benchmark OHLCV", exc_info=True)

        # --- Compute partial regime (TQ-60 primary + TQ-20 early-warning) ---
        if benchmark_bars and len(benchmark_bars) >= self._cfg.market_regime_lookback:
            tq60 = self._trend_quality(benchmark_bars, self._cfg.market_regime_lookback)
            tq20 = self._trend_quality(benchmark_bars, 20)
            if tq60 >= self._cfg.market_regime_tq_green and tq20 >= self._cfg.market_regime_tq20_green:
                status = "green"
            elif tq60 <= self._cfg.market_regime_tq_red and tq20 <= self._cfg.market_regime_tq20_red:
                status = "red"
            else:
                status = "yellow"
            display_name = self._cfg.market_regime_display_names.get(dominant_index, dominant_index or benchmark_symbol)
            market_regime = MarketRegime(
                symbol=benchmark_symbol,
                display_name=display_name,
                tq60=round(tq60, 4),
                tq20=round(tq20, 4),
                status=status,
            )
            logger.info("Market regime (%s): %s  TQ-60=%.3f  TQ-20=%.3f",
                        benchmark_symbol, status, tq60, tq20)

        return ResearchResult(
            tickers=valid_tickers,
            bars=bars,
            fundamentals=fundamentals,
            benchmark_symbol=benchmark_symbol,
            benchmark_bars=benchmark_bars,
            market_regime=market_regime,
        )
