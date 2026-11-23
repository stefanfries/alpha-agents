from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pytest

from app.agents.screening import SecuritySelectionAgent
from app.models.market import OHLCV, Ticker
from app.models.signals import (
    MarketRegime,
    ResearchResult,
)


@pytest.mark.asyncio
async def test_screening_filters_low_market_cap():
    from app.config import ScreeningSettings
    agent = SecuritySelectionAgent(ScreeningSettings(top_n=10, min_market_cap_eur=1_000_000_000))
    ticker = Ticker(symbol="SMALL")
    result = await agent.run(
        ResearchResult(
            tickers=[ticker],
            bars={"SMALL": []},
            fundamentals={"SMALL": {"marketCap": 100_000}},
        )
    )
    assert ticker not in result.selected
    assert "SMALL" in result.rationale


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ranking_metric", "expected_symbols", "expected_a_rank_change"),
    [
        ("tq60", ["A", "B"], 2),
        ("tq20", ["B", "C"], -2),
        ("tsi", ["C", "A"], 1),
    ],
)
async def test_screening_selected_uses_configured_rank_order_and_top_n(
    ranking_metric, expected_symbols, expected_a_rank_change
):
    from app.config import ScreeningSettings

    agent = SecuritySelectionAgent(ScreeningSettings(top_n=2, ranking_metric=ranking_metric))
    tickers = [Ticker(symbol=symbol) for symbol in ("A", "B", "C")]
    bars = {
        ticker.symbol: _make_synthetic_bars(ticker, [100.0 + i for i in range(100)])
        for ticker in tickers
    }

    current_tq = {
        "A": {60: 3.0, 20: 1.0},
        "B": {60: 2.0, 20: 3.0},
        "C": {60: 1.0, 20: 2.0},
    }
    historical_tq = {
        "A": {60: 1.0, 20: 3.0},
        "B": {60: 2.0, 20: 2.0},
        "C": {60: 3.0, 20: 1.0},
    }
    current_tsi = {"A": 20.0, "B": 10.0, "C": 30.0}
    historical_tsi = {"A": 10.0, "B": 30.0, "C": 20.0}
    agent._trend_quality = lambda ticker_bars, lookback: (
        current_tq if len(ticker_bars) == 100 else historical_tq
    )[ticker_bars[0].ticker.symbol][lookback]
    agent._tsi = lambda ticker_bars: (
        current_tsi if len(ticker_bars) == 100 else historical_tsi
    )[ticker_bars[0].ticker.symbol]
    agent._evaluate_policies = lambda _bars: {
        "supertrend": True,
        "supertrend_bearish": False,
        "ema20_rising": True,
        "ema20_falling": False,
        "adx_above": True,
        "adx_below": False,
        "adx_rising": True,
        "adx_falling": False,
        "price_above_ema50": True,
        "price_below_ema50": False,
        "tq60_above": True,
        "tq20_above": True,
        "tsi_above": True,
        "tsi_rising": True,
        "tsi_below": False,
    }
    agent._trend_signal = lambda *args, **kwargs: None
    agent._last_break_age = lambda *args, **kwargs: None

    result = await agent.run(
        ResearchResult(
            tickers=tickers,
            bars=bars,
            fundamentals={ticker.symbol: {"marketCap": 1_000_000_000} for ticker in tickers},
        )
    )

    assert [ticker.symbol for ticker in result.selected] == expected_symbols
    assert result.rank_changes["A"][0] == expected_a_rank_change


