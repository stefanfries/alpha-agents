from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.agents.monitoring import MonitoringAgent, MonitoringInput, WarrantSnapshot
from app.config import MonitoringSettings
from app.models.market import Position, Ticker
from app.models.signals import (
    SelectionResult,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gate", "expected_entries"),
    [
        ({"status": "open"}, 3),
        ({"status": "blocked", "reasons": ["Benchmark SuperTrend bearish"]}, 0),
        (None, 0),
    ],
)
async def test_monitoring_no_holdings_reports_full_free_slots_from_config_override(monkeypatch, gate, expected_entries):
    from app.models.signals import MarketGate, PortfolioAccountSnapshot
    from app.orchestrator import Pipeline

    pipeline = Pipeline()

    async def fake_fetch_holdings(_run: dict) -> list[Position]:
        return []

    async def fake_account_snapshot(*_args, **_kwargs):
        return PortfolioAccountSnapshot(
            source="virtual",
            available_cash_eur=Decimal("99636.35"),
            nav_eur=Decimal("99636.35"),
        )

    monkeypatch.setattr(pipeline, "_fetch_holdings", fake_fetch_holdings)
    monkeypatch.setattr(pipeline, "_fetch_portfolio_account_snapshot", fake_account_snapshot)

    screening = SelectionResult(
        selected=[Ticker(symbol="A"), Ticker(symbol="B"), Ticker(symbol="C")],
        scores={"A": 1.0, "B": 0.9, "C": 0.8},
        rationale={},
        market_gate=MarketGate.model_validate(gate) if gate else None,
    )
    run = {
        "stages": {"screening": {"result": screening.model_dump(mode="json")}},
        "config_overrides": {"portfolio": {"max_positions": 20}},
    }

    result = await pipeline._run_monitoring(run)

    assert result.free_positions == 20
    assert len(result.entry_candidates) == expected_entries
    assert result.positions_to_keep == []
    assert result.positions_to_sell == []
    assert result.nav_eur == Decimal("99636.35")
    assert result.available_cash_eur == Decimal("99636.35")
    assert result.valuation_errors == []


@pytest.mark.asyncio
async def test_market_gate_blocks_entries_but_not_sells_or_rolls():
    from app.models.signals import MarketGate

    agent = MonitoringAgent(settings=MonitoringSettings(), max_positions=5)
    result = await agent.run(MonitoringInput(
        candidates=[Ticker(symbol="NEW")],
        scores={"NEW": 1.0},
        trend_signals={"SELL": "BREAK", "ROLL": "HOLD"},
        current_holdings=[
            Position(ticker=Ticker(symbol="WS", isin="IS"), quantity=Decimal("1"), avg_cost=Decimal("1")),
            Position(ticker=Ticker(symbol="WR", isin="IR"), quantity=Decimal("1"), avg_cost=Decimal("1")),
        ],
        warrant_underlying_map={"IS": "SELL", "IR": "ROLL"},
        held_since_map={"WR": date.today() - timedelta(days=30)},
        warrant_snapshots={"IR": WarrantSnapshot(warrant_isin="IR", spread_pct=3.1)},
        market_gate=MarketGate(status="blocked", reasons=["Benchmark SuperTrend bearish"]),
    ))
    assert result.entry_candidates == []
    assert [p.underlying_symbol for p in result.positions_to_sell] == ["SELL"]
    assert [p.underlying_symbol for p in result.positions_to_roll] == ["ROLL"]
    assert result.market_gate.status == "blocked"


