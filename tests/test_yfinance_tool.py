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
async def test_fetch_ohlcv_batch_uses_isin_override_fallback(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def fake_download(symbols, **kwargs):
        calls.append(list(symbols))
        if symbols == ["GRMN.SW"]:
            return _frame_with_nan_ohlcv("GRMN.SW")
        if symbols == ["GRMN"]:
            return _frame_with_ohlcv("GRMN")
        return pd.DataFrame()

    monkeypatch.setattr(yf, "download", fake_download)

    tool = YFinanceTool()
    ticker = Ticker(symbol="GRMN.SW", isin="CH0114405324")
    bars = await tool.fetch_ohlcv_batch([ticker], lookback_days=30)

    assert "GRMN.SW" in bars
    assert len(bars["GRMN.SW"]) == 1
    assert calls[0] == ["GRMN.SW"]
    assert calls[1] == ["GRMN"]


@pytest.mark.asyncio
async def test_fetch_ohlcv_batch_uses_suffix_strip_fallback(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def fake_download(symbols, **kwargs):
        calls.append(list(symbols))
        if symbols == ["TEST.SW"]:
            return _frame_with_nan_ohlcv("TEST.SW")
        if symbols == ["TEST"]:
            return _frame_with_ohlcv("TEST")
        return pd.DataFrame()

    monkeypatch.setattr(yf, "download", fake_download)

    tool = YFinanceTool()
    ticker = Ticker(symbol="TEST.SW", isin="CH0000000000")
    bars = await tool.fetch_ohlcv_batch([ticker], lookback_days=30)

    assert "TEST.SW" in bars
    assert len(bars["TEST.SW"]) == 1
    assert calls[0] == ["TEST.SW"]
    assert calls[1] == ["TEST"]


@pytest.mark.asyncio
async def test_fetch_fundamentals_uses_resolved_ohlcv_symbol(monkeypatch: pytest.MonkeyPatch):
    download_calls: list[list[str]] = []
    ticker_calls: list[str] = []

    def fake_download(symbols, **kwargs):
        download_calls.append(list(symbols))
        if symbols == ["GRMN.SW"]:
            return _frame_with_nan_ohlcv("GRMN.SW")
        if symbols == ["GRMN"]:
            return _frame_with_ohlcv("GRMN")
        return pd.DataFrame()

    class FakeTicker:
        def __init__(self, symbol: str):
            self._symbol = symbol

        @property
        def info(self):
            ticker_calls.append(self._symbol)
            if self._symbol == "GRMN":
                return {"shortName": "Garmin", "marketCap": 1, "currency": "USD"}
            return {}

    monkeypatch.setattr(yf, "download", fake_download)
    monkeypatch.setattr(yf, "Ticker", FakeTicker)

    tool = YFinanceTool()
    ticker = Ticker(symbol="GRMN.SW", isin="CH0114405324")
    await tool.fetch_ohlcv_batch([ticker], lookback_days=30)
    info = await tool.fetch_fundamentals(ticker)

    assert info["shortName"] == "Garmin"
    assert download_calls[0] == ["GRMN.SW"]
    assert download_calls[1] == ["GRMN"]
    assert ticker_calls[0] == "GRMN"


@pytest.mark.asyncio
async def test_fetch_fundamentals_uses_symbol_override(monkeypatch: pytest.MonkeyPatch):
    ticker_calls: list[str] = []

    class FakeTicker:
        def __init__(self, symbol: str):
            self._symbol = symbol

        @property
        def info(self):
            ticker_calls.append(self._symbol)
            if self._symbol == "BG":
                return {"shortName": "Bunge", "marketCap": 1, "currency": "USD"}
            return {}

    monkeypatch.setattr(yf, "Ticker", FakeTicker)

    tool = YFinanceTool()
    info = await tool.fetch_fundamentals(Ticker(symbol="Q23.SW", isin="US74743L1008"))

    assert info["shortName"] == "Bunge"
    assert ticker_calls[0] == "BG"
