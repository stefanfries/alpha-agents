from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from app.models.market import Position, Ticker
from app.orchestrator import Pipeline


def _position(isin: str | None, wkn: str) -> Position:
    return Position(
        ticker=Ticker(symbol=wkn, isin=isin),
        quantity=Decimal("1"),
        avg_cost=Decimal("1"),
    )


@pytest.fixture
def fake_finhub(monkeypatch):
    import app.orchestrator as orchestrator_module

    responses = {}
    calls = []

    class FakeFinHub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get_instrument(self, identifier):
            calls.append(identifier)
            response = responses.get(identifier)
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(orchestrator_module, "FinHubTool", FakeFinHub)
    return responses, calls


@pytest.mark.asyncio
async def test_resolves_and_persists_warrant_underlying(fake_finhub, monkeypatch, caplog):
    import app.warrant_underlyings as underlying_module

    responses, calls = fake_finhub
    responses.update({
        "FAILED": httpx.ConnectError("unavailable"),
        "WARRANT": {
            "isin": "WARRANT",
            "details": {
                "underlying_link": "/inf/aktien/US0378331005",
                "underlying_name": "Apple",
            },
        },
        "US0378331005": {"global_identifiers": {"symbol_yfinance": "AAPL"}},
        "MISSING": None,
    })
    writes = []

    class FakeCollection:
        async def update_one(self, query, update, upsert):
            writes.append((query, update, upsert))

    monkeypatch.setattr(underlying_module, "warrant_underlying_map_collection", lambda: FakeCollection())

    result = await Pipeline()._fetch_warrant_underlying_map(
        {},
        [
            _position("FAILED", "FAIL"),
            _position("WARRANT", "WKN1"),
            _position("MISSING", "MISS"),
            _position(None, "NOISIN"),
        ],
    )

    assert result == {"WARRANT": "AAPL"}
    assert calls == ["FAILED", "WARRANT", "US0378331005", "MISSING"]
    assert len(writes) == 1
    query, update, upsert = writes[0]
    assert query == {"_id": "WARRANT"}
    assert upsert is True
    payload = update["$set"]
    assert {key: value for key, value in payload.items() if key != "checked_at"} == {
        "warrant_isin": "WARRANT",
        "warrant_wkn": "WKN1",
        "underlying_symbol": "AAPL",
        "underlying_isin": "US0378331005",
        "underlying_name": "Apple",
        "source": "finhub_instruments",
        "resolved_from": "isin",
    }
    assert payload["checked_at"].utcoffset() == timedelta(0)
    assert "Failed to fetch instrument FAILED: unavailable" in caplog.text


@pytest.mark.asyncio
async def test_universe_name_resolution_retries_same_symbol_until_name_is_found(fake_finhub):
    responses, calls = fake_finhub
    responses.update({
        "FIRST": None,
        "SECOND": {"details": {"underlying_link": "/inf/aktien/US0378331005"}},
        "DUPLICATE": {"details": {"underlying_link": "/inf/aktien/US0378331005"}},
    })
    holdings = [
        _position("FIRST", "W1"),
        _position("SECOND", "W2"),
        _position("DUPLICATE", "W3"),
    ]

    result = await Pipeline()._resolve_underlying_names_from_universe(
        holdings,
        {"FIRST": "AAPL", "SECOND": "AAPL", "DUPLICATE": "AAPL"},
        {"US0378331005": "Apple Inc."},
    )

    assert result == {"AAPL": "Apple Inc."}
    assert calls == ["FIRST", "SECOND"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("warrant_detail", "underlying_detail", "expected_calls"),
    [
        ({"details": {"underlying_link": "/not-an-isin"}}, None, ["WARRANT"]),
        (
            {"details": {"underlying_link": "/inf/aktien/US0378331005"}},
            {"global_identifiers": {}},
            ["WARRANT", "US0378331005"],
        ),
    ],
)
async def test_unresolved_warrants_are_not_persisted(
    fake_finhub, monkeypatch, warrant_detail, underlying_detail, expected_calls,
):
    import app.warrant_underlyings as underlying_module

    responses, calls = fake_finhub
    responses.update({"WARRANT": warrant_detail, "US0378331005": underlying_detail})

    def unexpected_collection():
        raise AssertionError("Unresolved warrants must not be persisted")

    monkeypatch.setattr(underlying_module, "warrant_underlying_map_collection", unexpected_collection)

    result = await Pipeline()._fetch_warrant_underlying_map({}, [_position("WARRANT", "WKN1")])

    assert result == {}
    assert calls == expected_calls


@pytest.mark.asyncio
async def test_mapping_persistence_errors_propagate(fake_finhub, monkeypatch):
    import app.warrant_underlyings as underlying_module

    responses, _ = fake_finhub
    responses.update({
        "WARRANT": {"details": {"underlying_link": "/inf/aktien/US0378331005"}},
        "US0378331005": {"global_identifiers": {"symbol_yfinance": "AAPL"}},
    })

    class FakeCollection:
        async def update_one(self, query, update, upsert):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(underlying_module, "warrant_underlying_map_collection", lambda: FakeCollection())

    with pytest.raises(RuntimeError, match="database unavailable"):
        await Pipeline()._fetch_warrant_underlying_map({}, [_position("WARRANT", "WKN1")])


@pytest.mark.asyncio
async def test_empty_inputs_do_not_connect_to_finhub(monkeypatch):
    import app.orchestrator as orchestrator_module

    class UnexpectedFinHub:
        def __init__(self):
            raise AssertionError("FinHub should not be constructed")

    monkeypatch.setattr(orchestrator_module, "FinHubTool", UnexpectedFinHub)
    pipeline = Pipeline()

    assert await pipeline._fetch_warrant_underlying_map({}, []) == {}
    assert await pipeline._resolve_underlying_names_from_universe([_position("WARRANT", "W1")], {}, {}) == {}
