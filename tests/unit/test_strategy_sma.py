from decimal import Decimal as D
from decimal import getcontext

import pytest

from cripto_bot.domain.portfolio.models import PaperError
from cripto_bot.domain.strategy.sma import Snapshot, TrendSMA


def evaluate(closes, position="0", fast=20, slow=50):
    return TrendSMA().evaluate(
        Snapshot(tuple(D(str(c)) for c in closes), 1000, D(position)), fast, slow, "sma"
    )


@pytest.mark.parametrize("count", [0, 1, 19, 20, 49, 50])
def test_warmup_never_opens_before_two_complete_windows(count):
    d = evaluate([100] * count)
    assert d.reason == "WARMUP" and d.target == "HOLD"


def test_equal_previous_crosses_but_equal_current_does_not_and_context_is_local():
    before = getcontext().copy()
    up = evaluate([100] * 50 + [110])
    assert (up.target, up.reason, up.previous_fast, up.previous_slow, up.fast, up.slow) == (
        "LONG",
        "CROSS_UP",
        "100",
        "100",
        "100.5",
        "100.2",
    )
    down = evaluate([100] * 50 + [110] + [100] * 20, position="1")
    assert down.target == "CASH" and down.fast == "100" and down.slow == "100.2"
    assert evaluate([100] * 51).reason == "NO_CROSS"
    assert evaluate([100] * 50 + [90], position="1").reason == "CROSS_DOWN"
    assert getcontext().prec == before.prec and getcontext().rounding == before.rounding


def test_position_and_inventory_prevent_pyramiding_or_short():
    assert evaluate([100] * 50 + [110], position="1").reason == "ALREADY_LONG"
    assert evaluate([100] * 50 + [90]).reason == "NO_INVENTORY"
    assert evaluate([100] * 50 + [110, 110], position="0").reason == "NO_CROSS"


@pytest.mark.parametrize("fast,slow", [(0, 50), (50, 50), (51, 50), (1, 501)])
def test_invalid_windows_fail(fast, slow):
    with pytest.raises(PaperError):
        evaluate([100] * 51, fast=fast, slow=slow)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", "-1"])
def test_nonpositive_or_nonfinite_closes_rejected(value):
    with pytest.raises(PaperError):
        evaluate([value] * 51)


def test_prefix_causality_and_snapshot_is_immutable():
    closes = [100] * 50 + [110] + [100] * 20
    decisions = [evaluate(closes[:end]) for end in range(1, len(closes) + 1)]
    extended = closes + [500] * 20
    assert decisions == [evaluate(extended[:end]) for end in range(1, len(closes) + 1)]
    snap = Snapshot((D("100"),), 1, D(0))
    with pytest.raises(AttributeError):
        snap.as_of = 2
