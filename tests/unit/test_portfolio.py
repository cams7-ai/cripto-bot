from copy import deepcopy
from decimal import Decimal as D
from decimal import getcontext

import pytest

from cripto_bot.domain.portfolio.engine import transition
from cripto_bot.domain.portfolio.models import (
    PaperError,
    Posting,
    Wallet,
    number,
    validate_postings,
)
from tests.paper_factories import initialized, order


def test_roundtrip_ledger_and_partial_sale(tmp_path):
    ctx, wallet = initialized(tmp_path)
    parameters, rules = ctx["parameters"], ctx["rules"]
    before = getcontext().copy()
    wallet, postings = transition(wallet, "ORDER", order(), parameters, rules)
    intent = wallet.intents[order()["action_key"]]
    assert wallet.usdt_reserved == D("100.20010000") and wallet.usdt_free == D("9899.7999")
    validate_postings(postings)
    wallet, postings = transition(
        wallet,
        "FILL",
        {"action_key": intent.plan.action, "fill_id": intent.plan.fill_id},
        parameters,
        rules,
    )
    assert wallet.btc_free == 1 and wallet.cost_basis == D("100.2001")
    validate_postings(postings)
    first = order(2, quantity="0.4", action_id="partial")
    wallet, _ = transition(wallet, "ORDER", first, parameters, rules)
    plan = wallet.intents[first["action_key"]].plan
    wallet, _ = transition(
        wallet, "FILL", {"action_key": plan.action, "fill_id": plan.fill_id}, parameters, rules
    )
    assert wallet.btc_free == D("0.6") and wallet.cost_basis == D("60.12006")
    last = order(
        2,
        quantity="0.6",
        action_id="last",
        timestamp="2026-10-01T00:45:01Z",
        decision_at="2026-10-01T00:45:00Z",
        fill_at="2026-10-01T00:45:01.001Z",
    )
    last["observation"]["observed_at"] = last["timestamp"]
    wallet, _ = transition(wallet, "ORDER", last, parameters, rules)
    plan = wallet.intents[last["action_key"]].plan
    wallet, _ = transition(
        wallet, "FILL", {"action_key": plan.action, "fill_id": plan.fill_id}, parameters, rules
    )
    assert wallet.quantity == 0 and wallet.cost_basis == 0
    assert wallet.usdt_free == D("10009.58001000") and wallet.realized == D("9.58001000")
    assert wallet.fees == D("0.20999000")
    assert getcontext().prec == before.prec and getcontext().rounding == before.rounding


@pytest.mark.parametrize("kind,state", [("CANCEL", "CANCELED"), ("REJECT", "REJECTED")])
def test_release_without_fill(tmp_path, kind, state):
    ctx, wallet = initialized(tmp_path)
    original = wallet.data()
    wallet, _ = transition(wallet, "ORDER", order(), ctx["parameters"], ctx["rules"])
    action = order()["action_key"]
    wallet, postings = transition(
        wallet, kind, {"action_key": action}, ctx["parameters"], ctx["rules"]
    )
    assert wallet.usdt_free == D("10000") and wallet.usdt_reserved == 0
    assert wallet.intents[action].state == state
    validate_postings(postings)
    assert original["usdt_free"] == "10000.00000000"
    with pytest.raises(PaperError, match="NOT_RESERVED"):
        transition(
            wallet,
            "FILL",
            {"action_key": action, "fill_id": "wrong"},
            ctx["parameters"],
            ctx["rules"],
        )


@pytest.mark.parametrize(
    "value", [1, True, 0.1, None, "NaN", "Infinity", "1e-8", "-1", "0.000000001", "1" * 19]
)
def test_financial_input_is_bounded_string(value):
    with pytest.raises(PaperError):
        number(value)


def test_conservation_negative_and_position_invariants():
    with pytest.raises(PaperError):
        validate_postings([Posting("USDT", "free", D(1))])
    with pytest.raises(PaperError):
        validate_postings([Posting("BNB", "free", D(0))])
    with pytest.raises(PaperError):
        Wallet(usdt_free=D(-1)).validate()
    with pytest.raises(PaperError):
        Wallet(cost_basis=D(1)).validate()
    with pytest.raises(PaperError):
        Wallet(usdt_reserved=D(1)).validate()


def test_duplicate_pending_and_fill_mismatch(tmp_path):
    ctx, wallet = initialized(tmp_path)
    wallet, _ = transition(wallet, "ORDER", order(), ctx["parameters"], ctx["rules"])
    with pytest.raises(PaperError, match="PENDING"):
        transition(wallet, "ORDER", order(), ctx["parameters"], ctx["rules"])
    with pytest.raises(PaperError, match="FILL_CONFLICT"):
        transition(
            wallet,
            "FILL",
            {"action_key": order()["action_key"], "fill_id": "wrong"},
            ctx["parameters"],
            ctx["rules"],
        )
    extra = deepcopy(wallet)
    extra.usdt_reserved += 1
    extra.usdt_free -= 1
    extra.intents[order()["action_key"]].reserved += 1
    plan = extra.intents[order()["action_key"]].plan
    final, postings = transition(
        extra,
        "FILL",
        {"action_key": plan.action, "fill_id": plan.fill_id},
        ctx["parameters"],
        ctx["rules"],
    )
    assert final.usdt_free == D("9899.7999") and final.usdt_reserved == 0
    validate_postings(postings)
