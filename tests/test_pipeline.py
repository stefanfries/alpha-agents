from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pytest

from app.agents.execution import TradeExecutionAgent
from app.agents.portfolio import PortfolioConstructionAgent
from app.agents.risk import RiskAgent
from app.agents.screening import SecuritySelectionAgent
from app.agents.warrant_selection import WarrantSelectionAgent
from app.models.market import OHLCV, Position, Ticker
from app.models.signals import ResearchResult, SelectionResult


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
async def test_portfolio_equal_weights():
    tickers = [Ticker(symbol=s) for s in ["A", "B", "C", "D"]]
    agent = PortfolioConstructionAgent(capital_eur=10_000, sizing_method="equal", max_position_weight=0.5)
    result = await agent.run(
        SelectionResult(
            selected=tickers,
            scores={"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0},
            rationale={},
        )
    )
    assert len(result.positions) == 4
    for w in result.target_weights.values():
        assert abs(w - 0.25) < 1e-9


@pytest.mark.asyncio
async def test_risk_rejects_oversized_position():
    from app.models.signals import PortfolioProposal

    ticker = Ticker(symbol="BIG")
    agent = RiskAgent(max_position_weight=0.10, max_positions=30)
    result = await agent.run(
        PortfolioProposal(
            positions=[Position(ticker=ticker, quantity=Decimal("5000"), avg_cost=Decimal("0"))],
            target_weights={"BIG": 0.50},
        )
    )
    assert ticker in [p.ticker for p in result.rejected_positions]
    assert "BIG" in result.risk_notes


@pytest.mark.asyncio
async def test_execution_dry_run_does_not_raise():
    from app.models.signals import RiskAssessment

    ticker = Ticker(symbol="AAPL")
    agent = TradeExecutionAgent(dry_run=True, min_trade_eur=100.0, order_type="limit")
    result = await agent.run(
        RiskAssessment(
            approved_positions=[
                Position(ticker=ticker, quantity=Decimal("500"), avg_cost=Decimal("0"))
            ],
            rejected_positions=[],
            risk_notes={},
        )
    )
    assert len(result.orders) == 1
    assert result.orders[0].side == "buy"


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


def test_trend_signal_k2_emits_break_after_regime_change():
    from app.config import ScreeningSettings

    # k=2 for both NEW and BREAK; BREAK should trigger after the downtrend regime starts.
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


def test_warrant_selection_extracts_midprice_from_bid_ask_quote():
    price = WarrantSelectionAgent._extract_quote_price(
        {
            "name": "ASML Holding",
            "isin": "NL0010273215",
            "bid": 1660.0,
            "ask": 1661.2,
            "spread_percent": 0.0722369371538674,
            "currency": "EUR",
        }
    )

    assert price == pytest.approx(1660.6)


@pytest.mark.asyncio
async def test_restart_stage_persists_screening_policy_form_values(monkeypatch):
    from app.routes import pipeline as pipeline_module

    class FakeCollection:
        def __init__(self) -> None:
            self.calls: list[tuple[dict, dict]] = []

        async def update_one(self, selector: dict, update: dict) -> None:
            self.calls.append((selector, update))

    class FakePipeline:
        async def run_stage(self, execution_id: str, from_stage: str) -> None:
            return None

    fake_collection = FakeCollection()

    monkeypatch.setattr(pipeline_module, "executions_collection", lambda: fake_collection)
    monkeypatch.setattr(pipeline_module, "get_pipeline", lambda: FakePipeline())
    monkeypatch.setattr(pipeline_module, "_fire", lambda coro: coro.close())

    response = await pipeline_module.restart_stage(
        qs_id="qs1",
        execution_id="exec1",
        stage="screening",
        from_stage="screening",
        policies_submitted="1",
        policy_supertrend="on",
        policy_ema20_rising="on",
        policy_adx_above=None,
        policy_adx_rising="on",
        policy_price_above_ema50="on",
        policy_tq60_above="on",
        policy_tq20_above=None,
        policy_tq60_min="0.07",
        policy_tq20_min="0.02",
        new_min_true="99",
        policy_supertrend_break="on",
        policy_ema20_falling_break=None,
        policy_adx_below_break="on",
        policy_adx_falling_break=None,
        policy_price_below_ema50_break=None,
        break_min_true="0",
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/quant-systems/qs1/executions/exec1/stages/screening"

    assert len(fake_collection.calls) == 1
    selector, update = fake_collection.calls[0]
    assert selector == {"execution_id": "exec1"}

    screening_cfg = update["$set"]["config_overrides.screening"]
    assert screening_cfg == {
        "policy_supertrend": True,
        "policy_ema20_rising": True,
        "policy_adx_above": False,
        "policy_adx_rising": True,
        "policy_price_above_ema50": True,
        "policy_tq60_above": True,
        "policy_tq20_above": False,
        "policy_tq60_min": 0.07,
        "policy_tq20_min": 0.02,
        "new_min_true": 5,
        "policy_supertrend_break": True,
        "policy_ema20_falling_break": False,
        "policy_adx_below_break": True,
        "policy_adx_falling_break": False,
        "policy_price_below_ema50_break": False,
        "break_min_true": 1,
    }


@pytest.mark.asyncio
async def test_restart_stage_clamps_tq_thresholds_and_handles_invalid(monkeypatch):
    from app.routes import pipeline as pipeline_module

    class FakeCollection:
        def __init__(self) -> None:
            self.calls: list[tuple[dict, dict]] = []

        async def update_one(self, selector: dict, update: dict) -> None:
            self.calls.append((selector, update))

    class FakePipeline:
        async def run_stage(self, execution_id: str, from_stage: str) -> None:
            return None

    fake_collection = FakeCollection()

    monkeypatch.setattr(pipeline_module, "executions_collection", lambda: fake_collection)
    monkeypatch.setattr(pipeline_module, "get_pipeline", lambda: FakePipeline())
    monkeypatch.setattr(pipeline_module, "_fire", lambda coro: coro.close())

    await pipeline_module.restart_stage(
        qs_id="qs1",
        execution_id="exec2",
        stage="screening",
        from_stage="screening",
        policies_submitted="1",
        policy_supertrend="on",
        policy_ema20_rising="on",
        policy_adx_above="on",
        policy_adx_rising="on",
        policy_price_above_ema50="on",
        policy_tq60_above="on",
        policy_tq20_above="on",
        policy_tq60_min="9.9",
        policy_tq20_min="invalid",
        new_min_true="2",
        policy_supertrend_break="on",
        policy_ema20_falling_break="on",
        policy_adx_below_break="on",
        policy_adx_falling_break="on",
        policy_price_below_ema50_break="on",
        break_min_true="2",
    )

    _, update = fake_collection.calls[0]
    screening_cfg = update["$set"]["config_overrides.screening"]

    assert screening_cfg["policy_tq60_min"] == 1.0
    assert screening_cfg["policy_tq20_min"] == 0.0
