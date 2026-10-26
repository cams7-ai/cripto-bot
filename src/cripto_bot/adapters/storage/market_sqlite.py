"""Store transacional e lock de escritor no filesystem local Linux."""

import fcntl
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from cripto_bot.application.ports import RawResponse
from cripto_bot.domain.market import PARSER, STEP, Candle, MarketError, canonical, digest, mapping
from cripto_bot.observability.logging import utc_now, utc_timestamp

DDL = (
    "CREATE TABLE raw (id INTEGER PRIMARY KEY, dataset TEXT NOT NULL, route TEXT NOT NULL, "
    "params TEXT NOT NULL, body BLOB NOT NULL, sha256 TEXT NOT NULL, status INTEGER NOT NULL, "
    "headers TEXT NOT NULL, received_at TEXT NOT NULL, parser TEXT NOT NULL)",
    "CREATE TABLE candles (dataset TEXT NOT NULL, open_time INTEGER NOT NULL, data TEXT NOT NULL, "
    "sha256 TEXT NOT NULL, raw_id INTEGER NOT NULL REFERENCES raw(id), processed_at TEXT NOT NULL, "
    "PRIMARY KEY(dataset, open_time))",
    "CREATE TABLE checkpoints (dataset TEXT NOT NULL, start INTEGER NOT NULL, "
    "next_open INTEGER NOT NULL, "
    "PRIMARY KEY(dataset, start))",
    "CREATE TABLE snapshots (id INTEGER PRIMARY KEY, dataset TEXT NOT NULL, "
    "raw_id INTEGER NOT NULL REFERENCES raw(id), data TEXT NOT NULL)",
    "CREATE TABLE restrictions (source TEXT PRIMARY KEY, not_before REAL NOT NULL)",
)


class SQLiteMarketStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.db: sqlite3.Connection
        self._lock: BinaryIO | None = None

    def __enter__(self) -> "SQLiteMarketStore":
        self.directory.mkdir(parents=True, exist_ok=True)
        if any((self.directory / name).is_symlink() for name in (".writer.lock", "market.sqlite3")):
            raise MarketError("UNSAFE_STORAGE_PATH")
        handle = (self.directory / ".writer.lock").open("ab")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise MarketError("WRITER_BUSY") from None
        self._lock = handle
        try:
            self.db = sqlite3.connect(
                self.directory / "market.sqlite3", timeout=1, isolation_level=None
            )
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA synchronous=FULL")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if version == 0 and not tables:
                with self.transaction():
                    for statement in DDL:
                        self.db.execute(statement)
                    self.db.execute("PRAGMA user_version=1")
            elif version != 1 or {row[0] for row in tables} != {
                "raw",
                "candles",
                "checkpoints",
                "snapshots",
                "restrictions",
            }:
                raise MarketError("INCOMPATIBLE_SCHEMA")
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise MarketError("CORRUPT_DATABASE")
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if hasattr(self, "db"):
            self.db.close()
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _raw(self, dataset: str, raw: RawResponse) -> int:
        cursor = self.db.execute(
            "INSERT INTO raw(dataset,route,params,body,sha256,status,headers,received_at,parser) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                dataset,
                raw.route,
                canonical(raw.params),
                raw.body,
                raw.sha256,
                raw.status,
                canonical(raw.headers),
                raw.received_at,
                PARSER,
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def save_snapshot(self, dataset: str, raw: RawResponse, normalized: object) -> None:
        with self.transaction():
            raw_id = self._raw(dataset, raw)
            self.db.execute(
                "INSERT INTO snapshots(dataset,raw_id,data) VALUES(?,?,?)",
                (dataset, raw_id, canonical(normalized)),
            )

    def checkpoint(self, dataset: str, start: int) -> int:
        row = self.db.execute(
            "SELECT next_open FROM checkpoints WHERE dataset=? AND start=?", (dataset, start)
        ).fetchone()
        return start if row is None else int(row[0])

    def records(self, dataset: str, start: int, end: int) -> list[dict[str, object]]:
        rows = self.db.execute(
            "SELECT open_time,data,candles.sha256,raw.body,raw.sha256 FROM candles "
            "JOIN raw ON raw.id=candles.raw_id WHERE candles.dataset=? "
            "AND open_time>=? AND open_time<? ORDER BY open_time",
            (dataset, start, end),
        ).fetchall()
        result: list[dict[str, object]] = []
        for opened, data, sha, body, raw_sha in rows:
            if digest(data.encode()) != sha or digest(body) != raw_sha:
                raise MarketError("CORRUPT_RECORD")
            item = mapping(RawResponse("stored", {}, data.encode(), "").json())
            if item.get("open_time") != opened:
                raise MarketError("CORRUPT_RECORD")
            result.append(item)
        return result

    def verify_prefix(self, dataset: str, start: int, end: int) -> None:
        if end < start or (end - start) % STEP:
            raise MarketError("CORRUPT_CHECKPOINT")
        records = self.records(dataset, start, end)
        if len(records) != (end - start) // STEP or any(
            row["open_time"] != start + i * STEP for i, row in enumerate(records)
        ):
            raise MarketError("CORRUPT_CHECKPOINT")

    def commit_page(
        self, dataset: str, start: int, next_open: int, raw: RawResponse, candles: list[Candle]
    ) -> tuple[int, int]:
        inserted = duplicated = 0
        expected = self.checkpoint(dataset, start)
        with self.transaction():
            raw_id = self._raw(dataset, raw)
            for candle in candles:
                text = canonical(candle.record())
                row = self.db.execute(
                    "SELECT data FROM candles WHERE dataset=? AND open_time=?",
                    (dataset, candle.open_time),
                ).fetchone()
                if row is not None:
                    if row[0] != text:
                        raise MarketError("CANDLE_CONFLICT")
                    duplicated += 1
                else:
                    self.db.execute(
                        "INSERT INTO candles VALUES(?,?,?,?,?,?)",
                        (
                            dataset,
                            candle.open_time,
                            text,
                            digest(text.encode()),
                            raw_id,
                            utc_timestamp(utc_now()),
                        ),
                    )
                    inserted += 1
            # Checkpoint so avanca junto a um prefixo efetivamente persistido.
            if next_open <= expected:
                raise MarketError("NO_PROGRESS")
            self.verify_prefix(dataset, start, next_open)
            self.db.execute(
                "INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(dataset,start) "
                "DO UPDATE SET next_open=excluded.next_open",
                (dataset, start, next_open),
            )
        return inserted, duplicated

    def not_before(self) -> float:
        row = self.db.execute(
            "SELECT not_before FROM restrictions WHERE source='binance_public'"
        ).fetchone()
        return 0.0 if row is None else float(row[0])

    def block_until(self, instant: float) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT INTO restrictions VALUES('binance_public',?) ON CONFLICT(source) "
                "DO UPDATE SET not_before=MAX(not_before,excluded.not_before)",
                (instant,),
            )
