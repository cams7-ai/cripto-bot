from copy import deepcopy
from decimal import Decimal as D

import pytest
from cripto_bot.domain.portfolio.models import PaperError
from cripto_bot.domain.risk.execution import make_plan

from tests.paper_factories import context, order


def test_prices_fees_steps_and_bid_ask(tmp_path):
    ctx = context(tmp_path)
    buy = make_plan(order(), "buy", ctx["parameters"], ctx["rules"])
    sell = make_plan(order(2), "sell", ctx["parameters"], ctx["rules"])
    assert (buy.price, buy.fee) == (D("100.10"), D("0.1001"))
    assert (sell.price, sell.fee) == (D("109.89"), D("0.10989"))
    rules = deepcopy(ctx["rules"])
    rules["filters"][1]["stepSize"] = "0.000002"
    rules["filters"][2]["stepSize"] = "0.000003"
    event = order(quantity="1.000001")
    plan = make_plan(event, "odd-step", ctx["parameters"], rules)
    assert plan.quantity == D("0.999996")
    event["observation"] = {
        "quote_id": "ba",
        "kind": "BID_ASK",
        "symbol": "BTCUSDT",
        "observed_at": event["timestamp"],
        "bid": "99.99",
        "ask": "100",
        "bid_qty": "1",
        "ask_qty": "1",
    }
    plan = make_plan(event, "bid-ask", ctx["parameters"], ctx["rules"])
    assert plan.price == D("100.05")


@pytest.mark.parametrize(
    "change", ["unknown", "symbol", "status", "spot", "qty", "notional", "price", "missing"]
)
def test_filter_rejection(tmp_path, change):
    ctx = context(tmp_path)
    rules = ctx["rules"]
    if change == "unknown":
        rules["filters"].append({"filterType": "UNSUPPORTED"})
    elif change == "symbol":
        rules["symbol"] = "ETHUSDT"
    elif change == "status":
        rules["status"] = "BREAK"
    elif change == "spot":
        rules["isSpotTradingAllowed"] = False
    elif change == "qty":
        rules["filters"][2]["maxQty"] = "0.5"
    elif change == "notional":
        rules["filters"][3]["minNotional"] = "1000"
    elif change == "price":
        rules["filters"][0]["maxPrice"] = "100"
    else:
        rules["filters"].pop()
    with pytest.raises(PaperError):
        make_plan(order(), "bad", ctx["parameters"], rules)


def test_notional_flags_disabled_rules_and_zero_quantity(tmp_path):
    ctx = context(tmp_path)
    rules = ctx["rules"]
    rules["filters"][3] = {
        "filterType": "NOTIONAL",
        "minNotional": "1000",
        "maxNotional": "1",
        "applyMinToMarket": False,
        "applyMaxToMarket": False,
    }
    make_plan(order(), "disabled", ctx["parameters"], rules)
    rules["filters"][3]["applyMaxToMarket"] = True
    with pytest.raises(PaperError, match="MAX_NOTIONAL"):
        make_plan(order(), "max", ctx["parameters"], rules)
    with pytest.raises(PaperError, match="ZERO_QUANTITY"):
        make_plan(order(quantity="0.00000001"), "zero", ctx["parameters"], rules)


@pytest.mark.parametrize("change", ["stale", "future", "decision", "symbol", "crossed"])
def test_quote_time_and_shape(tmp_path, change):
    ctx = context(tmp_path)
    event = order()
    if change == "stale":
        event["fill_at"] = "2026-10-01T00:15:06.001Z"
    elif change == "future":
        event["observation"]["observed_at"] = "2026-10-01T00:15:02Z"
    elif change == "decision":
        event["decision_at"] = event["timestamp"]
    elif change == "symbol":
        event["observation"]["symbol"] = "ETHUSDT"
    else:
        event["observation"] = {
            "quote_id": "ba",
            "kind": "BID_ASK",
            "symbol": "BTCUSDT",
            "observed_at": event["timestamp"],
            "bid": "101",
            "ask": "100",
            "bid_qty": "1",
            "ask_qty": "1",
        }
    with pytest.raises(PaperError):
        make_plan(event, "bad-time", ctx["parameters"], ctx["rules"])


def test_rounding_does_not_create_zero_proceeds(tmp_path):
    ctx = context(tmp_path)
    rules = ctx["rules"]
    for rule in rules["filters"]:
        for key in list(rule):
            if key not in ("filterType", "applyToMarket"):
                rule[key] = "0"
    params = {**ctx["parameters"], "slippage_bps": "0", "spread_bps": "0"}
    event = order(2, quantity="1")
    event["observation"]["price"] = "0.001"
    rules["filters"][0]["tickSize"] = "0.01"
    with pytest.raises(PaperError, match="INVALID_PRICE"):
        make_plan(event, "zero-price", params, rules)
    rules["filters"][0]["tickSize"] = "0.00000001"
    event["quantity"] = "0.00000001"
    event["observation"]["price"] = "0.00000001"
    with pytest.raises(PaperError, match="ZERO_NOTIONAL"):
        make_plan(event, "zero-notional", params, rules)
    event["quantity"] = "1"
    with pytest.raises(PaperError, match="NON_POSITIVE_PROCEEDS"):
        make_plan(event, "zero-net", params, rules)
    params["fee_bps"] = "0"
    assert make_plan(event, "positive-net", params, rules).notional == D("0.00000001")


def test_notional_zero_maximum_and_exact_age_boundary(tmp_path):
    ctx = context(tmp_path)
    ctx["rules"]["filters"].append(
        {
            "filterType": "NOTIONAL",
            "minNotional": "0",
            "maxNotional": "0",
            "applyMinToMarket": True,
            "applyMaxToMarket": True,
        }
    )
    event = order(fill_at="2026-10-01T00:15:06Z")
    assert make_plan(event, "age-boundary", ctx["parameters"], ctx["rules"]).price == D("100.10")


def test_model_rejects_price_overflow_before_reservation(tmp_path):
    ctx = context(tmp_path)
    ctx["rules"]["filters"][0]["maxPrice"] = "0"
    event = order()
    event["observation"]["price"] = "999999999999999999"
    with pytest.raises(PaperError, match="DECIMAL_RANGE"):
        make_plan(event, "overflow", ctx["parameters"], ctx["rules"])
