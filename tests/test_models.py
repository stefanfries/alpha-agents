from decimal import Decimal


from models.market import Order, Ticker
from models.signals import (
    ExecutionPlan,
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