@pytest.mark.asyncio
async def test_monitoring_uses_portfolio_max_positions_override_with_holdings(monkeypatch):
    from app.models.signals import MarketGate
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

    async def fake_warrant_snapshots(_isins: list[str]):
        return {}

    monkeypatch.setattr(pipeline, "_fetch_holdings", fake_fetch_holdings)
    monkeypatch.setattr(pipeline, "_fetch_warrant_underlying_map", fake_warrant_underlying_map)
    monkeypatch.setattr(pipeline, "_fetch_held_since", fake_held_since)
    monkeypatch.setattr(pipeline, "_fetch_warrant_snapshots", fake_warrant_snapshots)

    screening = SelectionResult(
        selected=[Ticker(symbol="A"), Ticker(symbol="B"), Ticker(symbol="C")],
        scores={"A": 1.0, "B": 0.9, "C": 0.8},
        rationale={},
        trend_signals={"A": "HOLD", "B": "NEW", "C": "NEW"},
        market_gate=MarketGate(status="open"),
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
    from app.models.signals import MarketGate
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

    async def fake_warrant_snapshots(_isins: list[str]):
        return {}

    monkeypatch.setattr(pipeline, "_fetch_holdings", fake_fetch_holdings)
    monkeypatch.setattr(pipeline, "_fetch_warrant_underlying_map", fake_warrant_underlying_map)
    monkeypatch.setattr(pipeline, "_fetch_held_since", fake_held_since)
    monkeypatch.setattr(pipeline, "_fetch_warrant_snapshots", fake_warrant_snapshots)

    screening = SelectionResult(
        selected=[Ticker(symbol="A"), Ticker(symbol="B")],
        scores={"A": 1.0, "B": 0.9},
        rationale={},
        trend_signals={"A": "BREAK", "B": "NEW"},
        market_gate=MarketGate(status="open"),
    )
    run = {
        "stages": {"screening": {"result": screening.model_dump(mode="json")}},
        "config_overrides": {"portfolio": {"max_positions": 5}},
    }

    result = await pipeline._run_monitoring(run)

    assert len(result.positions_to_sell) == 1
    assert result.positions_to_sell[0].underlying_symbol == "A"
    assert result.positions_to_sell[0].sell_reason == "exit_signal"
    assert result.free_positions == 5
    assert [t.symbol for t in result.entry_candidates] == ["B"]


@pytest.mark.asyncio
async def test_fetch_holdings_ignores_zero_quantity_positions(monkeypatch):
    import app.depot_data as depot_data_module
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

    monkeypatch.setattr(depot_data_module, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    monkeypatch.setattr(depot_data_module, "virtual_depot_snapshots_collection", lambda: FakeSnapshotsCollection())

    pipeline = Pipeline()
    holdings = await pipeline._fetch_holdings({"quant_system_id": "qs1"})

    assert len(holdings) == 1
    assert holdings[0].ticker.symbol == "W3"
    assert holdings[0].quantity == Decimal("2")


@pytest.mark.asyncio
async def test_fetch_holdings_maps_average_purchase_price_to_avg_cost(monkeypatch):
    import app.depot_data as depot_data_module
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

    monkeypatch.setattr(depot_data_module, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    monkeypatch.setattr(depot_data_module, "virtual_depot_snapshots_collection", lambda: FakeSnapshotsCollection())

    pipeline = Pipeline()
    holdings = await pipeline._fetch_holdings({"quant_system_id": "qs1"})

    assert len(holdings) == 1
    assert holdings[0].avg_cost == Decimal("12.34")


@pytest.mark.asyncio
async def test_fetch_holdings_fails_fast_on_legacy_position_fields(monkeypatch):
    import app.depot_data as depot_data_module
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

    monkeypatch.setattr(depot_data_module, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    monkeypatch.setattr(depot_data_module, "virtual_depot_snapshots_collection", lambda: FakeSnapshotsCollection())

    pipeline = Pipeline()
    with pytest.raises(RuntimeError, match="Legacy position fields"):
        await pipeline._fetch_holdings({"quant_system_id": "qs1"})


@pytest.mark.asyncio
async def test_fetch_held_since_uses_snapshot_held_since_date(monkeypatch):
    import app.depot_data as depot_data_module
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

    monkeypatch.setattr(depot_data_module, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    monkeypatch.setattr(depot_data_module, "finance_db", lambda: FakeFinanceDB())

    pipeline = Pipeline()
    held_since = await pipeline._fetch_held_since({"quant_system_id": "qs1"})

    assert held_since == {"W1": date(2026, 1, 3)}


@pytest.mark.asyncio
async def test_fetch_held_since_uses_latest_buy_for_current_virtual_positions(monkeypatch):
    import app.depot_data as depot_data_module
    from app.orchestrator import Pipeline

    class FakeQuantSystemsCollection:
        async def find_one(self, _query: dict) -> dict:
            return {"depot_id": "d1", "depot_type": "virtual"}

    class FakeSnapshotsCollection:
        async def find_one(self, _query: dict, sort: list[tuple[str, int]]) -> dict:
            return {"positions": [
                {"wkn": "W1", "held_since_date": "2026-01-03"},
                {"wkn": "W2", "held_since_date": None},
            ]}

    class FakeTransactionCursor:
        async def to_list(self):
            return [
                {"wkn": "W1", "booking_date": date(2026, 2, 1)},
                {"wkn": "W2", "booking_date": date(2026, 2, 2)},
                {"wkn": "W2", "booking_date": date(2026, 1, 1)},
                {"wkn": "W3", "booking_date": date(2026, 2, 3)},
            ]

    class FakeTransactionsCollection:
        def find(self, query, sort):
            assert query == {"depot_id": "d1", "transaction_type": "BUY"}
            assert sort == [("booking_date", -1)]
            return FakeTransactionCursor()

    monkeypatch.setattr(depot_data_module, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    monkeypatch.setattr(depot_data_module, "virtual_depot_snapshots_collection", lambda: FakeSnapshotsCollection())
    monkeypatch.setattr(depot_data_module, "virtual_depot_transactions_collection", lambda: FakeTransactionsCollection())

    held_since = await Pipeline()._fetch_held_since({"quant_system_id": "qs1"})

    assert held_since == {"W1": date(2026, 1, 3), "W2": date(2026, 2, 2)}


def test_positions_from_snapshot_warns_when_amounts_are_invalid(caplog):
    from app.depot_positions import positions_from_snapshot

    positions = positions_from_snapshot({"positions": [
        {"isin": "X1", "quantity": {"value": "invalid"}},
        {"isin": "X2", "quantity": {"value": "2"}, "average_purchase_price": {"value": "invalid"}},
    ]})

    assert len(positions) == 1
    assert positions[0].ticker.isin == "X2"
    assert positions[0].avg_cost == Decimal("0")
    assert "Invalid decimal amount in depot position field quantity" in caplog.text
    assert "Invalid decimal amount in depot position field average_purchase_price" in caplog.text


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
    from app.warrant_snapshots import _parse_utc_timestamp

    today = date.today()
    quote_calls = []
    detail_calls = []
    connections = []

    class FakeFinHubTool:
        async def __aenter__(self):
            connections.append(True)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get_warrant_detail(self, isin: str) -> dict | None:
            detail_calls.append(isin)
            if isin == "ISIN1":
                return {
                    "market_data": {
                        "spread_percent": 1.8,
                        "bid": 1.9,
                        "ask": 2.1,
                        "prev_close": 1.8,
                        "timestamp_utc": "2026-10-02T19:59:00Z",
                    },
                    "analytics": {"leverage": 4.2, "delta": 0.44},
                    "reference_data": {
                        "maturity_date": (today + timedelta(days=120)).isoformat(),
                        "currency": "EUR",
                        "issuer_action": True,
                        "issuer_no_fee_action": False,
                    },
                }
            if isin == "ISIN2":
                return {"market_data": {}, "analytics": {}, "reference_data": {}}
            return None

        async def get_quote(self, isin: str) -> dict | None:
            quote_calls.append(isin)
            return {"prev_close": 0.1}

    monkeypatch.setattr(orchestrator_module, "FinHubTool", FakeFinHubTool)

    pipeline = Pipeline()
    assert await pipeline._fetch_warrant_snapshots([]) == {}
    assert connections == []
    snapshots = await pipeline._fetch_warrant_snapshots(["ISIN1", "", "ISIN2", "ISIN1", "ISIN3"])

    assert detail_calls == ["ISIN1", "ISIN2", "ISIN3"]
    assert set(snapshots.keys()) == {"ISIN1"}
    snap = snapshots["ISIN1"]
    assert snap.spread_pct == 1.8
    assert snap.leverage == 4.2
    assert snap.delta == 0.44
    assert snap.days_to_maturity == 120
    assert snap.bid == 1.9
    assert snap.ask == 2.1
    assert snap.currency == "EUR"
    assert snap.timestamp_utc is not None
    assert snap.issuer_action is True
    assert snap.issuer_no_fee_action is False
    assert snap.timestamp_utc.isoformat() == "2026-10-02T19:59:00+00:00"
    assert snap.bid_ask_midprice == 2.0
    assert snap.prev_close == 1.8
    assert quote_calls == []
    assert _parse_utc_timestamp("2026-10-02T19:59:00") is None


@pytest.mark.asyncio
async def test_fetch_warrant_snapshots_continues_after_detail_failure(monkeypatch, caplog):
    import app.orchestrator as orchestrator_module
    import app.warrant_snapshots as snapshot_module
    from app.orchestrator import Pipeline

    class FakeFinHubTool:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get_warrant_detail(self, isin: str) -> dict:
            if isin == "FAILED":
                raise RuntimeError("unavailable")
            return {"market_data": {"spread_percent": 1.5}}

    async def fake_retry_call(fn, *args):
        return await fn(*args)

    monkeypatch.setattr(orchestrator_module, "FinHubTool", FakeFinHubTool)
    monkeypatch.setattr(snapshot_module, "retry_call", fake_retry_call)

    snapshots = await Pipeline()._fetch_warrant_snapshots(["FAILED", "OK"])

    assert set(snapshots) == {"OK"}
    assert snapshots["OK"].spread_pct == 1.5
    assert "Monitoring snapshots: detail fetch failed for FAILED" in caplog.text
