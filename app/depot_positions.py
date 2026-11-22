import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.models.market import Position, Ticker

logger = logging.getLogger(__name__)

_LEGACY_POSITION_FIELDS = frozenset({"purchase_price", "buy_price_at_entry"})


def _assert_canonical_position_schema(position: dict[str, Any]) -> None:
    legacy_present = sorted(_LEGACY_POSITION_FIELDS.intersection(position.keys()))
    if legacy_present:
        raise RuntimeError(
            "Legacy position fields detected in snapshot: "
            f"{', '.join(legacy_present)}. Expected canonical fields "
            "average_purchase_price and purchase_price_at_entry."
        )


def _decimal_from_amount_field(container: dict[str, Any], field_name: str) -> Decimal:
    amount_obj = container.get(field_name)
    raw = amount_obj.get("value") if isinstance(amount_obj, dict) else amount_obj
    if raw in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        logger.warning("Invalid decimal amount in depot position field %s", field_name)
        return Decimal("0")


def _parse_snapshot_held_since(raw: Any) -> date | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    return None


def positions_from_snapshot(snapshot: dict[str, Any]) -> list[Position]:
    holdings: list[Position] = []
    for pos in snapshot.get("positions", []):
        if not isinstance(pos, dict):
            continue
        _assert_canonical_position_schema(pos)
        isin = pos.get("isin")
        wkn = pos.get("wkn") or isin or ""
        quantity = _decimal_from_amount_field(pos, "quantity")
        if quantity <= 0:
            continue
        avg_cost = _decimal_from_amount_field(pos, "average_purchase_price")
        holdings.append(Position(
            ticker=Ticker(symbol=wkn, isin=isin, name=pos.get("instrument_name")),
            quantity=quantity,
            avg_cost=avg_cost,
        ))
    return holdings


def held_since_from_snapshot(
    snapshot: dict[str, Any] | None,
) -> tuple[dict[str, date], set[str]]:
    held_since: dict[str, date] = {}
    snapshot_wkns: set[str] = set()

    if snapshot:
        for pos in snapshot.get("positions", []):
            if not isinstance(pos, dict):
                continue
            _assert_canonical_position_schema(pos)
            wkn = pos.get("wkn")
            if not wkn:
                continue
            snapshot_wkns.add(wkn)
            held = _parse_snapshot_held_since(pos.get("held_since_date"))
            if held and wkn not in held_since:
                held_since[wkn] = held

    return held_since, snapshot_wkns


def add_held_since_from_transactions(
    held_since: dict[str, date],
    snapshot_wkns: set[str],
    transactions: list[dict[str, Any]],
) -> None:
    for txn in transactions:
        wkn = txn.get("wkn")
        if not wkn or wkn in held_since:
            continue
        if snapshot_wkns and wkn not in snapshot_wkns:
            continue
        bd = txn.get("booking_date")
        if isinstance(bd, datetime):
            held_since[wkn] = bd.date()
        elif isinstance(bd, date):
            held_since[wkn] = bd
