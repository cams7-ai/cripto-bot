"""Compatibility adapters; one analytical implementation lives in cripto_analytics."""

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from cripto_bot.adapters.analytics_dependency import load
from cripto_bot.adapters.storage.analytics_source import AnalyticsRows
from cripto_bot.adapters.storage.backtest_sqlite import BacktestStore

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def __getattr__(name: str) -> Any:
    if name in {"COLUMNS", "TIMESTAMPS", "PRECISION"}:
        return getattr(load("cripto_analytics.domain.contracts"), name)
    raise AttributeError(name)


def local_connection(directory: Path) -> "DuckDBPyConnection":
    return cast(
        "DuckDBPyConnection",
        load("cripto_analytics.adapters.duckdb_export").local_connection(directory),
    )


def _parquet(db: "DuckDBPyConnection", directory: Path, name: str) -> dict[str, int]:
    return cast(
        dict[str, int],
        load("cripto_analytics.adapters.duckdb_export")._parquet(db, directory, name),
    )


def export_analytics(
    store: BacktestStore, report: dict[str, object], directory: Path
) -> dict[str, object]:
    return cast(
        dict[str, object],
        load("cripto_analytics.adapters.duckdb_export").export_analytics(
            AnalyticsRows(store),
            report,
            directory,
            connection_factory=local_connection,
            parquet_writer=_parquet,
        ),
    )
