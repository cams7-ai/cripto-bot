"""Adversarios de dados/publicacao e fronteiras que nao mudam o protocolo."""

import gzip
import hashlib
import json
import shutil
from dataclasses import replace
from decimal import Decimal

import pytest

from cripto_bot.adapters.backtest_dataset import file_hash, prepare
from cripto_bot.adapters.storage.backtest_report import export_report, snapshot
from cripto_bot.adapters.storage.backtest_sqlite import BacktestStore
from cripto_bot.application.backtest_run import execute_curve, protocol
from cripto_bot.domain.backtest.engine import reduce
from cripto_bot.domain.market import STEP, canonical
from cripto_bot.domain.portfolio.models import PaperError, money
from tests.unit.test_backtest import candle, context, initial, signal
from tests.unit.test_backtest import config as config


def rewrite_source(path, rows):
    raw = ("".join(canonical(row) + "\n" for row in rows)).encode()
    (path.parent / "candles.jsonl.gz").write_bytes(gzip.compress(raw, mtime=0))
    manifest = json.loads(path.read_text())
    manifest["count"] = len(rows)
    manifest["data_sha256"] = hashlib.sha256(raw).hexdigest()
    manifest["files"]["candles.jsonl.gz"] = file_hash(path.parent / "candles.jsonl.gz")
    path.write_text(canonical(manifest))


def source_rows(config):
    with gzip.open(config.source_manifests[0].parent / "candles.jsonl.gz", "rt") as stream:
        return [json.loads(line) for line in stream]


def test_identical_duplicates_and_conflicting_duplicates(config):
    second = config.root / "config/fixtures/second"
    shutil.copytree(config.source_manifests[0].parent, second)
    combined = replace(
        config, source_manifests=(*config.source_manifests, second / "manifest.json")
    )
    result = prepare(combined)
    assert result["duplicates"] == 1010 and result["count"] == 1010
    rows = source_rows(config)
    rows[10]["close"] = "101"
    rows[10]["high"] = "101"
    rewrite_source(second / "manifest.json", rows)
    with pytest.raises(PaperError, match="CONFLICTING_DUPLICATE"):
        prepare(combined)


@pytest.mark.parametrize("mutation", ["gap", "conflict", "limit", "count", "parser", "schema"])
def test_rehashed_invalid_sources_still_fail(config, mutation):
    path = config.source_manifests[0]
    rows = source_rows(config)
    if mutation == "gap":
        rows.pop(50)
    if mutation == "conflict":
        rows[51]["open_time"] = rows[50]["open_time"]
    rewrite_source(path, rows)
    manifest = json.loads(path.read_text())
    if mutation == "count":
        manifest["count"] += 1
    if mutation == "parser":
        manifest["parser"] = "other"
    if mutation == "schema":
        manifest["schema"] = True
    if mutation == "limit":
        data = (" " * 17000 + "\n").encode()
        (path.parent / "candles.jsonl.gz").write_bytes(gzip.compress(data, mtime=0))
        manifest["files"]["candles.jsonl.gz"] = file_hash(path.parent / "candles.jsonl.gz")
    path.write_text(canonical(manifest))
    with pytest.raises((PaperError, ValueError)):
        prepare(config)
    assert not config.dataset_dir.exists() and not config.data_dir.exists()


def test_reducer_does_not_mutate_previous_state(config):
    ctx = context(config)
    state = initial(ctx)
    before = canonical(state.data())
    after = reduce(
        state, "BOUNDARY", {"at": ctx["start"], "closed": candle(ctx["start"] - STEP)}, ctx
    ).state
    assert canonical(state.data()) == before
    frozen = canonical(after.data())
    observed = reduce(after, "OBSERVE", {"bar_open": ctx["start"], "price": "120"}, ctx)
    assert canonical(after.data()) == frozen and observed.state.wallet.mark == 120


