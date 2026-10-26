"""Atomic financial history, independent controls and killed-process recovery."""

import json
import multiprocessing
import os
import sqlite3
from decimal import Decimal

import pytest
from cripto_core.errors import PaperError
from cripto_core.serialization import fingerprint

from cripto_bot.adapters.storage.continuous_report import snapshot
from cripto_bot.adapters.storage.continuous_sqlite import ContinuousStore
from cripto_bot.application.continuous_control import apply_pending, submit
from cripto_bot.application.continuous_run import next_midnight, tick
from cripto_bot.domain.continuous.models import DAY, STEP
from tests.continuous_factories import BASE, Clock, Feed, arm_payload, context, quote


def initialize(path):
    with ContinuousStore(path, create=True) as store:
        store.initialize(context(), BASE - 2000)
        store.append("arm", "ARM", arm_payload())
    return path


def admit(store):
    return store.append(
        "buy",
        "ADMIT",
        {
            "at": BASE + 2000,
            "order_id": "buy",
            "side": "BUY",
            "quantity": "1",
            "quote": quote(BASE + 2000),
        },
    )


def crash(path, committed):
    with ContinuousStore(path) as store:
        if committed:
            store.append("fill", "POLL", {"at": BASE + 7000, "quote": quote(BASE + 7000, qty="4")})
        else:

            def trace(statement):
                if statement == "COMMIT":
                    os._exit(23)

            store.db.set_trace_callback(trace)
            store.append("fill", "POLL", {"at": BASE + 7000, "quote": quote(BASE + 7000, qty="4")})
        os._exit(23)


@pytest.mark.parametrize("committed", [False, True])
def test_process_death_before_and_after_commit(tmp_path, committed):
    path = initialize(tmp_path / "financial")
    with ContinuousStore(path) as store:
        admit(store)
    child = multiprocessing.get_context("fork").Process(target=crash, args=(path, committed))
    child.start()
    child.join(10)
    assert child.exitcode == 23
    with ContinuousStore(path) as store:
        state = store.reconcile()
        assert state.wallet.quantity == Decimal("0.4" if committed else "0")
        assert len(state.orders["buy"].fills) == int(committed)
        if committed:
            before = state.data()
            assert (
                store.append(
                    "fill", "POLL", {"at": BASE + 7000, "quote": quote(BASE + 7000, qty="4")}
                ).data()
                == before
            )
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_control_can_submit_while_financial_writer_locked_and_ack_once(tmp_path):
    path = initialize(tmp_path / "financial")
    with ContinuousStore(path) as store:
        admit(store)
        with pytest.raises(PaperError, match="WRITER_BUSY"):
            with ContinuousStore(path):
                pass
        with ContinuousStore(path, readonly=True) as reader:
            assert reader.state().pending().order_id == "buy"
            with pytest.raises(PaperError, match="READONLY_STORE"):
                reader.append("x", "POLL", {"at": BASE + 3000})
        args = (
            path,
            "drill",
            "pause",
            "operator drill",
            "request",
            BASE + 3000,
            fingerprint(context()),
        )
        assert submit(*args)["application"] == "PENDING"
        assert submit(*args)["status"] == "ACCEPTED"
        assert apply_pending(store, BASE + 4000) == 1
        assert apply_pending(store, BASE + 5000) == 0
        assert store.reconcile().wallet.usdt_reserved == 0
        with pytest.raises(PaperError, match="CONTROL_CONFLICT"):
            submit(path, "drill", "kill", "changed", "request", BASE + 6000, fingerprint(context()))


@pytest.mark.parametrize(
    "table,column,value,error",
    [
        ("projection", "data", "'{}'", "PROJECTION_DIVERGENCE"),
        ("orders", "data", "'{}'", "PROJECTION_DIVERGENCE"),
    ],
)
def test_reject_projection_tampering(tmp_path, table, column, value, error):
    path = initialize(tmp_path / "financial")
    with ContinuousStore(path) as store:
        admit(store)
        store.db.execute(f"UPDATE {table} SET {column}={value}")
    with pytest.raises(PaperError, match=error):
        with ContinuousStore(path):
            pass


