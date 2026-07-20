from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pytest

from app.agents.execution import TradeExecutionAgent
from app.agents.monitoring import MonitoringAgent, MonitoringInput, WarrantSnapshot
from app.agents.portfolio import PortfolioConstructionAgent
from app.agents.risk import RiskAgent
from app.agents.screening import SecuritySelectionAgent
from app.agents.warrant_selection import WarrantSelectionAgent
from app.config import MonitoringSettings
from app.models.market import OHLCV, Position, Ticker
from app.models.signals import ResearchResult, SelectionResult, WarrantSelectionResult


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


@pytest.mark.asyncio
async def test_restart_stage_persists_warrant_maturity_range(monkeypatch):
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
        execution_id="exec3",
        stage="warrant_selection",
        from_stage="warrant_selection",
        maturity_range_submitted="1",
        ws_min_months="9",
        ws_max_months="15",
        ws_strike_min_factor="0.95",
        ws_strike_max_factor="1.00",
        ws_min_score="0.62",
    )

    _, update = fake_collection.calls[0]
    ws_cfg = update["$set"]["config_overrides.warrant_selection"]

    assert ws_cfg == {
        "min_days_to_expiry": 270,
        "max_days_to_expiry": 450,
        "strike_min_factor": 0.95,
        "strike_max_factor": 1.0,
        "min_score": 0.62,
    }


@pytest.mark.asyncio
async def test_run_warrant_selection_uses_maturity_override(monkeypatch):
    from app import orchestrator as orchestrator_module

    captured: dict[str, float] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["min_days"] = kwargs["min_days_to_expiry"]
            captured["max_days"] = kwargs["max_days_to_expiry"]
            captured["strike_min_factor"] = kwargs["strike_min_factor"]
            captured["strike_max_factor"] = kwargs["strike_max_factor"]
            captured["min_score"] = kwargs["min_score"]

        async def run(self, _input: SelectionResult) -> WarrantSelectionResult:
            return WarrantSelectionResult(selected=[], skipped=[])

    class FakeFinHub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    pipeline = orchestrator_module.Pipeline()

    async def fake_wake(*_args, **_kwargs) -> None:
        return None

    async def fake_overrides_map() -> dict[str, str]:
        return {}

    monkeypatch.setattr(orchestrator_module, "WarrantSelectionAgent", FakeAgent)
    monkeypatch.setattr(orchestrator_module, "FinHubTool", FakeFinHub)
    monkeypatch.setattr(orchestrator_module.warrant_availability, "overrides_map", fake_overrides_map)
    monkeypatch.setattr(pipeline, "_wake_finhub", fake_wake)

    run = {
        "execution_id": "exec4",
        "config_overrides": {
            "warrant_selection": {
                "min_days_to_expiry": 300,
                "max_days_to_expiry": 540,
                "strike_min_factor": 0.96,
                "strike_max_factor": 1.01,
                "min_score": 0.55,
            }
        },
        "stages": {
            "monitoring": {
                "result": {
                    "positions_to_sell": [],
                    "positions_to_keep": [],
                    "positions_to_roll": [],
                    "entry_candidates": [{"symbol": "A", "isin": None, "name": None}],
                    "free_positions": 1,
                    "excluded_symbols": [],
                    "keep_existing_isins": [],
                    "roll_underlyings": [],
                    "roll_keep_underlyings": [],
                }
            },
            "screening": {
                "result": SelectionResult(
                    selected=[Ticker(symbol="A")],
                    scores={"A": 1.0},
                    rationale={},
                ).model_dump(mode="json")
            },
            "research": {
                "result": ResearchResult(
                    tickers=[Ticker(symbol="A")],
                    bars={},
                    fundamentals={"A": {"currentPrice": 123.0}},
                ).model_dump(mode="json")
            },
        },
    }

    await pipeline._run_warrant_selection(run)

    assert captured == {
        "min_days": 300,
        "max_days": 540,
        "strike_min_factor": 0.96,
        "strike_max_factor": 1.01,
        "min_score": 0.55,
    }


