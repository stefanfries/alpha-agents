from decimal import Decimal

from babel.numbers import format_currency
from babel.numbers import format_percent as babel_format_percent
from jinja2 import Environment

from app.config import settings


def format_eur(value: Decimal | float | int) -> str:
    return format_currency(value, "EUR", locale=settings.currency_locale)


def format_usd(value: Decimal | float | int) -> str:
    return format_currency(value, "USD", locale=settings.currency_locale)


def format_percentage(value: Decimal | float | int) -> str:
    return babel_format_percent(value, format="#,##0.0%", locale=settings.currency_locale)


def register_currency_filters(environment: Environment) -> None:
    environment.filters["eur"] = format_eur
    environment.filters["usd"] = format_usd
    environment.filters["pct"] = format_percentage
    environment.globals["currency_locale"] = settings.currency_locale