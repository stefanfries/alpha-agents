import logging
from typing import Any

import httpx

from app.config import settings
from app.tools.base import Tool

logger = logging.getLogger(__name__)


class FinHubTool(Tool):
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.finhub.base_url,
            timeout=settings.finhub.timeout_s,
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("FinHubTool not connected — use async with")
        return self._client

    async def ping(self) -> None:
        """Wake up a scale-to-zero container before bulk requests."""
        r = await self._http.get("/health")
        r.raise_for_status()

    async def get_index_constituents(self, index_name: str) -> list[dict[str, Any]]:
        """Returns list of IndexMember dicts (fields: name, isin, link, asset_class, instrument_url)."""
        r = await self._http.get(f"/v1/indices/{index_name}")
        r.raise_for_status()
        return r.json()

    async def get_instrument(self, identifier: str) -> dict[str, Any] | None:
        """Returns Instrument dict or None if not found."""
        r = await self._http.get(f"/v1/instruments/{identifier}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def get_quote(self, identifier: str) -> dict[str, Any] | None:
        """Returns current quote dict or None if not found."""
        r = await self._http.get(f"/v1/quotes/{identifier}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def get_warrants(
        self,
        underlying: str,
        preselection: str = "CALL",
        maturity_from: str | None = None,
        maturity_to: str | None = None,
        strike_min: float | None = None,
        strike_max: float | None = None,
        delta_min: float | None = None,
        delta_max: float | None = None,
        spread_ask_pct_max: float | None = None,
    ) -> list[dict[str, Any]]:
        """Returns list of Warrant dicts from WarrantFinderResponse.results."""
        params: dict[str, Any] = {"underlying": underlying, "preselection": preselection}
        if maturity_from is not None:
            params["maturity_from"] = maturity_from
        if maturity_to is not None:
            params["maturity_to"] = maturity_to
        if strike_min is not None:
            params["strike_min"] = strike_min
        if strike_max is not None:
            params["strike_max"] = strike_max
        if delta_min is not None:
            params["delta_min"] = delta_min
        if delta_max is not None:
            params["delta_max"] = delta_max
        if spread_ask_pct_max is not None:
            params["spread_ask_pct_max"] = spread_ask_pct_max
        r = await self._http.get("/v1/warrants", params=params)
        r.raise_for_status()
        return r.json().get("results", [])

    async def get_warrant_detail(self, identifier: str) -> dict[str, Any] | None:
        """Returns WarrantDetailResponse dict or None if not found."""
        r = await self._http.get(f"/v1/warrants/{identifier}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def get_history(self, identifier: str, id_notation: str) -> list[dict[str, Any]]:
        """Returns list of HistoryRecord dicts (fields: datetime, open, high, low, close, volume)."""
        r = await self._http.get(
            f"/v1/history/{identifier}",
            params={"id_notation": id_notation},
        )
        r.raise_for_status()
        return r.json().get("data", [])
