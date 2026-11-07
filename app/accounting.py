from decimal import Decimal, InvalidOperation
from typing import Any

_CASH_ACCOUNT_TYPES = {
    "girokonto": "Giro-Konto",
    "tagesgeldpluskonto": "Tagesgeld PLUS-Konto",
    "verrechnungskonto": "Verrechnungskonto",
}


def sum_latest_cash_balances_eur(
    records_newest_first: list[dict[str, Any]],
    *,
    use_available_cash_for_verrechnungskonto: bool = False,
) -> tuple[Decimal | None, list[str]]:
    """Sum the newest EUR cash amount per supported account type.

    The caller supplies records sorted by recorded_at descending. A balance can be old
    when unchanged, so freshness is determined per account type by record ordering, not age.
    Use available cash for Verrechnungskonto when calculating spendable funds.
    """
    latest_by_type: dict[str, dict[str, Any]] = {}
    for record in records_newest_first:
        account_type = record.get("account_type")
        if not isinstance(account_type, str):
            continue
        normalized_type = "".join(character for character in account_type.casefold() if character.isalnum())
        if normalized_type in _CASH_ACCOUNT_TYPES and normalized_type not in latest_by_type:
            latest_by_type[normalized_type] = record

    total = Decimal("0")
    errors: list[str] = []
    for normalized_type, display_name in _CASH_ACCOUNT_TYPES.items():
        record = latest_by_type.get(normalized_type)
        if record is None:
            errors.append(f"Missing cash balance for {display_name}")
            continue
        use_available_cash = (
            use_available_cash_for_verrechnungskonto
            and normalized_type == "verrechnungskonto"
        )
        amount_field = "available_cash_amount" if use_available_cash else "balance"
        balance = record.get(amount_field) or {}
        if not isinstance(balance, dict) or (balance.get("unit") or "").upper() != "EUR":
            errors.append(f"Cash amount for {display_name} is not explicitly denominated in EUR")
            continue
        try:
            value = Decimal(str(balance.get("value")))
        except (InvalidOperation, TypeError, ValueError):
            errors.append(f"Cash amount for {display_name} is invalid")
            continue
        if not value.is_finite():
            errors.append(f"Cash amount for {display_name} is invalid")
            continue
        total += value

    return (None if errors else total), errors