@pytest.mark.asyncio
async def test_screening_exposes_atr_normalized_extension_without_changing_selection():
    from app.config import ScreeningSettings

    ticker = Ticker(symbol="EXT")
    bars = _make_synthetic_bars(ticker, [100.0 + i for i in range(70)])
    agent = SecuritySelectionAgent(ScreeningSettings(top_n=1))
    agent._evaluate_policies = lambda _bars: {
        "supertrend": True,
        "supertrend_bearish": False,
        "ema20_rising": True,
        "ema20_falling": False,
        "adx_above": True,
        "adx_below": False,
        "adx_rising": True,
        "adx_falling": False,
        "price_above_ema50": True,
        "price_below_ema50": False,
        "tq60_above": True,
        "tq20_above": True,
        "tsi_above": True,
        "tsi_rising": True,
        "tsi_below": False,
    }
    agent._trend_signal = lambda *args, **kwargs: None
    agent._last_break_age = lambda *args, **kwargs: None

    result = await agent.run(ResearchResult(
        tickers=[ticker],
        bars={ticker.symbol: bars},
        fundamentals={ticker.symbol: {"marketCap": 1_000_000_000}},
    ))

    assert result.selected == [ticker]
    assert result.extension_scores[ticker.symbol] > 0
    assert result.weekly_confirmed[ticker.symbol] is False


def test_market_regime_advisory_action_differentiates_by_status():
    green = MarketRegime(symbol="^NDX", tq60=0.05, tq20=0.02, status="green")
    yellow = MarketRegime(symbol="^NDX", tq60=0.00, tq20=0.00, status="yellow")
    red = MarketRegime(symbol="^NDX", tq60=-0.05, tq20=-0.02, status="red")

    assert green.advisory_action == "normal review"
    assert yellow.advisory_action == "stricter timing review; no automatic early entries"
    assert red.advisory_action == "no new entries recommended; review existing positions"


def test_screening_downgrades_green_regime_when_breadth_is_narrow():
    agent = SecuritySelectionAgent()
    regime = MarketRegime(symbol="^NDX", display_name="Nasdaq 100", tq60=0.05, tq20=0.02, status="green")
    policy_results = {
        "A": {"supertrend": True, "ema20_above_ema50": True, "adx_above": True},
        "B": {"supertrend": False, "ema20_above_ema50": False, "adx_above": True},
        "C": {"supertrend": False, "ema20_above_ema50": False, "adx_above": True},
        "D": {"supertrend": False, "ema20_above_ema50": False, "adx_above": True},
    }

    result = agent._enrich_regime(regime, policy_results)

    assert result is regime
    assert result.status == "yellow"
    assert result.breadth_score == pytest.approx(0.25)
    assert result.breadth_components["pct_supertrend_long"] == pytest.approx(0.25)
    assert result.breadth_components["pct_ema20_above_ema50"] == pytest.approx(0.25)
    assert result.breadth_components["pct_adx_above_20"] == pytest.approx(1.0)
    assert result.advisory_action == "stricter timing review; no automatic early entries"


def test_screening_policy_group_defaults_match_legacy_behavior():
    from app.config import ScreeningSettings

    agent = SecuritySelectionAgent(ScreeningSettings())
    values = {"a": True, "b": False, "c": True}

    # NEW default: all selected must pass
    assert agent._passes_policy_group(
        values,
        {"a": True, "b": True, "c": False},
        min_true=None,
    ) is False

    # BREAK default: all selected must pass (same semantics as NEW).
    assert agent._passes_policy_group(
        values,
        {"a": False, "b": True, "c": True},
        min_true=None,
    ) is False


def test_screening_policy_group_k_of_n_and_clamp():
    from app.config import ScreeningSettings

    agent = SecuritySelectionAgent(ScreeningSettings())
    values = {"a": True, "b": True, "c": False}
    enabled = {"a": True, "b": True, "c": True}

    assert agent._passes_policy_group(values, enabled, min_true=2) is True
    assert agent._passes_policy_group(values, enabled, min_true=3) is False

    # Configured min_true above selected policy count is clamped down.
    assert agent._passes_policy_group(values, enabled, min_true=9) is False


def test_screening_policy_group_no_selected_policy_fails():
    from app.config import ScreeningSettings

    agent = SecuritySelectionAgent(ScreeningSettings())
    values = {"a": True}
    assert agent._passes_policy_group(
        values,
        {"a": False},
        min_true=1,
    ) is False


