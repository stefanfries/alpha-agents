"""Quick real-data smoke test for SecuritySelectionAgent.

Fetches OHLCV + fundamentals for a small set of DAX tickers via yfinance,
runs the screening agent, and prints the results. No MongoDB required.

Usage:
    uv run python scripts/test_screening.py
    uv run python scripts/test_screening.py --tickers SAP.DE ADS.DE MBG.DE BMW.DE ALV.DE
"""

import argparse
import asyncio
import logging

import yfinance as yf

from app.agents.screening import SecuritySelectionAgent
from app.config import ScreeningSettings
from app.models.market import OHLCV, Ticker
from app.models.signals import ResearchResult

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

DEFAULT_TICKERS = [
    "SAP.DE", "ADS.DE", "MBG.DE", "BMW.DE", "ALV.DE",
    "SIE.DE", "DTE.DE", "BAS.DE", "BAYN.DE", "VOW3.DE",
]
LOOKBACK_DAYS = 400  # enough for 60-bar regression warmup


def fetch_bars(symbol: str) -> list[OHLCV]:
    df = yf.download(symbol, period=f"{LOOKBACK_DAYS}d", auto_adjust=True, progress=False)
    if df.empty:
        return []
    ticker_obj = Ticker(symbol=symbol)
    bars = []
    for date, row in df.iterrows():
        try:
            bars.append(OHLCV(
                ticker=ticker_obj,
                date=date.date(),
                open=float(row["Open"].iloc[0] if hasattr(row["Open"], "iloc") else row["Open"]),
                high=float(row["High"].iloc[0] if hasattr(row["High"], "iloc") else row["High"]),
                low=float(row["Low"].iloc[0] if hasattr(row["Low"], "iloc") else row["Low"]),
                close=float(row["Close"].iloc[0] if hasattr(row["Close"], "iloc") else row["Close"]),
                volume=int(row["Volume"].iloc[0] if hasattr(row["Volume"], "iloc") else row["Volume"]),
            ))
        except Exception as e:
            logging.warning("Skipping bar for %s: %s", symbol, e)
    return bars


def fetch_fundamentals(symbol: str) -> dict:
    try:
        info = yf.Ticker(symbol).info
        return {"marketCap": info.get("marketCap", 0), "trailingPE": info.get("trailingPE")}
    except Exception:
        return {}


async def main(tickers: list[str]) -> None:
    print(f"\nFetching data for {len(tickers)} tickers...\n")

    ticker_objs = [Ticker(symbol=s) for s in tickers]
    bars = {s: fetch_bars(s) for s in tickers}
    fundamentals = {s: fetch_fundamentals(s) for s in tickers}

    for s in tickers:
        print(f"  {s}: {len(bars[s])} bars, marketCap={fundamentals[s].get('marketCap', 0):,.0f}")

    research = ResearchResult(tickers=ticker_objs, bars=bars, fundamentals=fundamentals)

    settings = ScreeningSettings(top_n=20, min_market_cap_eur=0)  # no market cap filter for testing
    agent = SecuritySelectionAgent(settings=settings)
    result = await agent.run(research)

    status_colours = {
        True:  "\033[92m",  # green  — selected
        False: "\033[90m",  # grey   — not selected
    }
    reset = "\033[0m"

    print(f"\n{'─'*90}")
    print(f"{'Symbol':<12} {'TQ':>8} {'TQ-20':>8} {'TSI':>7}  ST  E20  ADX  E50  Sel")
    print(f"{'─'*90}")

    all_syms = sorted(result.scores, key=lambda s: result.scores[s], reverse=True)
    selected_set = {t.symbol for t in result.selected}
    for sym in all_syms:
        selected = sym in selected_set
        colour = status_colours[selected]
        pol = result.policy_results.get(sym, {})
        def p(key: str) -> str:
            return "✓" if pol.get(key) else "✗"
        marker = " ✓" if selected else "  "
        print(
            f"{colour}{sym:<12} {result.scores[sym]:>8.4f} {result.tq_short.get(sym, 0):>8.4f}"
            f" {result.tsi.get(sym, 0):>7.1f}  {p('supertrend')}   {p('ema20_rising')}    {p('adx')}    {p('price_above_ema50')}"
            f" {marker}{reset}"
        )

    print(f"{'─'*90}")
    print(f"\nSelected: {len(result.selected)}/{len(tickers)} tickers\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS)
    args = parser.parse_args()
    asyncio.run(main(args.tickers))
