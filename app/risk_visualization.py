from decimal import Decimal
from typing import Any

from app.models.signals import (
    PlannedPosition,
    PortfolioProposal,
    RiskAssessment,
    WarrantSelectionResult,
)

_SECTOR_COLORS = [
    "#2a9d8f",
    "#e76f51",
    "#457b9d",
    "#e9c46a",
    "#8ab17d",
    "#b56576",
    "#f4a261",
    "#6d8299",
]
_CASH_COLOR = "#89939e"


def build_risk_view(
    proposal: PortfolioProposal,
    assessment: RiskAssessment,
    warrants: WarrantSelectionResult,
    *,
    max_positions: int,
    max_position_multiple: float,
) -> dict[str, Any]:
    warrant_by_isin = {
        warrant.warrant_isin: warrant
        for warrant in [*warrants.selected, *warrants.roll_selected]
    }

    def position_details(position: PlannedPosition) -> dict[str, Any]:
        warrant = warrant_by_isin.get(position.ticker.isin or "")
        underlying = warrant.underlying if warrant else None
        return {
            "underlying_symbol": position.underlying_symbol
            or (underlying.symbol if underlying else position.ticker.symbol),
            "underlying_name": (underlying.name if underlying else None)
            or position.ticker.name
            or "—",
            "warrant_wkn": warrant.warrant_wkn if warrant else position.ticker.symbol,
        }

    review_rows: list[dict[str, Any]] = []
    for status, positions in (
        ("approved", assessment.approved_positions),
        ("rejected", assessment.rejected_positions),
    ):
        for position in positions:
            review_rows.append({
                **position_details(position),
                "status": status,
                "action": "BUY",
                "notional_eur": float(position.notional_eur),
                "note": assessment.risk_notes.get(position.ticker.symbol, ""),
            })

    for status, trades in (
        ("approved", assessment.approved_roll_trades),
        ("rejected", assessment.rejected_roll_trades),
    ):
        for trade in trades:
            position = trade.replacement
            review_rows.append({
                **position_details(position),
                "status": status,
                "action": "ROLL BUY",
                "notional_eur": float(position.notional_eur),
                "note": assessment.risk_notes.get(position.ticker.symbol, ""),
            })

    snapshot = proposal.account_snapshot
    if snapshot is None or snapshot.nav_eur is None or snapshot.nav_eur <= 0:
        return {"review_rows": review_rows, "available": False, "message": "Projected allocation unavailable: NAV is missing."}
    if snapshot.valuation_errors:
        return {"review_rows": review_rows, "available": False, "message": "Projected allocation unavailable: account valuation is incomplete."}

    closing_isins = {
        position.ticker.isin or position.ticker.symbol
        for position in assessment.close_positions
    }
    rolled_isins = {
        trade.incumbent.ticker.isin or trade.incumbent.ticker.symbol
        for trade in assessment.approved_roll_trades
    }
    projected_positions: list[dict[str, Any]] = []

    for holding in snapshot.holdings:
        key = holding.position.ticker.isin or holding.position.ticker.symbol
        if key in closing_isins or key in rolled_isins:
            continue
        if holding.market_value_eur is None:
            return {"review_rows": review_rows, "available": False, "message": "Projected allocation unavailable: a held position has no current value."}
        value = holding.market_value_eur
        if value <= 0:
            continue
        projected_positions.append({
            "underlying_symbol": holding.underlying_symbol or holding.position.ticker.symbol,
            "underlying_name": holding.underlying_name or holding.position.ticker.name or "—",
            "warrant_wkn": holding.position.ticker.symbol,
            "sector": holding.sector or "Unclassified",
            "value_eur": value,
        })

    for position in assessment.approved_positions:
        details = position_details(position)
        if position.notional_eur > 0:
            projected_positions.append({
                **details,
                "sector": position.sector or "Unclassified",
                "value_eur": position.notional_eur,
            })

    for trade in assessment.approved_roll_trades:
        position = trade.replacement
        details = position_details(position)
        if position.notional_eur > 0:
            projected_positions.append({
                **details,
                "sector": position.sector or "Unclassified",
                "value_eur": position.notional_eur,
            })

    nav = snapshot.nav_eur
    invested = sum((item["value_eur"] for item in projected_positions), start=Decimal("0"))
    cash = max(Decimal("0"), nav - invested)
    projected_positions.append({
        "underlying_symbol": "Cash",
        "underlying_name": "Cash",
        "warrant_wkn": "—",
        "sector": "Cash",
        "value_eur": cash,
    })

    sector_values: dict[str, Decimal] = {}
    for item in projected_positions:
        sector = item["sector"]
        sector_values[sector] = sector_values.get(sector, Decimal("0")) + item["value_eur"]

    sorted_sectors = sorted(sector_values.items(), key=lambda item: (-item[1], item[0]))
    colors = {
        sector: _CASH_COLOR if sector == "Cash" else _SECTOR_COLORS[index % len(_SECTOR_COLORS)]
        for index, (sector, _value) in enumerate(sorted_sectors)
    }
    total_projected = sum(sector_values.values(), start=Decimal("0"))
    cursor = 0.0
    gradient_stops: list[str] = []
    sector_rows: list[dict[str, Any]] = []
    for sector, value in sorted_sectors:
        start = cursor
        cursor += float(value / total_projected * 100) if total_projected else 0.0
        gradient_stops.append(f"{colors[sector]} {start:.3f}% {cursor:.3f}%")
        sector_rows.append({
            "sector": sector,
            "value_eur": float(value),
            "weight_pct": float(value / nav * 100),
            "color": colors[sector],
        })

    position_limit_pct = max_position_multiple / max_positions * 100 if max_positions > 0 else 0.0
    position_rows = []
    for item in sorted(projected_positions, key=lambda row: (-row["value_eur"], row["underlying_symbol"])):
        weight_pct = float(item["value_eur"] / nav * 100)
        position_rows.append({
            **item,
            "value_eur": float(item["value_eur"]),
            "weight_pct": weight_pct,
            "color": colors.get(item["sector"], _CASH_COLOR),
            "is_cash": item["sector"] == "Cash",
        })

    chart_scale_pct = max(
        [position_limit_pct, *(item["weight_pct"] for item in position_rows), 1.0]
    ) * 1.1
    for item in position_rows:
        item["bar_width_pct"] = item["weight_pct"] / chart_scale_pct * 100

    return {
        "review_rows": review_rows,
        "available": True,
        "nav_eur": float(nav),
        "cash_shortfall_eur": float(max(Decimal("0"), invested - nav)),
        "sector_rows": sector_rows,
        "sector_gradient": ", ".join(gradient_stops),
        "position_rows": position_rows,
        "position_limit_pct": position_limit_pct,
        "position_limit_marker_pct": position_limit_pct / chart_scale_pct * 100,
    }