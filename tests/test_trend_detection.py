import numpy as np
import pytest

from app.agents.screening import SecuritySelectionAgent
from app.config import ScreeningSettings
from app.policies.trend_detection import (
    TrendDetectionPolicyConfig,
    TrendIndicatorSeries,
    bar_indicator_values,
)


@pytest.mark.parametrize(
    ("tsi_values", "expected"),
    [
        ([1.0, 2.0, 3.0, 4.0, 5.0], True),
        ([5.0, 4.0, 3.0, 2.0, 1.0], False),
        ([5.0, 5.0, 5.0, 5.0, 5.0], False),
        ([1.0, 2.0, np.nan, 4.0, 5.0], False),
        ([1.0, 2.0, 3.0, 4.0], False),
    ],
)
def test_tsi_rising_uses_positive_slope_of_five_valid_values(tsi_values, expected):
    length = len(tsi_values)
    series = TrendIndicatorSeries(
        close=np.arange(length, dtype=float),
        ema20=np.full(length, np.nan),
        ema50=np.full(length, np.nan),
        adx=np.full(length, np.nan),
        atr20=np.ones(length),
        st_bull=np.zeros(length, dtype=bool),
        tsi=np.array(tsi_values, dtype=float),
    )

    values = bar_indicator_values(
        idx=length - 1,
        series=series,
        policy_cfg=TrendDetectionPolicyConfig(),
        lookback_regression=60,
        lookback_regression_short=20,
    )

    assert values["tsi_rising"] is expected
    assert values["tsi_falling"] is (not expected)


def test_tsi_rising_can_be_disabled_from_policy_mapping():
    policy = TrendDetectionPolicyConfig.from_mapping({"policy_tsi_rising": False})

    assert policy.entry_enabled_rules()["tsi_rising"] is False


def test_tsi_falling_break_policy_is_opt_in():
    default_policy = TrendDetectionPolicyConfig()
    enabled_policy = TrendDetectionPolicyConfig.from_mapping(
        {"policy_tsi_falling_break": True}
    )
    screening_policy = SecuritySelectionAgent(
        ScreeningSettings(policy_tsi_falling_break=True)
    )._trend_policy

    assert default_policy.exit_enabled_rules()["tsi_falling"] is False
    assert enabled_policy.exit_enabled_rules()["tsi_falling"] is True
    assert screening_policy.exit_enabled_rules()["tsi_falling"] is True


def test_default_policy_restores_existing_entry_and_break_rules():
    policy = SecuritySelectionAgent(ScreeningSettings())._trend_policy

    assert [key for key, enabled in policy.entry_enabled_rules().items() if enabled] == [
        "supertrend",
        "ema20_rising",
        "adx_above",
        "adx_rising",
        "price_above_ema50",
        "tq60_above",
        "tq20_above",
        "tsi_above",
    ]
    assert policy.entry_enabled_rules()["tsi_rising"] is False
    assert [key for key, enabled in policy.exit_enabled_rules().items() if enabled] == [
        "supertrend_bearish",
        "ema20_falling",
        "adx_below",
        "price_below_ema50",
        "tsi_below",
    ]

    chart_policy = TrendDetectionPolicyConfig.from_mapping({})
    assert chart_policy.entry_enabled_rules() == policy.entry_enabled_rules()
    assert chart_policy.exit_enabled_rules() == policy.exit_enabled_rules()