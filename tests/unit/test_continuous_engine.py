"""Financial oracles and failure/custody invariants for paper-continuous-v1."""

import copy
from decimal import Decimal

import pytest
from cripto_core.errors import PaperError

from cripto_bot.domain.continuous.engine import reduce
from tests.continuous_factories import BASE, STEP, arm_payload, context, normalized, quote, rows


def prepared():
    return reduce(None, "PREPARE", {"at": BASE - 2000, "context": context()})[0]


def armed():
    return reduce(prepared(), "ARM", arm_payload())[0]


def admitted(side="BUY", qty="1", at=BASE + 2000, price="100", key="buy", state=None, **kwargs):
    return reduce(
        state or armed(),
        "ADMIT",
        {
            "at": at,
            "order_id": key,
            "side": side,
            "quantity": qty,
            "quote": quote(at, price, key="admit-" + key),
            **kwargs,
        },
    )[0]


def poll(state, at, qty="4", price="100", key="fill"):
    return reduce(state, "POLL", {"at": at, "quote": quote(at, price, qty, key)})


def cancel(state, at, order="buy"):
    return reduce(
        state,
        "CONTROL",
        {"at": at, "request_id": "cancel-" + order, "action": "cancel", "order_id": order},
    )[0]


def test_partial_cancel_buy_and_sell_oracles():
    state = admitted()
    assert state.wallet.usdt_reserved == Decimal("100.10")
    state, p1 = poll(state, BASE + 7000, "4", key="first")
    state, p2 = poll(state, BASE + 12000, "3", key="second")
    assert state.orders["buy"].filled == Decimal("0.7")
    state = cancel(state, BASE + 13000)
    w = state.wallet
    assert (w.usdt_free, w.btc_free, w.cost_basis, w.fees, w.usdt_reserved) == tuple(
        map(Decimal, ["9929.93", "0.7", "70.07", "0.07", "0"])
    )
    state = admitted("SELL", "0.7", BASE + 14000, "110", "sell", state)
    state, posts = poll(state, BASE + 19000, "2", "110", "sell-fill")
    state = cancel(state, BASE + 20000, "sell")
    assert state.wallet.quantity == Decimal("0.5") and state.wallet.cost_basis == Decimal("50.05")
    assert state.wallet.realized == Decimal("1.958") and state.wallet.btc_reserved == 0
    for group in [p1, p2, posts]:
        for asset in ["BTC", "USDT"]:
            assert sum(p.quantity for p in group if p.asset == asset) == 0
    assert armed().wallet.quantity == 0


def test_complete_fill_releases_rounding_and_idempotent_book_capacity():
    state = admitted()
    state, _ = poll(state, BASE + 7000, "4", key="a")
    state, _ = poll(state, BASE + 12000, "4", key="b")  # same book cannot be consumed again
    assert state.orders["buy"].filled == Decimal("0.4")
    state, _ = poll(state, BASE + 17000, "6", key="c")
    assert state.orders["buy"].state == "FILLED" and state.wallet.usdt_reserved == 0
    assert state.wallet.quantity == 1 and len(state.orders["buy"].fills) == 2


@pytest.mark.parametrize(
    "at,qty,price,expected",
    [
        (BASE + 2500, "10", "100", "OPEN"),
        (BASE + 7000, "0", "100", "OPEN"),
        (BASE + 7000, "0.000001", "100", "OPEN"),
        (BASE + 7000, "10", "101", "OPEN"),
        (BASE + 62000, "10", "100", "EXPIRED"),
    ],
)
def test_no_fill_without_capacity_delay_price_or_after_expiry(at, qty, price, expected):
    state, _ = poll(admitted(), at, qty, price)
    assert state.orders["buy"].state == expected and state.wallet.quantity == 0
    if expected == "EXPIRED":
        assert state.wallet.usdt_reserved == 0


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda q: q.update(observed_at="2028-01-01T00:00:00+00:00"), "STALE_OR_FUTURE_QUOTE"),
        (lambda q: q.update(observed_at="2020-01-01T00:00:00+00:00"), "STALE_OR_FUTURE_QUOTE"),
        (lambda q: q.update(ask="99"), "INVALID_QUOTE"),
        (lambda q: q.update(bid_qty="-1"), "INVALID_DECIMAL"),
        (lambda q: q.update(symbol="ETHUSDT"), "INVALID_SYMBOL"),
    ],
)
def test_invalid_quote_never_changes_original(mutation, error):
    state = admitted()
    before = state.data()
    q = quote(BASE + 7000, key="bad")
    mutation(q)
    with pytest.raises(PaperError, match=error):
        reduce(state, "POLL", {"at": BASE + 7000, "quote": q})
    assert state.data() == before


