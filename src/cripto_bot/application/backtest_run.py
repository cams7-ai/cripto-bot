"""Orquestracao offline: protocolo, fases independentes e retomada causal."""

import json
import sqlite3
from collections import deque
from contextlib import closing

from cripto_bot.adapters.backtest_dataset import records, validate
from cripto_bot.adapters.storage.backtest_sqlite import BacktestStore
from cripto_bot.adapters.storage.market_export import source_fingerprint
from cripto_bot.domain.backtest.engine import State
from cripto_bot.domain.backtest.models import (
    MODEL,
    PROFILES,
    SEGMENTS,
    VARIANTS,
    BacktestSettings,
    build_context,
    fingerprint,
    run_key,
    split,
)
from cripto_bot.domain.market import canonical
from cripto_bot.domain.portfolio.models import PaperError, require_object


def protocol(config: BacktestSettings) -> dict[str, object]:
    dataset = validate(config)
    module = config.module
    if module is None:
        raise PaperError("NO_ACTIVE_BACKTEST_MODULE")
    body = {
        "model": MODEL,
        "configuration": json.loads(canonical(config.snapshot())),
        "dataset": dataset,
        "code_sha256": source_fingerprint(config.root),
        "segments": split(config.start, config.end, module.slow_window),
        "metrics": "full event series; time weighted milliseconds; ratios16 HALF_EVEN; money8",
        "execution": "close available boundary; next open observed +1ms; fill +2ms; proxy integral",
        "human_foreknowledge": (
            "Software cannot prove human foreknowledge absence. "
            "Final window consumption is recorded locally."
        ),
    }
    return {**body, "protocol_hash": fingerprint(body)}


def verify_protocol(store: BacktestStore, config: BacktestSettings) -> dict[str, object]:
    sealed = store.protocol()
    if protocol(config) != sealed:
        raise PaperError("FROZEN_PROTOCOL_CHANGED")
    if store.blocked():
        raise PaperError("BACKTEST_BLOCKED")
    return sealed


def curve_context(
    config: BacktestSettings, sealed: dict[str, object], segment: str, variant: str, profile: str
) -> dict[str, object]:
    return build_context(sealed, segment, variant, profile)


def consume_test(config: BacktestSettings, sealed: dict[str, object]) -> None:
    # Registro comum a todos os IDs/diretorios de experimentos da mesma raiz.
    directory = config.root / "data/backtest-usage"
    directory.mkdir(parents=True, exist_ok=True)
    bounds = require_object(sealed["segments"])["test"]
    assert isinstance(bounds, list)
    start, end = int(str(bounds[0])), int(str(bounds[1]))
    identity = fingerprint({"symbol": "BTCUSDT", "interval": "15m", "test": bounds})
    path = directory / "test-usage.sqlite3"
    if path.is_symlink():
        raise PaperError("UNSAFE_USAGE_REGISTRY")
    with closing(sqlite3.connect(path, timeout=1)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS consumed (identity TEXT PRIMARY KEY, p"
            "rotocol TEXT NOT NULL, source TEXT NOT NULL, start INTEGER NOT NU"
            "LL, end INTEGER NOT NULL)"
        )
        db.execute("BEGIN IMMEDIATE")
        origin = require_object(sealed["dataset"])["source"]
        conflict = db.execute(
            (
                "SELECT 1 FROM consumed WHERE source='binance_public' AND start<? "
                "AND end>? AND protocol!=?"
            ),
            (end, start, sealed["protocol_hash"]),
        ).fetchone()
        if origin == "binance_public" and conflict:
            raise PaperError("FINAL_WINDOW_ALREADY_CONSUMED")
        db.execute(
            "INSERT OR IGNORE INTO consumed VALUES(?,?,?,?,?)",
            (identity, sealed["protocol_hash"], origin, start, end),
        )


def execute_curve(
    store: BacktestStore, config: BacktestSettings, context: dict[str, object]
) -> State:
    start = int(str(context["start"]))
    end = int(str(context["end"]))
    slow = int(str(require_object(context["module"])["slow_window"]))
    prefix: deque[dict[str, object]] = deque(maxlen=slow)
    baseline: object = None
    state: State | None = None
    previous: dict[str, object] | None = None
    key = str(context["run_key"])
    for candle in records(config):
        opened = int(str(candle["open_time"]))
        if int(str(candle["available_at"])) == start // 86400000 * 86400000:
            baseline = candle["close"]
        if opened < start:
            prefix.append(candle)
            previous = candle
            continue
        if opened >= end:
            break
        if state is None:
            if baseline is None or len(prefix) != slow:
                raise PaperError("MISSING_CAUSAL_WARMUP_OR_BASELINE")
            state = store.create_run(context, list(prefix), baseline)
            if state.finished:
                return state
            if state.wallet.intents:
                state = finish_fill(store, key, state)
        assert previous is not None
        if opened > state.last_boundary:
            state = store.append(
                key, f"boundary:{opened}", "BOUNDARY", {"at": opened, "closed": previous}, state
            )
        if opened > state.last_observed:
            state = store.append(
                key,
                f"observe:{opened}",
                "OBSERVE",
                {"bar_open": opened, "price": candle["open"]},
                state,
            )
        if state.wallet.intents:
            state = finish_fill(store, key, state)
        previous = candle
    if state is None or previous is None:
        raise PaperError("EMPTY_SEGMENT")
    state = store.append(key, "end", "END", {"closed": previous}, state)
    store.flush()
    return store.reconcile(key)


def finish_fill(store: BacktestStore, key: str, state: State) -> State:
    action, intent = next(iter(state.wallet.intents.items()))
    if intent.plan is None:
        raise PaperError("MISSING_RESERVED_PLAN")
    return store.append(
        key, f"fill:{action}", "FILL", {"action_key": action, "fill_id": intent.plan.fill_id}, state
    )


def run_segment(store: BacktestStore, config: BacktestSettings, segment: str) -> dict[str, object]:
    sealed = verify_protocol(store, config)
    for earlier in SEGMENTS[: SEGMENTS.index(segment)]:
        for variant in VARIANTS:
            for profile in PROFILES:
                key = run_key(str(sealed["protocol_hash"]), earlier, variant, profile)
                row = store.db.execute("SELECT status FROM runs WHERE run_key=?", (key,)).fetchone()
                if row != ("COMPLETED",):
                    raise PaperError("PREVIOUS_SEGMENT_REQUIRED")
                store.reconcile(key)
    if segment == "test":
        consume_test(config, sealed)
    before_events = store.db.execute("SELECT coalesce(sum(version),0) FROM runs").fetchone()[0]
    before = store.db.execute("SELECT count(*) FROM fills").fetchone()[0]
    for variant in VARIANTS:
        for profile in PROFILES:
            execute_curve(store, config, curve_context(config, sealed, segment, variant, profile))
    try:
        verify_protocol(store, config)
    except PaperError:
        store.block()
        raise
    after = store.db.execute("SELECT count(*) FROM fills").fetchone()[0]
    return {
        "segment": segment,
        "simulated_fills": after - before,
        "curves": len(PROFILES) * len(VARIANTS),
        "final_window_consumed": segment == "test",
        "reproduction": store.db.execute("SELECT coalesce(sum(version),0) FROM runs").fetchone()[0]
        == before_events,
    }