@pytest.mark.parametrize(
    "table",
    ["journal", "postings", "fills", "cycles", "configurations", "controls", "notification_events"],
)
def test_history_is_immutable(tmp_path, table):
    with ContinuousStore(initialize(tmp_path / "financial")) as store:
        admit(store)
        store.append("fill", "POLL", {"at": BASE + 7000, "quote": quote(BASE + 7000, qty="4")})
        store.append(
            "control", "CONTROL", {"at": BASE + 8000, "request_id": "pause", "action": "pause"}
        )
        store.append("cycle", "MISSED", {"at": BASE + 90001, "close": BASE})
        assert store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] > 0
        with pytest.raises(sqlite3.IntegrityError, match="immutable continuous history"):
            store.db.execute(f"DELETE FROM {table}")


def test_backup_restore_manifest_and_outbox_reconcile(tmp_path):
    path = initialize(tmp_path / "financial")
    with ContinuousStore(path) as store:
        admit(store)
        store.freeze_manifest()
        before = store.reconcile().data()
        restore = tmp_path / "restored"
        restore.mkdir()
        store.backup(restore / "continuous.sqlite3")
    with ContinuousStore(restore) as store:
        assert store.reconcile().data() == before
        store.freeze_manifest()
        assert json.loads((restore / "pilot-manifest.json").read_text())["eligible_slots"] == 672
        with pytest.raises(PaperError, match="UNSAFE_BACKUP_PATH"):
            store.backup(restore / "continuous.sqlite3")
        store.db.execute(
            "INSERT INTO notification_events SELECT 'extra',seq,source_hash,data "
            "FROM notification_events LIMIT 1"
        )
        with pytest.raises(PaperError, match="NOTIFICATION_EVENT_DIVERGENCE"):
            store.reconcile()


def test_fixed_denominator_never_certifies_accelerated_week(tmp_path):
    path = initialize(tmp_path / "financial")
    clock = Clock(BASE + 7 * DAY)
    with ContinuousStore(path) as store:
        output = tick(store, Feed(clock, fail=True), clock, free_bytes=lambda: 10 * 1024**3)
        report = snapshot(store, clock.now())
        assert output["status"] == "ENDED"
        assert report["recorded_slots"] == 672 and report["processed_slots"] == 0
        assert report["missing_or_failed_slots"] == 672
        assert report["observation_status"] == "SYNTHETIC_NOT_PILOT"
        assert report["pilot_approved"] is False
        assert len(store.state().daily) == 7
        assert report["equity"] is None


def test_fresh_slot_and_unavailable_following_slot_are_distinct(tmp_path):
    path = initialize(tmp_path / "financial")
    clock = Clock(BASE + 2000)
    with ContinuousStore(path) as store:
        tick(store, Feed(clock), clock, free_bytes=lambda: 10 * 1024**3)
        clock.advance(STEP)
        tick(store, Feed(clock, fail=True), clock, free_bytes=lambda: 10 * 1024**3)
        report = snapshot(store, clock.now())
        assert report["processed_slots"] == 1 and report["recorded_slots"] == 2
        assert report["late_or_failed"][0]["reason"] == "TEST_DATA_FAILURE"
        assert not store.state().orders


def test_disk_pause_and_startup_reserve(tmp_path):
    path = initialize(tmp_path / "financial")
    clock = Clock(BASE + 3000)
    with ContinuousStore(path) as store:
        admit(store)
        tick(store, Feed(clock), clock, free_bytes=lambda: 100)
        assert store.state().paused and store.state().wallet.usdt_reserved == 0
    with ContinuousStore(tmp_path / "empty", create=True) as store:
        store.initialize(context(), BASE - 2000)
        with pytest.raises(PaperError, match="PILOT_DISK_RESERVE"):
            tick(store, Feed(clock), clock, free_bytes=lambda: 4 * 1024**3)
        assert store.state().t0 is None


