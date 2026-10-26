"""Four-route market adapter and explicit synthetic fixtures; no account endpoints."""

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx
from cripto_core.errors import MarketError, PaperError
from cripto_core.execution import filters
from cripto_core.serialization import fingerprint
from cripto_core.validation import require_object, sequence
from cripto_market_data.adapters.binance.public import Budget, PublicSource
from cripto_market_data.adapters.storage.sqlite import SQLiteMarketStore
from cripto_market_data.application.ports import RawResponse
from cripto_market_data.domain.market import (
    STEP,
    iso,
    parse_metadata,
    parse_page,
    parse_quote,
    parse_time,
)

from cripto_bot.adapters.continuous_config import ContinuousSettings


class Clock(Protocol):
    def now(self) -> int: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> int:
        return time.time_ns() // 1000000

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class Frame:
    at: int
    server_time: int
    rules: dict[str, object]
    quote: dict[str, object]
    candles: list[dict[str, object]]
    metadata_at: int
    source_sha256: str


class Feed(Protocol):
    def sample(
        self, close: int | None = None, count: int = 50, *, quote_only: bool = False
    ) -> Frame: ...
    def close(self) -> None: ...


def check_clock(server: int, start: int, end: int, elapsed_ms: float) -> None:
    if (
        elapsed_ms < 0
        or elapsed_ms > 1000
        or end < start
        or abs((end - start) - elapsed_ms) > 1000
        or abs(server - (start + end) // 2) > 1000
    ):
        raise PaperError("UNSYNCHRONIZED_CLOCK")


def book(value: object, observed: int, key: str) -> dict[str, object]:
    parsed = parse_quote(value)
    return {
        "symbol": "BTCUSDT",
        "kind": "BID_ASK",
        "bid": parsed["bidPrice"],
        "ask": parsed["askPrice"],
        "bid_qty": parsed["bidQty"],
        "ask_qty": parsed["askQty"],
        "observed_at": iso(observed),
        "quote_id": key,
        "market_timestamp": None,
    }


def received(raw: RawResponse) -> int:
    instant = datetime.fromisoformat(raw.received_at)
    if instant.utcoffset() is None:
        raise PaperError("INVALID_RECEIVE_TIME")
    delta = instant.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000


def normalize(raw: object, server: int, close: int, count: int) -> list[dict[str, object]]:
    parsed, ignored = parse_page(raw, server)
    if (
        ignored
        or len(parsed) != count
        or not parsed
        or parsed[0].open_time != close - count * STEP
        or parsed[-1].open_time != close - STEP
        or any(b.open_time != a.open_time + STEP for a, b in zip(parsed, parsed[1:], strict=False))
    ):
        raise PaperError("DATA_GAP")
    return [c.record() for c in parsed]


class FixtureFeed:
    def __init__(self, path: Path, clock: Clock) -> None:
        with path.open("rb") as f:
            body = f.read(5 * 1024 * 1024 + 1)
        if len(body) > 5 * 1024 * 1024:
            raise PaperError("FIXTURE_TOO_LARGE")
        data = require_object(RawResponse("fixture", {}, body, "").json())
        if set(data) != {"schema", "exchange_info", "frames"} or data["schema"] != 1:
            raise PaperError("INVALID_CONTINUOUS_FIXTURE")
        self.data = data
        self.clock = clock
        self.sha = fingerprint(data)
        self.rules = require_object(sequence(parse_metadata(data["exchange_info"])["symbols"])[0])
        filters(self.rules)
        frames = [require_object(f) for f in sequence(data["frames"])]
        if (
            not frames
            or any(type(f.get("at")) is not int for f in frames)
            or any(
                int(str(b["at"])) <= int(str(a["at"]))
                for a, b in zip(frames, frames[1:], strict=False)
            )
        ):
            raise PaperError("FIXTURE_FRAME_ORDER")
        self.frames = frames

    def sample(
        self, close: int | None = None, count: int = 50, *, quote_only: bool = False
    ) -> Frame:
        now = self.clock.now()
        eligible = [f for f in self.frames if int(str(f["at"])) <= now]
        if not eligible:
            raise PaperError("FIXTURE_OBSERVATION_UNAVAILABLE")
        frame = eligible[-1]
        observed = int(str(frame["at"]))
        server = int(str(frame["server_time"]))
        # Fixed fixtures emulate a captured response, never claim fresh data after its timestamp.
        check_clock(server, observed, observed, float(str(frame.get("rtt_ms", 0))))
        if now - observed > 5000:
            raise PaperError("STALE_OR_FUTURE_QUOTE")
        if close is None:
            close = server // STEP * STEP
        rows = []
        if not quote_only:
            selected = [
                r
                for r in sequence(frame["klines"])
                if isinstance(r, list)
                and r
                and type(r[0]) is int
                and close - count * STEP <= r[0] < close
            ]
            rows = normalize(selected, server, close, count)
        quote = book(
            frame["book_ticker"],
            observed,
            fingerprint({"fixture": self.sha, "at": observed, "quote": frame["book_ticker"]}),
        )
        return Frame(now, server, self.rules, quote, rows, now, self.sha)

    def close(self) -> None:
        pass


class PublicFeed:
    def __init__(
        self,
        settings: ContinuousSettings,
        clock: Clock,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.clock = clock
        self.settings = settings
        self.market = SQLiteMarketStore(settings.market_data_dir).__enter__()
        self.dataset = f"continuous:{settings.pilot_id}:v1"
        self.rules: dict[str, object] = {}
        self.metadata_at = -1
        self.sync_at = -1
        self.server_base = 0
        self.sync_mono = 0.0
        try:
            self.source = PublicSource(
                budget=Budget(monotonic=clock.monotonic, seconds=30),
                transport=transport,
                blocked_until=self.market.block_until,
                wall_time=lambda: clock.now() / 1000,
                sleep=clock.sleep,
            )
        except BaseException:
            self.market.close()
            raise

    def sample(
        self, close: int | None = None, count: int = 50, *, quote_only: bool = False
    ) -> Frame:
        try:
            return self._sample(close, count, quote_only=quote_only)
        except MarketError as error:
            raise PaperError(str(error)) from None

    def _sample(
        self, close: int | None = None, count: int = 50, *, quote_only: bool = False
    ) -> Frame:
        now = self.clock.now()
        if self.market.not_before() > now / 1000:
            raise PaperError("MARKET_THROTTLED")
        seconds = 5.0 if quote_only else 30.0
        if close is not None and not quote_only:
            seconds = min(seconds, max(0.0, (close + 90000 - now) / 1000))
        self.source.budget = Budget(monotonic=self.clock.monotonic, seconds=seconds)
        if self.sync_at < 0 or now - self.sync_at >= 300000:
            start = self.clock.now()
            mono = self.clock.monotonic()
            raw = self.source.server_time()
            end = self.clock.now()
            server = parse_time(raw.json())
            check_clock(server, start, end, (self.clock.monotonic() - mono) * 1000)
            self.market.save_snapshot(self.dataset, raw, {"server_time": server})
            self.server_base = server
            self.sync_at = end
            self.sync_mono = self.clock.monotonic()
        server = self.server_base + int((self.clock.monotonic() - self.sync_mono) * 1000)
        if self.metadata_at < 0 or now - self.metadata_at >= 3600000:
            raw = self.source.metadata()
            parsed = parse_metadata(raw.json())
            rules = require_object(sequence(parsed["symbols"])[0])
            self.market.save_snapshot(self.dataset, raw, parsed)
            filters(rules)
            self.rules = rules
            self.metadata_at = self.clock.now()
        if close is None:
            close = server // STEP * STEP
        if close > server // STEP * STEP:
            raise PaperError("WINDOW_NOT_CLOSED")
        rows = []
        if not quote_only:
            start = close - count * STEP
            raw = self.source.klines(start, close - 1, count)
            rows = normalize(raw.json(), server, close, count)
            parsed_candles, _ = parse_page(raw.json(), server)
            self.market.commit_page(self.dataset, start, close, raw, parsed_candles)
        # Quote is fetched after candles so processing never re-labels an old quote as fresh.
        raw = self.source.quote()
        value = parse_quote(raw.json())
        observed = received(raw)
        self.market.save_snapshot(self.dataset, raw, value)
        quote = book(
            raw.json(), observed, fingerprint({"sha": raw.sha256, "received_at": raw.received_at})
        )
        at = self.clock.now()
        server = self.server_base + int((self.clock.monotonic() - self.sync_mono) * 1000)
        if abs(at - server) > 1000:
            raise PaperError("UNSYNCHRONIZED_CLOCK")
        if not observed <= at <= observed + 5000:
            raise PaperError("STALE_OR_FUTURE_QUOTE")
        return Frame(at, server, self.rules, quote, rows, self.metadata_at, raw.sha256)

    def close(self) -> None:
        self.source.close()
        self.market.close()
