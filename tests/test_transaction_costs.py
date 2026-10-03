from decimal import Decimal

import pytest

from app.policies.transaction_costs import (
    calculate_equal_buy_amount_eur,
    calculate_order_fee_eur,
    calculate_slippage_eur,
)


@pytest.mark.parametrize(
    ("notional", "expected"),
    [
        (Decimal("1000"), Decimal("9.90")),
        (Decimal("2000"), Decimal("9.90")),
        (Decimal("10000"), Decimal("29.90")),
        (Decimal("50000"), Decimal("59.90")),
    ],
)
def test_standard_order_fee_applies_base_provision_and_limits(notional, expected):
    assert calculate_order_fee_eur(notional) == expected


def test_issuer_action_fee_overrides_standard_fee():
    assert calculate_order_fee_eur(Decimal("10000"), issuer_action=True) == Decimal("3.90")


def test_issuer_no_fee_action_takes_precedence_when_both_flags_are_true():
    assert calculate_order_fee_eur(
        Decimal("10000"),
        issuer_action=True,
        issuer_no_fee_action=True,
    ) == Decimal("0.00")


def test_slippage_is_basis_points_of_notional():
    assert calculate_slippage_eur(Decimal("10000"), 12) == Decimal("12.00")
    assert calculate_slippage_eur(Decimal("10000"), None) is None


def test_equal_buy_sizing_reserves_each_order_fee_and_slippage():
    amount, costs = calculate_equal_buy_amount_eur(
        Decimal("45000"),
        6,
        [(False, False), (True, False)],
        10,
    )

    assert amount == Decimal("7492.91")
    assert costs == Decimal("42.51")
    assert amount * 6 + costs <= Decimal("45000")


def test_equal_buy_sizing_requires_slippage_assumption():
    assert calculate_equal_buy_amount_eur(
        Decimal("45000"),
        6,
        [(False, False)],
        None,
    ) == (None, None)