"""Portas da coleta, independentes de HTTPX e SQLite."""

import json
from dataclasses import dataclass, field
from typing import Protocol

from cripto_bot.domain.market import Candle, MarketError, digest


@dataclass(frozen=True)
class RawResponse:
    route: str
    params: dict[str, str | int]
    body: bytes
    received_at: str
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return digest(self.body)

    def json(self) -> object:
        def invalid(text: str) -> None:
            raise ValueError

        try:
            result: object = json.loads(self.body, parse_constant=invalid)
            return result
        except (ValueError, UnicodeError):
            raise MarketError("INVALID_JSON") from None


class MarketSource(Protocol):
    def server_time(self) -> RawResponse: ...
    def metadata(self) -> RawResponse: ...
    def quote(self) -> RawResponse: ...
    def klines(self, start: int, end: int, limit: int) -> RawResponse: ...
    def close(self) -> None: ...


class MarketStore(Protocol):
    def checkpoint(self, dataset: str, start: int) -> int: ...
    def verify_prefix(self, dataset: str, start: int, end: int) -> None: ...
    def save_snapshot(self, dataset: str, raw: RawResponse, normalized: object) -> None: ...
    def commit_page(
        self, dataset: str, start: int, next_open: int, raw: RawResponse, candles: list[Candle]
    ) -> tuple[int, int]: ...
