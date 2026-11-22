import logging
from datetime import date, datetime, timezone
from typing import Any

from app.agents.monitoring import WarrantSnapshot
from app.tools.finhub import FinHubTool
from app.tools.retry import retry_call

logger = logging.getLogger(__name__)


async def fetch_warrant_snapshots(
    finhub: FinHubTool,
    unique_isins: list[str],
    today: date,
) -> dict[str, WarrantSnapshot]:
    snapshots: dict[str, WarrantSnapshot] = {}

    for isin in unique_isins:
        try:
            detail = await retry_call(finhub.get_warrant_detail, isin)
        except Exception:
            logger.warning("Monitoring snapshots: detail fetch failed for %s", isin)
            continue
        if not detail:
            logger.warning("Monitoring snapshots: no detail for %s", isin)
            continue

        md = detail.get("market_data") or {}
        an = detail.get("analytics") or {}
        rd = detail.get("reference_data") or {}

        spread_pct = _as_float(md.get("spread_percent"))
        leverage = _as_float(an.get("leverage"))
        delta = _as_float(an.get("delta"))
        days_to_maturity = _days_to_maturity(rd.get("maturity_date"), today)
        strike = _as_float(rd.get("strike"))
        maturity_date = _parse_maturity_date(rd.get("maturity_date"))

        bid = _as_float(md.get("bid"))
        ask = _as_float(md.get("ask"))
        bid_ask_midprice = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        timestamp_utc = _parse_utc_timestamp(md.get("timestamp_utc"))

        if all(
            value is None
            for value in (spread_pct, leverage, delta, days_to_maturity, bid_ask_midprice)
        ):
            logger.warning("Monitoring snapshots: empty metrics for %s", isin)
            continue

        snapshots[isin] = WarrantSnapshot(
            warrant_isin=isin,
            spread_pct=spread_pct,
            leverage=leverage,
            days_to_maturity=days_to_maturity,
            delta=delta,
            bid=bid,
            ask=ask,
            currency=md.get("currency") or rd.get("currency"),
            timestamp_utc=timestamp_utc,
            issuer_action=bool(rd.get("issuer_action")),
            issuer_no_fee_action=bool(rd.get("issuer_no_fee_action")),
            bid_ask_midprice=bid_ask_midprice,
            prev_close=_as_float(md.get("prev_close")),
            strike=strike,
            maturity_date=maturity_date,
        )

    return snapshots


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_maturity_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(raw).date()
        except ValueError:
            try:
                return date.fromisoformat(raw)
            except ValueError:
                return None
    return None


def _days_to_maturity(value: Any, today: date) -> int | None:
    maturity_date = _parse_maturity_date(value)
    if maturity_date is None:
        return None
    return (maturity_date - today).days