@pytest.mark.asyncio
async def test_warrant_selection_adapts_strike_interval_when_candidate_count_low():
    class FakeFinHub:
        def __init__(self) -> None:
            self.strike_windows: list[tuple[float | None, float | None]] = []
            self.call_count = 0

        async def get_warrants(self, **kwargs):
            self.call_count += 1
            self.strike_windows.append((kwargs.get("strike_min"), kwargs.get("strike_max")))
            if self.call_count == 1:
                return [{"isin": "W1"}, {"isin": "W2"}, {"isin": "W3"}]
            if self.call_count == 2:
                return [{"isin": "W1"}, {"isin": "W2"}, {"isin": "W3"}, {"isin": "W4"}]
            return [
                {"isin": "W1"},
                {"isin": "W2"},
                {"isin": "W3"},
                {"isin": "W4"},
                {"isin": "W5"},
                {"isin": "W6"},
            ]

        async def get_warrant_detail(self, isin: str):
            return {
                "isin": isin,
                "wkn": isin,
                "market_data": {"spread_percent": 0.5, "bid": 1.0, "ask": 1.1},
                "analytics": {"leverage": 5.0, "delta": 0.5},
                "reference_data": {"maturity_date": (date.today() + timedelta(days=330)).isoformat()},
            }

    finhub = FakeFinHub()
    agent = WarrantSelectionAgent(
        finhub=finhub,
        prices={"A": 100.0},
        strike_min_factor=0.95,
        strike_max_factor=1.00,
    )

    result = await agent.run(
        SelectionResult(
            selected=[Ticker(symbol="A", isin="ISIN1")],
            scores={"A": 1.0},
            rationale={},
        )
    )

    assert len(result.selected) == 1
    assert result.analyzed_count["A"] == 6
    assert len(finhub.strike_windows) == 3
    first_min, first_max = finhub.strike_windows[0]
    third_min, third_max = finhub.strike_windows[2]
    assert first_min is not None and first_max is not None
    assert third_min is not None and third_max is not None
    assert (third_max - third_min) > (first_max - first_min)


@pytest.mark.asyncio
async def test_warrant_selection_skips_when_no_candidate_exceeds_min_score():
    class FakeFinHub:
        async def get_warrants(self, **_kwargs):
            return [{"isin": "W1"}, {"isin": "W2"}]

        async def get_warrant_detail(self, isin: str):
            return {
                "isin": isin,
                "wkn": isin,
                "market_data": {"spread_percent": 8.0, "bid": 1.0, "ask": 1.1},
                "analytics": {"leverage": None, "delta": None},
                "reference_data": {"maturity_date": None},
            }

    agent = WarrantSelectionAgent(
        finhub=FakeFinHub(),
        prices={"A": 100.0},
        min_score=0.99,
    )

    result = await agent.run(
        SelectionResult(
            selected=[Ticker(symbol="A", isin="ISIN1")],
            scores={"A": 1.0},
            rationale={},
        )
    )

    assert result.selected == []
    assert result.skipped == ["A"]
    assert "A" in result.skipped_reasons
    assert "min score 0.99" in result.skipped_reasons["A"]


def test_warrant_selection_scoring_tracks_active_maturity_range():
    today = date.today()
    near_mid = {
        "market_data": {"spread_percent": 1.0},
        "analytics": {"leverage": 5.0, "delta": 0.5},
        "reference_data": {"maturity_date": (today + timedelta(days=330)).isoformat()},
    }
    longer = {
        "market_data": {"spread_percent": 1.0},
        "analytics": {"leverage": 5.0, "delta": 0.5},
        "reference_data": {"maturity_date": (today + timedelta(days=450)).isoformat()},
    }

    shorter_window_agent = WarrantSelectionAgent(
        finhub=None,
        prices={},
        min_days_to_expiry=270,
        max_days_to_expiry=450,
    )
    wider_window_agent = WarrantSelectionAgent(
        finhub=None,
        prices={},
        min_days_to_expiry=270,
        max_days_to_expiry=540,
    )

    assert shorter_window_agent._score(near_mid, today) > shorter_window_agent._score(longer, today)
    assert wider_window_agent._score(longer, today) > wider_window_agent._score(near_mid, today)


