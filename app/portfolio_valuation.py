from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from app.agents.monitoring import WarrantSnapshot
from app.models.market import Position
from app.models.signals import PortfolioAccountSnapshot, PortfolioHoldingValue


def build_portfolio_account_snapshot(
    *,
    source: Literal["real", "virtual"],
    cash_eur: Decimal | None,
    holdings: list[Position],
    underlying_map: dict[str, str],
    warrant_quotes: dict[str, WarrantSnapshot],
    sectors_by_underlying_isin: dict[str, str],
    underlying_isins_by_symbol: dict[str, str],
    underlying_names_by_symbol: dict[str, str] | None,
    errors: list[str],
    now: datetime,
    quote_max_age_hours: int,
) -> PortfolioAccountSnapshot:
    errors = list(errors)
    valued_holdings: list[PortfolioHoldingValue] = []
    total_position_value = Decimal("0")

    for position in holdings:
        isin = position.ticker.isin or ""
        underlying = underlying_map.get(isin)
        quote = warrant_quotes.get(isin)
        quote_error = portfolio_quote_error(quote, now, quote_max_age_hours)
        bid_eur = None
        market_value_eur = None
        if quote_error:
            errors.append(f"{isin or position.ticker.symbol}: {quote_error}")
        else:
            assert quote is not None and quote.bid is not None
            bid_eur = Decimal(str(quote.bid))
            market_value_eur = position.quantity * bid_eur
            total_position_value += market_value_eur

        valued_holdings.append(PortfolioHoldingValue(
            position=position,
            underlying_isin=underlying_isins_by_symbol.get(underlying or ""),
            underlying_symbol=underlying,
            underlying_name=(underlying_names_by_symbol or {}).get(underlying or ""),
            sector=sectors_by_underlying_isin.get(
                underlying_isins_by_symbol.get(underlying or "", "")
            ),
            bid_price_eur=bid_eur,
            market_value_eur=market_value_eur,
            quote_timestamp_utc=quote.timestamp_utc if quote else None,
            issuer_action=quote.issuer_action if quote else False,
            issuer_no_fee_action=quote.issuer_no_fee_action if quote else False,
            quote_error=quote_error,
        ))

    all_quotes_valid = all(item.market_value_eur is not None for item in valued_holdings)
    nav_eur = cash_eur + total_position_value if cash_eur is not None and all_quotes_valid else None
    if not all_quotes_valid:
        errors.append("NAV is incomplete because one or more held-position quotes are invalid")

    return PortfolioAccountSnapshot(
        source=source,
        available_cash_eur=cash_eur,
        holdings=valued_holdings,
        nav_eur=nav_eur,
        recorded_at_utc=now,
        valuation_errors=list(dict.fromkeys(errors)),
    )


def portfolio_quote_error(
    quote: WarrantSnapshot | None,
    now: datetime,
    max_age_hours: int = 72,
) -> str | None:
    if quote is None or quote.bid is None or quote.bid <= 0:
        return "missing or invalid bid quote"
    if (quote.currency or "").upper() != "EUR":
        return "quote currency is not EUR"
    timestamp = quote.timestamp_utc
    if timestamp is None or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return "quote timestamp is missing or not timezone-aware"
    age = now - timestamp.astimezone(timezone.utc)
    if age.total_seconds() < 0:
        return "quote timestamp is in the future"
    if age > timedelta(hours=max_age_hours):
        return f"quote is older than {max_age_hours} hours"
    return None