def test_unobserved_suffix_changes_protocol_but_not_causal_content(config):
    ctx = context(config)
    other = {
        **ctx,
        "run_key": "other-run",
        "config_hash": "other-config",
        "dataset_sha256": "future-changed",
    }
    first = signal(ctx).decision
    second = signal(other).decision
    assert first["content_id"] == second["content_id"]
    assert first["decision_id"] != second["decision_id"]


def test_pending_coordinator_keeps_original_slot(config, monkeypatch):
    from cripto_bot.domain.strategy.sma import Decision, TrendSMA

    ctx = context(config, "delay_1candle")
    state = signal(ctx).state
    action = state.pending["decision_id"]
    at = ctx["start"] + STEP
    state = reduce(state, "OBSERVE", {"bar_open": at, "price": "110"}, ctx).state
    monkeypatch.setattr(
        TrendSMA,
        "evaluate",
        lambda *args: Decision(ctx["module"]["instance_id"], "LONG", "FAKE_CONFLICT"),
    )
    effect = reduce(state, "BOUNDARY", {"at": at + STEP, "closed": candle(at)}, ctx)
    assert effect.decision["status"] == "COORDINATOR_REJECTED"
    assert effect.state.pending["decision_id"] == action
    assert effect.state.wallet.usdt_reserved == 0


def test_actual_utc_day_start_uses_close_before_new_open(config):
    ctx = context(config)
    ctx["end"] = ctx["start"] + 100 * STEP
    state = initial(ctx)
    closed = candle(ctx["start"] - STEP)
    for i in range(96):
        at = ctx["start"] + i * STEP
        state = reduce(state, "BOUNDARY", {"at": at, "closed": closed}, ctx).state
        price = str(100 + min(i, 10))
        state = reduce(state, "OBSERVE", {"bar_open": at, "price": price}, ctx).state
        if state.wallet.intents:
            action, intent = next(iter(state.wallet.intents.items()))
            state = reduce(
                state, "FILL", {"action_key": action, "fill_id": intent.plan.fill_id}, ctx
            ).state
        closed = candle(at, price)
    assert state.wallet.quantity > 0
    expected = state.wallet.usdt_free + money(state.wallet.quantity * Decimal("110"))
    midnight = ctx["start"] + 96 * STEP
    boundary = reduce(state, "BOUNDARY", {"at": midnight, "closed": closed}, ctx).state
    observed = reduce(boundary, "OBSERVE", {"bar_open": midnight, "price": "1000"}, ctx).state
    assert boundary.wallet.baseline == expected == observed.wallet.baseline
    assert observed.wallet.equity > expected


def test_report_failure_preserves_financial_database_and_can_retry(config, monkeypatch):
    from pathlib import Path

    ctx = context(config)
    with BacktestStore(config.data_dir, create=True) as store:
        store.freeze(protocol(config))
        execute_curve(store, config, ctx)
    before = file_hash(config.data_dir / "backtest.sqlite3")
    with BacktestStore(config.data_dir, readonly=True) as store:
        report = snapshot(store, store.reconcile_all())
        rename = Path.rename

        def fail(self, target):
            raise OSError("synthetic publication failure")

        with monkeypatch.context() as patch:
            patch.setattr(Path, "rename", fail)
            with pytest.raises(OSError):
                export_report(store, report, "failed-publication")
        assert Path.rename == rename
        assert not list((config.data_dir / "exports").glob(".pending-*"))
        path = export_report(store, report, "retry-publication")
        assert path.is_file()
    assert file_hash(config.data_dir / "backtest.sqlite3") == before


@pytest.mark.parametrize("kind", ["paper.sqlite3", "market.sqlite3"])
def test_rejects_existing_store_without_migration(config, kind):
    config.data_dir.mkdir(parents=True)
    (config.data_dir / kind).write_bytes(b"original")
    with pytest.raises(PaperError, match="INCOMPATIBLE"):
        with BacktestStore(config.data_dir, create=True):
            pass
    assert (config.data_dir / kind).read_bytes() == b"original"


