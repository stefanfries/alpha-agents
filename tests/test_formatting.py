from decimal import Decimal

from app.config import settings
from app.formatting import format_eur, format_percentage, format_usd


def test_currency_formatters_use_configured_german_locale(monkeypatch):
    monkeypatch.setattr(settings, "currency_locale", "de_DE")

    assert format_eur(Decimal("12345.67")) == "12.345,67\u00a0€"
    assert format_usd(Decimal("12345.67")) == "12.345,67\u00a0$"
    assert format_percentage(Decimal("0.215")) == "21,5%"


def test_currency_formatters_use_configured_us_locale(monkeypatch):
    monkeypatch.setattr(settings, "currency_locale", "en_US")

    assert format_eur(Decimal("12345.67")) == "€12,345.67"
    assert format_usd(Decimal("12345.67")) == "$12,345.67"
    assert format_percentage(Decimal("0.215")) == "21.5%"