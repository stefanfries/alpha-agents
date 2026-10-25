import pytest
from starlette.requests import Request


@pytest.mark.asyncio
@pytest.mark.parametrize("period,interval,days", [("3M", "day", 92), ("4W", "day", 28), ("2W", "hour", 14)])
async def test_warrant_price_chart_ranges(monkeypatch, period, interval, days):
    import json
    from datetime import datetime
    from html.parser import HTMLParser

    from app.routes import pipeline

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 25, 12, tzinfo=tz)

    class FakeFinHub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get_history_data(self, identifier, notation, **kwargs):
            assert identifier == "TEST123"
            assert notation == "preferred_id_notation_life_trading"
            assert kwargs["interval"] == interval
            assert (kwargs["end"] - kwargs["start"]).days == days
            assert kwargs["end"].tzinfo is None
            return {
                "wkn": "TEST123", "currency": "EUR", "trading_venue": "Issuer <venue>",
                "data": [
                    {"datetime": "2026-10-23T11:00:00", "open": 1, "high": 2, "low": 1, "close": 1.5},
                    {"datetime": "2026-10-23T10:00:00", "open": 1, "high": 2, "low": 1, "close": 1.2},
                    {"datetime": "2026-10-23T11:00:00", "open": 1, "high": 2, "low": 1, "close": 1.6},
                ],
            }

    monkeypatch.setattr(pipeline, "datetime", FixedDatetime)
    monkeypatch.setattr(pipeline, "FinHubTool", FakeFinHub)
    response = await pipeline.chart_warrant_price("qs", "execution", "TEST123", period)
    text = response.body.decode()
    assert "Issuer &lt;venue&gt;" in text
    assert "EUR" in text

    class ChartParser(HTMLParser):
        chart_data = None

        def handle_starttag(self, tag, attrs):
            for key, value in attrs:
                if key == "data-chart":
                    self.chart_data = json.loads(value)

    parser = ChartParser()
    parser.feed(text)
    chart = parser.chart_data
    assert chart["fit_content"] is True
    assert chart["ohlcv"][-1]["close"] == 1.6
    if interval == "hour":
        assert len(chart["ohlcv"]) == 2
        assert chart["ohlcv"][1]["time"] - chart["ohlcv"][0]["time"] == 3600
        assert chart["ohlcv"][0]["time"] == 1792742400
        assert chart["intraday"] is True
    else:
        assert chart["ohlcv"][0]["time"] == "2026-10-23"
        assert len(chart["ohlcv"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_warrant_price_chart_empty_and_error_keep_controls(monkeypatch, failed):
    from app.routes import pipeline

    class FakeFinHub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get_history_data(self, *_args, **_kwargs):
            if failed:
                raise RuntimeError("private API details")
            return {"data": []}

    async def no_retry(function, *args, **kwargs):
        return await function(*args, **kwargs)

    monkeypatch.setattr(pipeline, "FinHubTool", FakeFinHub)
    monkeypatch.setattr(pipeline, "retry_call", no_retry)
    response = await pipeline.chart_warrant_price("qs", "execution", "TEST123", "2W")
    text = response.body.decode()
    assert 'data-warrant-range="2W"' in text
    assert "data-chart=" not in text
    assert "private API details" not in text
    assert ("unavailable" if failed else "No traded-price history") in text


@pytest.mark.asyncio
async def test_warrant_price_chart_rejects_unknown_range():
    import httpx
    from fastapi import FastAPI

    from app.routes.pipeline import router

    application = FastAPI()
    application.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.get("/quant-systems/qs/executions/execution/charts/warrant_price/TEST?range=1Y")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_depot_capital_converts_string_amount_values(monkeypatch):
    from app.routes import quant_systems as quant_systems_module

    class FakeDepotSnapshots:
        async def find_one(self, _query: dict, _projection: dict, sort: list[tuple[str, int]]) -> dict:
            return {
                "account_name": "acc-1",
                "positions": [
                    {"current_value": {"value": "100.25", "unit": "EUR"}},
                    {"current_value": {"value": "bad-number", "unit": "EUR"}},
                ],
            }

    class FakeBalanceCursor:
        def sort(self, *_args):
            return self

        async def to_list(self, length=None):
            return [
                {"account_type": "Verrechnungskonto", "balance": {"value": "101865.98", "unit": "EUR"}},
                {"account_type": "Tagesgeld PLUS-Konto", "balance": {"value": "0.03", "unit": "EUR"}},
                {"account_type": "Girokonto", "balance": {"value": "106.03", "unit": "EUR"}},
            ]

    class FakeAccountBalances:
        def find(self, _query: dict, _projection: dict):
            return FakeBalanceCursor()

    class FakeFinanceDB:
        def __getitem__(self, name: str):
            if name == "depot_snapshots":
                return FakeDepotSnapshots()
            if name == "account_balances":
                return FakeAccountBalances()
            raise KeyError(name)

    monkeypatch.setattr(quant_systems_module, "finance_db", lambda: FakeFinanceDB())

    response = await quant_systems_module.depot_capital("d1")

    assert response.status_code == 200
    assert response.body
    assert b"102072.29" in response.body


@pytest.mark.asyncio
async def test_depot_capital_fails_fast_on_legacy_position_fields(monkeypatch):
    from app.routes import quant_systems as quant_systems_module

    class FakeDepotSnapshots:
        async def find_one(self, _query: dict, _projection: dict, sort: list[tuple[str, int]]) -> dict:
            return {
                "account_name": "acc-1",
                "positions": [
                    {
                        "current_value": {"value": "1.00", "unit": "EUR"},
                        "purchase_price": {"value": "1.00", "unit": "EUR"},
                    }
                ],
            }

    class FakeAccountBalances:
        async def find_one(self, _query: dict, _projection: dict, sort: list[tuple[str, int]]) -> dict:
            return {"balance": {"value": "0", "unit": "EUR"}}

    class FakeFinanceDB:
        def __getitem__(self, name: str):
            if name == "depot_snapshots":
                return FakeDepotSnapshots()
            if name == "account_balances":
                return FakeAccountBalances()
            raise KeyError(name)

    monkeypatch.setattr(quant_systems_module, "finance_db", lambda: FakeFinanceDB())

    with pytest.raises(RuntimeError, match="Legacy position fields"):
        await quant_systems_module.depot_capital("d1")


@pytest.mark.asyncio
async def test_new_quant_system_form_includes_dow_jones(monkeypatch):
    from app.routes import quant_systems as quant_systems_module

    class FakeVirtualDepotsCursor:
        def sort(self, *_args, **_kwargs):
            return self

        async def to_list(self):
            return []

    class FakeVirtualDepotsCollection:
        def find(self, *_args, **_kwargs):
            return FakeVirtualDepotsCursor()

    async def fake_real_depots() -> list[dict]:
        return []

    monkeypatch.setattr(quant_systems_module, "_real_depots", fake_real_depots)
    monkeypatch.setattr(
        quant_systems_module,
        "virtual_depots_collection",
        lambda: FakeVirtualDepotsCollection(),
    )

    request = Request({"type": "http", "method": "GET", "path": "/quant-systems/new", "headers": []})
    response = await quant_systems_module.new_quant_system(request)

    assert "indices" in response.context
    assert "Dow Jones" in response.context["indices"]


@pytest.mark.asyncio
async def test_edit_quant_system_form_includes_dow_jones(monkeypatch):
    from app.routes import quant_systems as quant_systems_module

    class FakeVirtualDepotsCursor:
        def sort(self, *_args, **_kwargs):
            return self

        async def to_list(self):
            return []

    class FakeVirtualDepotsCollection:
        def find(self, *_args, **_kwargs):
            return FakeVirtualDepotsCursor()

    class FakeQuantSystemsCollection:
        async def find_one(self, *_args, **_kwargs):
            return {
                "quant_system_id": "qs1",
                "name": "Test QS",
                "depot_id": "d1",
                "depot_type": "virtual",
                "indices": ["DAX"],
                "capital_eur": 10_000.0,
                "status": "draft",
                "config_overrides": {},
            }

    async def fake_real_depots() -> list[dict]:
        return []

    monkeypatch.setattr(quant_systems_module, "_real_depots", fake_real_depots)
    monkeypatch.setattr(
        quant_systems_module,
        "virtual_depots_collection",
        lambda: FakeVirtualDepotsCollection(),
    )
    monkeypatch.setattr(
        quant_systems_module,
        "quant_systems_collection",
        lambda: FakeQuantSystemsCollection(),
    )

    request = Request({"type": "http", "method": "GET", "path": "/quant-systems/qs1/edit", "headers": []})
    response = await quant_systems_module.edit_quant_system(request, "qs1")

    assert "indices" in response.context
    assert "Dow Jones" in response.context["indices"]
    assert response.context["default_slippage_bps"] == 25.0


@pytest.mark.asyncio
async def test_save_quant_system_persists_slippage_override(monkeypatch):
    from app.routes import quant_systems as quant_systems_module

    saved: dict = {}

    class FakeQuantSystemsCollection:
        async def update_one(self, query: dict, update: dict) -> None:
            saved["query"] = query
            saved["update"] = update

    monkeypatch.setattr(
        quant_systems_module,
        "quant_systems_collection",
        lambda: FakeQuantSystemsCollection(),
    )

    response = await quant_systems_module.save_quant_system(
        qs_id="qs1",
        name="Test QS",
        depot_id="d1",
        depot_type="virtual",
        indices=["DAX"],
        capital_eur=100_000,
        max_positions=15,
        slippage_bps="40",
        status="active",
    )

    assert response.status_code == 303
    assert saved["update"]["$set"]["config_overrides.portfolio.slippage_bps"] == 40.0


@pytest.mark.asyncio
async def test_save_quant_system_blank_slippage_clears_system_override(monkeypatch):
    from app.routes import quant_systems as quant_systems_module

    saved: dict = {}

    class FakeQuantSystemsCollection:
        async def update_one(self, _query: dict, update: dict) -> None:
            saved["update"] = update

    monkeypatch.setattr(
        quant_systems_module,
        "quant_systems_collection",
        lambda: FakeQuantSystemsCollection(),
    )

    await quant_systems_module.save_quant_system(
        qs_id="qs1",
        name="Test QS",
        depot_id="d1",
        depot_type="virtual",
        indices=["DAX"],
        capital_eur=100_000,
        slippage_bps="",
    )

    assert saved["update"]["$unset"] == {"config_overrides.portfolio.slippage_bps": ""}
