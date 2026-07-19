import asyncio
from datetime import date, timedelta
from decimal import Decimal

import yfinance as yf
from tenacity import retry, retry_if_result, stop_after_attempt, wait_fixed

from app.models.market import OHLCV, Ticker
from app.tools.base import Tool
from app.tools.retry import ATTEMPTS, WAIT_SECONDS


class YFinanceTool(Tool):
    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def fetch_ohlcv_batch(self, tickers: list[Ticker], lookback_days: int) -> dict[str, list[OHLCV]]:
        if not tickers:
            return {}
        end = date.today() + timedelta(days=1)
        start = end - timedelta(days=lookback_days) # yfinance's end date is exclusive, so we add one day to include today
        symbol_map = {t.symbol: t for t in tickers}
        symbols = list(symbol_map.keys())

        def _download_with_mapping(yf_to_original: dict[str, str]) -> dict[str, list[OHLCV]]:
            yf_symbols = list(yf_to_original.keys())

            def _missing(df) -> bool:
                # yfinance silently drops throttled symbols (no exception); a
                # missing column means the download should be retried.
                if df is None or df.empty:
                    return True
                present = set(df.columns.get_level_values(0))
                return any(s not in present for s in yf_symbols)

            @retry(
                stop=stop_after_attempt(ATTEMPTS),
                wait=wait_fixed(WAIT_SECONDS),
                retry=retry_if_result(_missing),
                retry_error_callback=lambda rs: rs.outcome.result(),  # keep partial data
            )
            def _fetch():
                return yf.download(
                    yf_symbols, start=start, end=end, progress=False, auto_adjust=True, group_by="ticker"
                )

            df = _fetch()
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

        # Primary pass using the provided symbols (with slash normalization for Yahoo)
        primary_map = {s.replace("/", "-"): s for s in symbols}
        return await asyncio.to_thread(_download_with_mapping, primary_map)

    async def fetch_fundamentals(self, ticker: Ticker) -> dict:
        symbol = ticker.symbol.replace("/", "-")

        def _info(symbol: str) -> dict:
            info = yf.Ticker(symbol).info
            # yfinance can return a near-empty stub dict without raising
            if len(info) <= 2:
                raise ValueError(f"Empty fundamentals stub for {symbol}")
            return info

        return await asyncio.to_thread(_info, symbol)
