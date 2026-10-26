"""Bounded partial execution uses the existing pure pricing/filter contracts."""

from decimal import ROUND_FLOOR, Decimal

from cripto_core.contracts import Plan
from cripto_core.errors import PaperError
from cripto_core.execution import filters, make_plan, observation, quantity_step
from cripto_core.numeric import ZERO, number
from cripto_core.serialization import fingerprint
from cripto_core.validation import identifier

from cripto_bot.domain.continuous.models import Order
from cripto_bot.domain.market import iso


def integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise PaperError("INVALID_CONTINUOUS_TIME")
    return value


def quote_at(value: object, at: int, max_age_ms: int = 5000) -> dict[str, object]:
    quote, _ = observation(value, at, max_age_ms)
    if quote.get("kind") != "BID_ASK":
        raise PaperError("CONTINUOUS_REQUIRES_BOOK")
    return quote


def signature(quote: dict[str, object]) -> str:
    return fingerprint({k: quote[k] for k in ["bid", "ask", "bid_qty", "ask_qty"]})


def plan_for(
    side: str,
    quantity: Decimal,
    quote: dict[str, object],
    at: int,
    decision_at: int,
    parameters: dict[str, object],
    rules: dict[str, object],
    key: str,
) -> Plan:
    return make_plan(
        {
            "side": side,
            "quantity": format(quantity, "f"),
            "observation": quote,
            "fill_at": iso(at),
            "decision_at": iso(decision_at),
            "timestamp": quote["observed_at"],
        },
        identifier(key),
        parameters,
        rules,
    )


def partial_plan(
    order: Order,
    quote: dict[str, object],
    at: int,
    parameters: dict[str, object],
    rules: dict[str, object],
) -> Plan | None:
    if at < order.earliest_at or at >= order.expires_at:
        return None
    quote = quote_at(quote, at)
    key = str(quote["quote_id"])
    if key in order.quote_ids or signature(quote) in order.book_signatures:
        return None
    amount = min(
        order.requested - order.filled,
        number(quote["ask_qty" if order.side == "BUY" else "bid_qty"]) * Decimal("0.10"),
    )
    step = quantity_step(filters(rules))
    amount = (amount / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if amount <= ZERO:
        return None
    try:
        plan = plan_for(
            order.side,
            amount,
            quote,
            at,
            order.created_at,
            parameters,
            rules,
            fingerprint({"order": order.order_id, "quote": key}),
        )
    except PaperError as error:
        if str(error) in {"ZERO_QUANTITY", "MIN_NOTIONAL", "FILTER_RANGE", "ZERO_NOTIONAL"}:
            return None
        raise
    if (order.side == "BUY" and plan.price > order.cap) or (
        order.side == "SELL" and plan.price < order.cap
    ):
        return None
    return plan