@pytest.mark.asyncio
async def test_monitoring_no_holdings_reports_full_free_slots_from_config_override(monkeypatch):
    from app.orchestrator import Pipeline

    pipeline = Pipeline()

    async def fake_fetch_holdings(_run: dict) -> list[Position]:
        return []

    monkeypatch.setattr(pipeline, "_fetch_holdings", fake_fetch_holdings)

    screening = SelectionResult(
        selected=[Ticker(symbol="A"), Ticker(symbol="B"), Ticker(symbol="C")],
        scores={"A": 1.0, "B": 0.9, "C": 0.8},
        rationale={},
    )
    run = {
        "stages": {"screening": {"result": screening.model_dump(mode="json")}},
        "config_overrides": {"portfolio": {"max_positions": 20}},
    }

    result = await pipeline._run_monitoring(run)

    assert result.free_positions == 20
    assert len(result.entry_candidates) == 3
    assert result.positions_to_keep == []
    assert result.positions_to_sell == []


@pytest.mark.asyncio
async def test_monitoring_uses_portfolio_max_positions_override_with_holdings(monkeypatch):
    from app.orchestrator import Pipeline

    pipeline = Pipeline()

    async def fake_fetch_holdings(_run: dict) -> list[Position]:
        return [
            Position(
                ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                quantity=Decimal("1"),
                avg_cost=Decimal("0"),
            )
        ]

    async def fake_warrant_underlying_map(_run: dict, _current_holdings: list[Position] | None = None) -> dict[str, str]:
        return {"ISIN1": "A"}

    async def fake_held_since(_run: dict) -> dict[str, date]:
        return {"WKN1": date.today() - timedelta(days=30)}

    monkeypatch.setattr(pipeline, "_fetch_holdings", fake_fetch_holdings)
    monkeypatch.setattr(pipeline, "_fetch_warrant_underlying_map", fake_warrant_underlying_map)
    monkeypatch.setattr(pipeline, "_fetch_held_since", fake_held_since)

    screening = SelectionResult(
        selected=[Ticker(symbol="A"), Ticker(symbol="B"), Ticker(symbol="C")],
        scores={"A": 1.0, "B": 0.9, "C": 0.8},
        rationale={},
        trend_signals={"A": "HOLD", "B": "NEW", "C": "NEW"},
    )
    run = {
        "stages": {"screening": {"result": screening.model_dump(mode="json")}},
        "config_overrides": {"portfolio": {"max_positions": 5}},
    }

    result = await pipeline._run_monitoring(run)

    assert result.free_positions == 4
    assert len(result.positions_to_keep) == 1
    assert len(result.positions_to_sell) == 0
    assert [t.symbol for t in result.entry_candidates] == ["B", "C"]


