"""Compare ADX-slope variants without changing production trend-detection behavior.

Usage:
    uv run python scripts/analyze_adx_slope.py
    uv run python scripts/analyze_adx_slope.py --index DAX

The report compares the current five-bar raw ADX slope with a nine-bar raw slope and a
five-bar EMA-smoothed ADX slope. It is diagnostic only.
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import numpy as np
import talib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.market import OHLCV
from app.policies.trend_detection import (
    TrendDetectionPolicyConfig,
    bar_indicator_values,
    build_trend_indicator_series,
    passes_rule_group,
)
from app.tools.yfinance import YFinanceTool
from scripts.analyze_entry_extension import DEFAULT_INDEX, resolve_index_tickers


@dataclass(frozen=True)
class SignalEvent:
    index: int
    side: str


def signal_events(
    bars: list[OHLCV],
    policy: TrendDetectionPolicyConfig,
    adx_slope_window: int,
    smooth_adx: bool,
) -> list[SignalEvent]:
    """Replay NEW/BREAK events with only the ADX-rising calculation varied."""
    if len(bars) < 70:
        return []

    series = build_trend_indicator_series(bars, policy)
    adx = talib.EMA(series.adx, timeperiod=5) if smooth_adx else series.adx
    entry_rules = policy.entry_enabled_rules()
    exit_rules = policy.exit_enabled_rules()
    passes_new = np.zeros(len(bars), dtype=bool)
    passes_break = np.zeros(len(bars), dtype=bool)

    for index in range(len(bars)):
        values = bar_indicator_values(index, series, policy, 60, 20)
        if index >= adx_slope_window - 1:
            segment = adx[index - adx_slope_window + 1:index + 1]
            if not np.any(np.isnan(segment)):
                values["adx_rising"] = bool(np.polyfit(np.arange(adx_slope_window), segment, 1)[0] > 0)
                values["adx_falling"] = not values["adx_rising"]
            else:
                values["adx_rising"] = values["adx_falling"] = False
        else:
            values["adx_rising"] = values["adx_falling"] = False
        passes_new[index] = passes_rule_group(values, entry_rules, policy.new_min_true)
        passes_break[index] = passes_rule_group(values, exit_rules, policy.break_min_true)

    state = "OUT"
    events: list[SignalEvent] = []
    for index in range(1, len(bars)):
        if state == "OUT" and passes_new[index] and not passes_new[index - 1]:
            state = "IN_TREND"
            events.append(SignalEvent(index, "NEW"))
        elif state == "IN_TREND" and passes_break[index]:
            state = "OUT"
            events.append(SignalEvent(index, "BREAK"))
    return events


def churn_rate(events: list[SignalEvent], horizon: int) -> float:
    new_events = [event for event in events if event.side == "NEW"]
    if not new_events:
        return 0.0
    breaks = [event.index for event in events if event.side == "BREAK"]
    churned = sum(any(new.index < broken <= new.index + horizon for broken in breaks) for new in new_events)
    return churned / len(new_events)


def matching_delays(baseline: list[SignalEvent], variant: list[SignalEvent], side: str) -> list[int]:
    variant_indices = [event.index for event in variant if event.side == side]
    return [
        min((index - event.index for index in variant_indices if abs(index - event.index) <= 10), key=abs)
        for event in baseline if event.side == side
        if any(abs(index - event.index) <= 10 for index in variant_indices)
    ]


def print_variant(label: str, baseline: list[SignalEvent], variant: list[SignalEvent]) -> None:
    new_count = sum(event.side == "NEW" for event in variant)
    break_count = sum(event.side == "BREAK" for event in variant)
    break_delays = matching_delays(baseline, variant, "BREAK")
    baseline_new = sum(event.side == "NEW" for event in baseline)
    only_variant = len(set(variant) - set(baseline))
    only_baseline = len(set(baseline) - set(variant))
    print(
        f"{label}: NEW={new_count} ({(new_count / baseline_new - 1.0) * 100 if baseline_new else 0:+.1f}% vs baseline), "
        f"BREAK={break_count}, churn5={churn_rate(variant, 5):.1%}, churn10={churn_rate(variant, 10):.1%}, "
        f"median BREAK delay={median(break_delays) if break_delays else 'n/a'} bars, "
        f"variant-only={only_variant}, baseline-only={only_baseline}"
    )


async def main(index_name: str, lookback_days: int) -> None:
    tickers = await resolve_index_tickers(index_name)
    async with YFinanceTool() as tool:
        bars_by_symbol = await tool.fetch_ohlcv_batch(tickers, lookback_days)

    policy = TrendDetectionPolicyConfig()
    aggregate: dict[str, list[SignalEvent]] = {"baseline": [], "window9": [], "smooth": []}
    offset = 0
    for ticker in tickers:
        bars = bars_by_symbol.get(ticker.symbol, [])
        variants = {
            "baseline": signal_events(bars, policy, adx_slope_window=5, smooth_adx=False),
            "window9": signal_events(bars, policy, adx_slope_window=9, smooth_adx=False),
            "smooth": signal_events(bars, policy, adx_slope_window=5, smooth_adx=True),
        }
        for name, events in variants.items():
            aggregate[name].extend(SignalEvent(event.index + offset, event.side) for event in events)
        offset += len(bars) + 100

    print(f"Resolved {len(tickers)} {index_name} tickers; compared {len(bars_by_symbol)} histories")
    print_variant("Baseline raw ADX slope (5 bars)", aggregate["baseline"], aggregate["baseline"])
    print_variant("Raw ADX slope (9 bars)", aggregate["baseline"], aggregate["window9"])
    print_variant("EMA(5) ADX + slope (5 bars)", aggregate["baseline"], aggregate["smooth"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--lookback-days", type=int, default=1825)
    args = parser.parse_args()
    asyncio.run(main(args.index, args.lookback_days))
