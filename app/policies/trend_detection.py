from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import numpy as np
import talib

from app.indicators import supertrend_bands


@dataclass(frozen=True, slots=True)
class TrendDetectionPolicyConfig:
    """Boolean trend-detection policy configuration for NEW/BREAK logic."""

    min_adx: int = 20

    # NEW (entry) detection policies
    policy_supertrend: bool = True
    policy_ema20_rising: bool = True
    policy_adx_above: bool = True
    policy_adx_rising: bool = True
    policy_price_above_ema50: bool = True
    policy_tq60_above: bool = False
    policy_tq20_above: bool = False
    policy_tq60_min: float = 0.05
    policy_tq20_min: float = 0.0
    new_min_true: int | None = None

    # BREAK (exit) detection policies
    policy_supertrend_break: bool = True
    policy_ema20_falling_break: bool = True
    policy_adx_below_break: bool = True
    policy_adx_falling_break: bool = True
    policy_price_below_ema50_break: bool = True
    break_min_true: int | None = None

    # Indicator settings used by marker computation
    supertrend_period: int = 10
    supertrend_multiplier: float = 3.0

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "TrendDetectionPolicyConfig":
        src = values or {}
        return cls(
            min_adx=_as_int(src.get("min_adx"), 20),
            policy_supertrend=_as_bool(src.get("policy_supertrend"), True),
            policy_ema20_rising=_as_bool(src.get("policy_ema20_rising"), True),
            policy_adx_above=_as_bool(src.get("policy_adx_above"), True),
            policy_adx_rising=_as_bool(src.get("policy_adx_rising"), True),
            policy_price_above_ema50=_as_bool(src.get("policy_price_above_ema50"), True),
            policy_tq60_above=_as_bool(src.get("policy_tq60_above"), False),
            policy_tq20_above=_as_bool(src.get("policy_tq20_above"), False),
            policy_tq60_min=_as_float(src.get("policy_tq60_min"), 0.05),
            policy_tq20_min=_as_float(src.get("policy_tq20_min"), 0.0),
            new_min_true=_as_optional_int(src.get("new_min_true")),
            policy_supertrend_break=_as_bool(src.get("policy_supertrend_break"), True),
            policy_ema20_falling_break=_as_bool(src.get("policy_ema20_falling_break"), True),
            policy_adx_below_break=_as_bool(src.get("policy_adx_below_break"), True),
            policy_adx_falling_break=_as_bool(src.get("policy_adx_falling_break"), True),
            policy_price_below_ema50_break=_as_bool(src.get("policy_price_below_ema50_break"), True),
            break_min_true=_as_optional_int(src.get("break_min_true")),
            supertrend_period=_as_int(src.get("supertrend_period"), 10),
            supertrend_multiplier=_as_float(src.get("supertrend_multiplier"), 3.0),
        )

    def entry_enabled_rules(self) -> dict[str, bool]:
        return {
            "supertrend": self.policy_supertrend,
            "ema20_rising": self.policy_ema20_rising,
            "adx_above": self.policy_adx_above,
            "adx_rising": self.policy_adx_rising,
            "price_above_ema50": self.policy_price_above_ema50,
            "tq60_above": self.policy_tq60_above,
            "tq20_above": self.policy_tq20_above,
        }

    def exit_enabled_rules(self) -> dict[str, bool]:
        return {
            "supertrend_bearish": self.policy_supertrend_break,
            "ema20_falling": self.policy_ema20_falling_break,
            "adx_below": self.policy_adx_below_break,
            "adx_falling": self.policy_adx_falling_break,
            "price_below_ema50": self.policy_price_below_ema50_break,
        }


@dataclass(frozen=True, slots=True)
class TrendIndicatorSeries:
    close: np.ndarray
    ema20: np.ndarray
    ema50: np.ndarray
    adx: np.ndarray
    atr20: np.ndarray
    st_bull: np.ndarray


