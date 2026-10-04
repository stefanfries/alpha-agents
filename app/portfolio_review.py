from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from app.models.signals import (
    MonitoringResult,
    PlannedPosition,
    PortfolioAccountSnapshot,
    PortfolioHoldingValue,
    PortfolioProposal,
    PositionReview,
    WarrantSelectionResult,
)


@dataclass(frozen=True)
class PortfolioActionRow:
    action_type: str
    symbol: str
    underlying_name: str | None
    warrant_wkn: str
    held_since: str | None
    weight: Decimal | None
    current_price_eur: Decimal | None
    total_price_eur: Decimal | None
    quantity: Decimal | None
    quantity_label: str
    buy_budget_eur: Decimal | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PortfolioActionTables:
    sells: list[PortfolioActionRow]
    buys: list[PortfolioActionRow]
    keeps: list[PortfolioActionRow]


def _weight(value: Decimal | None, nav: Decimal | None) -> Decimal | None:
    return value / nav if value is not None and nav is not None and nav > 0 else None


def _keep_reason(review: PositionReview | None) -> str:
    if review is None:
        return "Monitoring assessment unavailable"
    if review.warrant_health_status == "healthy" and review.trend_status == "trend intact":
        return "Trend intact; warrant healthy"

    trend = review.trend_status or "Trend status unavailable"
    health = review.warrant_health_status or "warrant health unknown"
    if review.warrant_health_reason:
        health = f"{health}: {review.warrant_health_reason}"
    elif review.decision_reason == "degraded but within grace period":
        health = "warrant degraded (within grace period)"
    return f"{trend}; {health}"


def _sell_reason(review: PositionReview | None, action_context: str | None = None) -> str:
    details: list[str] = []
    if review:
        trend = review.trend_status or review.decision_reason
        if trend:
            details.append(trend)
        warrant_status = review.warrant_health_status or "unknown"
        warrant = f"Warrant {warrant_status}"
        if review.warrant_health_reason:
            warrant = f"{warrant}: {review.warrant_health_reason}"
        details.append(warrant)
    if action_context:
        details.append(action_context)
    return "; ".join(details) if details else "Not selected for continued holding"


def _held_value(
    holding: PortfolioHoldingValue | None,
    review: PositionReview | None,
    nav: Decimal | None,
) -> PortfolioActionRow | None:
    if holding is None:
        return None
    position = holding.position
    return PortfolioActionRow(
        action_type="KEEP",
        symbol=holding.underlying_symbol or (review.underlying_symbol if review else None) or "—",
        underlying_name=holding.underlying_name or (review.underlying_name if review else None),
        warrant_wkn=position.ticker.symbol,
        held_since=review.held_since.isoformat() if review and review.held_since else None,
        weight=_weight(holding.market_value_eur, nav),
        current_price_eur=holding.bid_price_eur,
        total_price_eur=holding.market_value_eur,
        quantity=position.quantity,
        quantity_label="Qty held",
        reason=_keep_reason(review),
    )


def _buy_value(position: PlannedPosition, action_type: str, weight: Decimal) -> PortfolioActionRow:
    price = position.buy_price_eur
    quantity = (
        (position.notional_eur / price).to_integral_value(rounding=ROUND_DOWN)
        if price is not None and price > 0
        else None
    )
    estimated_cost = quantity * price if quantity is not None and price is not None else None
    selection_reason = position.reason or "Warrant selected"
    reason = (
        f"Replacement for degraded warrant; {selection_reason}"
        if action_type == "ROLL/BUY"
        else f"Entry selected; {selection_reason}"
    )
    return PortfolioActionRow(
        action_type=action_type,
        symbol=position.underlying_symbol or position.ticker.symbol,
        underlying_name=position.ticker.name,
        warrant_wkn=position.ticker.symbol,
        held_since="Next business day (planned)",
        weight=weight,
        current_price_eur=price,
        total_price_eur=estimated_cost,
        quantity=quantity,
        quantity_label="Qty to buy (est.)",
        buy_budget_eur=position.notional_eur,
        reason=reason,
    )


