"""Shared technical indicator computations used by agents and chart routes."""

from __future__ import annotations

import numpy as np
import talib


def supertrend_bands(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 10,
    multiplier: float = 3.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (final_upper, final_lower) SuperTrend bands, NaN where ATR is not yet available.

    Both arrays have the same length as the input arrays.
    """
    n = len(close)
    atr = talib.ATR(high, low, close, timeperiod=period)
    hl2 = (high + low) / 2.0
    raw_upper = hl2 + multiplier * atr
    raw_lower = hl2 - multiplier * atr

    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)

    for i in range(n):
        if np.isnan(atr[i]):
            continue
        if np.isnan(final_upper[i - 1]) if i > 0 else True:
            final_upper[i] = raw_upper[i]
            final_lower[i] = raw_lower[i]
        else:
            final_upper[i] = (
                raw_upper[i]
                if raw_upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]
                else final_upper[i - 1]
            )
            final_lower[i] = (
                raw_lower[i]
                if raw_lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]
                else final_lower[i - 1]
            )

    return final_upper, final_lower


def supertrend_bullish(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 10,
    multiplier: float = 3.0,
) -> bool:
    """Return True if SuperTrend is in bullish state on the last bar."""
    final_upper, final_lower = supertrend_bands(high, low, close, period, multiplier)

    trend = 1
    started = False
    for i in range(len(close)):
        if np.isnan(final_upper[i]):
            continue
        if not started:
            started = True
        elif trend == 1 and close[i] < final_lower[i]:
            trend = -1
        elif trend == -1 and close[i] > final_upper[i]:
            trend = 1

    return trend == 1
