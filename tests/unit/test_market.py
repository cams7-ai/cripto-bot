import copy
from decimal import Decimal

import pytest
from cripto_bot.domain.market import (
    STEP,
    MarketError,
    decimal_text,
    integer,
    parse_candle,
    parse_metadata,
    parse_page,
    parse_quote,
    parse_time,
    timestamp,
    window,
)

BASE = 1790812800000


@pytest.fixture
def kline():
    return [
        BASE,
        "100.00",
        "102",
        "99",
        "101",
        "1.5",
        BASE + STEP - 1,
        "151.5",
        5,
        "0.5",
        "50.5",
        "0",
    ]


def test_decimal_exact_and_normalized(kline):
    candle = parse_candle(kline)
    assert candle.open == "100" and candle.base_volume == "1.5"
    assert decimal_text("-0.0") == "0"
    assert Decimal(decimal_text("0.1")) + Decimal(decimal_text("0.2")) == Decimal("0.3")


@pytest.mark.parametrize(
    "value", [True, 1, 0.1, None, "NaN", "Infinity", "-1", "", "1e9999999", "1" * 81]
)
def test_decimal_invalid(value):
    with pytest.raises(MarketError):
        decimal_text(value)


@pytest.mark.parametrize("value", [True, -1, 1.0, None, 253402300800000])
def test_integer_invalid(value):
    with pytest.raises(MarketError):
        integer(value)


@pytest.mark.parametrize(
    "index,value",
    [
        (0, BASE + 1),
        (6, BASE + STEP),
        (1, "0"),
        (2, "98"),
        (5, "-1"),
        (7, 1.2),
        (8, True),
        (9, "NaN"),
        (11, 1),
    ],
)
def test_bad_candle(kline, index, value):
    kline[index] = value
    with pytest.raises(MarketError):
        parse_candle(kline)


@pytest.mark.parametrize("value", [{}, [1], None])
def test_bad_candle_shape(value):
    with pytest.raises(MarketError):
        parse_candle(value)


def test_time_boundary_open_and_closed(kline):
    assert parse_page([kline], BASE + STEP - 1) == ([], 1)
    assert len(parse_page([kline], BASE + STEP)[0]) == 1
    later = copy.deepcopy(kline)
    later[0] += STEP
    later[6] += STEP
    with pytest.raises(MarketError, match="UNORDERED"):
        parse_page([later, kline], BASE + 3 * STEP)


@pytest.mark.parametrize(
    "text",
    [
        "2026-10-01",
        "2026-10-01T00:00:00",
        "bad",
        "2026-10-01T00:01:00Z",
        "2026-10-01T00:00:00.000001Z",
        "1960-01-01T00:00:00Z",
    ],
)
def test_bad_window(text):
    with pytest.raises(MarketError):
        timestamp(text)


def test_utc_and_window():
    assert timestamp("2026-09-30T21:00:00-03:00") == BASE
    window(BASE, BASE + STEP, 1)
    for start, end, capacity in [
        (BASE, BASE, 1),
        (BASE + 1, BASE + STEP, 1),
        (BASE, BASE + 2 * STEP, 1),
    ]:
        with pytest.raises(MarketError):
            window(start, end, capacity)


def test_metadata_quote_and_clock():
    info = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "isSpotTradingAllowed": True,
                "filters": [{"filterType": "NEW"}],
                "futureField": True,
            }
        ]
    }
    assert parse_metadata(info) == info
    assert parse_time({"serverTime": BASE}) == BASE
    quote = {
        "symbol": "BTCUSDT",
        "bidPrice": "100.00",
        "askPrice": "100.01",
        "bidQty": "0",
        "askQty": "1",
    }
    assert parse_quote(quote)["market_timestamp"] is None
    quote["bidPrice"] = "101"
    with pytest.raises(MarketError, match="CROSSED"):
        parse_quote(quote)
    for changed in [
        {"symbols": []},
        {"symbols": [{}]},
        {"symbols": [{**info["symbols"][0], "filters": [{}]}]},
    ]:
        with pytest.raises(MarketError):
            parse_metadata(changed)
    with pytest.raises(MarketError):
        parse_quote({"symbol": "ETHUSDT"})


def test_invalid_metadata_object_and_window_end():
    with pytest.raises(MarketError):
        parse_time({1: "invalid key"})
    with pytest.raises(MarketError):
        window(BASE, BASE + STEP + 1, 2)
