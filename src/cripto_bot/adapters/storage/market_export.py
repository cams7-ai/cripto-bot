"""Snapshots derivados do banco, publicados por rename no mesmo filesystem."""

import base64
import gzip
import importlib.metadata
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from cripto_bot.adapters.provenance import runtime_identity
from cripto_bot.adapters.storage.market_sqlite import SQLiteMarketStore
from cripto_bot.domain.market import PARSER, SCHEMA, MarketError, canonical, digest
from cripto_bot.observability.logging import utc_now, utc_timestamp


def code_revision(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
            timeout=2,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
            timeout=2,
        )
        return result.stdout.strip() + ("+dirty" if status.stdout else "")
    except (OSError, subprocess.SubprocessError):
        return "unversioned"


def source_fingerprint(root: Path) -> str:
    return digest(canonical(runtime_identity(root)).encode())


def export_market(
    store: SQLiteMarketStore,
    dataset: str,
    start: int,
    end: int,
    run_id: str,
    context: dict[str, object],
) -> Path:
    store.verify_prefix(dataset, start, end)
    records = store.records(dataset, start, end)
    content = "".join(canonical(row) + "\n" for row in records).encode()
    parent = store.directory / "exports"
    if not parent.resolve().is_relative_to(store.directory.resolve()) or not re.fullmatch(
        r"[A-Za-z0-9-]{1,64}", run_id
    ):
        raise MarketError("UNSAFE_EXPORT_PATH")
    parent.mkdir(exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=parent))
    target = parent / run_id
    try:
        blobs: dict[str, bytes] = {"candles.jsonl.gz": gzip.compress(content, mtime=0)}
        raw_lines: list[str] = []
        for row in store.db.execute(
            "SELECT id,route,params,body,sha256,status,headers,received_at,parser "
            "FROM raw WHERE dataset=? ORDER BY id",
            (dataset,),
        ):
            raw_lines.append(
                canonical(
                    dict(
                        zip(
                            (
                                "id",
                                "route",
                                "params",
                                "body_base64",
                                "sha256",
                                "status",
                                "headers",
                                "received_at",
                                "parser",
                            ),
                            (row[0], row[1], row[2], base64.b64encode(row[3]).decode(), *row[4:]),
                            strict=True,
                        )
                    )
                )
            )
        blobs["raw.jsonl.gz"] = gzip.compress(("\n".join(raw_lines) + "\n").encode(), mtime=0)
        snapshots = [
            {"raw_id": row[0], "data": row[1]}
            for row in store.db.execute(
                "SELECT raw_id,data FROM snapshots WHERE dataset=? ORDER BY id", (dataset,)
            )
        ]
        blobs["snapshots.json"] = canonical(snapshots).encode()
        provenance = [
            {
                "open_time": row[0],
                "raw_id": row[1],
                "raw_sha256": row[2],
                "received_at": row[3],
                "processed_at": row[4],
            }
            for row in store.db.execute(
                "SELECT open_time,raw_id,raw.sha256,received_at,processed_at FROM candles "
                "JOIN raw ON raw.id=candles.raw_id WHERE candles.dataset=? "
                "AND open_time>=? AND open_time<? ORDER BY open_time",
                (dataset, start, end),
            )
        ]
        blobs["provenance.json"] = canonical(provenance).encode()
        for name, body in blobs.items():
            with (temporary / name).open("wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
        manifest = {
            **context,
            "dataset": dataset,
            "symbol": "BTCUSDT",
            "interval": "15m",
            "start": start,
            "end": end,
            "count": len(records),
            "complete": True,
            "schema": SCHEMA,
            "parser": PARSER,
            "run_id": run_id,
            "exported_at": utc_timestamp(utc_now()),
            "package_version": importlib.metadata.version("cripto-bot"),
            "data_sha256": digest(content),
            "files": {name: digest(body) for name, body in blobs.items()},
        }
        with (temporary / "manifest.json").open("w") as stream:
            stream.write(canonical(manifest) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.rename(target)
        return target / "manifest.json"
    except BaseException:
        shutil.rmtree(temporary)
        raise
