"""Analyze NEW-event forward returns by relative-volume quintile.

Usage:
    uv run python scripts/analyze_volume_confirmation.py
    uv run python scripts/analyze_volume_confirmation.py --index DAX

This diagnostic replay does not alter trend policies or selection behavior.
"""

import argparse
import asyncio
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.market import OHLCV
from app.policies.trend_detection import TrendDetectionPolicyConfig
from app.tools.yfinance import YFinanceTool
from scripts.analyze_entry_extension import (
    DEFAULT_INDEX,
    HORIZONS,
    resolve_index_tickers,
)
from scripts.analyze_relative_strength import new_signal_indices


@dataclass(frozen=True)
class VolumeObservation:
    symbol: str
    date: str
    relative_volume20: float
    returns_pct: dict[int, float]


def collect_volume_observations(
    symbol: str,
    bars: list[OHLCV],
    policy: TrendDetectionPolicyConfig,
) -> list[VolumeObservation]:
    observations: list[VolumeObservation] = []
    for index in new_signal_indices(bars, policy):
        if index < 20:
            continue
        baseline = sum(bar.volume for bar in bars[index - 20:index]) / 20
        relative_volume = bars[index].volume / baseline if baseline > 0 else math.nan
        if not math.isfinite(relative_volume):
            continue
        entry_close = float(bars[index].close)
        returns = {
            horizon: ((float(bars[index + horizon].close) / entry_close) - 1.0) * 100.0
            for horizon in HORIZONS
            if index + horizon < len(bars)
        }
        observations.append(VolumeObservation(
            symbol=symbol,
            date=bars[index].date.isoformat(),
            relative_volume20=relative_volume,
            returns_pct=returns,
        ))
    return observations


def volume_quintiles(observations: list[VolumeObservation]) -> list[list[VolumeObservation]]:
    ordered = sorted(observations, key=lambda observation: observation.relative_volume20)
    if not ordered:
        return []
    group_count = min(5, len(ordered))
    return [
        ordered[math.floor(index * len(ordered) / group_count):math.floor((index + 1) * len(ordered) / group_count)]
        for index in range(group_count)
    ]


def print_report(observations: list[VolumeObservation]) -> None:
    groups = volume_quintiles(observations)
    print(f"NEW observations with valid relative volume: {len(observations)}")
    print("\nGroup  N  Relative volume range    Mean return (%)       Median return (%)")
    print("-----  -  -----------------------  --------------------  -----------------")
    for index, group in enumerate(groups, start=1):
        value_range = f"{group[0].relative_volume20:6.2f} to {group[-1].relative_volume20:6.2f}"
        means = " ".join(
            f"{h}d={sum(item.returns_pct[h] for item in group if h in item.returns_pct) / count:6.2f}"
            if (count := sum(1 for item in group if h in item.returns_pct)) else f"{h}d=   n/a"
            for h in HORIZONS
        )
        medians = " ".join(
            f"{h}d={median([item.returns_pct[h] for item in group if h in item.returns_pct]):6.2f}"
            if any(h in item.returns_pct for item in group) else f"{h}d=   n/a"
            for h in HORIZONS
        )
        print(f"Q{index:<4} {len(group):>2}  {value_range:<23}  {means:<20}  {medians}")


async def main(index_name: str, lookback_days: int) -> None:
    tickers = await resolve_index_tickers(index_name)
    async with YFinanceTool() as tool:
        bars_by_symbol = await tool.fetch_ohlcv_batch(tickers, lookback_days)

    policy = TrendDetectionPolicyConfig()
    observations = [
        observation
        for ticker in tickers
        for observation in collect_volume_observations(
            ticker.symbol, bars_by_symbol.get(ticker.symbol, []), policy
        )
    ]
    print(f"Resolved {len(tickers)} {index_name} tickers")
    print_report(observations)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--lookback-days", type=int, default=1825)
    args = parser.parse_args()
    asyncio.run(main(args.index, args.lookback_days))
