import asyncio
from datetime import date, timedelta
from decimal import Decimal

import yfinance as yf

from models.market import OHLCV, Ticker
from tools.base import Tool


class YFinanceTool(Tool):
    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def fetch_ohlcv(self, ticker: Ticker, lookback_days: int) -> list[OHLCV]:
        end = date.today()
        start = end - timedelta(days=lookback_days)

        def _download() -> list[OHLCV]:
            df = yf.download(ticker.symbol, start=start, end=end, progress=False, auto_adjust=True)
            if df.empty:
                return []
            bars: list[OHLCV] = []
            for row_date, row in df.iterrows():
                bars.append(
                    OHLCV(
                        ticker=ticker,
                        date=row_date.date(),
                        open=Decimal(str(row["Open"].item())),
                        high=Decimal(str(row["High"].item())),
                        low=Decimal(str(row["Low"].item())),
                        close=Decimal(str(row["Close"].item())),
                        volume=int(row["Volume"].item()),
                    )
                )
            return bars

        return await asyncio.to_thread(_download)

    async def fetch_fundamentals(self, ticker: Ticker) -> dict:
        def _info() -> dict:
            return yf.Ticker(ticker.symbol).info

        return await asyncio.to_thread(_info)
