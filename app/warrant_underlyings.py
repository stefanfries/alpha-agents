import logging
import re
from datetime import datetime, timezone
from typing import Any, TypedDict

from app.db import warrant_underlying_map_collection
from app.models.market import Position
from app.tools.finhub import FinHubTool

logger = logging.getLogger(__name__)


class _ResolvedUnderlying(TypedDict):
    warrant_isin: str
    underlying_symbol: str
    underlying_isin: str
    underlying_name: str | None
    resolved_from: str


async def _get_instrument(finhub: FinHubTool, identifier: str) -> dict[str, Any] | None:
    try:
        return await finhub.get_instrument(identifier)
    except Exception as exc:
        logger.warning("Failed to fetch instrument %s: %s", identifier, exc)
        return None


def _underlying_isin(instrument: dict[str, Any]) -> str | None:
    details = instrument.get("details") or {}
    underlying_link = details.get("underlying_link") or ""
    match = re.search(r"([A-Z]{2}[A-Z0-9]{10})$", underlying_link)
    return match.group(1) if match else None


async def _resolve_warrant_underlying(
    finhub: FinHubTool,
    pos: Position,
) -> _ResolvedUnderlying | None:
    if not pos.ticker.isin:
        return None

    inst = await _get_instrument(finhub, pos.ticker.isin)
    if not inst:
        return None

    underlying_isin = _underlying_isin(inst)
    if not underlying_isin:
        return None

    underlying_inst = await _get_instrument(finhub, underlying_isin)
    if not underlying_inst:
        return None

    gids = underlying_inst.get("global_identifiers") or {}
    underlying_symbol = gids.get("symbol_yfinance")
    if not underlying_symbol:
        return None

    details = inst.get("details") or {}
    underlying_name = (
        details.get("underlying_name")
        or underlying_inst.get("name")
        or (underlying_inst.get("global_identifiers") or {}).get("name_openfigi")
    )
    warrant_gids = inst.get("global_identifiers") or {}
    return {
        "warrant_isin": inst.get("isin") or warrant_gids.get("isin") or pos.ticker.isin or "",
        "underlying_symbol": underlying_symbol,
        "underlying_isin": underlying_isin,
        "underlying_name": underlying_name,
        "resolved_from": "isin",
    }


async def _persist_mapping(resolved: _ResolvedUnderlying, warrant_wkn: str) -> None:
    coll = warrant_underlying_map_collection()
    warrant_isin = resolved["warrant_isin"]
    payload = {
        "warrant_isin": warrant_isin,
        "warrant_wkn": warrant_wkn,
        "underlying_symbol": resolved["underlying_symbol"],
        "underlying_isin": resolved["underlying_isin"],
        "underlying_name": resolved["underlying_name"],
        "source": "finhub_instruments",
        "resolved_from": "isin",
        "checked_at": datetime.now(timezone.utc),
    }
    if warrant_isin:
        await coll.update_one({"_id": warrant_isin}, {"$set": payload}, upsert=True)


async def fetch_warrant_underlying_map(
    finhub: FinHubTool,
    holdings: list[Position],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for pos in holdings:
        resolved = await _resolve_warrant_underlying(finhub, pos)
        if not resolved:
            continue
        underlying_symbol = resolved["underlying_symbol"]
        warrant_isin = resolved["warrant_isin"]
        if warrant_isin:
            result[warrant_isin] = underlying_symbol
        await _persist_mapping(resolved, pos.ticker.symbol)
    return result


async def resolve_underlying_names_from_universe(
    finhub: FinHubTool,
    holdings: list[Position],
    warrant_underlying_map: dict[str, str],
    universe_names_by_isin: dict[str, str],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for pos in holdings:
        underlying_symbol = warrant_underlying_map.get(pos.ticker.isin) if pos.ticker.isin else None
        if not underlying_symbol or underlying_symbol in result:
            continue

        inst = await _get_instrument(finhub, pos.ticker.isin) if pos.ticker.isin else None
        if not inst:
            continue

        underlying_isin = _underlying_isin(inst)
        if not underlying_isin:
            continue

        universe_name = universe_names_by_isin.get(underlying_isin)
        if universe_name:
            result[underlying_symbol] = universe_name
    return result
