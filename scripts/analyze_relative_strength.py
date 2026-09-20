"""Analyze NEW-event excess returns by relative TQ-60 quintile.

Usage:
    uv run python scripts/analyze_relative_strength.py
    uv run python scripts/analyze_relative_strength.py --index DAX

This diagnostic replay does not alter trend policies or selection behavior.
"""

import argparse
import asyncio
import bisect
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.models.market import OHLCV, Ticker
from app.policies.trend_detection import (
    TrendDetectionPolicyConfig,
    bar_indicator_values,
    build_trend_indicator_series,
    passes_rule_group,
    trend_quality_at_index,
)
from app.tools.yfinance import YFinanceTool
from scripts.analyze_entry_extension import (
    DEFAULT_INDEX,
    HORIZONS,
    resolve_index_tickers,
)


@dataclass(frozen=True)
class RelativeStrengthObservation:
    symbol: str
    date: str
    relative_tq60: float
    excess_returns_pct: dict[int, float]


def new_signal_indices(bars: list[OHLCV], policy: TrendDetectionPolicyConfig) -> list[int]:
    """Return historical NEW event indexes from the unchanged state machine."""
    if len(bars) < 70:
        return []
    series = build_trend_indicator_series(bars, policy)
    entry_rules = policy.entry_enabled_rules()
    exit_rules = policy.exit_enabled_rules()
    passes_new = np.zeros(len(bars), dtype=bool)
    passes_break = np.zeros(len(bars), dtype=bool)
    for index in range(len(bars)):
        values = bar_indicator_values(index, series, policy, 60, 20)
        passes_new[index] = passes_rule_group(values, entry_rules, policy.new_min_true)
        passes_break[index] = passes_rule_group(values, exit_rules, policy.break_min_true)

    state = "OUT"
    result: list[int] = []
    for index in range(1, len(bars)):
        if state == "OUT" and passes_new[index] and not passes_new[index - 1]:
            state = "IN_TREND"
            result.append(index)
        elif state == "IN_TREND" and passes_break[index]:
            state = "OUT"
    return result


def collect_relative_strength_observations(
    symbol: str,
    bars: list[OHLCV],
    benchmark_bars: list[OHLCV],
    policy: TrendDetectionPolicyConfig,
) -> list[RelativeStrengthObservation]:
    benchmark_dates = [bar.date for bar in benchmark_bars]
    benchmark_series = build_trend_indicator_series(benchmark_bars, policy)
    stock_series = build_trend_indicator_series(bars, policy)
    observations: list[RelativeStrengthObservation] = []

    for stock_index in new_signal_indices(bars, policy):
        benchmark_index = bisect.bisect_right(benchmark_dates, bars[stock_index].date) - 1
        if benchmark_index < 59:
            continue
        stock_tq = trend_quality_at_index(stock_series.close, stock_series.atr20, stock_index, 60)
        benchmark_tq = trend_quality_at_index(
            benchmark_series.close, benchmark_series.atr20, benchmark_index, 60
        )
        relative_tq = stock_tq - benchmark_tq
        if not math.isfinite(relative_tq):
            continue

        returns = {
            horizon: (
                ((stock_series.close[stock_index + horizon] / stock_series.close[stock_index]) - 1.0)
                - ((benchmark_series.close[benchmark_index + horizon] / benchmark_series.close[benchmark_index]) - 1.0)
            ) * 100.0
            for horizon in HORIZONS
            if stock_index + horizon < len(bars) and benchmark_index + horizon < len(benchmark_bars)
        }
        observations.append(RelativeStrengthObservation(
            symbol=symbol,
            date=bars[stock_index].date.isoformat(),
            relative_tq60=relative_tq,
            excess_returns_pct=returns,
        ))
    return observations


def relative_tq_quintiles(observations: list[RelativeStrengthObservation]) -> list[list[RelativeStrengthObservation]]:
    ordered = sorted(observations, key=lambda observation: observation.relative_tq60)
    if not ordered:
        return []
    group_count = min(5, len(ordered))
    return [
        ordered[math.floor(index * len(ordered) / group_count):math.floor((index + 1) * len(ordered) / group_count)]
        for index in range(group_count)
    ]


def print_report(observations: list[RelativeStrengthObservation]) -> None:
    groups = relative_tq_quintiles(observations)
    print(f"NEW observations with aligned benchmark: {len(observations)}")
    print("\nGroup  N  Relative TQ-60 range      Mean excess return (%)  Median excess return (%)")
    print("-----  -  ------------------------  ----------------------  ------------------------")
    for index, group in enumerate(groups, start=1):
        value_range = f"{group[0].relative_tq60:6.3f} to {group[-1].relative_tq60:6.3f}"
        means = " ".join(
            f"{h}d={sum(item.excess_returns_pct[h] for item in group if h in item.excess_returns_pct) / count:6.2f}"
            if (count := sum(1 for item in group if h in item.excess_returns_pct)) else f"{h}d=   n/a"
            for h in HORIZONS
        )
        medians = " ".join(
            f"{h}d={median([item.excess_returns_pct[h] for item in group if h in item.excess_returns_pct]):6.2f}"
            if any(h in item.excess_returns_pct for item in group) else f"{h}d=   n/a"
            for h in HORIZONS
        )
        print(f"Q{index:<4} {len(group):>2}  {value_range:<24}  {means:<22}  {medians}")


async def main(index_name: str, lookback_days: int) -> None:
    benchmark_symbol = settings.research.market_regime_symbols.get(index_name)
    if not benchmark_symbol:
        raise RuntimeError(f"No benchmark configured for {index_name}")
    tickers = await resolve_index_tickers(index_name)
    async with YFinanceTool() as tool:
        bars_by_symbol = await tool.fetch_ohlcv_batch(tickers, lookback_days)
        benchmark_data = await tool.fetch_ohlcv_batch([Ticker(symbol=benchmark_symbol)], lookback_days)
    benchmark_bars = benchmark_data.get(benchmark_symbol, [])
    if not benchmark_bars:
        raise RuntimeError(f"No OHLCV data for benchmark {benchmark_symbol}")

    policy = TrendDetectionPolicyConfig()
    observations = [
        observation
        for ticker in tickers
        for observation in collect_relative_strength_observations(
            ticker.symbol, bars_by_symbol.get(ticker.symbol, []), benchmark_bars, policy
        )
    ]
    print(f"Resolved {len(tickers)} {index_name} tickers; benchmark={benchmark_symbol}")
    print_report(observations)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--lookback-days", type=int, default=1825)
    args = parser.parse_args()
    asyncio.run(main(args.index, args.lookback_days))