def test_calendar_at_least_fifteen_minutes_and_current_warmup(tmp_path):
    assert next_midnight(BASE - 900000) == BASE
    assert next_midnight(BASE - 899999) == BASE + DAY
    clock = Clock(BASE - 1000000)
    with ContinuousStore(tmp_path / "financial", create=True) as store:
        store.initialize(context(), clock.now())
        tick(store, Feed(clock), clock, free_bytes=lambda: 10 * 1024**3)
        assert store.state().t0 == BASE
        assert max(int(c["close_time"]) for c in store.state().candles) < clock.now()


def test_wrong_schema_and_old_store_never_migrate(tmp_path):
    path = tmp_path / "old"
    path.mkdir()
    old = path / "paper.sqlite3"
    old.write_bytes(b"preserved")
    with pytest.raises(PaperError, match="INCOMPATIBLE_CONTINUOUS_DIRECTORY"):
        with ContinuousStore(path, create=True):
            pass
    assert old.read_bytes() == b"preserved"
    path = initialize(tmp_path / "financial")
    with sqlite3.connect(path / "continuous.sqlite3") as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(PaperError, match="INCOMPATIBLE_CONTINUOUS_SCHEMA"):
        with ContinuousStore(path):
            pass


def crash_control(path, after):
    from cripto_bot.application import continuous_control

    original = continuous_control.os.link

    def link(source, target):
        if after:
            original(source, target)
        os._exit(31)

    continuous_control.os.link = link
    submit(
        path, "drill", "pause", "death drill", "crash-pause", BASE + 3000, fingerprint(context())
    )


@pytest.mark.parametrize("after", [False, True])
def test_control_publish_process_death_is_atomic(tmp_path, after):
    path = initialize(tmp_path / "financial")
    child = multiprocessing.get_context("fork").Process(target=crash_control, args=(path, after))
    child.start()
    child.join(10)
    assert child.exitcode == 31
    with ContinuousStore(path) as store:
        assert apply_pending(store, BASE + 4000) == int(after)
        assert apply_pending(store, BASE + 5000) == 0
        assert store.state().paused == after


def test_event_conflict_and_stale_cached_version_rejected(tmp_path):
    path = initialize(tmp_path / "financial")
    with ContinuousStore(path) as store:
        admit(store)
        with pytest.raises(PaperError, match="EVENT_CONFLICT"):
            store.append("buy", "ADMIT", {"at": BASE + 3000})
        store._state.wallet.version -= 1
        with pytest.raises(PaperError, match="JOURNAL_DIVERGENCE"):
            store.append("stale", "POLL", {"at": BASE + 7000, "quote": quote(BASE + 7000)})
    with ContinuousStore(path) as store:
        assert store.state().wallet.quantity == 0


def crash_reservation(path, committed):
    with ContinuousStore(path) as store:
        if not committed:

            def trace(statement):
                if statement == "COMMIT":
                    os._exit(41)

            store.db.set_trace_callback(trace)
        admit(store)
        os._exit(41)


@pytest.mark.parametrize("committed", [False, True])
def test_reservation_commit_process_death(tmp_path, committed):
    path = initialize(tmp_path / "financial")
    child = multiprocessing.get_context("fork").Process(
        target=crash_reservation, args=(path, committed)
    )
    child.start()
    child.join(10)
    assert child.exitcode == 41
    with ContinuousStore(path) as store:
        state = store.reconcile()
        assert bool(state.orders) == committed
        assert state.wallet.usdt_reserved == Decimal("100.10" if committed else "0")


def test_report_risk_metrics_and_unavailable_current_mark(tmp_path):
    path = initialize(tmp_path / "financial")
    with ContinuousStore(path) as store:
        admit(store)
        store.append("fill", "POLL", {"at": BASE + 7000, "quote": quote(BASE + 7000, qty="4")})
        fresh = snapshot(store, BASE + 8000)
        assert fresh["wallet"]["unrealized"] == "-0.04000000"
        assert fresh["risk_parameters"]["max_exposure_fraction"] == "0.10"
        before = store.state().data()
        stale = snapshot(store, BASE + 13000)
        assert stale["wallet"]["unrealized"] is None and stale["equity"] is None
        assert stale["metrics_at_last_valid_quote"]["unrealized"] == "-0.04000000"
        assert store.state().data() == before
