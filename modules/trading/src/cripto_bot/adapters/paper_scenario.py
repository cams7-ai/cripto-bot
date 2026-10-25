"""Leitura limitada e validação estrita do roteiro, sem efeitos financeiros."""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from cripto_bot.domain.market import mapping
from cripto_bot.domain.portfolio.models import PaperError, fingerprint, identifier, instant, number


class QuoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    quote_id: str
    kind: Literal["REFERENCE", "BID_ASK"]
    symbol: Literal["BTCUSDT"]
    observed_at: str
    price: str | None = None
    bid: str | None = None
    ask: str | None = None
    bid_qty: str | None = None
    ask_qty: str | None = None


class EventInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    event_id: str
    type: Literal["DAY_START", "MARK", "ORDER"]
    timestamp: str
    observation: QuoteInput
    action_id: str | None = None
    side: Literal["BUY", "SELL"] | None = None
    quantity: str | None = None
    decision_at: str | None = None
    fill_at: str | None = None


class ScenarioInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    schema_version: Literal[1]
    scenario_id: str
    symbol: Literal["BTCUSDT"]
    rules: dict[str, object]
    events: list[EventInput]


def validate_rules(rules: dict[str, object]) -> None:
    required = {
        "rule_version",
        "symbol",
        "status",
        "isSpotTradingAllowed",
        "baseAsset",
        "quoteAsset",
        "filters",
    }
    if set(rules) != required or type(rules["isSpotTradingAllowed"]) is not bool:
        raise PaperError("INVALID_RULES")
    identifier(rules["rule_version"])
    if not isinstance(rules["filters"], list):
        raise PaperError("INVALID_RULES")
    fields = {
        "PRICE_FILTER": ({"minPrice", "maxPrice", "tickSize"}, set()),
        "LOT_SIZE": ({"minQty", "maxQty", "stepSize"}, set()),
        "MARKET_LOT_SIZE": ({"minQty", "maxQty", "stepSize"}, set()),
        "MIN_NOTIONAL": ({"minNotional"}, {"applyToMarket"}),
        "NOTIONAL": ({"minNotional", "maxNotional"}, {"applyMinToMarket", "applyMaxToMarket"}),
    }
    for item in rules["filters"]:
        rule = mapping(item)
        name = rule.get("filterType")
        if not isinstance(name, str):
            raise PaperError("INVALID_RULES")
        if name in fields:
            decimal_keys, bool_keys = fields[name]
            if set(rule) != {"filterType"} | decimal_keys | bool_keys:
                raise PaperError("INVALID_RULES")
            for key in decimal_keys:
                number(rule[key])
            if any(type(rule[key]) is not bool for key in bool_keys):
                raise PaperError("INVALID_RULES")


def load_scenario(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            body = stream.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError

        def invalid(value: str) -> None:
            raise ValueError

        data: object = json.loads(body, parse_constant=invalid)
        if type(mapping(data).get("schema_version")) is not int:
            raise ValueError
        parsed = ScenarioInput.model_validate(data)
        identifier(parsed.scenario_id)
        validate_rules(parsed.rules)
        if not 1 <= len(parsed.events) <= 1000:
            raise ValueError
        previous = 0
        identities: dict[str, str] = {}
        actions: set[str] = set()
        quotes: dict[str, str] = {}
        days: set[int] = set()
        for event in parsed.events:
            identifier(event.event_id)
            now = instant(event.timestamp)
            if now < previous:
                raise ValueError
            previous = now
            quote = event.observation
            identifier(quote.quote_id)
            instant(quote.observed_at)
            quote_hash = fingerprint(quote.model_dump(exclude_none=True))
            if quote.quote_id in quotes and quotes[quote.quote_id] != quote_hash:
                raise ValueError
            quotes[quote.quote_id] = quote_hash
            values = (
                (quote.price,)
                if quote.kind == "REFERENCE"
                else (quote.bid, quote.ask, quote.bid_qty, quote.ask_qty)
            )
            for value in values:
                number(value)
            if quote.kind == "REFERENCE" and any(
                v is not None for v in (quote.bid, quote.ask, quote.bid_qty, quote.ask_qty)
            ):
                raise ValueError
            if quote.kind == "BID_ASK" and quote.price is not None:
                raise ValueError
            payload_hash = fingerprint(event.model_dump(exclude_none=True))
            if event.event_id in identities:
                raise ValueError
            identities[event.event_id] = payload_hash
            if event.type == "ORDER":
                identifier(event.action_id)
                if event.action_id in actions or event.side is None:
                    raise ValueError
                actions.add(str(event.action_id))
                number(event.quantity)
                instant(event.decision_at)
                previous = max(previous, instant(event.fill_at))
            else:
                if any(
                    v is not None
                    for v in (
                        event.action_id,
                        event.side,
                        event.quantity,
                        event.decision_at,
                        event.fill_at,
                    )
                ):
                    raise ValueError
                if event.type == "DAY_START":
                    if now % 86400000 or now in days:
                        raise ValueError
                    days.add(now)
        return parsed.model_dump(exclude_none=True)
    except (OSError, ValueError, ValidationError, KeyError):
        raise PaperError("INVALID_SCENARIO") from None
