from decimal import Decimal

from app.accounting import sum_latest_cash_balances_eur


def test_sum_latest_eur_cash_balance_per_account_type():
    balances = [
        {"account_type": "Verrechnungskonto", "balance": {"value": "101865.98", "unit": "EUR"}},
        {"account_type": "Tagesgeld PLUS-Konto", "balance": {"value": "0.03", "unit": "EUR"}},
        {"account_type": "Girokonto", "balance": {"value": "106.03", "unit": "EUR"}},
        # Older records must not be added to the latest values for the same account type.
        {"account_type": "Verrechnungskonto", "balance": {"value": "99636.35", "unit": "EUR"}},
    ]

    total, errors = sum_latest_cash_balances_eur(balances)

    assert total == Decimal("101972.04")
    assert errors == []


def test_sum_latest_eur_cash_requires_each_account_type():
    total, errors = sum_latest_cash_balances_eur([
        {"account_type": "Verrechnungskonto", "balance": {"value": "101865.98", "unit": "EUR"}},
    ])

    assert total is None
    assert errors == [
        "Missing cash balance for Giro-Konto",
        "Missing cash balance for Tagesgeld PLUS-Konto",
    ]