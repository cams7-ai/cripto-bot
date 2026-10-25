"""Dados Spot puros e contratos estritos para a coleta local."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

from cripto_bot.domain.models import parse_decimal

STEP = 900_000
SCHEMA = 1
PARSER = "spot-market-v1"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class MarketError(ValueError):
    """Codigo local seguro; nao transportar mensagens de servidores."""


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise MarketError("INVALID_OBJECT")
    return cast(dict[str, object], value)


def sequence(value: object) -> list[object]:
    if not isinstance(value, list):
        raise MarketError("INVALID_ARRAY")
    return cast(list[object], value)


def integer(value: object) -> int:
    if type(value) is not int or value < 0 or value > 253402300799999:
        raise MarketError("INVALID_INTEGER")
    return value


def decimal_text(value: object, *, positive: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 80
        or not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", value)
    ):
        raise MarketError("INVALID_DECIMAL")
    number = parse_decimal(value)
    if number < 0 or (positive and number == 0):
        raise MarketError("INVALID_DECIMAL")
    result = format(number, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if number == 0 else result


def timestamp(value: str) -> int:
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError
        delta = instant.astimezone(UTC) - EPOCH
        if delta.microseconds % 1000:
            raise ValueError
        result = delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000
        integer(result)
        if result % STEP:
            raise ValueError
        return result
    except (ValueError, OverflowError):
        raise MarketError("INVALID_WINDOW") from None


def iso(milliseconds: int) -> str:
    return (EPOCH + timedelta(milliseconds=milliseconds)).isoformat()


def window(start: int, end: int, capacity: int) -> None:
    integer(start)
    integer(end)
    if start >= end or start % STEP or end % STEP or (end - start) // STEP > capacity:
        raise MarketError("INVALID_WINDOW")


@dataclass(frozen=True)
class Candle:
    open_time: int
    close_time: int
    open: str
    high: str
    low: str
    close: str
    base_volume: str
    quote_volume: str
    trades: int
    symbol: str = "BTCUSDT"
    interval: str = "15m"

    def record(self) -> dict[str, object]:
        return asdict(self)


def parse_candle(value: object) -> Candle:
    row = sequence(value)
    if len(row) != 12:
        raise MarketError("INVALID_KLINE")
    opened, closed = integer(row[0]), integer(row[6])
    if opened % STEP or closed != opened + STEP - 1:
        raise MarketError("INVALID_KLINE_TIME")
    prices = [decimal_text(item, positive=True) for item in row[1:5]]
    o, h, low, c = map(Decimal, prices)
    if not low <= min(o, c) <= max(o, c) <= h:
        raise MarketError("INVALID_OHLC")
    # Validar inclusive campos nao utilizados pelo modelo normalizado.
    for item in row[9:11]:
        decimal_text(item)
    if not isinstance(row[11], str):
        raise MarketError("INVALID_KLINE")
    return Candle(
        opened,
        closed,
        prices[0],
        prices[1],
        prices[2],
        prices[3],
        decimal_text(row[5]),
        decimal_text(row[7]),
        integer(row[8]),
    )


def parse_page(value: object, server_time: int) -> tuple[list[Candle], int]:
    result: list[Candle] = []
    ignored = 0
    previous = -1
    for row in sequence(value):
        candle = parse_candle(row)
        if candle.open_time < previous:
            raise MarketError("UNORDERED_PAGE")
        previous = candle.open_time
        if candle.close_time >= server_time:
            ignored += 1
        else:
            result.append(candle)
    return result, ignored


def parse_time(value: object) -> int:
    return integer(mapping(value).get("serverTime"))


def parse_metadata(value: object) -> dict[str, object]:
    data = mapping(value)
    symbols = sequence(data.get("symbols"))
    if len(symbols) != 1:
        raise MarketError("INVALID_SYMBOL")
    item = mapping(symbols[0])
    if (
        item.get("symbol") != "BTCUSDT"
        or item.get("status") != "TRADING"
        or item.get("isSpotTradingAllowed") is not True
        or item.get("baseAsset") != "BTC"
        or item.get("quoteAsset") != "USDT"
    ):
        raise MarketError("INVALID_SYMBOL")
    for entry in sequence(item.get("filters")):
        if not isinstance(mapping(entry).get("filterType"), str):
            raise MarketError("INVALID_FILTER")
    return data


def parse_quote(value: object) -> dict[str, object]:
    data = mapping(value)
    if data.get("symbol") != "BTCUSDT":
        raise MarketError("INVALID_SYMBOL")
    result: dict[str, object] = {"symbol": "BTCUSDT", "market_timestamp": None}
    for key in ("bidPrice", "askPrice", "bidQty", "askQty"):
        result[key] = decimal_text(data.get(key), positive=key.endswith("Price"))
    if Decimal(str(result["bidPrice"])) > Decimal(str(result["askPrice"])):
        raise MarketError("CROSSED_QUOTE")
    return result
