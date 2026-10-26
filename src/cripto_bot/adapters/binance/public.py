"""Quatro GETs publicos, com host fixo e transporte substituivel."""

import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from cripto_bot.application.ports import RawResponse
from cripto_bot.domain.market import MarketError
from cripto_bot.observability.logging import utc_now, utc_timestamp

HOST = "https://data-api.binance.vision"
MAX_BYTES = 5 * 1024 * 1024
ROUTES = {"/api/v3/time", "/api/v3/exchangeInfo", "/api/v3/klines", "/api/v3/ticker/bookTicker"}


def guard_request(request: httpx.Request) -> None:
    url = request.url
    params = dict(url.params)
    expected = {
        "/api/v3/time": set(),
        "/api/v3/exchangeInfo": {"symbol"},
        "/api/v3/ticker/bookTicker": {"symbol"},
        "/api/v3/klines": {"symbol", "interval", "startTime", "endTime", "limit", "timeZone"},
    }
    if (
        request.method != "GET"
        or url.scheme != "https"
        or url.host != "data-api.binance.vision"
        or url.port not in (None, 443)
        or url.username
        or url.password
        or url.path not in expected
        or set(params) != expected[url.path]
        or len(url.params.multi_items()) != len(params)
        or any(
            key in request.headers
            for key in ("authorization", "x-mbx-apikey", "proxy-authorization")
        )
    ):
        raise MarketError("FORBIDDEN_REQUEST")
    if url.path != "/api/v3/time" and params["symbol"] != "BTCUSDT":
        raise MarketError("FORBIDDEN_REQUEST")
    if url.path == "/api/v3/klines":
        if (
            params["interval"] != "15m"
            or params["timeZone"] != "0"
            or not all(
                params[key].isascii() and params[key].isdigit()
                for key in ("startTime", "endTime", "limit")
            )
            or int(params["startTime"]) > int(params["endTime"])
            or not 1 <= int(params["limit"]) <= 1000
        ):
            raise MarketError("FORBIDDEN_REQUEST")


@dataclass
class Budget:
    monotonic: Callable[[], float] = time.monotonic
    seconds: float = 90.0
    started: float | None = None

    def remaining(self) -> float:
        if self.started is None:
            self.started = self.monotonic()
        remaining = self.seconds - (self.monotonic() - self.started)
        if remaining <= 0:
            raise MarketError("DEADLINE_EXCEEDED")
        return remaining


class PublicSource:
    def __init__(
        self,
        *,
        budget: Budget,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0, 0.1),
        blocked_until: Callable[[float], None] = lambda _: None,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        self.budget = budget
        self.sleep = sleep
        self.jitter = jitter
        self.blocked_until = blocked_until
        self.wall_time = wall_time
        self._client = httpx.Client(
            base_url=HOST,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            verify=True,
            event_hooks={"request": [guard_request]},
        )

    def close(self) -> None:
        self._client.close()

    def _get(self, route: str, params: dict[str, str | int]) -> RawResponse:
        if route not in ROUTES:
            raise MarketError("FORBIDDEN_ROUTE")
        for attempt in range(3):
            remaining = self.budget.remaining()
            timeout = httpx.Timeout(
                min(10.0, remaining), connect=min(5.0, remaining), pool=min(5.0, remaining)
            )
            wait = 0.5 * 2**attempt + min(0.1, max(0.0, self.jitter()))
            try:
                with self._client.stream("GET", route, params=params, timeout=timeout) as response:
                    if response.status_code in (418, 429):
                        try:
                            retry_after = float(response.headers["Retry-After"])
                            if not math.isfinite(retry_after) or retry_after < 0:
                                raise ValueError
                        except (KeyError, ValueError):
                            retry_after = 90.0
                        self.blocked_until(self.wall_time() + retry_after)
                        if response.status_code == 418:
                            raise MarketError("IP_BANNED")
                        wait = retry_after
                    elif response.status_code >= 500:
                        pass
                    elif response.status_code != 200:
                        raise MarketError("HTTP_REJECTED")
                    else:
                        body = bytearray()
                        for chunk in response.iter_bytes():
                            self.budget.remaining()
                            if len(body) + len(chunk) > MAX_BYTES:
                                raise MarketError("RESPONSE_TOO_LARGE")
                            body.extend(chunk)
                        self.budget.remaining()
                        allowed = {
                            key: value
                            for key, value in response.headers.items()
                            if key.lower().startswith("x-mbx-used-weight")
                        }
                        return RawResponse(
                            route,
                            params,
                            bytes(body),
                            utc_timestamp(utc_now()),
                            response.status_code,
                            allowed,
                        )
            except httpx.DecodingError:
                raise MarketError("INVALID_ENCODING") from None
            except httpx.TransportError:
                pass
            if attempt == 2:
                raise MarketError("RETRIES_EXHAUSTED")
            if wait >= self.budget.remaining():
                raise MarketError("DEADLINE_EXCEEDED")
            self.sleep(wait)
        raise MarketError("RETRIES_EXHAUSTED")

    def server_time(self) -> RawResponse:
        return self._get("/api/v3/time", {})

    def metadata(self) -> RawResponse:
        return self._get("/api/v3/exchangeInfo", {"symbol": "BTCUSDT"})

    def quote(self) -> RawResponse:
        return self._get("/api/v3/ticker/bookTicker", {"symbol": "BTCUSDT"})

    def klines(self, start: int, end: int, limit: int) -> RawResponse:
        if (
            type(start) is not int
            or type(end) is not int
            or start < 0
            or end < start
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise MarketError("INVALID_REQUEST")
        return self._get(
            "/api/v3/klines",
            {
                "symbol": "BTCUSDT",
                "interval": "15m",
                "startTime": start,
                "endTime": end,
                "limit": limit,
                "timeZone": "0",
            },
        )