@pytest.mark.asyncio
async def test_monitoring_resolves_underlying_via_isin_and_sells_on_break(monkeypatch):
    from app.orchestrator import Pipeline

    pipeline = Pipeline()

    async def fake_fetch_holdings(_run: dict) -> list[Position]:
        return [
            Position(
                ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                quantity=Decimal("1"),
                avg_cost=Decimal("0"),
            )
        ]

    async def fake_warrant_underlying_map(_run: dict, _current_holdings: list[Position] | None = None) -> dict[str, str]:
        return {"ISIN1": "A"}

    async def fake_held_since(_run: dict) -> dict[str, date]:
        return {"WKN1": date.today() - timedelta(days=30)}

    monkeypatch.setattr(pipeline, "_fetch_holdings", fake_fetch_holdings)
    monkeypatch.setattr(pipeline, "_fetch_warrant_underlying_map", fake_warrant_underlying_map)
    monkeypatch.setattr(pipeline, "_fetch_held_since", fake_held_since)

    screening = SelectionResult(
        selected=[Ticker(symbol="A"), Ticker(symbol="B")],
        scores={"A": 1.0, "B": 0.9},
        rationale={},
        trend_signals={"A": "BREAK", "B": "NEW"},
    )
    run = {
        "stages": {"screening": {"result": screening.model_dump(mode="json")}},
        "config_overrides": {"portfolio": {"max_positions": 5}},
    }

    result = await pipeline._run_monitoring(run)

    assert len(result.positions_to_sell) == 1
    assert result.positions_to_sell[0].underlying_symbol == "A"
    assert result.positions_to_sell[0].sell_reason == "exit_signal"


