"""Projecoes analiticas descartaveis; SQLite continua sendo o ledger autoritativo."""

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from cripto_bot.domain.market import canonical, sequence
from cripto_bot.domain.portfolio.models import PaperError, require_object

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

    from cripto_bot.adapters.storage.backtest_sqlite import BacktestStore

COLUMNS = {
    "equity": (
        "run_key",
        "at",
        "equity",
        "usdt_free",
        "usdt_reserved",
        "btc",
        "mark",
        "cost_basis",
        "fees",
        "realized",
        "unrealized",
        "exposure",
        "drawdown",
    ),
    "trades": (
        "run_key",
        "action_key",
        "fill_id",
        "side",
        "quantity",
        "price",
        "notional",
        "fee",
        "mark",
        "fill_at",
        "realized_delta",
        "cost_delta",
        "data_json",
    ),
    "decisions": (
        "run_key",
        "decision_id",
        "as_of",
        "target",
        "reason",
        "status",
        "scheduled_at",
        "valid_until",
        "budget_cap",
        "prefix_sha256",
        "data_json",
    ),
}
TIMESTAMPS = {"at", "fill_at", "scheduled_at", "valid_until"}
SOURCE_TABLES = {"equity": "equity", "trades": "fills", "decisions": "decisions"}
PRECISION = (
    "Financial values/as_of remain canonical VARCHAR; "
    "millisecond timestamps/export_row BIGINT; no float"
)


def local_connection(directory: Path) -> "DuckDBPyConnection":
    """Nenhuma extensao/rede; somente arquivos dentro da publicacao pendente."""
    import duckdb

    db = duckdb.connect(
        str(directory / "analytics.duckdb"),
        config={
            "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false",
            "threads": "1",
            "memory_limit": "256MB",
            "temp_directory": str(directory / ".duckdb-tmp"),
        },
    )
    try:
        # allowed_directories must be set before disabling external access (DuckDB 1.5).
        db.execute("SET allowed_directories = ?", [[str(directory.resolve())]])
        db.execute("SET enable_external_access = false")
        db.execute("SET lock_configuration = true")
        return db
    except BaseException:
        db.close()
        raise


def _parquet(db: "DuckDBPyConnection", directory: Path, name: str) -> dict[str, int]:
    columns = COLUMNS[name]
    definitions = ",".join(f"'{column}':'VARCHAR'" for column in columns)
    selected = ",".join(
        f'CAST("{column}" AS BIGINT) AS "{column}"' if column in TIMESTAMPS else f'"{column}"'
        for column in columns
    )
    # Named bindings avoid COPY's positional parameter ordering and escape paths correctly.
    db.execute(
        f"COPY (SELECT row_number() OVER () AS export_row,{selected} "
        f"FROM read_csv($source,header=true,columns={{{definitions}}},nullstr='')) "
        "TO $target (FORMAT PARQUET,COMPRESSION ZSTD,ROW_GROUP_SIZE 8192)",
        {"source": str(directory / f"{name}.csv"), "target": str(directory / f"{name}.parquet")},
    )
    return {
        str(key): int(count)
        for key, count in db.execute(
            "SELECT run_key,count(*) FROM read_parquet(?) GROUP BY run_key",
            [str(directory / f"{name}.parquet")],
        ).fetchall()
    }


def export_analytics(
    store: "BacktestStore", report: dict[str, object], directory: Path
) -> dict[str, object]:
    """SQL consulta os Parquets; totais sao confrontados com o snapshot SQLite."""
    import duckdb

    try:
        with local_connection(directory) as db:
            db.execute("CREATE TABLE metadata (key VARCHAR PRIMARY KEY,value VARCHAR NOT NULL)")
            db.execute(
                "CREATE TABLE curve_summary (run_key VARCHAR PRIMARY KEY,segment VARCHAR,"
                "variant VARCHAR,profile VARCHAR,completed BOOLEAN,measured_until BIGINT,"
                "final_equity VARCHAR,net_return VARCHAR,maximum_drawdown VARCHAR,fees VARCHAR,"
                "equity_rows BIGINT,decisions BIGINT,fills BIGINT)"
            )
            counts = {name: _parquet(db, directory, name) for name in COLUMNS}
            tables: dict[str, object] = {}
            for name, source in SOURCE_TABLES.items():
                expected = {
                    str(key): int(count)
                    for key, count in store.db.execute(
                        f"SELECT run_key,count(*) FROM {source} GROUP BY run_key"
                    )
                }
                if counts[name] != expected:
                    raise PaperError("ANALYTICS_ROW_COUNT_DIVERGENCE")
                tables[name] = {
                    "file": f"{name}.parquet",
                    "rows": sum(counts[name].values()),
                    "columns": {
                        "export_row": "BIGINT",
                        **{
                            col: "BIGINT" if col in TIMESTAMPS else "VARCHAR"
                            for col in COLUMNS[name]
                        },
                    },
                }
            keys = set()
            for raw in sequence(report["curves"]):
                curve = require_object(raw)
                key = str(curve["run_key"])
                keys.add(key)
                metrics = require_object(curve["metrics"])
                if (
                    counts["trades"].get(key, 0) != metrics["fills"]
                    or counts["decisions"].get(key, 0) != metrics["decisions"]
                ):
                    raise PaperError("ANALYTICS_METRIC_COUNT_DIVERGENCE")
                db.execute(
                    "INSERT INTO curve_summary VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        key,
                        curve["segment"],
                        curve["variant"],
                        curve["profile"],
                        curve["completed"],
                        curve["measured_until"],
                        metrics["final_equity"],
                        metrics["net_return"],
                        metrics["maximum_drawdown"],
                        metrics["fees"],
                        counts["equity"].get(key, 0),
                        counts["decisions"].get(key, 0),
                        counts["trades"].get(key, 0),
                    ],
                )
            if any(set(rows) - keys for rows in counts.values()):
                raise PaperError("ANALYTICS_UNKNOWN_CURVE")
            for name, fields in (("decisions", "status,reason"), ("trades", "side")):
                db.execute(
                    f"CREATE TABLE {name}_counts AS SELECT run_key,{fields},count(*) AS count "
                    f"FROM read_parquet(?) GROUP BY ALL",
                    [str(directory / f"{name}.parquet")],
                )
            metadata = {
                "schema": 1,
                "engine": "duckdb",
                "engine_version": duckdb.__version__,
                "catalog": "analytics.duckdb",
                "compression": "ZSTD",
                "precision": PRECISION,
                "protocol_hash": require_object(report["protocol"])["protocol_hash"],
                "result_sha256": report["result_sha256"],
                "tables": tables,
                "curves": len(keys),
                "reconciled_source": True,
            }
            db.executemany(
                "INSERT INTO metadata VALUES (?,?)",
                [(key, canonical(value)) for key, value in metadata.items()],
            )
            db.execute("CHECKPOINT")
        shutil.rmtree(directory / ".duckdb-tmp", ignore_errors=True)
        (directory / "analytics.json").write_text(canonical(metadata) + "\n")
        return metadata
    except duckdb.Error as error:
        raise PaperError("ANALYTICS_EXPORT_FAILED") from error
