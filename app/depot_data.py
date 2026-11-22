from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.db import (
    finance_db,
    quant_systems_collection,
    virtual_depot_snapshots_collection,
    virtual_depot_transactions_collection,
    warrant_underlying_map_collection,
)
from app.depot_positions import (
    add_held_since_from_transactions,
    held_since_from_snapshot,
    positions_from_snapshot,
)
from app.models.market import Position


async def _linked_depot(run: dict) -> dict[str, Any] | None:
    qs_id = run.get("quant_system_id")
    if not qs_id:
        return None
    qs = await quant_systems_collection().find_one({"quant_system_id": qs_id})
    return qs if qs and qs.get("depot_id") else None


async def _latest_snapshot(qs: dict[str, Any]) -> dict[str, Any] | None:
    depot_id = qs["depot_id"]
    if qs.get("depot_type", "virtual") == "real":
        return await finance_db()["depot_snapshots"].find_one(
            {"depot_id": depot_id}, sort=[("recorded_at", -1)]
        )
    return await virtual_depot_snapshots_collection().find_one(
        {"depot_id": depot_id}, sort=[("recorded_at", -1)]
    )


async def fetch_holdings(run: dict) -> list[Position]:
    qs = await _linked_depot(run)
    if not qs:
        return []
    snapshot = await _latest_snapshot(qs)
    if not snapshot:
        return []
    return positions_from_snapshot(snapshot)


async def fetch_held_since(run: dict) -> dict[str, date]:
    qs = await _linked_depot(run)
    if not qs:
        return {}
    depot_id = qs["depot_id"]
    snapshot = await _latest_snapshot(qs)
    held_since, snapshot_wkns = held_since_from_snapshot(snapshot)

    if qs.get("depot_type", "virtual") == "virtual":
        transactions = await virtual_depot_transactions_collection().find(
            {"depot_id": depot_id, "transaction_type": "BUY"},
            sort=[("booking_date", -1)],
        ).to_list()
        add_held_since_from_transactions(held_since, snapshot_wkns, transactions)

    return held_since


async def fetch_reentry_blocked_symbols(
    run: dict,
    *,
    screening_symbols: set[str],
    prevention_days: int,
) -> set[str]:
    if prevention_days <= 0 or run.get("depot_type", "virtual") == "real":
        return set()
    qs = await _linked_depot(run)
    if not qs or qs.get("depot_type", "virtual") == "real":
        return set()

    cutoff = datetime.now(timezone.utc) - timedelta(days=prevention_days)
    transactions = await virtual_depot_transactions_collection().find(
        {
            "depot_id": qs["depot_id"],
            "transaction_type": "SELL",
            "booking_date": {"$gte": cutoff},
        },
        sort=[("booking_date", -1)],
    ).to_list()
    wkns = {txn.get("wkn") for txn in transactions if txn.get("wkn")}
    if not wkns:
        return set()

    mappings = await warrant_underlying_map_collection().find(
        {"warrant_wkn": {"$in": sorted(wkns)}},
        projection={"warrant_wkn": 1, "underlying_symbol": 1},
    ).to_list()
    return {
        symbol
        for mapping in mappings
        if (symbol := mapping.get("underlying_symbol"))
        and (not screening_symbols or symbol in screening_symbols)
    }
