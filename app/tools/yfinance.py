import asyncio
from datetime import date, timedelta
from decimal import Decimal

import yfinance as yf

from app.models.market import OHLCV, Ticker
from app.tools.base import Tool


class YFinanceTool(Tool):
    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def fetch_ohlcv_batch(self, tickers: list[Ticker], lookback_days: int) -> dict[str, list[OHLCV]]:
        if not tickers:
            return {}
        end = date.today()
        start = end - timedelta(days=lookback_days)
        symbol_map = {t.symbol: t for t in tickers}
        symbols = list(symbol_map.keys())

        # Some Comdirect symbols use "/" (e.g. BRK/B) but yfinance requires "-" (BRK-B)
        yf_symbols = [s.replace("/", "-") for s in symbols]
        yf_to_original = {yf_s: orig_s for yf_s, orig_s in zip(yf_symbols, symbols)}

        def _download() -> dict[str, list[OHLCV]]:
            df = yf.download(yf_symbols, start=start, end=end, progress=False, auto_adjust=True, group_by="ticker")
            result: dict[str, list[OHLCV]] = {}
            for yf_symbol, original_symbol in yf_to_original.items():
                ticker = symbol_map[original_symbol]
                try:
                    sub = df[yf_symbol]
                except KeyError:
                    continue
                sub = sub.dropna(subset=["Open", "High", "Low", "Close"])
                if sub.empty:
                    continue
                bars = [
                    OHLCV(
                        ticker=ticker,
                        date=row_date.date(),
                        open=Decimal(str(row["Open"].item())),
                        high=Decimal(str(row["High"].item())),
                        low=Decimal(str(row["Low"].item())),
                        close=Decimal(str(row["Close"].item())),
                        volume=int(row["Volume"].item()) if row["Volume"] == row["Volume"] else 0,
                    )
                    for row_date, row in sub.iterrows()
                ]
                if bars:
                    result[original_symbol] = bars
            return result

        return await asyncio.to_thread(_download)

    async def fetch_fundamentals(self, ticker: Ticker) -> dict:
        def _info() -> dict:
            return yf.Ticker(ticker.symbol).info

        return await asyncio.to_thread(_info)
