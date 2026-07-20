import pytest


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

    class FakeAccountBalances:
        async def find_one(self, _query: dict, _projection: dict, sort: list[tuple[str, int]]) -> dict:
            return {"balance": {"value": "10.75", "unit": "EUR"}}

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
    assert b"111.0" in response.body


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