@pytest.mark.parametrize("action", ["pause", "kill", "disable"])
def test_controls_cancel_only_rest_and_keep_custody(action):
    state, _ = poll(admitted(), BASE + 7000)
    state, _ = reduce(state, "CONTROL", {"at": BASE + 8000, "request_id": action, "action": action})
    assert state.wallet.quantity == Decimal("0.4") and state.wallet.usdt_reserved == 0
    assert state.orders["buy"].state == "CANCELED"
    with pytest.raises(PaperError, match="CONTINUOUS_PAUSED"):
        admitted(at=BASE + 9000, state=state, key="new")


def test_unknown_keeps_reserve_until_explicit_cancel_after_reconciliation():
    state, _ = reduce(admitted(), "UNKNOWN", {"at": BASE + 3000})
    state, _ = reduce(state, "POLL", {"at": BASE + 70000})
    assert state.orders["buy"].state == "UNKNOWN" and state.wallet.usdt_reserved > 0
    with pytest.raises(PaperError, match="UNRESOLVED_ORDER"):
        reduce(state, "CONTROL", {"at": BASE + 71000, "request_id": "resume", "action": "resume"})
    state = cancel(state, BASE + 72000)
    assert state.wallet.usdt_reserved == 0 and state.wallet.btc_free == 0


@pytest.mark.parametrize(
    "quantity,error", [("100", "INSUFFICIENT_USDT"), ("20", "ENTRY_LIMIT"), ("0", "ZERO_QUANTITY")]
)
def test_entry_risk_cannot_be_bypassed(quantity, error):
    with pytest.raises(PaperError, match=error):
        admitted(qty=quantity)


def test_sell_requires_own_inventory_and_only_one_pending():
    with pytest.raises(PaperError, match="INSUFFICIENT_BTC"):
        admitted("SELL")
    with pytest.raises(PaperError, match="PENDING_INTENT"):
        admitted(state=admitted(), key="other", at=BASE + 3000)


def test_limit_touch_not_fill_and_terminal_cannot_refill():
    state = admitted(kind="LIMIT", limit_price="99")
    state, _ = poll(state, BASE + 7000, "100", "100")
    assert state.wallet.quantity == 0
    state = cancel(state, BASE + 8000)
    state, _ = poll(state, BASE + 13000, "100", "99")
    assert state.wallet.quantity == 0


def test_cycles_use_closed_data_and_missed_never_trade():
    state = armed()
    payload = {
        "at": BASE + 2000,
        "close": BASE,
        "quote": quote(BASE + 2000),
        "candles": normalized(rows(BASE, 51)),
    }
    state, _ = reduce(state, "CYCLE", payload)
    assert state.cycles[str(BASE)]["valid"] is True and state.cycles[str(BASE)]["target"] == "HOLD"
    state, _ = reduce(
        state, "MISSED", {"at": BASE + STEP + 100000, "close": BASE + STEP, "reason": "DATA_GAP"}
    )
    assert state.cycles[str(BASE + STEP)]["valid"] is False and not state.orders
    with pytest.raises(PaperError, match="SLOT_GAP"):
        reduce(state, "MISSED", {"at": BASE + 3 * STEP, "close": BASE + 3 * STEP})


@pytest.mark.parametrize("case", ["future", "gap", "warmup", "deadline", "metadata"])
def test_invalid_cycle_rejects_before_any_financial_change(case):
    state = armed()
    before = state.data()
    payload = {
        "at": BASE + 2000,
        "close": BASE,
        "quote": quote(BASE + 2000),
        "candles": normalized(rows(BASE, 51)),
    }
    if case == "future":
        payload["candles"] = normalized(rows(BASE + STEP, 51))
    elif case == "gap":
        payload["candles"].pop(20)
    elif case == "warmup":
        state.candles = []
        payload["candles"] = payload["candles"][-1:]
        before = state.data()
    elif case == "deadline":
        payload["at"] = BASE + 90001
    else:
        state.metadata_at = BASE - 4000000
        before = state.data()
    with pytest.raises(PaperError):
        reduce(state, "CYCLE", payload)
    assert state.data() == before