def build_portfolio_action_tables(
    proposal: PortfolioProposal,
    monitoring: MonitoringResult | None = None,
    warrant_selection: WarrantSelectionResult | None = None,
) -> PortfolioActionTables:
    snapshot: PortfolioAccountSnapshot | None = proposal.account_snapshot
    nav = snapshot.nav_eur if snapshot else None
    holdings = snapshot.holdings if snapshot else []
    holdings_by_isin = {
        holding.position.ticker.isin or holding.position.ticker.symbol: holding
        for holding in holdings
    }
    reviews_by_isin = {
        review.warrant_isin: review
        for review in (
            [
                *(monitoring.positions_to_sell if monitoring else []),
                *(monitoring.positions_to_roll if monitoring else []),
                *(monitoring.positions_to_keep if monitoring else []),
            ]
        )
    }
    trend_sell_isins = {
        review.warrant_isin for review in (monitoring.positions_to_sell if monitoring else [])
    }
    failed_roll_isins = set(warrant_selection.sell_existing_isins if warrant_selection else [])
    close_isins = {position.ticker.isin for position in proposal.close_positions}
    roll_incumbent_isins = {trade.incumbent.ticker.isin for trade in proposal.roll_trades}

    trend_sells: list[PortfolioActionRow] = []
    other_sells: list[PortfolioActionRow] = []
    failed_roll_sells: list[PortfolioActionRow] = []
    for position in proposal.close_positions:
        isin = position.ticker.isin or position.ticker.symbol
        holding = holdings_by_isin.get(isin)
        review = reviews_by_isin.get(isin)
        action_type = "ROLL/SELL" if isin in failed_roll_isins else "SELL"
        reason = _sell_reason(
            review,
            "No replacement met the roll criteria" if isin in failed_roll_isins else None,
        )
        row = PortfolioActionRow(
            action_type=action_type,
            symbol=(holding.underlying_symbol if holding else None)
            or (review.underlying_symbol if review else None)
            or "—",
            underlying_name=(holding.underlying_name if holding else None)
            or (review.underlying_name if review else None),
            warrant_wkn=position.ticker.symbol,
            held_since=review.held_since.isoformat() if review and review.held_since else None,
            weight=_weight(holding.market_value_eur if holding else None, nav),
            current_price_eur=holding.bid_price_eur if holding else None,
            total_price_eur=holding.market_value_eur if holding else None,
            quantity=position.quantity,
            quantity_label="Qty to sell",
            reason=reason,
        )
        if isin in trend_sell_isins:
            trend_sells.append(row)
        elif isin in failed_roll_isins:
            failed_roll_sells.append(row)
        else:
            other_sells.append(row)

    paired_roll_sells: list[PortfolioActionRow] = []
    roll_buys: list[PortfolioActionRow] = []
    for trade in proposal.roll_trades:
        incumbent = trade.incumbent
        isin = incumbent.ticker.isin or incumbent.ticker.symbol
        holding = holdings_by_isin.get(isin)
        review = reviews_by_isin.get(isin)
        paired_roll_sells.append(PortfolioActionRow(
            action_type="ROLL/SELL",
            symbol=(holding.underlying_symbol if holding else None)
            or (review.underlying_symbol if review else None)
            or trade.replacement.underlying_symbol
            or "—",
            underlying_name=(holding.underlying_name if holding else None)
            or (review.underlying_name if review else None)
            or trade.replacement.ticker.name,
            warrant_wkn=incumbent.ticker.symbol,
            held_since=review.held_since.isoformat() if review and review.held_since else None,
            weight=_weight(holding.market_value_eur if holding else None, nav),
            current_price_eur=holding.bid_price_eur if holding else None,
            total_price_eur=holding.market_value_eur if holding else None,
            quantity=incumbent.quantity,
            quantity_label="Qty to sell",
            reason=_sell_reason(review, "Paired with replacement BUY"),
        ))
        roll_buys.append(_buy_value(trade.replacement, "ROLL/BUY", Decimal(str(trade.target_weight))))

    sells = [*trend_sells, *other_sells, *failed_roll_sells, *paired_roll_sells]
    buys = [
        *roll_buys,
        *[
            _buy_value(position, "BUY", Decimal(str(position.target_weight)))
            for position in proposal.new_positions
        ],
    ]
    keeps = [
        row
        for holding in holdings
        if (row := _held_value(
            holding,
            reviews_by_isin.get(holding.position.ticker.isin or holding.position.ticker.symbol),
            nav,
        )) is not None
        and (holding.position.ticker.isin or holding.position.ticker.symbol) not in close_isins
        and (holding.position.ticker.isin or holding.position.ticker.symbol) not in roll_incumbent_isins
    ]
    return PortfolioActionTables(sells=sells, buys=buys, keeps=keeps)