def _make_synthetic_bars(ticker: Ticker, closes: list[float]) -> list[OHLCV]:
    start = date(2025, 1, 1)
    bars: list[OHLCV] = []
    prev = closes[0]
    for i, close in enumerate(closes):
        open_ = prev
        high = max(open_, close) + 1.0
        low = min(open_, close) - 1.0
        bars.append(
            OHLCV(
                ticker=ticker,
                date=start + timedelta(days=i),
                open=Decimal(str(round(open_, 4))),
                high=Decimal(str(round(high, 4))),
                low=Decimal(str(round(low, 4))),
                close=Decimal(str(round(close, 4))),
                volume=1_000_000,
            )
        )
        prev = close
    return bars


def test_trend_indicator_snapshots_cover_bars_and_mark_state_changes():
    import json

    from app.policies.trend_detection import TrendDetectionPolicyConfig
    from app.routes.pipeline import _compute_trend_indicator_snapshots

    ticker = Ticker(symbol="SYN")
    closes = [100.0 + i * 0.5 for i in range(60)] + [130.0 - i * 0.8 for i in range(20)]
    bars = _make_synthetic_bars(ticker, closes)

    snapshots = _compute_trend_indicator_snapshots(bars, TrendDetectionPolicyConfig())

    assert len(snapshots) == len(bars)
    assert [snapshot["time"] for snapshot in snapshots] == [bar.date.isoformat() for bar in bars]
    assert {
        "ema20", "ema50", "sma200", "adx", "plus_di", "minus_di",
        "tsi", "tq60", "tq20", "supertrend",
    } <= snapshots[-1]["values"].keys()
    json.dumps(snapshots)
    assert any(snapshot["changed"] for snapshot in snapshots[1:])
    for previous, current in zip(snapshots, snapshots[1:]):
        expected = [
            key
            for key, value in current["states"].items()
            if previous["states"][key] != value
        ]
        assert current["changed"] == expected


def test_trend_signal_k2_emits_new_before_break_phase():
    from app.config import ScreeningSettings

    # Keep ADX intentionally hard to satisfy, so NEW relies on the other policies.
    agent = SecuritySelectionAgent(
        ScreeningSettings(min_adx=90, new_min_true=2, break_min_true=2)
    )
    ticker = Ticker(symbol="SYN")

    closes = [100.0 + 0.05 * i for i in range(80)] + [104.0 + 1.8 * i for i in range(8)]
    bars = _make_synthetic_bars(ticker, closes)

    new_enabled = {
        "ema20_rising": True,
        "price_above_ema50": True,
        "adx_above": True,
    }
    break_enabled = {
        "ema20_falling": True,
        "price_below_ema50": True,
        "adx_below": True,
    }

    signal = agent._trend_signal(
        bars,
        new_enabled,
        break_enabled,
        new_min_true=2,
        break_min_true=2,
    )

    assert signal in {"NEW", "HOLD"}


def test_trend_signal_k2_keeps_break_visible_for_five_bars_total():
    from app.config import ScreeningSettings

    # k=2 for both NEW and BREAK. BREAK remains visible for five bars total.
    agent = SecuritySelectionAgent(
        ScreeningSettings(min_adx=90, new_min_true=2, break_min_true=2)
    )
    ticker = Ticker(symbol="SYN")

    closes = (
        [100.0 + 0.05 * i for i in range(80)]
        + [104.0 + 1.8 * i for i in range(8)]
        + [118.0 - 3.2 * i for i in range(8)]
    )
    bars = _make_synthetic_bars(ticker, closes)

    new_enabled = {
        "ema20_rising": True,
        "price_above_ema50": True,
        "adx_above": True,
    }
    break_enabled = {
        "ema20_falling": True,
        "price_below_ema50": True,
        "adx_below": True,
    }

    signal = agent._trend_signal(
        bars,
        new_enabled,
        break_enabled,
        new_min_true=2,
        break_min_true=2,
    )

    assert signal == "BREAK"


