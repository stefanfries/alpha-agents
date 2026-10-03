"""Pure transaction fee and slippage calculations for warrant orders."""

from collections.abc import Sequence
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

_CENT = Decimal("0.01")
_BASE_FEE_EUR = Decimal("4.90")
_ORDER_PROVISION_RATE = Decimal("0.0025")
_MINIMUM_FEE_EUR = Decimal("9.90")
_MAXIMUM_FEE_EUR = Decimal("59.90")
_ISSUER_ACTION_FEE_EUR = Decimal("3.90")


def calculate_order_fee_eur(
    notional_eur: Decimal,
    *,
    issuer_action: bool = False,
    issuer_no_fee_action: bool = False,
) -> Decimal:
    if notional_eur < 0:
        raise ValueError("Order notional must not be negative")
    if issuer_no_fee_action:
        return Decimal("0.00")
    if issuer_action:
        return _ISSUER_ACTION_FEE_EUR

    fee = _BASE_FEE_EUR + notional_eur * _ORDER_PROVISION_RATE
    return min(max(fee, _MINIMUM_FEE_EUR), _MAXIMUM_FEE_EUR).quantize(
        _CENT,
        rounding=ROUND_HALF_UP,
    )


def calculate_slippage_eur(notional_eur: Decimal, slippage_bps: float | Decimal | None) -> Decimal | None:
    if slippage_bps is None:
        return None
    basis_points = Decimal(str(slippage_bps))
    if notional_eur < 0 or basis_points < 0:
        raise ValueError("Notional and slippage basis points must not be negative")
    return (notional_eur * basis_points / Decimal("10000")).quantize(
        _CENT,
        rounding=ROUND_HALF_UP,
    )


def calculate_equal_buy_amount_eur(
    available_funds_eur: Decimal,
    max_positions: int,
    buy_issuer_flags: Sequence[tuple[bool, bool]],
    slippage_bps: float | Decimal | None,
) -> tuple[Decimal | None, Decimal | None]:
    if available_funds_eur <= 0 or max_positions <= 0 or not buy_issuer_flags or slippage_bps is None:
        return None, None

    low_cents = 0
    high_cents = int(
        (available_funds_eur / Decimal(max_positions) / _CENT).to_integral_value(rounding=ROUND_DOWN)
    )
    while low_cents <= high_cents:
        middle_cents = (low_cents + high_cents) // 2
        candidate = Decimal(middle_cents) * _CENT
        buy_costs = sum(
            (
                calculate_order_fee_eur(
                    candidate,
                    issuer_action=issuer_action,
                    issuer_no_fee_action=issuer_no_fee_action,
                )
                + (calculate_slippage_eur(candidate, slippage_bps) or Decimal("0"))
                for issuer_action, issuer_no_fee_action in buy_issuer_flags
            ),
            start=Decimal("0"),
        )
        if candidate * max_positions + buy_costs <= available_funds_eur:
            low_cents = middle_cents + 1
        else:
            high_cents = middle_cents - 1

    amount = Decimal(max(0, high_cents)) * _CENT
    costs = sum(
        (
            calculate_order_fee_eur(
                amount,
                issuer_action=issuer_action,
                issuer_no_fee_action=issuer_no_fee_action,
            )
            + (calculate_slippage_eur(amount, slippage_bps) or Decimal("0"))
            for issuer_action, issuer_no_fee_action in buy_issuer_flags
        ),
        start=Decimal("0"),
    )
    return amount, costs