def test_risk_price_loss_blocks_partial_and_does_not_liquidate():
    state, _ = poll(admitted(), BASE + 7000)
    # A tighter permitted 0.1% loss limit makes this small position breach risk.
    state.parameters["max_daily_loss_fraction"] = "0.001"
    state, _ = poll(state, BASE + 12000, "10", "1", "crash")
    assert state.orders["buy"].state == "CANCELED" and state.wallet.quantity == Decimal("0.4")
    assert state.wallet.usdt_reserved == 0


def test_metadata_change_and_late_commit_pause_without_rewriting_cycles():
    state = armed()
    payload = {
        "at": BASE + 2000,
        "close": BASE,
        "quote": quote(BASE + 2000),
        "candles": normalized(rows(BASE, 51)),
    }
    state, _ = reduce(state, "CYCLE", payload)
    cycle = copy.deepcopy(state.cycles)
    state, _ = reduce(state, "LATE_COMMIT", {"at": BASE + 90001, "close": BASE})
    assert state.paused and state.late_slots == [BASE] and state.cycles == cycle
    changed = copy.deepcopy(state.rules)
    changed["filters"][0]["tickSize"] = "0.1"
    state, _ = reduce(state, "METADATA", {"at": BASE + 100000, "rules": changed})
    assert state.paused and state.rules != changed


def test_sma_signal_uses_pure_core_and_creates_bounded_order():
    state = armed()
    values = [100] * 50 + [110]
    state, _ = reduce(
        state,
        "CYCLE",
        {
            "at": BASE + 2000,
            "close": BASE,
            "quote": quote(BASE + 2000, price="110"),
            "candles": normalized(rows(BASE, 51, values)),
        },
    )
    assert state.cycles[str(BASE)]["target"] == "LONG" and state.pending() is not None
    assert state.wallet.usdt_reserved <= Decimal("1000")


@pytest.mark.parametrize("action", ["pause", "kill", "disable"])
def test_unknown_reserve_survives_protective_control(action):
    state, _ = reduce(admitted(), "UNKNOWN", {"at": BASE + 3000})
    reserved = state.wallet.usdt_reserved
    state, _ = reduce(state, "CONTROL", {"at": BASE + 4000, "request_id": action, "action": action})
    assert state.wallet.usdt_reserved == reserved and state.pending().state == "UNKNOWN"
    state = cancel(state, BASE + 5000)
    assert state.wallet.usdt_reserved == 0


def test_earlier_book_cannot_be_consumed_after_intervening_book():
    state, _ = poll(admitted(), BASE + 7000, "1", key="a")
    state, _ = poll(state, BASE + 12000, "2", key="b")
    state, _ = poll(state, BASE + 17000, "1", key="c")
    assert state.orders["buy"].filled == Decimal("0.3")


def test_custody_refusal_with_valid_data_keeps_fixed_denominator():
    state, _ = reduce(armed(), "CONTROL", {"at": BASE, "request_id": "pause", "action": "pause"})
    state, _ = reduce(
        state,
        "CYCLE",
        {
            "at": BASE + 2000,
            "close": BASE,
            "quote": quote(BASE + 2000),
            "candles": normalized(rows(BASE, 51)),
        },
    )
    assert state.cycles[str(BASE)]["valid"] is True
    assert state.cycles[str(BASE)]["reason"] == "CUSTODY" and not state.orders


def test_stricter_freshness_applies_even_without_directional_signal():
    state = armed()
    state.parameters["max_quote_age_ms"] = 1
    with pytest.raises(PaperError, match="STALE_OR_FUTURE_QUOTE"):
        reduce(
            state,
            "CYCLE",
            {
                "at": BASE + 2000,
                "close": BASE,
                "quote": quote(BASE + 1998),
                "candles": normalized(rows(BASE, 51)),
            },
        )
