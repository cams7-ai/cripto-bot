"""Comandos historicos locais, sem importar transporte HTTP."""

import sqlite3

from cripto_bot.adapters.analytics_dependency import require_analytics
from cripto_bot.adapters.backtest_dataset import prepare
from cripto_bot.adapters.storage.backtest_report import export_report, snapshot
from cripto_bot.adapters.storage.backtest_sqlite import BacktestStore
from cripto_bot.application.backtest_run import protocol, run_segment, verify_protocol
from cripto_bot.domain.models import Settings
from cripto_bot.domain.portfolio.models import PaperError


def execute_backtest(
    settings: Settings, command: str, run_id: str, segment: str = "development"
) -> tuple[int, dict[str, object]]:
    config = settings.backtest
    result: dict[str, object] = {"orders_sent": 0, "simulated_fills": 0}
    if config is None and command != "backtest-run":
        return 2, {**result, "status": "BACKTEST_FAILED", "error_code": "BACKTEST_CONFIG_REQUIRED"}
    if config is None or config.module is None:
        return 0, {**result, "status": "BACKTEST_NOOP"}
    try:
        if command in {"backtest-run", "backtest-report"}:
            require_analytics(engine=True)
        if command == "backtest-prepare":
            return 0, {**result, "status": "BACKTEST_PREPARED", "dataset": prepare(config)}
        with BacktestStore(
            config.data_dir,
            create=command == "backtest-freeze",
            readonly=command == "backtest-report",
        ) as store:
            if command == "backtest-freeze":
                sealed = protocol(config)
                store.freeze(sealed)
                result.update(protocol_hash=sealed["protocol_hash"], status="BACKTEST_FROZEN")
            elif command == "backtest-run":
                result.update(run_segment(store, config, segment))
                result["status"] = "BACKTEST_COMPLETED"
                report = snapshot(store, store.reconcile_all())
                manifest = export_report(store, report, run_id)
                result.update(
                    manifest=str(manifest),
                    html_report=str(manifest.parent / "report.html"),
                    result_sha256=report["result_sha256"],
                    analytics_database=str(manifest.parent / "analytics.duckdb"),
                    analytics_manifest=str(manifest.parent / "analytics.json"),
                )
            elif command == "backtest-reconcile":
                verify_protocol(store, config)
                result.update(
                    status="BACKTEST_RECONCILED", curves=len(store.reconcile_all()), reconciled=True
                )
            elif command == "backtest-report":
                # A report remains exportable after a code/config change: sealed journal is authoritative.  # noqa: E501
                report = snapshot(store, store.reconcile_all())
                manifest = export_report(store, report, run_id)
                result.update(
                    status="BACKTEST_REPORTED",
                    manifest=str(manifest),
                    html_report=str(manifest.parent / "report.html"),
                    result_sha256=report["result_sha256"],
                    analytics_database=str(manifest.parent / "analytics.duckdb"),
                    analytics_manifest=str(manifest.parent / "analytics.json"),
                )
            return 0, result
    except (
        PaperError,
        OSError,
        EOFError,
        RecursionError,
        ValueError,
        KeyError,
        sqlite3.Error,
    ) as error:
        result.update(
            status="BACKTEST_FAILED",
            error_code=str(error)
            if isinstance(error, PaperError)
            else "BACKTEST_DATA_OR_STORAGE_FAILURE",
        )
        return (2 if command == "backtest-prepare" else 3), result
