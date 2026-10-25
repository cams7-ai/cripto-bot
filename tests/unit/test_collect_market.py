import json

import pytest
from cripto_bot.adapters.binance.fixture import fixture_source
from cripto_bot.adapters.binance.public import Budget
from cripto_bot.adapters.storage.market_sqlite import SQLiteMarketStore
from cripto_bot.application.collect_market import Progress, collect_market
from cripto_bot.domain.market import STEP, MarketError

from tests.factories import BASE, fixture_file, raw, sample


def test_paginated_1001_and_idempotent_resume(tmp_path):
    dataset, source = fixture_source(fixture_file(tmp_path / "fixture.json", 1001), Budget())
    calls = []
    original = source.klines

    def record(start, end, limit):
        calls.append((start, end, limit))
        return original(start, end, limit)

    source.klines = record
    with SQLiteMarketStore(tmp_path / "db") as store:
        progress = Progress(BASE)
        collect_market(
            source, store, dataset, BASE, BASE + 1001 * STEP, 1000, 10, progress, lambda: 90
        )
        assert progress.inserted == 1001 and progress.next_open_time == BASE + 1001 * STEP
        assert len(calls) == 2 and calls[1][0] == BASE + 1000 * STEP
        again = Progress(BASE)
        collect_market(
            source, store, dataset, BASE, BASE + 1001 * STEP, 1000, 10, again, lambda: 90
        )
        assert again.inserted == 0 and len(calls) == 2
        # Mesmo inicio e end menor nao regride checkpoint confirmado.
        collect_market(source, store, dataset, BASE, BASE + STEP, 1000, 10, again, lambda: 90)
        assert store.checkpoint(dataset, BASE) == BASE + 1001 * STEP
    source.close()


@pytest.mark.parametrize("kind", ["gap", "empty", "unordered", "conflict", "outside"])
def test_bad_page_no_advance_and_preserve_raw(tmp_path, kind):
    dataset, source = fixture_source(fixture_file(tmp_path / "fixture.json", 3), Budget())
    rows = sample(3)["klines"]
    if kind == "gap":
        rows = rows[1:]
    elif kind == "empty":
        rows = []
    elif kind == "unordered":
        rows = list(reversed(rows))
    elif kind == "conflict":
        changed = list(rows[0])
        changed[4] = "100.5"
        rows = [rows[0], changed]
    elif kind == "outside":
        rows = sample(4)["klines"]
    source.klines = lambda *_: raw(rows)
    with SQLiteMarketStore(tmp_path / "db") as store:
        progress = Progress(BASE)
        with pytest.raises(MarketError):
            collect_market(
                source, store, dataset, BASE, BASE + 3 * STEP, 1000, 10, progress, lambda: 90
            )
        assert store.checkpoint(dataset, BASE) == BASE
        assert store.records(dataset, BASE, BASE + 3 * STEP) == []
        assert (
            store.db.execute("SELECT count(*) FROM raw WHERE route='/api/v3/klines'").fetchone()[0]
            == 1
        )
    source.close()


def test_duplicate_within_page(tmp_path):
    dataset, source = fixture_source(fixture_file(tmp_path / "fixture.json"), Budget())
    rows = sample()["klines"]
    source.klines = lambda *_: raw([rows[0], rows[0], rows[1]])
    with SQLiteMarketStore(tmp_path / "db") as store:
        progress = Progress(BASE)
        collect_market(
            source, store, dataset, BASE, BASE + 2 * STEP, 1000, 10, progress, lambda: 90
        )
        assert progress.inserted == 2 and progress.duplicated == 1
    source.close()


def test_resume_after_later_page_failure(tmp_path):
    dataset, source = fixture_source(fixture_file(tmp_path / "fixture.json", 3), Budget())
    original = source.klines

    def fail(start, end, limit):
        if start > BASE:
            raise MarketError("INJECTED_TIMEOUT")
        return original(start, end, limit)

    source.klines = fail
    with SQLiteMarketStore(tmp_path / "db") as store:
        progress = Progress(BASE)
        with pytest.raises(MarketError, match="TIMEOUT"):
            collect_market(
                source, store, dataset, BASE, BASE + 3 * STEP, 1, 10, progress, lambda: 90
            )
        assert progress.next_open_time == BASE + STEP
    source.klines = original
    with SQLiteMarketStore(tmp_path / "db") as store:
        progress = Progress(BASE)
        collect_market(source, store, dataset, BASE, BASE + 3 * STEP, 1, 10, progress, lambda: 90)
        assert progress.inserted == 2
        assert len(store.records(dataset, BASE, BASE + 3 * STEP)) == 3
    source.close()


def test_page_limit_and_future_window(tmp_path):
    dataset, source = fixture_source(fixture_file(tmp_path / "fixture.json", 3), Budget())
    with SQLiteMarketStore(tmp_path / "db") as store:
        with pytest.raises(MarketError, match="PAGE_LIMIT"):
            collect_market(
                source, store, dataset, BASE, BASE + 3 * STEP, 1, 1, Progress(BASE), lambda: 90
            )
        with pytest.raises(MarketError, match="NOT_CLOSED"):
            collect_market(
                source, store, dataset, BASE, BASE + 5 * STEP, 1, 10, Progress(BASE), lambda: 90
            )
    source.close()


def test_fixture_namespace_changes_with_content(tmp_path):
    path = fixture_file(tmp_path / "fixture.json")
    first, source = fixture_source(path, Budget())
    source.close()
    data = json.loads(path.read_text())
    data["book_ticker"]["bidQty"] = "10"
    path.write_text(json.dumps(data))
    second, source = fixture_source(path, Budget())
    source.close()
    assert first != second


def test_page_size_guard_and_exact_page_budget(tmp_path):
    dataset, source = fixture_source(fixture_file(tmp_path / "fixture.json"), Budget())
    original = source.klines
    source.klines = lambda *_: raw(sample()["klines"])
    with SQLiteMarketStore(tmp_path / "db") as store:
        with pytest.raises(MarketError, match="INVALID_PAGE_SIZE"):
            collect_market(
                source, store, dataset, BASE, BASE + 2 * STEP, 1, 2, Progress(BASE), lambda: 90
            )
        assert store.checkpoint(dataset, BASE) == BASE
        source.klines = original
        progress = Progress(BASE)
        collect_market(source, store, dataset, BASE, BASE + 2 * STEP, 1, 2, progress, lambda: 90)
        assert progress.next_open_time == BASE + 2 * STEP
    source.close()
