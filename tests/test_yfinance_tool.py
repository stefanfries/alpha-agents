from datetime import date

import pandas as pd
import pytest
import yfinance as yf

from app.models.market import Ticker
from app.tools.yfinance import YFinanceTool


def _frame_with_nan_ohlcv(symbol: str) -> pd.DataFrame:
    cols = pd.MultiIndex.from_product([[symbol], ["Open", "High", "Low", "Close", "Volume"]])
    return pd.DataFrame(
        [[float("nan"), float("nan"), float("nan"), float("nan"), float("nan")]],
        index=[pd.Timestamp(date(2026, 1, 2))],
        columns=cols,
    )


def _frame_with_ohlcv(symbol: str) -> pd.DataFrame:
    cols = pd.MultiIndex.from_product([[symbol], ["Open", "High", "Low", "Close", "Volume"]])
    return pd.DataFrame(
        [[100.0, 101.0, 99.0, 100.5, 12345.0]],
        index=[pd.Timestamp(date(2026, 1, 2))],
        columns=cols,
    )


@pytest.mark.asyncio
async def test_fetch_ohlcv_batch_does_not_strip_suffix(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def fake_download(symbols, **kwargs):
        calls.append(list(symbols))
        if symbols == ["TEST.SW"]:
            return _frame_with_nan_ohlcv("TEST.SW")
        return pd.DataFrame()

    monkeypatch.setattr(yf, "download", fake_download)

    tool = YFinanceTool()
    ticker = Ticker(symbol="TEST.SW", isin="CH0000000000")
    bars = await tool.fetch_ohlcv_batch([ticker], lookback_days=30)

    assert bars == {}
    assert calls[0] == ["TEST.SW"]


@pytest.mark.asyncio
async def test_fetch_fundamentals_uses_primary_symbol(monkeypatch: pytest.MonkeyPatch):
    ticker_calls: list[str] = []

    class FakeTicker:
        def __init__(self, symbol: str):
            self._symbol = symbol

        @property
        def info(self):
            ticker_calls.append(self._symbol)
            if self._symbol == "GRMN.SW":
                return {"shortName": "Garmin", "marketCap": 1, "currency": "USD"}
            return {}

    monkeypatch.setattr(yf, "Ticker", FakeTicker)

    tool = YFinanceTool()
    ticker = Ticker(symbol="GRMN.SW", isin="CH0114405324")
    info = await tool.fetch_fundamentals(ticker)

    assert info["shortName"] == "Garmin"
    assert ticker_calls[0] == "GRMN.SW"


