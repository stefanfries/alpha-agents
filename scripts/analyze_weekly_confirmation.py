"""Compare daily NEW outcomes with and without completed-week EMA confirmation.

Usage:
    uv run python scripts/analyze_weekly_confirmation.py
    uv run python scripts/analyze_weekly_confirmation.py --index DAX
"""

import argparse
import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import numpy as np
import talib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.market import OHLCV
from app.policies.trend_detection import TrendDetectionPolicyConfig
from app.tools.yfinance import YFinanceTool
from scripts.analyze_adx_slope import signal_events
from scripts.analyze_entry_extension import (
    DEFAULT_INDEX,
    HORIZONS,
    resolve_index_tickers,
)
from scripts.analyze_relative_strength import new_signal_indices


@dataclass(frozen=True)
class WeeklyObservation:
    confirmed: bool
    returns_pct: dict[int, float]


def completed_weekly_state(bars: list[OHLCV]) -> dict[int, bool]:
    """Map each daily index to a weekly state using no still-open weekly bar."""
    by_week: dict[tuple[int, int], list[tuple[int, OHLCV]]] = defaultdict(list)
    for index, bar in enumerate(bars):
        iso = bar.date.isocalendar()
        by_week[(iso.year, iso.week)].append((index, bar))
    weeks = list(by_week.values())
    weekly_end_indexes = [entries[-1][0] for entries in weeks]
    weekly_close = np.array([float(entries[-1][1].close) for entries in weeks])
    ema20 = talib.EMA(weekly_close, timeperiod=20)

    states: dict[int, bool] = {}
    for week_index, entries in enumerate(weeks):
        for daily_index, _bar in entries:
            completed_index = week_index if daily_index == weekly_end_indexes[week_index] else week_index - 1
            states[daily_index] = bool(
                completed_index >= 24
                and not np.isnan(ema20[completed_index])
                and not np.isnan(ema20[completed_index - 5])
                and weekly_close[completed_index] > ema20[completed_index]
                and ema20[completed_index] > ema20[completed_index - 5]
            )
    return states


def collect_weekly_observations(bars: list[OHLCV], policy: TrendDetectionPolicyConfig) -> list[WeeklyObservation]:
    weekly_states = completed_weekly_state(bars)
    observations: list[WeeklyObservation] = []
    for index in new_signal_indices(bars, policy):
        entry_close = float(bars[index].close)
        observations.append(WeeklyObservation(
            confirmed=weekly_states.get(index, False),
            returns_pct={
                horizon: ((float(bars[index + horizon].close) / entry_close) - 1.0) * 100.0
                for horizon in HORIZONS
                if index + horizon < len(bars)
            },
        ))
    return observations


def print_group(label: str, observations: list[WeeklyObservation]) -> None:
    means = " ".join(
        f"{h}d={sum(item.returns_pct[h] for item in observations if h in item.returns_pct) / count:6.2f}"
        if (count := sum(1 for item in observations if h in item.returns_pct)) else f"{h}d=   n/a"
        for h in HORIZONS
    )
    medians = " ".join(
        f"{h}d={median([item.returns_pct[h] for item in observations if h in item.returns_pct]):6.2f}"
        if any(h in item.returns_pct for item in observations) else f"{h}d=   n/a"
        for h in HORIZONS
    )
    print(f"{label:<14} N={len(observations):>3}  mean {means}  median {medians}")


def churn_counts(events, weekly_states: dict[int, bool], confirmed: bool, horizon: int) -> tuple[int, int]:
    breaks = [event.index for event in events if event.side == "BREAK"]
    new_indexes = [
        event.index for event in events
        if event.side == "NEW" and weekly_states.get(event.index, False) is confirmed
    ]
    churned = sum(any(index < broken <= index + horizon for broken in breaks) for index in new_indexes)
    return churned, len(new_indexes)


async def main(index_name: str, lookback_days: int) -> None:
    tickers = await resolve_index_tickers(index_name)
    async with YFinanceTool() as tool:
        bars_by_symbol = await tool.fetch_ohlcv_batch(tickers, lookback_days)
    policy = TrendDetectionPolicyConfig()
    confirmed: list[WeeklyObservation] = []
    unconfirmed: list[WeeklyObservation] = []
    churn: dict[tuple[bool, int], list[int]] = {(state, horizon): [0, 0] for state in (True, False) for horizon in (5, 10)}
    for ticker in tickers:
        bars = bars_by_symbol.get(ticker.symbol, [])
        states = completed_weekly_state(bars)
        new_indexes = new_signal_indices(bars, policy)
        for observation, _daily_index in zip(collect_weekly_observations(bars, policy), new_indexes):
            (confirmed if observation.confirmed else unconfirmed).append(observation)
        events = signal_events(bars, policy, adx_slope_window=5, smooth_adx=False)
        for state in (True, False):
            for horizon in (5, 10):
                numerator, denominator = churn_counts(events, states, state, horizon)
                churn[(state, horizon)][0] += numerator
                churn[(state, horizon)][1] += denominator

    print(f"Resolved {len(tickers)} {index_name} tickers")
    print_group("Weekly confirmed", confirmed)
    print_group("Not confirmed", unconfirmed)
    for state, label in ((True, "Weekly confirmed"), (False, "Not confirmed")):
        churn_text = " ".join(
            f"churn{h}={numerator / denominator:.1%}" if denominator else f"churn{h}=n/a"
            for h in (5, 10)
            for numerator, denominator in [churn[(state, h)]]
        )
        print(f"{label:<14} {churn_text}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--lookback-days", type=int, default=1825)
    args = parser.parse_args()
    asyncio.run(main(args.index, args.lookback_days))
