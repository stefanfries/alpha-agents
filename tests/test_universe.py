import pytest

from app.agents.universe import UniverseAgent, UniverseInput


class FakeFinHub:
    async def ping(self) -> None:
        return None

    async def get_index_constituents(self, _index_name: str):
        return [
            {"isin": "CH1300646267", "name": "Bunge Global S.A."},
            {"isin": "US74743L1008", "name": "Bunge Global S.A."},
        ]

    async def get_instrument(self, isin: str):
        return {
            "details": {"security_type": "Equity"},
            "global_identifiers": {"symbol_yfinance": "BG"},
            "isin": isin,
        }


class FakeWikipedia:
    async def get_index_constituents(self, _index_name: str):
        return []


@pytest.mark.asyncio
async def test_universe_deduplicates_same_symbol_with_different_isins():
    agent = UniverseAgent(finhub=FakeFinHub(), wikipedia=FakeWikipedia())

    result = await agent.run(UniverseInput(indices=["SP500"]))

    assert len(result.tickers) == 1
    assert result.tickers[0].symbol == "BG"