def build_trend_indicator_series(
    bars: list[Any],
    policy_cfg: TrendDetectionPolicyConfig,
    supertrend_fn: Callable[[np.ndarray, np.ndarray, np.ndarray, int, float], tuple[np.ndarray, np.ndarray]] = supertrend_bands,
) -> TrendIndicatorSeries:
    high = np.array([float(b.high) for b in bars])
    low = np.array([float(b.low) for b in bars])
    close = np.array([float(b.close) for b in bars])

    ema20 = talib.EMA(close, timeperiod=20)
    ema50 = talib.EMA(close, timeperiod=50)
    adx_vals = talib.ADX(high, low, close, timeperiod=14)
    atr20 = talib.ATR(high, low, close, timeperiod=20)
    final_upper, final_lower = supertrend_fn(
        high,
        low,
        close,
        policy_cfg.supertrend_period,
        policy_cfg.supertrend_multiplier,
    )

    st_bull = np.zeros(len(close), dtype=bool)
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
        st_bull[i] = trend == 1

    return TrendIndicatorSeries(
        close=close,
        ema20=ema20,
        ema50=ema50,
        adx=adx_vals,
        atr20=atr20,
        st_bull=st_bull,
    )


def trend_quality_at_index(
    close: np.ndarray,
    atr20: np.ndarray,
    idx: int,
    lookback: int,
) -> float:
    if idx + 1 < lookback:
        return 0.0
    atr_val = float(atr20[idx])
    if np.isnan(atr_val) or atr_val <= 0:
        return 0.0
    segment = close[idx - lookback + 1 : idx + 1]
    x = np.arange(lookback, dtype=float)
    slope, intercept = np.polyfit(x, segment, 1)
    fitted = slope * x + intercept
    ss_res = float(np.sum((segment - fitted) ** 2))
    ss_tot = float(np.sum((segment - segment.mean()) ** 2))
    r2 = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0
    return r2 * (slope / atr_val)


def bar_indicator_values(
    idx: int,
    series: TrendIndicatorSeries,
    policy_cfg: TrendDetectionPolicyConfig,
    lookback_regression: int,
    lookback_regression_short: int,
) -> dict[str, bool]:
    adx_seg = series.adx[idx - 4 : idx + 1] if idx >= 4 else np.array([np.nan])
    if not np.any(np.isnan(adx_seg)):
        adx_slope = float(np.polyfit(np.arange(5, dtype=float), adx_seg, 1)[0])
        adx_above = float(series.adx[idx]) > policy_cfg.min_adx
        adx_rising = adx_slope > 0
    else:
        adx_above = adx_rising = False

    ema20_rising = (
        bool(float(series.ema20[idx]) > float(series.ema20[idx - 5]))
        if idx >= 5 and not (np.isnan(series.ema20[idx]) or np.isnan(series.ema20[idx - 5]))
        else False
    )
    price_above_ema50 = bool(
        not np.isnan(series.ema50[idx]) and float(series.close[idx]) > float(series.ema50[idx])
    )
    tq60 = trend_quality_at_index(series.close, series.atr20, idx, lookback_regression)
    tq20 = trend_quality_at_index(series.close, series.atr20, idx, lookback_regression_short)

    return {
        "supertrend": bool(series.st_bull[idx]),
        "supertrend_bearish": not bool(series.st_bull[idx]),
        "ema20_rising": ema20_rising,
        "ema20_falling": not ema20_rising,
        "adx_above": adx_above,
        "adx_below": not adx_above,
        "adx_rising": adx_rising,
        "adx_falling": not adx_rising,
        "price_above_ema50": price_above_ema50,
        "price_below_ema50": not price_above_ema50,
        "tq60_above": tq60 > policy_cfg.policy_tq60_min,
        "tq20_above": tq20 > policy_cfg.policy_tq20_min,
    }


def passes_rule_group(
    indicator_values: dict[str, bool],
    enabled_rules: dict[str, bool],
    min_true: int | None,
) -> bool:
    active = [k for k, on in enabled_rules.items() if on]
    if not active:
        return False
    true_count = sum(1 for k in active if indicator_values.get(k, False))
    required = min_true if min_true is not None else len(active)
    required = max(1, min(required, len(active)))
    return true_count >= required


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default
