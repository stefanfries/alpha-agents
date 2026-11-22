from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import app.depot_data as depot_data
from app.orchestrator import Pipeline


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run", "prevention_days"),
    [
        ({}, 10),
        ({"quant_system_id": "qs1"}, 0),
        ({"quant_system_id": "qs1", "depot_type": "real"}, 10),
    ],
)
async def test_reentry_without_virtual_history_skips_database(monkeypatch, run, prevention_days):
    def unexpected_collection():
        raise AssertionError("Database must not be accessed")

    monkeypatch.setattr(depot_data, "quant_systems_collection", unexpected_collection)

    result = await Pipeline()._fetch_reentry_blocked_symbols(
        run, screening_symbols={"AAPL"}, prevention_days=prevention_days
    )

    assert result == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_depot", [None, {}, {"depot_type": "virtual"}, {"depot_id": "d1", "depot_type": "real"}])
async def test_reentry_without_linked_virtual_depot_skips_transactions(monkeypatch, linked_depot):
    class FakeQuantSystemsCollection:
        async def find_one(self, query):
            return linked_depot

    def unexpected_collection():
        raise AssertionError("Transactions must not be accessed")

    monkeypatch.setattr(depot_data, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    monkeypatch.setattr(depot_data, "virtual_depot_transactions_collection", unexpected_collection)

    result = await Pipeline()._fetch_reentry_blocked_symbols(
        {"quant_system_id": "qs1"}, screening_symbols=set(), prevention_days=10
    )

    assert result == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("screening_symbols", "expected"),
    [({"AAPL"}, {"AAPL"}), (set(), {"AAPL", "MSFT"})],
)
async def test_reentry_filters_recent_sells_by_screening_symbols(monkeypatch, screening_symbols, expected):
    before = datetime.now(timezone.utc)
    queries = []

    class FakeQuantSystemsCollection:
        async def find_one(self, query):
            assert query == {"quant_system_id": "qs1"}
            return {"depot_id": "d1", "depot_type": "virtual"}

    class FakeCursor:
        def __init__(self, rows):
            self.rows = rows

        async def to_list(self):
            return self.rows

    class FakeTransactionsCollection:
        def find(self, query, sort):
            assert query["depot_id"] == "d1"
            assert query["transaction_type"] == "SELL"
            queries.append(query)
            assert sort == [("booking_date", -1)]
            return FakeCursor([{"wkn": "W2"}, {"wkn": "W1"}, {"wkn": "W1"}, {"wkn": None}])

    class FakeMappingsCollection:
        def find(self, query, projection):
            assert query == {"warrant_wkn": {"$in": ["W1", "W2"]}}
            assert projection == {"warrant_wkn": 1, "underlying_symbol": 1}
            return FakeCursor([
                {"warrant_wkn": "W1", "underlying_symbol": "AAPL"},
                {"warrant_wkn": "W2", "underlying_symbol": "MSFT"},
                {"warrant_wkn": "W2", "underlying_symbol": None},
            ])

    monkeypatch.setattr(depot_data, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    monkeypatch.setattr(depot_data, "virtual_depot_transactions_collection", lambda: FakeTransactionsCollection())
    monkeypatch.setattr(depot_data, "warrant_underlying_map_collection", lambda: FakeMappingsCollection())

    result = await Pipeline()._fetch_reentry_blocked_symbols(
        {"quant_system_id": "qs1"},
        screening_symbols=screening_symbols,
        prevention_days=10,
    )

    assert result == expected
    cutoff = queries[0]["booking_date"]["$gte"]
    assert before - timedelta(days=10) <= cutoff <= datetime.now(timezone.utc) - timedelta(days=10)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["holdings", "held_since", "reentry"])
async def test_depot_database_errors_propagate(monkeypatch, method):
    class FakeQuantSystemsCollection:
        async def find_one(self, query):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(depot_data, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    pipeline = Pipeline()
    run = {"quant_system_id": "qs1"}

    with pytest.raises(RuntimeError, match="database unavailable"):
        if method == "holdings":
            await pipeline._fetch_holdings(run)
        elif method == "held_since":
            await pipeline._fetch_held_since(run)
        else:
            await pipeline._fetch_reentry_blocked_symbols(run, screening_symbols=set(), prevention_days=10)


@pytest.mark.asyncio
async def test_fetch_holdings_reads_latest_real_depot_snapshot(monkeypatch):
    class FakeQuantSystemsCollection:
        async def find_one(self, query):
            assert query == {"quant_system_id": "qs1"}
            return {"depot_id": "d1", "depot_type": "real"}

    class FakeSnapshotsCollection:
        async def find_one(self, query, sort):
            assert query == {"depot_id": "d1"}
            assert sort == [("recorded_at", -1)]
            return {"positions": [{
                "isin": "ISIN1",
                "wkn": "WKN1",
                "quantity": {"value": "3"},
                "average_purchase_price": {"value": "2.5"},
            }]}

    monkeypatch.setattr(depot_data, "quant_systems_collection", lambda: FakeQuantSystemsCollection())
    monkeypatch.setattr(depot_data, "finance_db", lambda: {"depot_snapshots": FakeSnapshotsCollection()})

    holdings = await Pipeline()._fetch_holdings({"quant_system_id": "qs1"})

    assert len(holdings) == 1
    assert holdings[0].ticker.isin == "ISIN1"
    assert holdings[0].quantity == Decimal("3")
    assert holdings[0].avg_cost == Decimal("2.5")
