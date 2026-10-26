from decimal import Decimal, getcontext

import pytest

from cripto_bot.domain.models import parse_decimal


@pytest.mark.parametrize("value", ["0.1", "0.2", "0", "10000.00", "-1.25"])
def test_finite_strings(value):
    assert parse_decimal(value) == Decimal(value)


def test_exact_sum_and_context():
    before = getcontext().copy()
    assert parse_decimal("0.1") + parse_decimal("0.2") == Decimal("0.3")
    assert getcontext().prec == before.prec
    assert getcontext().rounding == before.rounding


@pytest.mark.parametrize(
    "value", [0.1, 1, True, None, "", "oops", "NaN", "sNaN", "Infinity", "-Infinity", "   "]
)
def test_invalid_values(value):
    with pytest.raises(ValueError):
        parse_decimal(value)
