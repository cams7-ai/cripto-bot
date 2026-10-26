"""Parquet exato, consultas SQL e publicacao atomica sem efeitos financeiros."""

import csv
import hashlib
import json
from dataclasses import replace
from itertools import islice

import duckdb
import pytest

from cripto_bot.adapters.backtest_dataset import file_hash, records
from cripto_bot.adapters.storage import backtest_analytics as analytics
from cripto_bot.adapters.storage.backtest_report import export_report, snapshot
from cripto_bot.adapters.storage.backtest_sqlite import BacktestStore
from cripto_bot.application.backtest_run import execute_curve, protocol
from cripto_bot.domain.models import Settings
from cripto_bot.entrypoints.backtest import execute_backtest
from tests.unit.test_backtest import config as config
from tests.unit.test_backtest import context


@pytest.mark.parametrize("stage", ["empty", "partial", "completed"])
def test_sql_parquet_csv_and_report_agree_without_changing_ledger(config, stage):
    config = replace(
        config, paper=replace(config.paper, data_dir=config.data_dir.parent / "wallet's data")
    )
    ctx = context(config)
    with BacktestStore(config.data_dir, create=True) as store:
        store.freeze(protocol(config))
        if stage == "partial":
            store.create_run(ctx, list(islice(records(config), 50)), "100")
        if stage == "completed":
            execute_curve(store, config, ctx)
    before = file_hash(config.data_dir / "backtest.sqlite3")
    with BacktestStore(config.data_dir, readonly=True) as store:
        report = snapshot(store, store.reconcile_all())
        for attempt in ("first", "reexport"):
            manifest_path = export_report(store, report, attempt)
            directory = manifest_path.parent
            manifest = json.loads(manifest_path.read_text())
            assert manifest["result_sha256"] == report["result_sha256"]
            for name, digest in manifest["files"].items():
                assert file_hash(directory / name) == digest
            info = json.loads((directory / "analytics.json").read_text())
            assert info == manifest["analytics"]
            assert info["curves"] == (0 if stage == "empty" else 1)
            with duckdb.connect(str(directory / "analytics.duckdb"), read_only=True) as db:
                for name, columns in analytics.COLUMNS.items():
                    with (directory / f"{name}.csv").open(newline="") as stream:
                        expected = list(csv.DictReader(stream))
                    rows = db.execute(
                        "SELECT * FROM read_parquet(?) ORDER BY export_row",
                        [str(directory / f"{name}.parquet")],
                    ).fetchall()
                    assert len(rows) == len(expected) == info["tables"][name]["rows"]
                    for ordinal, (row, original) in enumerate(zip(rows, expected, strict=True), 1):
                        assert row[0] == ordinal
                        assert row[1:] == tuple(
                            (int(original[col]) if original[col] else None)
                            if col in analytics.TIMESTAMPS
                            else original[col] or None
                            for col in columns
                        )
                curves = db.execute("SELECT * FROM curve_summary").fetchall()
                assert len(curves) == info["curves"]
                if curves:
                    c = report["curves"][0]
                    row = curves[0]
                    assert row[4] == (stage == "completed")
                    assert row[6:10] == tuple(
                        c["metrics"][k]
                        for k in ("final_equity", "net_return", "maximum_drawdown", "fees")
                    )
                    assert row[-1] == c["metrics"]["fills"]
                    assert row[-2] == c["metrics"]["decisions"]
                    assert (
                        db.execute("SELECT coalesce(sum(count),0) FROM trades_counts").fetchone()[0]
                        == row[-1]
                    )
                    assert (
                        db.execute(
                            "SELECT coalesce(sum(count),0) FROM decisions_counts"
                        ).fetchone()[0]
                        == row[-2]
                    )
            page = (directory / "report.html").read_text()
            assert 'href="equity.parquet"' in page and 'href="analytics.duckdb"' in page
    assert file_hash(config.data_dir / "backtest.sqlite3") == before


@pytest.mark.parametrize("failure", ["count", "engine"])
def test_analytic_failure_does_not_publish_or_modify_ledger(config, monkeypatch, failure):
    ctx = context(config)
    with BacktestStore(config.data_dir, create=True) as store:
        store.freeze(protocol(config))
        execute_curve(store, config, ctx)
    before = file_hash(config.data_dir / "backtest.sqlite3")
    if failure == "count":
        original = analytics._parquet

        def changed(*args):
            original(*args)
            return {}

        monkeypatch.setattr(analytics, "_parquet", changed)
    else:

        def broken(*args):
            raise duckdb.IOException("controlled failure")

        monkeypatch.setattr(analytics, "local_connection", broken)
    code, result = execute_backtest(Settings(backtest=config), "backtest-report", "failed")
    assert code == 3 and result["simulated_fills"] == 0
    assert result["error_code"] == (
        "ANALYTICS_ROW_COUNT_DIVERGENCE" if failure == "count" else "ANALYTICS_EXPORT_FAILED"
    )
    assert list((config.data_dir / "exports").iterdir()) == []
    assert file_hash(config.data_dir / "backtest.sqlite3") == before


def test_financial_strings_beyond_decimal38_and_json_remain_exact(tmp_path):
    long_value = "123456789012345678901234567890123456789012345678901234567890123456789012.12345678"
    columns = analytics.COLUMNS["trades"]
    original = dict.fromkeys(columns, long_value)
    original.update(
        run_key="run",
        side="BUY",
        fill_at="1792886400002",
        data_json='{"reason":"razão, com \\"aspas\\" e Unicode"}',
    )
    with (tmp_path / "trades.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerow(original)
    with analytics.local_connection(tmp_path) as db:
        assert analytics._parquet(db, tmp_path, "trades") == {"run": 1}
        row = db.execute(
            "SELECT quantity,data_json,fill_at FROM read_parquet(?)",
            [str(tmp_path / "trades.parquet")],
        ).fetchone()
        assert row == (long_value, original["data_json"], 1792886400002)
        assert db.execute("SELECT current_setting('enable_external_access')").fetchone() == (False,)
        outside = tmp_path.parent / "outside.csv"
        outside.write_text("value\n1\n")
        with pytest.raises(duckdb.Error):
            db.execute("SELECT * FROM read_csv(?)", [str(outside)])
        with pytest.raises(duckdb.Error):
            db.execute("INSTALL httpfs")


def test_wide_decision_export_uses_bounded_parquet_row_groups(tmp_path):
    # More than one old default row group of wide, distinct decision payloads.
    total = 150_000
    columns = analytics.COLUMNS["decisions"]
    with (tmp_path / "decisions.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for index in range(total):
            digest = hashlib.sha256(str(index).encode()).hexdigest()
            writer.writerow(
                {
                    "run_key": "wide",
                    "decision_id": digest,
                    "as_of": "2026-10-25T00:00:00+00:00",
                    "target": "HOLD",
                    "reason": "NO_CROSS",
                    "status": "HOLD",
                    "prefix_sha256": digest,
                    "data_json": json.dumps({"id": index, "payload": digest * 20}),
                }
            )
    with analytics.local_connection(tmp_path) as db:
        assert analytics._parquet(db, tmp_path, "decisions") == {"wide": total}
        assert (
            db.execute(
                "SELECT max(row_group_num_rows) FROM parquet_metadata(?)",
                [str(tmp_path / "decisions.parquet")],
            ).fetchone()[0]
            < 16_384
        )
        assert db.execute(
            "SELECT min(export_row),max(export_row) FROM read_parquet(?)",
            [str(tmp_path / "decisions.parquet")],
        ).fetchone() == (1, total)
