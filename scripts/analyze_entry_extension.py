"""Analyze forward returns after NEW signals by EMA20/ATR20 extension quintile.

Usage:
    uv run python scripts/analyze_entry_extension.py
    uv run python scripts/analyze_entry_extension.py --index NASDAQ100 --lookback-days 1825
    uv run python scripts/analyze_entry_extension.py --tickers SAP.DE ADS.DE --lookback-days 1460

This report measures underlying returns only. It does not model warrant spread, slippage,
or leverage; use it to decide whether a follow-up warrant-cost analysis is justified.
"""

import argparse
import asyncio
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.universe import UniverseAgent, UniverseInput
from app.models.market import OHLCV, Ticker
from app.policies.trend_detection import (
    TrendDetectionPolicyConfig,
    bar_indicator_values,
    build_trend_indicator_series,
    passes_rule_group,
)
from app.tools.finhub import FinHubTool
from app.tools.wikipedia import WikipediaIndexTool
from app.tools.yfinance import YFinanceTool

DEFAULT_INDEX = "NASDAQ100"
HORIZONS = (5, 10, 20)


@dataclass(frozen=True)
class ExtensionObservation:
    symbol: str
    date: str
    extension_atr: float
    returns_pct: dict[int, float]


def collect_new_observations(
    symbol: str,
    bars: list[OHLCV],
    policy: TrendDetectionPolicyConfig,
    lookback_regression: int = 60,
    lookback_regression_short: int = 20,
) -> list[ExtensionObservation]:
    """Replay the current NEW/BREAK state machine and collect NEW-event outcomes."""
    if len(bars) < 70:
        return []

    series = build_trend_indicator_series(bars, policy)
    entry_rules = policy.entry_enabled_rules()
    exit_rules = policy.exit_enabled_rules()
    passes_new = np.zeros(len(bars), dtype=bool)
    passes_break = np.zeros(len(bars), dtype=bool)

    for index in range(len(bars)):
        values = bar_indicator_values(
            index,
            series,
            policy,
            lookback_regression,
            lookback_regression_short,
        )
        passes_new[index] = passes_rule_group(values, entry_rules, policy.new_min_true)
        passes_break[index] = passes_rule_group(values, exit_rules, policy.break_min_true)

    observations: list[ExtensionObservation] = []
    state = "OUT"
    for index in range(1, len(bars)):
        if state == "OUT" and passes_new[index] and not passes_new[index - 1]:
            state = "IN_TREND"
            extension = (series.close[index] - series.ema20[index]) / series.atr20[index]
            if math.isfinite(float(extension)):
                entry_close = series.close[index]
                returns = {
                    horizon: ((series.close[index + horizon] / entry_close) - 1.0) * 100.0
                    for horizon in HORIZONS
                    if index + horizon < len(bars)
                }
                observations.append(ExtensionObservation(
                    symbol=symbol,
                    date=bars[index].date.isoformat(),
                    extension_atr=float(extension),
                    returns_pct=returns,
                ))
        elif state == "IN_TREND" and passes_break[index]:
            state = "OUT"
    return observations


def extension_quintiles(observations: list[ExtensionObservation]) -> list[list[ExtensionObservation]]:
    """Split observations into equally sized extension-ranked groups without thresholds."""
    ordered = sorted(observations, key=lambda observation: observation.extension_atr)
    if not ordered:
        return []
    group_count = min(5, len(ordered))
    return [
        ordered[math.floor(index * len(ordered) / group_count):math.floor((index + 1) * len(ordered) / group_count)]
        for index in range(group_count)
    ]


def print_report(observations: list[ExtensionObservation]) -> None:
    groups = extension_quintiles(observations)
    print(f"NEW observations with valid extension: {len(observations)}")
    if not groups:
        return

    print("\nGroup  N  Extension ATR range       Mean return (%)       Median return (%)")
    print("-----  -  ------------------------  --------------------  -----------------")
    for index, group in enumerate(groups, start=1):
        extension_range = f"{group[0].extension_atr:6.2f} to {group[-1].extension_atr:6.2f}"
        mean_returns = " ".join(
            f"{h}d={sum(item.returns_pct[h] for item in group if h in item.returns_pct) / count:6.2f}"
            if (count := sum(1 for item in group if h in item.returns_pct)) else f"{h}d=   n/a"
            for h in HORIZONS
        )
        median_returns = " ".join(
            f"{h}d={median([item.returns_pct[h] for item in group if h in item.returns_pct]):6.2f}"
            if any(h in item.returns_pct for item in group) else f"{h}d=   n/a"
            for h in HORIZONS
        )
        print(f"Q{index:<4} {len(group):>2}  {extension_range:<24}  {mean_returns:<20}  {median_returns}")


async def resolve_index_tickers(index_name: str) -> list[Ticker]:
    async with FinHubTool() as finhub, WikipediaIndexTool() as wikipedia:
        result = await UniverseAgent(finhub, wikipedia).run(UniverseInput(indices=[index_name]))
    if result.unresolved_indices:
        raise RuntimeError(f"Could not resolve index: {', '.join(result.unresolved_indices)}")
    if not result.tickers:
        raise RuntimeError(f"Index {index_name} resolved to no usable tickers")
    print(f"Resolved {len(result.tickers)} tickers from {index_name}")
    return result.tickers


async def main(tickers: list[str] | None, index_name: str | None, lookback_days: int) -> None:
    ticker_models = [Ticker(symbol=symbol) for symbol in tickers] if tickers else await resolve_index_tickers(index_name or DEFAULT_INDEX)
    async with YFinanceTool() as tool:
        bars_by_symbol = await tool.fetch_ohlcv_batch(ticker_models, lookback_days)

    policy = TrendDetectionPolicyConfig()
    observations = [
        observation
        for ticker in ticker_models
        for observation in collect_new_observations(
            ticker.symbol,
            bars_by_symbol.get(ticker.symbol, []),
            policy,
        )
    ]
    print_report(observations)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--index", default=DEFAULT_INDEX)
    source.add_argument("--tickers", nargs="+")
    parser.add_argument("--lookback-days", type=int, default=1825)
    args = parser.parse_args()
    asyncio.run(main(args.tickers, args.index, args.lookback_days))
