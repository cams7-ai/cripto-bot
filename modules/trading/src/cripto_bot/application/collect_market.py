"""Coordenacao pura da paginacao; adapters entram por portas."""

from collections.abc import Callable
from dataclasses import dataclass

from cripto_bot.application.ports import MarketSource, MarketStore
from cripto_bot.domain.market import (
    STEP,
    MarketError,
    canonical,
    parse_metadata,
    parse_page,
    parse_quote,
    parse_time,
    sequence,
)


@dataclass
class Progress:
    next_open_time: int
    inserted: int = 0
    duplicated: int = 0
    ignored: int = 0


def collect_market(
    source: MarketSource,
    store: MarketStore,
    dataset: str,
    start: int,
    end: int,
    limit: int,
    max_pages: int,
    progress: Progress,
    check_budget: Callable[[], float],
) -> None:
    raw = source.server_time()
    server_time = parse_time(raw.json())
    if end > server_time // STEP * STEP:
        raise MarketError("WINDOW_NOT_CLOSED")
    store.save_snapshot(dataset, raw, {"server_time": server_time})
    raw = source.metadata()
    store.save_snapshot(dataset, raw, parse_metadata(raw.json()))
    raw = source.quote()
    store.save_snapshot(dataset, raw, parse_quote(raw.json()))
    checkpoint = store.checkpoint(dataset, start)
    store.verify_prefix(dataset, start, checkpoint)
    progress.next_open_time = min(checkpoint, end)
    for _ in range(max_pages):
        check_budget()
        if progress.next_open_time == end:
            return
        raw = source.klines(progress.next_open_time, end - 1, limit)
        try:
            payload = raw.json()
            if len(sequence(payload)) > limit:
                raise MarketError("INVALID_PAGE_SIZE")
            candles, ignored = parse_page(payload, server_time)
            next_open = progress.next_open_time
            seen: dict[int, str] = {}
            for candle in candles:
                if candle.open_time < progress.next_open_time or candle.open_time >= end:
                    raise MarketError("OUT_OF_WINDOW")
                text = canonical(candle.record())
                if candle.open_time in seen:
                    if seen[candle.open_time] != text:
                        raise MarketError("CANDLE_CONFLICT")
                elif candle.open_time != next_open:
                    raise MarketError("DATA_GAP")
                else:
                    next_open += STEP
                seen[candle.open_time] = text
            if next_open == progress.next_open_time:
                raise MarketError("NO_PROGRESS")
            inserted, duplicated = store.commit_page(dataset, start, next_open, raw, candles)
        except MarketError as error:
            store.save_snapshot(dataset, raw, {"rejected": str(error)})
            raise
        progress.next_open_time = next_open
        progress.inserted += inserted
        progress.duplicated += duplicated
        progress.ignored += ignored
    if progress.next_open_time != end:
        raise MarketError("PAGE_LIMIT_EXCEEDED")