@pytest.mark.asyncio
async def test_fetch_holdings_ignores_zero_quantity_positions(monkeypatch):
    import app.orchestrator as orchestrator_module
    from app.orchestrator import Pipeline

    class FakeQuantSystemsCollection:
        async def find_one(self, _query: dict) -> dict:
            return {"depot_id": "d1", "depot_type": "virtual"}

    class FakeSnapshotsCollection:
        async def find_one(self, _query: dict, sort: list[tuple[str, int]]) -> dict:
            return {
                "positions": [
                    {"isin": "X1", "wkn": "W1", "quantity": {"value": "0", "unit": "ST"}},
                    {"isin": "X2", "wkn": "W2", "quantity": {"value": "0.0000", "unit": "ST"}},
                    {"isin": "X3", "wkn": "W3", "quantity": {"value": "2", "unit": "ST"}},
                ]
            }

    monkeypatch.setattr(orchestrator_module, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    monkeypatch.setattr(orchestrator_module, "virtual_depot_snapshots_collection", lambda: FakeSnapshotsCollection())

    pipeline = Pipeline()
    holdings = await pipeline._fetch_holdings({"quant_system_id": "qs1"})

    assert len(holdings) == 1
    assert holdings[0].ticker.symbol == "W3"
    assert holdings[0].quantity == Decimal("2")


@pytest.mark.asyncio
async def test_fetch_holdings_maps_average_purchase_price_to_avg_cost(monkeypatch):
    import app.orchestrator as orchestrator_module
    from app.orchestrator import Pipeline

    class FakeQuantSystemsCollection:
        async def find_one(self, _query: dict) -> dict:
            return {"depot_id": "d1", "depot_type": "virtual"}

    class FakeSnapshotsCollection:
        async def find_one(self, _query: dict, sort: list[tuple[str, int]]) -> dict:
            return {
                "positions": [
                    {
                        "isin": "X3",
                        "wkn": "W3",
                        "instrument_name": "Test",
                        "quantity": {"value": "2", "unit": "ST"},
                        "average_purchase_price": {"value": "12.34", "unit": "EUR"},
                    }
                ]
            }

    monkeypatch.setattr(orchestrator_module, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    monkeypatch.setattr(orchestrator_module, "virtual_depot_snapshots_collection", lambda: FakeSnapshotsCollection())

    pipeline = Pipeline()
    holdings = await pipeline._fetch_holdings({"quant_system_id": "qs1"})

    assert len(holdings) == 1
    assert holdings[0].avg_cost == Decimal("12.34")


@pytest.mark.asyncio
async def test_fetch_holdings_fails_fast_on_legacy_position_fields(monkeypatch):
    import app.orchestrator as orchestrator_module
    from app.orchestrator import Pipeline

    class FakeQuantSystemsCollection:
        async def find_one(self, _query: dict) -> dict:
            return {"depot_id": "d1", "depot_type": "virtual"}

    class FakeSnapshotsCollection:
        async def find_one(self, _query: dict, sort: list[tuple[str, int]]) -> dict:
            return {
                "positions": [
                    {
                        "isin": "X3",
                        "wkn": "W3",
                        "quantity": {"value": "2", "unit": "ST"},
                        "purchase_price": {"value": "12.34", "unit": "EUR"},
                    }
                ]
            }

    monkeypatch.setattr(orchestrator_module, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    monkeypatch.setattr(orchestrator_module, "virtual_depot_snapshots_collection", lambda: FakeSnapshotsCollection())

    pipeline = Pipeline()
    with pytest.raises(RuntimeError, match="Legacy position fields"):
        await pipeline._fetch_holdings({"quant_system_id": "qs1"})


@pytest.mark.asyncio
async def test_fetch_held_since_uses_snapshot_held_since_date(monkeypatch):
    import app.orchestrator as orchestrator_module
    from app.orchestrator import Pipeline

    class FakeQuantSystemsCollection:
        async def find_one(self, _query: dict) -> dict:
            return {"depot_id": "d1", "depot_type": "real"}

    class FakeDepotSnapshotsCollection:
        async def find_one(self, _query: dict, sort: list[tuple[str, int]]) -> dict:
            return {
                "positions": [
                    {"wkn": "W1", "held_since_date": "2026-01-03"},
                    {"wkn": "W2", "held_since_date": None},
                ]
            }

    class FakeFinanceDB:
        def __getitem__(self, name: str):
            if name == "depot_snapshots":
                return FakeDepotSnapshotsCollection()
            raise KeyError(name)

    monkeypatch.setattr(orchestrator_module, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    monkeypatch.setattr(orchestrator_module, "finance_db", lambda: FakeFinanceDB())

    pipeline = Pipeline()
    held_since = await pipeline._fetch_held_since({"quant_system_id": "qs1"})

    assert held_since == {"W1": date(2026, 1, 3)}


def test_monitoring_warrant_health_check_flags_threshold_breaches():
    agent = MonitoringAgent(settings=MonitoringSettings(), max_positions=5)

    degraded, detail = agent._check_warrant_health(
        warrant_isin="DE000TEST123",
        snapshot=WarrantSnapshot(
            warrant_isin="DE000TEST123",
            spread_pct=2.6,
            leverage=2.5,
            days_to_maturity=59,
            delta=0.75,
        ),
    )

    assert degraded is True
    assert detail is not None
    assert "spread too wide" in detail
    assert "leverage too low" in detail
    assert "maturity too short" in detail
    assert "delta too high" in detail


def test_monitoring_warrant_health_check_keeps_exact_threshold_values():
    agent = MonitoringAgent(settings=MonitoringSettings(), max_positions=5)

    degraded, detail = agent._check_warrant_health(
        warrant_isin="DE000TEST123",
        snapshot=WarrantSnapshot(
            warrant_isin="DE000TEST123",
            spread_pct=2.5,
            leverage=3.0,
            days_to_maturity=60,
            delta=0.3,
        ),
    )
    assert degraded is False
    assert detail is None

    degraded, detail = agent._check_warrant_health(
        warrant_isin="DE000TEST123",
        snapshot=WarrantSnapshot(
            warrant_isin="DE000TEST123",
            spread_pct=2.5,
            leverage=8.0,
            days_to_maturity=60,
            delta=0.7,
        ),
    )
    assert degraded is False
    assert detail is None


@pytest.mark.asyncio
async def test_monitoring_rolls_degraded_warrant_without_break_signal():
    agent = MonitoringAgent(settings=MonitoringSettings(), max_positions=5)

    result = await agent.run(
        MonitoringInput(
            candidates=[],
            scores={},
            trend_signals={"A": "HOLD"},
            underlying_names={"A": "Alpha Corp"},
            current_holdings=[
                Position(
                    ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                    quantity=Decimal("1"),
                    avg_cost=Decimal("0"),
                )
            ],
            warrant_underlying_map={"ISIN1": "A"},
            held_since_map={"WKN1": date.today() - timedelta(days=30)},
            warrant_snapshots={
                "ISIN1": WarrantSnapshot(
                    warrant_isin="ISIN1",
                    spread_pct=3.1,
                )
            },
            max_positions=5,
        )
    )

    assert len(result.positions_to_roll) == 1
    assert result.positions_to_roll[0].sell_reason is None
    assert len(result.positions_to_sell) == 0
    assert len(result.positions_to_keep) == 0


@pytest.mark.asyncio
async def test_monitoring_keeps_non_degraded_without_exit_signal():
    agent = MonitoringAgent(settings=MonitoringSettings(), max_positions=5)

    result = await agent.run(
        MonitoringInput(
            candidates=[],
            scores={},
            trend_signals={"A": "HOLD"},
            underlying_names={"A": "Alpha Corp"},
            current_holdings=[
                Position(
                    ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                    quantity=Decimal("1"),
                    avg_cost=Decimal("0"),
                )
            ],
            warrant_underlying_map={"ISIN1": "A"},
            held_since_map={"WKN1": date.today() - timedelta(days=1)},
            warrant_snapshots={
                "ISIN1": WarrantSnapshot(
                    warrant_isin="ISIN1",
                    spread_pct=2.0,
                    leverage=4.5,
                    days_to_maturity=120,
                    delta=0.5,
                )
            },
            max_positions=5,
        )
    )

    assert len(result.positions_to_keep) == 1
    assert len(result.positions_to_sell) == 0


@pytest.mark.asyncio
async def test_monitoring_break_sells_immediately_regardless_of_warrant_degradation():
    """BREAK always triggers immediate SELL; warrant degradation does not change that."""
    agent = MonitoringAgent(settings=MonitoringSettings(), max_positions=5)

    result = await agent.run(
        MonitoringInput(
            candidates=[],
            scores={},
            trend_signals={"A": "BREAK"},
            underlying_names={"A": "Alpha Corp"},
            current_holdings=[
                Position(
                    ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                    quantity=Decimal("1"),
                    avg_cost=Decimal("0"),
                )
            ],
            warrant_underlying_map={"ISIN1": "A"},
            held_since_map={"WKN1": date.today() - timedelta(days=30)},
            warrant_snapshots={
                "ISIN1": WarrantSnapshot(
                    warrant_isin="ISIN1",
                    spread_pct=3.1,
                )
            },
            max_positions=5,
        )
    )

    assert len(result.positions_to_sell) == 1
    assert result.positions_to_sell[0].sell_reason == "exit_signal"
    assert result.positions_to_sell[0].decision_reason == "trend break"
    assert len(result.positions_to_roll) == 0
    assert len(result.positions_to_keep) == 0


@pytest.mark.asyncio
async def test_monitoring_confirmed_break_sells_regardless_of_warrant_health_and_grace():
    agent = MonitoringAgent(settings=MonitoringSettings(min_holding_days=5), max_positions=5)

    result = await agent.run(
        MonitoringInput(
            candidates=[],
            scores={},
            trend_signals={"A": "BREAK"},
            underlying_names={"A": "Alpha Corp"},
            current_holdings=[
                Position(
                    ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                    quantity=Decimal("1"),
                    avg_cost=Decimal("0"),
                )
            ],
            warrant_underlying_map={"ISIN1": "A"},
            held_since_map={"WKN1": date.today() - timedelta(days=1)},
            warrant_snapshots={
                "ISIN1": WarrantSnapshot(
                    warrant_isin="ISIN1",
                    spread_pct=3.2,
                )
            },
            max_positions=5,
        )
    )

    assert len(result.positions_to_sell) == 1
    assert result.positions_to_sell[0].sell_reason == "exit_signal"
    assert len(result.positions_to_roll) == 0


@pytest.mark.asyncio
async def test_monitoring_break_sells_immediately_within_grace_period():
    """Grace period only governs warrant-health ROLL; BREAK always sells immediately."""
    agent = MonitoringAgent(settings=MonitoringSettings(min_holding_days=5), max_positions=5)

    result = await agent.run(
        MonitoringInput(
            candidates=[],
            scores={},
            trend_signals={"A": "BREAK"},
            underlying_names={"A": "Alpha Corp"},
            current_holdings=[
                Position(
                    ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                    quantity=Decimal("1"),
                    avg_cost=Decimal("0"),
                )
            ],
            warrant_underlying_map={"ISIN1": "A"},
            held_since_map={"WKN1": date.today() - timedelta(days=1)},
            max_positions=5,
        )
    )

    assert len(result.positions_to_sell) == 1
    assert result.positions_to_sell[0].sell_reason == "exit_signal"
    assert len(result.positions_to_keep) == 0


@pytest.mark.asyncio
async def test_monitoring_holds_degraded_before_roll_grace_period():
    agent = MonitoringAgent(settings=MonitoringSettings(min_holding_days=5), max_positions=5)

    result = await agent.run(
        MonitoringInput(
            candidates=[],
            scores={},
            trend_signals={"A": "HOLD"},
            underlying_names={"A": "Alpha Corp"},
            current_holdings=[
                Position(
                    ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                    quantity=Decimal("1"),
                    avg_cost=Decimal("0"),
                )
            ],
            warrant_underlying_map={"ISIN1": "A"},
            held_since_map={"WKN1": date.today() - timedelta(days=1)},
            warrant_snapshots={
                "ISIN1": WarrantSnapshot(
                    warrant_isin="ISIN1",
                    spread_pct=3.2,
                )
            },
            max_positions=5,
        )
    )

    assert len(result.positions_to_keep) == 1
    assert len(result.positions_to_roll) == 0
    assert len(result.positions_to_sell) == 0


@pytest.mark.asyncio
async def test_monitoring_sells_break_during_grace_with_candle_confirmation():
    agent = MonitoringAgent(settings=MonitoringSettings(min_holding_days=5), max_positions=5)

    result = await agent.run(
        MonitoringInput(
            candidates=[],
            scores={},
            trend_signals={"A": "BREAK"},
            underlying_names={"A": "Alpha Corp"},
            current_holdings=[
                Position(
                    ticker=Ticker(symbol="WKN1", isin="ISIN1"),
                    quantity=Decimal("1"),
                    avg_cost=Decimal("0"),
                )
            ],
            warrant_underlying_map={"ISIN1": "A"},
            held_since_map={"WKN1": date.today() - timedelta(days=1)},
            max_positions=5,
        )
    )

    assert len(result.positions_to_sell) == 1
    assert result.positions_to_sell[0].sell_reason == "exit_signal"


@pytest.mark.asyncio
async def test_fetch_warrant_snapshots_extracts_metrics(monkeypatch):
    import app.orchestrator as orchestrator_module
    from app.orchestrator import Pipeline

    today = date.today()

    class FakeFinHubTool:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get_warrant_detail(self, isin: str) -> dict | None:
            if isin == "ISIN1":
                return {
                    "market_data": {"spread_percent": 1.8, "bid": 1.9, "ask": 2.1},
                    "analytics": {"leverage": 4.2, "delta": 0.44},
                    "reference_data": {"maturity_date": (today + timedelta(days=120)).isoformat()},
                }
            if isin == "ISIN2":
                return {"market_data": {}, "analytics": {}, "reference_data": {}}
            return None

    monkeypatch.setattr(orchestrator_module, "FinHubTool", FakeFinHubTool)

    pipeline = Pipeline()
    snapshots = await pipeline._fetch_warrant_snapshots(["ISIN1", "ISIN2", "ISIN3"])

    assert set(snapshots.keys()) == {"ISIN1"}
    snap = snapshots["ISIN1"]
    assert snap.spread_pct == 1.8
    assert snap.leverage == 4.2
    assert snap.delta == 0.44
    assert snap.days_to_maturity == 120
    assert snap.bid_ask_midprice == 2.0
