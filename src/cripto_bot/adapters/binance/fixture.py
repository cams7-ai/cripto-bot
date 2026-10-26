"""Fixture sintetica usa as mesmas rotas e parsers do adapter publico."""

from pathlib import Path

import httpx

from cripto_bot.adapters.binance.public import Budget, PublicSource
from cripto_bot.application.ports import RawResponse
from cripto_bot.domain.market import MarketError, digest, mapping, sequence


def fixture_source(path: Path, budget: Budget) -> tuple[str, PublicSource]:
    try:
        with path.open("rb") as stream:
            body = stream.read(5 * 1024 * 1024 + 1)
    except OSError:
        raise MarketError("FIXTURE_UNREADABLE") from None
    if len(body) > 5 * 1024 * 1024:
        raise MarketError("FIXTURE_TOO_LARGE")
    data = mapping(RawResponse("fixture", {}, body, "").json())
    if set(data) != {"server_time", "exchange_info", "klines", "book_ticker"}:
        raise MarketError("INVALID_FIXTURE")

    def handler(request: httpx.Request) -> httpx.Response:
        route = request.url.path
        if route == "/api/v3/klines":
            start, end, limit = (
                int(request.url.params[k]) for k in ("startTime", "endTime", "limit")
            )
            # Somente simulacao da consulta: validacao semantica ocorre no parser comum.
            rows = [
                row
                for row in sequence(data["klines"])
                if isinstance(row, list) and row and type(row[0]) is int and start <= row[0] <= end
            ]
            result: object = rows[:limit]
        else:
            keys = {
                "/api/v3/time": "server_time",
                "/api/v3/exchangeInfo": "exchange_info",
                "/api/v3/ticker/bookTicker": "book_ticker",
            }
            result = data[keys[route]]
        return httpx.Response(200, json=result)

    return "fixture:" + digest(body) + ":v1", PublicSource(
        budget=budget, transport=httpx.MockTransport(handler)
    )
