from datetime import datetime

import httpx
import pytest

from app.tools.finhub import FinHubTool


@pytest.mark.asyncio
async def test_history_data_preserves_metadata_and_sends_range():
    def respond(request):
        assert request.url.path == "/v1/history/DE000TEST123"
        assert dict(request.url.params) == {
            "id_notation": "preferred_id_notation_life_trading",
            "interval": "hour",
            "start": "2026-10-11T00:00:00",
            "end": "2026-10-25T00:00:00",
        }
        return httpx.Response(200, json={"currency": "EUR", "trading_venue": "Issuer", "data": []})

    tool = FinHubTool()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="https://test") as client:
        tool._client = client
        result = await tool.get_history_data(
            "DE000TEST123", "preferred_id_notation_life_trading",
            start=datetime(2026, 10, 11), end=datetime(2026, 10, 25), interval="hour",
        )
    assert result["currency"] == "EUR"
    assert result["trading_venue"] == "Issuer"


@pytest.mark.asyncio
async def test_history_keeps_list_contract():
    records = [{"datetime": "2026-10-23T00:00:00", "close": 1.5}]
    tool = FinHubTool()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": records})),
        base_url="https://test",
    ) as client:
        tool._client = client
        assert await tool.get_history("TEST", "123") == records