"""Modelo sintetico de precos e subconjunto explicito de filtros."""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from math import lcm

from cripto_bot.domain.market import sequence
from cripto_bot.domain.portfolio.models import (
    PaperError,
    Plan,
    Q,
    bounded,
    identifier,
    instant,
    money,
    number,
    require_object,
)


def observation(value: object, at: int, max_age: int) -> tuple[dict[str, object], Decimal]:
    quote = require_object(value)
    if quote.get("symbol") != "BTCUSDT":
        raise PaperError("INVALID_SYMBOL")
    observed = instant(quote.get("observed_at"))
    identifier(quote.get("quote_id"))
    if not observed <= at <= observed + max_age:
        raise PaperError("STALE_OR_FUTURE_QUOTE")
    if quote.get("kind") == "REFERENCE":
        mark = number(quote.get("price"))
    elif quote.get("kind") == "BID_ASK":
        mark = number(quote.get("bid"))
        ask = number(quote.get("ask"))
        if ask < mark or number(quote.get("bid_qty")) < 0 or number(quote.get("ask_qty")) < 0:
            raise PaperError("INVALID_QUOTE")
    else:
        raise PaperError("INVALID_QUOTE")
    if mark <= 0 or (quote.get("kind") == "BID_ASK" and number(quote.get("ask")) <= 0):
        raise PaperError("INVALID_QUOTE")
    return quote, mark


def filters(rules: dict[str, object]) -> dict[str, dict[str, object]]:
    if (
        rules.get("symbol") != "BTCUSDT"
        or rules.get("status") != "TRADING"
        or rules.get("isSpotTradingAllowed") is not True
        or rules.get("baseAsset") != "BTC"
        or rules.get("quoteAsset") != "USDT"
    ):
        raise PaperError("INVALID_SYMBOL")
    result: dict[str, dict[str, object]] = {}
    for item in sequence(rules.get("filters")):
        entry = require_object(item)
        name = entry.get("filterType")
        if name not in ("PRICE_FILTER", "LOT_SIZE", "MARKET_LOT_SIZE", "MIN_NOTIONAL", "NOTIONAL"):
            raise PaperError("UNSUPPORTED_FILTER")
        assert isinstance(name, str)
        if name in result:
            raise PaperError("DUPLICATE_FILTER")
        result[name] = entry
    if not {"PRICE_FILTER", "LOT_SIZE", "MARKET_LOT_SIZE"}.issubset(result) or not (
        set(result) & {"MIN_NOTIONAL", "NOTIONAL"}
    ):
        raise PaperError("MISSING_FILTER")
    return result


def range_check(value: Decimal, rule: dict[str, object], minimum: str, maximum: str) -> None:
    low, high = number(rule.get(minimum)), number(rule.get(maximum))
    if (low > 0 and value < low) or (high > 0 and value > high):
        raise PaperError("FILTER_RANGE")


def make_plan(
    event: dict[str, object], action: str, parameters: dict[str, object], rules: dict[str, object]
) -> Plan:
    with localcontext() as context:
        context.prec = 80
        side = event.get("side")
        if side not in ("BUY", "SELL"):
            raise PaperError("INVALID_SIDE")
        at = instant(event.get("fill_at"))
        decision = instant(event.get("decision_at"))
        quote, mark = observation(
            event.get("observation"), at, int(str(parameters["max_quote_age_ms"]))
        )
        observed = instant(quote["observed_at"])
        if not decision < observed <= at or instant(event.get("timestamp")) != observed:
            raise PaperError("INVALID_EXECUTION_TIME")
        supplied = filters(rules)
        requested = number(event.get("quantity"))
        steps = []
        for name in ("LOT_SIZE", "MARKET_LOT_SIZE"):
            step = number(supplied[name].get("stepSize"))
            if step:
                steps.append(int(step / Q))
        step = Decimal(lcm(*steps)) * Q if steps else Q
        quantity = (requested / step).to_integral_value(rounding=ROUND_FLOOR) * step
        if quantity <= 0:
            raise PaperError("ZERO_QUANTITY")
        for name in ("LOT_SIZE", "MARKET_LOT_SIZE"):
            range_check(quantity, supplied[name], "minQty", "maxQty")
        bps = Decimal("10000")
        slippage = number(parameters["slippage_bps"]) / bps
        sign = Decimal(1 if side == "BUY" else -1)
        if quote["kind"] == "REFERENCE":
            price = mark * (1 + sign * (number(parameters["spread_bps"]) / (bps * 2) + slippage))
        else:
            reference = number(quote["ask"] if side == "BUY" else quote["bid"])
            price = reference * (1 + sign * slippage)
        tick = number(supplied["PRICE_FILTER"].get("tickSize"))
        tick = tick if tick else Q
        price = (price / tick).to_integral_value(
            rounding=ROUND_CEILING if side == "BUY" else ROUND_FLOOR
        ) * tick
        if price <= 0:
            raise PaperError("INVALID_PRICE")
        range_check(price, supplied["PRICE_FILTER"], "minPrice", "maxPrice")
        bounded(price)
        bounded(quantity)
        notional = money(price * quantity, up=side == "BUY")
        if notional <= 0:
            raise PaperError("ZERO_NOTIONAL")
        for name in ("MIN_NOTIONAL", "NOTIONAL"):
            rule = supplied.get(name)
            if rule is None:
                continue
            minimum_flag = "applyToMarket" if name == "MIN_NOTIONAL" else "applyMinToMarket"
            if rule.get(minimum_flag) is True and notional < number(rule.get("minNotional")):
                raise PaperError("MIN_NOTIONAL")
            if name == "NOTIONAL" and rule.get("applyMaxToMarket") is True:
                maximum = number(rule.get("maxNotional"))
                if maximum > 0 and notional > maximum:
                    raise PaperError("MAX_NOTIONAL")
        fee = money(notional * number(parameters["fee_bps"]) / bps, up=True)
        if side == "SELL" and fee >= notional:
            raise PaperError("NON_POSITIVE_PROCEEDS")
        return Plan(
            action, str(side), requested, quantity, price, notional, fee, mark, at, observed
        )