def test_scheduled_reserve_and_fill_are_three_visible_commits(config):
    import sqlite3

    ctx = context(config)
    start = ctx["start"]
    key = ctx["run_key"]
    with BacktestStore(config.data_dir, create=True) as store:
        store.freeze(protocol(config))
        warmup = [candle(start + (i - 50) * STEP) for i in range(50)]
        state = store.create_run(ctx, warmup, "100")
        state = store.append(key, "b0", "BOUNDARY", {"at": start, "closed": warmup[-1]}, state)
        state = store.append(key, "o0", "OBSERVE", {"bar_open": start, "price": "100"}, state)
        state = store.append(
            key, "b1", "BOUNDARY", {"at": start + STEP, "closed": candle(start, "110")}, state
        )
        db = sqlite3.connect((config.data_dir / "backtest.sqlite3").as_uri() + "?mode=ro", uri=True)

        def persisted():
            return json.loads(
                db.execute("SELECT projection FROM runs WHERE run_key=?", (key,)).fetchone()[0]
            )

        assert persisted()["pending"]["status"] == "SCHEDULED"
        assert Decimal(persisted()["wallet"]["usdt_reserved"]) == 0
        state = store.append(
            key, "o1", "OBSERVE", {"bar_open": start + STEP, "price": "110"}, state
        )
        assert Decimal(persisted()["wallet"]["usdt_reserved"]) > 0
        assert db.execute("SELECT count(*) FROM fills").fetchone()[0] == 0
        action, intent = next(iter(state.wallet.intents.items()))
        state = store.append(
            key, "f1", "FILL", {"action_key": action, "fill_id": intent.plan.fill_id}, state
        )
        assert db.execute("SELECT count(*) FROM fills").fetchone()[0] == 1
        assert persisted()["pending"] is None and state.metrics.fills == 1
        db.close()


@pytest.mark.parametrize(
    "field,value", [("count", 1010.0), ("count", True), ("start", 1729814400000.0), ("end", True)]
)
def test_manifest_integers_are_strict(config, field, value):
    path = config.source_manifests[0]
    manifest = json.loads(path.read_text())
    manifest[field] = value
    path.write_text(canonical(manifest))
    with pytest.raises(ValueError):
        prepare(config)
    assert not config.data_dir.exists()


def test_disabled_instance_does_not_extend_active_warmup(config):
    from cripto_bot.adapters.config import load_config

    path = config.root / "config/backtest.toml"
    path.write_text(
        path.read_text()
        + """\n[modules.aaa_disabled]
module_id = "trend_sma"
enabled = false
[modules.aaa_disabled.params]
fast_window = 20
slow_window = 500
interval = "15m"
"""
    )
    updated = load_config(path, {}, root=config.root).backtest
    assert updated.module.slow_window == 50
    assert prepare(updated)["warmup"] == 50


def test_store_rejects_unfrozen_risk_and_blocked_initialization(config):
    ctx = context(config)
    start = ctx["start"]
    with BacktestStore(config.data_dir, create=True) as store:
        store.freeze(protocol(config))
        bad = {**ctx, "parameters": {**ctx["parameters"], "max_entry_fraction": "1"}}
        warmup = [candle(start + (i - 50) * STEP) for i in range(50)]
        with pytest.raises(PaperError, match="FROZEN_CURVE"):
            store.create_run(bad, warmup, "100")
        assert store.db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        store.block()
        with pytest.raises(PaperError, match="BLOCKED"):
            store.create_run(ctx, warmup, "100")


def test_foreign_schema_rejected_without_changing_journal_mode(config):
    import sqlite3
    from contextlib import closing

    config.data_dir.mkdir(parents=True)
    path = config.data_dir / "backtest.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE original (value TEXT)")
        db.execute("INSERT INTO original VALUES('preserve')")
    before = file_hash(path)
    with pytest.raises(PaperError, match="INCOMPATIBLE"):
        with BacktestStore(config.data_dir, create=True):
            pass
    assert file_hash(path) == before


