from decimal import Decimal

from app.agents.monitoring import MonitoringInput, WarrantSnapshot
from app.config import MonitoringSettings
from app.models.market import Order, Ticker
from app.models.quant_system import VirtualDepotPosition
from app.models.signals import (
    ExecutionPlan,
    MonitoringResult,
    PositionReview,
    RollReplacement,
    SelectionResult,
)


def test_ticker_uppercases_symbol():
    t = Ticker(symbol="aapl")
    assert t.symbol == "AAPL"


def test_ticker_with_exchange():
    t = Ticker(symbol="SAP", exchange="XETRA")
    assert t.exchange == "XETRA"


def test_order_requires_limit_price_when_limit_type():
    order = Order(
        ticker=Ticker(symbol="AAPL"),
        side="buy",
        quantity=Decimal("10"),
        order_type="limit",
        limit_price=Decimal("150.00"),
    )
    assert order.limit_price == Decimal("150.00")


def test_selection_result_roundtrip():
    result = SelectionResult(
        selected=[Ticker(symbol="AAPL")],
        scores={"AAPL": 1.5},
        rationale={"AAPL": "Score: 1.50"},
    )
    assert result.selected[0].symbol == "AAPL"


def test_execution_plan_empty():
    plan = ExecutionPlan(orders=[], skipped=[])
    assert plan.orders == []
    assert plan.skipped == []


def test_monitoring_settings_has_warrant_health_defaults():
    settings = MonitoringSettings()

    assert settings.warrant_health.enabled is True
    assert settings.warrant_health.spread_max_pct == 2.5
    assert settings.warrant_health.leverage_min == 3.0
    assert settings.warrant_health.leverage_max == 8.0
    assert settings.warrant_health.min_days_to_maturity == 60
    assert settings.warrant_health.delta_min == 0.3
    assert settings.warrant_health.delta_max == 0.7
    assert settings.warrant_health.min_warrant_score is None


def test_position_review_accepts_warrant_degraded_sell_reason():
    review = PositionReview(
        underlying_symbol="AAPL",
        warrant_isin="DE000TEST123",
        warrant_wkn="TEST12",
        sell_reason="warrant_degraded",
    )

    assert review.sell_reason == "warrant_degraded"


def test_position_review_accepts_roll_replacement():
    review = PositionReview(
        underlying_symbol="AAPL",
        warrant_isin="DE000TEST123",
        warrant_wkn="TEST12",
        roll_replacement=RollReplacement(
            warrant_isin="DE000TEST456",
            warrant_wkn="TEST34",
            strike=200.0,
            maturity_date=None,
        ),
    )

    assert review.roll_replacement is not None
    assert review.roll_replacement.warrant_isin == "DE000TEST456"


def test_monitoring_result_defaults_positions_to_roll():
    result = MonitoringResult(
        positions_to_sell=[],
        positions_to_keep=[],
        entry_candidates=[],
        free_positions=0,
        excluded_symbols=[],
    )

    assert result.positions_to_roll == []


def test_monitoring_input_accepts_warrant_snapshots():
    inp = MonitoringInput(
        candidates=[Ticker(symbol="AAPL")],
        scores={"AAPL": 1.0},
        trend_signals={"AAPL": "HOLD"},
        current_holdings=[],
        warrant_underlying_map={},
        held_since_map={},
        warrant_snapshots={
            "DE000TEST123": WarrantSnapshot(
                warrant_isin="DE000TEST123",
                spread_pct=1.2,
                leverage=4.5,
                days_to_maturity=120,
                delta=0.55,
                bid_ask_midprice=2.15,
            )
        },
    )

    assert "DE000TEST123" in inp.warrant_snapshots
    assert inp.warrant_snapshots["DE000TEST123"].spread_pct == 1.2


def test_virtual_depot_position_uses_canonical_snapshot_fields():
    fields = VirtualDepotPosition.model_fields

    assert "average_purchase_price" in fields
    assert "purchase_price_at_entry" in fields
    assert "held_since_date" in fields

    assert "purchase_price" not in fields
    assert "buy_price_at_entry" not in fields