def test_active_break_after_new_transition_emits_break_without_new_edge(monkeypatch):
    from app.agents import screening as screening_module
    from app.config import ScreeningSettings

    agent = SecuritySelectionAgent(ScreeningSettings())
    ticker = Ticker(symbol="SYN")
    bars = _make_synthetic_bars(ticker, [100.0 + 0.1 * i for i in range(70)])

    monkeypatch.setattr(screening_module, "build_trend_indicator_series", lambda *args, **kwargs: object())

    def fake_bar_indicator_values(idx, *args, **kwargs):
        return {"new": idx == len(bars) - 2, "break": idx >= 0}

    monkeypatch.setattr(screening_module, "bar_indicator_values", fake_bar_indicator_values)

    signal = agent._trend_signal(
        bars,
        {"new": True},
        {"break": True},
        new_min_true=None,
        break_min_true=None,
    )

    assert signal == "BREAK"


@pytest.mark.asyncio
async def test_screening_records_conditions_on_break_bar(monkeypatch):
    from app.agents import screening as screening_module
    from app.config import ScreeningSettings

    ticker = Ticker(symbol="SYN")
    bars = _make_synthetic_bars(ticker, [100.0 + 0.1 * i for i in range(70)])
    agent = SecuritySelectionAgent(ScreeningSettings(
        new_min_true=1, break_min_true=1, policy_tsi_falling_break=True,
        policy_supertrend_break=False, policy_ema20_falling_break=False,
        policy_adx_below_break=False, policy_price_below_ema50_break=False,
        policy_tsi_below_break=False,
    ))
    monkeypatch.setattr(
        screening_module, "build_trend_indicator_series", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        screening_module, "bar_indicator_values",
        lambda idx, *args, **kwargs: {
            "ema20_rising": idx >= len(bars) - 4,
            "tsi_falling": idx == len(bars) - 3,
            "adx_below": idx == len(bars) - 1,
        },
    )

    result = await agent.run(ResearchResult(
        tickers=[ticker], bars={"SYN": bars},
        fundamentals={"SYN": {"marketCap": 1_000_000_000}},
    ))

    assert result.trend_signals["SYN"] == "BREAK"
    assert result.last_break_age_bars["SYN"] == 2
    assert result.break_policy_results["SYN"]["tsi_falling"] is True
    assert result.break_policy_results["SYN"]["adx_below"] is False
    assert result.policy_results["SYN"]["tsi_falling"] is False
    assert result.policy_results["SYN"]["adx_below"] is True


def test_recent_new_downgrades_to_hold_when_current_bar_fails_selected_policy(monkeypatch):
    from app.agents import screening as screening_module
    from app.config import ScreeningSettings

    agent = SecuritySelectionAgent(ScreeningSettings())
    ticker = Ticker(symbol="SYN")
    bars = _make_synthetic_bars(ticker, [100.0 + 0.1 * i for i in range(75)])

    ema20 = np.array([0.0] * 70 + [0.0, 1.0, 2.0, 2.0, 0.0])
    ema50 = np.zeros(75, dtype=float)
    adx = np.full(75, np.nan, dtype=float)
    atr = np.ones(75, dtype=float)
    upper = np.full(75, np.nan, dtype=float)
    lower = np.full(75, np.nan, dtype=float)

    def fake_ema(close: np.ndarray, timeperiod: int) -> np.ndarray:
        if timeperiod == 20:
            return ema20
        if timeperiod == 50:
            return ema50
        if timeperiod in (13, 25):
            return np.zeros(len(close), dtype=float)
        raise AssertionError(f"unexpected EMA period {timeperiod}")

    monkeypatch.setattr(screening_module.talib, "EMA", fake_ema)
    monkeypatch.setattr(screening_module.talib, "ADX", lambda *args, **kwargs: adx)
    monkeypatch.setattr(screening_module.talib, "ATR", lambda *args, **kwargs: atr)
    monkeypatch.setattr(screening_module, "supertrend_bands", lambda *args, **kwargs: (upper, lower))

    signal = agent._trend_signal(
        bars,
        {"ema20_rising": True},
        {},
        new_min_true=None,
        break_min_true=None,
    )

    assert signal == "HOLD"