def test_protocol_tampering_is_detected_and_blocks(config):
    ctx = context(config)
    with BacktestStore(config.data_dir, create=True) as store:
        store.freeze(protocol(config))
        execute_curve(store, config, ctx)
        trigger = store.db.execute(
            "SELECT sql FROM sqlite_schema WHERE name='immutable_protocol'"
        ).fetchone()[0]
        store.db.execute("DROP TRIGGER immutable_protocol")
        seal = store.protocol()
        seal["code_sha256"] = "tampered"
        store.db.execute("UPDATE protocol SET sealed=?", (canonical(seal),))
        store.db.execute(trigger)
        with pytest.raises(PaperError, match="RECONCILIATION"):
            store.reconcile(ctx["run_key"])
        assert store.blocked()


def test_zero_fills_first_execution_is_not_reported_as_reproduction(config):
    from cripto_bot.application.backtest_run import run_segment

    rows = source_rows(config)
    for row in rows:
        for field in ("open", "high", "low", "close"):
            row[field] = "0.01"
        row["quote_volume"] = "1"
    rewrite_source(config.source_manifests[0], rows)
    prepare(config)
    with BacktestStore(config.data_dir, create=True) as store:
        store.freeze(protocol(config))
        first = run_segment(store, config, "development")
        assert first["simulated_fills"] == 0 and first["reproduction"] is False
        again = run_segment(store, config, "development")
        assert again["simulated_fills"] == 0 and again["reproduction"] is True


def test_signal_deadline_is_separate_from_one_ms_quote_freshness(config):
    restricted = replace(config, paper=replace(config.paper, max_quote_age_ms=1))
    ctx = context(restricted)
    effect = signal(ctx)
    at = ctx["start"] + STEP
    assert effect.state.pending["valid_until"] == at + 5000
    admission = reduce(effect.state, "OBSERVE", {"bar_open": at, "price": "110"}, ctx).state
    action, intent = next(iter(admission.wallet.intents.items()))
    final = reduce(
        admission, "FILL", {"action_key": action, "fill_id": intent.plan.fill_id}, ctx
    ).state
    assert final.metrics.fills == 1


def test_historical_wallet_copy_keeps_mutable_intents_isolated():
    from copy import deepcopy

    # Mesmo contrato de deepcopy usado pelo financeiro compartilhado.
    from cripto_bot.domain.backtest.engine import State
    from cripto_bot.domain.portfolio.models import Intent

    state = State()
    state.wallet.intents["original"] = Intent("RISK_REJECTED", "hash")
    copied = deepcopy(state.wallet)
    assert copied.data() == state.wallet.data()
    copied.intents["original"].reason = "different"
    copied.usdt_free = Decimal("500")
    assert state.wallet.intents["original"].reason == "" and state.wallet.usdt_free == 0


def test_partial_report_marks_checkpoint_and_undefined_duration(config):
    from cripto_bot.adapters.storage.backtest_report import document

    ctx = context(config)
    start = ctx["start"]
    with BacktestStore(config.data_dir, create=True) as store:
        store.freeze(protocol(config))
        store.create_run(ctx, [candle(start + (i - 50) * STEP) for i in range(50)], "100")
        report = snapshot(store, store.reconcile_all())
        page = document(report)
        assert "PARCIAL" in page and "N/A" in page
        assert "Recusas / expirações" in page and "Janelas e custos congelados" in page
        assert report["curves"][0]["metrics"]["average_exposure"] is None


def test_market_and_backtest_cannot_share_directory(config):
    from cripto_bot.adapters.config import ConfigError, load_config

    path = config.root / "config/backtest.toml"
    folder = str(config.data_dir.relative_to(config.root))
    path.write_text(
        path.read_text()
        + f'''\n[market]
source = "fixture"
fixture_file = "config/unused-market.json"
data_dir = "{folder}"
'''
    )
    with pytest.raises(ConfigError):
        load_config(path, {}, root=config.root)
    assert not config.data_dir.exists()
