"""Single-writer event journal and exact projections for the new continuous model."""

import fcntl
import json
import os
import sqlite3
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from cripto_core.errors import PaperError
from cripto_core.serialization import canonical, fingerprint
from cripto_core.validation import require_object

from cripto_bot.domain.continuous.engine import reduce
from cripto_bot.domain.continuous.models import MODEL, ContinuousState

DDL = (
    (
        "CREATE TABLE journal (seq INTEGER PRIMARY KEY,event_key TEXT NOT NULL "
        "UNIQUE,kind TEXT NOT NULL,at INTEGER NOT NULL,payload TEXT NOT "
        "NULL,payload_hash TEXT NOT NULL,previous_hash TEXT NOT NULL,chain_hash "
        "TEXT NOT NULL,state_hash TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE postings (seq INTEGER NOT NULL REFERENCES journal(seq),idx "
        "INTEGER NOT NULL,asset TEXT NOT NULL CHECK(asset IN "
        "('USDT','BTC')),account TEXT NOT NULL,quantity TEXT NOT NULL,PRIMARY "
        "KEY(seq,idx))"
    ),
    (
        "CREATE TABLE projection (id INTEGER PRIMARY KEY CHECK(id=1),version "
        "INTEGER NOT NULL,data TEXT NOT NULL)"
    ),
    "CREATE TABLE orders (order_id TEXT PRIMARY KEY,data TEXT NOT NULL)",
    (
        "CREATE TABLE fills (fill_id TEXT PRIMARY KEY,order_id TEXT NOT NULL "
        "REFERENCES orders(order_id),seq INTEGER NOT NULL REFERENCES "
        "journal(seq),data TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE cycles (close_time INTEGER PRIMARY KEY,decision_id TEXT "
        "NOT NULL UNIQUE,seq INTEGER NOT NULL REFERENCES journal(seq),data TEXT "
        "NOT NULL)"
    ),
    (
        "CREATE TABLE configurations (generation INTEGER PRIMARY KEY,seq INTEGER "
        "NOT NULL REFERENCES journal(seq),data TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE controls (request_id TEXT PRIMARY KEY,seq INTEGER NOT NULL "
        "REFERENCES journal(seq),data TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE notification_events (event_id TEXT PRIMARY KEY,seq INTEGER "
        "NOT NULL REFERENCES journal(seq),source_hash TEXT NOT NULL,data TEXT "
        "NOT NULL)"
    ),
    "CREATE INDEX notification_seq ON notification_events(seq)",
)
IMMUTABLE = tuple(
    f"CREATE TRIGGER immutable_{table}_{mutation} BEFORE {mutation} ON {table} "
    "BEGIN SELECT RAISE(ABORT,'immutable continuous history'); END"
    for table in (
        "journal",
        "postings",
        "fills",
        "cycles",
        "configurations",
        "controls",
        "notification_events",
    )
    for mutation in ("UPDATE", "DELETE")
)


def notice(
    kind: str,
    payload: dict[str, object],
    state: ContinuousState,
    seq: int,
    old: ContinuousState | None,
) -> dict[str, object] | None:
    category = None
    if kind == "ARM":
        category = "STARTED"
    elif kind == "CONTROL":
        category = "CONTROL"
    elif kind in {"MISSED", "LATE_COMMIT"}:
        category = "DATA_UNAVAILABLE"
    elif kind == "DAILY":
        category = "DAILY"
    elif kind == "END":
        category = "ENDED"
    elif kind == "UNKNOWN":
        category = "UNKNOWN"
    elif kind == "METADATA" and old and state.paused and not old.paused:
        category = "DATA_UNAVAILABLE"
    elif (
        kind == "POLL"
        and old
        and (
            (state.wallet.daily_blocked and not old.wallet.daily_blocked)
            or (state.wallet.drawdown_blocked and not old.wallet.drawdown_blocked)
        )
    ):
        category = "BLOCKED"
    elif kind == "POLL" and old and old.wallet.fees != state.wallet.fees:
        category = "CUSTODY"
    if category is None:
        return None
    event = {
        "pilot_id": state.pilot_id,
        "category": category,
        "at": payload["at"],
        "seq": seq,
        "reason": str(payload.get("reason", ""))[:128],
        "day": str(payload.get("day", "")),
        "financial_sha256": fingerprint(
            {"wallet": state.wallet.data(), "orders": state.data()["orders"]}
        ),
    }
    event["event_id"] = fingerprint(event)
    return event


class ContinuousStore:
    def __init__(self, directory: Path, *, create: bool = False, readonly: bool = False) -> None:
        self.directory = directory
        self.create = create
        self.readonly = readonly
        self.db: sqlite3.Connection
        self.lock: BinaryIO | None = None
        self._state: ContinuousState | None = None

    def __enter__(self) -> "ContinuousStore":
        path = self.directory / "continuous.sqlite3"
        if self.create:
            if any(
                (self.directory / name).exists()
                for name in [
                    "paper.sqlite3",
                    "backtest.sqlite3",
                    "market.sqlite3",
                    "notifications.sqlite3",
                ]
            ):
                raise PaperError("INCOMPATIBLE_CONTINUOUS_DIRECTORY")
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.directory.is_dir():
            raise PaperError("CONTINUOUS_STORE_MISSING")
        if path.is_symlink() or (self.directory / ".writer.lock").is_symlink():
            raise PaperError("UNSAFE_STORE_PATH")
        if not self.readonly:
            self.lock = (self.directory / ".writer.lock").open("ab")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.lock.close()
                self.lock = None
                raise PaperError("WRITER_BUSY") from None
        try:
            exists = path.exists()
            if not exists and (not self.create or self.readonly):
                raise PaperError("CONTINUOUS_STORE_MISSING")
            uri = path.as_uri() + (
                "?mode=ro" if self.readonly else "?mode=rwc" if self.create else "?mode=rw"
            )
            self.db = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=0)
            self.db.execute("PRAGMA foreign_keys=ON")
            if not self.readonly:
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.execute("PRAGMA synchronous=FULL")
            if not exists:
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in (*DDL, *IMMUTABLE):
                        self.db.execute(stmt)
                    self.db.execute("PRAGMA user_version=1")
                    self.db.execute("COMMIT")
                except BaseException:
                    self.db.execute("ROLLBACK")
                    raise
            actual = {
                n: " ".join(sql.split())
                for n, sql in self.db.execute(
                    "SELECT name,sql FROM sqlite_schema WHERE sql IS NOT NULL"
                )
            }
            expected = {stmt.split()[2]: " ".join(stmt.split()) for stmt in (*DDL, *IMMUTABLE)}
            if actual != expected or self.db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise PaperError("INCOMPATIBLE_CONTINUOUS_SCHEMA")
            if self.db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise PaperError("CONTINUOUS_CORRUPTION")
            if self.readonly:
                self.db.execute("BEGIN")
            if self.db.execute("SELECT count(*) FROM journal").fetchone()[0]:
                self._state = self.reconcile()
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if hasattr(self, "db"):
            self.db.close()
        if self.lock:
            self.lock.close()
            self.lock = None

    def state(self) -> ContinuousState:
        if self._state is None:
            raise PaperError("MISSING_GENESIS")
        return self._state.copy()

    def initialize(self, context: dict[str, object], at: int) -> ContinuousState:
        if self._state is not None:
            self.verify_context(context)
            return self.state()
        return self.append("prepare", "PREPARE", {"at": at, "context": context})

    def verify_context(self, context: dict[str, object]) -> None:
        if canonical(self.state().context) != canonical(context):
            raise PaperError("FROZEN_CONTINUOUS_PROTOCOL_CHANGED")

    def _projections(self, state: ContinuousState) -> dict[str, dict[object, str]]:
        raw = state.data()
        return {
            "orders": {k: canonical(v) for k, v in require_object(raw["orders"]).items()},
            "cycles": {int(k): canonical(v) for k, v in state.cycles.items()},
            "configurations": {int(k): canonical(v) for k, v in state.generations.items()},
            "controls": {k: canonical(v) for k, v in state.controls.items()},
        }

    def _check(self, state: ContinuousState) -> None:
        if self.db.execute("SELECT version,data FROM projection WHERE id=1").fetchone() != (
            state.wallet.version,
            canonical(state.data()),
        ):
            raise PaperError("PROJECTION_DIVERGENCE")
        for name, key in [
            ("orders", "order_id"),
            ("cycles", "close_time"),
            ("configurations", "generation"),
            ("controls", "request_id"),
        ]:
            if (
                dict(self.db.execute(f"SELECT {key},data FROM {name}"))
                != self._projections(state)[name]
            ):
                raise PaperError("PROJECTION_DIVERGENCE")
        fills = {
            fid: (o.order_id, canonical(f))
            for o in state.orders.values()
            for fid, f in o.fills.items()
        }
        actual = {
            fid: (oid, data)
            for fid, oid, data in self.db.execute("SELECT fill_id,order_id,data FROM fills")
        }
        if actual != fills:
            raise PaperError("FILL_DIVERGENCE")

    def append(self, event_key: str, kind: str, payload: dict[str, object]) -> ContinuousState:
        if self.readonly:
            raise PaperError("READONLY_STORE")
        body = canonical(payload)
        ph = fingerprint(payload)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute(
                "SELECT kind,payload FROM journal WHERE event_key=?", (event_key,)
            ).fetchone()
            if existing:
                if existing != (kind, body):
                    raise PaperError("EVENT_CONFLICT")
                self.db.execute("ROLLBACK")
                self._state = self.reconcile()
                return self.state()
            old = self._state
            row = self.db.execute(
                "SELECT seq,chain_hash FROM journal ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            seq = 1 if row is None else row[0] + 1
            previous = "" if row is None else row[1]
            if old:
                if old.t0 is not None:
                    self.freeze_manifest()
                if seq != old.wallet.version + 1:
                    raise PaperError("JOURNAL_DIVERGENCE")
                self._check(old)
            state, posts = reduce(old, kind, payload)
            sh = fingerprint(state.data())
            chain = fingerprint(
                {
                    "seq": seq,
                    "event_key": event_key,
                    "kind": kind,
                    "payload_hash": ph,
                    "previous_hash": previous,
                    "state_hash": sh,
                    "postings": [
                        {
                            "asset": p.asset,
                            "account": p.account,
                            "quantity": format(p.quantity, "f"),
                        }
                        for p in posts
                    ],
                }
            )
            self.db.execute(
                "INSERT INTO journal VALUES (?,?,?,?,?,?,?,?,?)",
                (seq, event_key, kind, payload["at"], body, ph, previous, chain, sh),
            )
            for index, p in enumerate(posts):
                self.db.execute(
                    "INSERT INTO postings VALUES (?,?,?,?,?)",
                    (seq, index, p.asset, p.account, format(p.quantity, "f")),
                )
            self.db.execute(
                (
                    "INSERT INTO projection VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "version=excluded.version,data=excluded.data"
                ),
                (state.wallet.version, canonical(state.data())),
            )
            projections = self._projections(state)
            for key, data in projections["orders"].items():
                self.db.execute(
                    (
                        "INSERT INTO orders VALUES (?,?) ON CONFLICT(order_id) DO UPDATE SET "
                        "data=excluded.data"
                    ),
                    (key, data),
                )
            for key, data in projections["cycles"].items():
                item = state.cycles[str(key)]
                self.db.execute(
                    "INSERT OR IGNORE INTO cycles VALUES (?,?,?,?)",
                    (key, item["decision_id"], seq, data),
                )
            for key, data in projections["configurations"].items():
                self.db.execute(
                    "INSERT OR IGNORE INTO configurations VALUES (?,?,?)", (key, seq, data)
                )
            for key, data in projections["controls"].items():
                self.db.execute("INSERT OR IGNORE INTO controls VALUES (?,?,?)", (key, seq, data))
            for order in state.orders.values():
                for fid, fill_data in order.fills.items():
                    self.db.execute(
                        "INSERT OR IGNORE INTO fills VALUES (?,?,?,?)",
                        (fid, order.order_id, seq, canonical(fill_data)),
                    )
            event = notice(kind, payload, state, seq, old)
            if event:
                self.db.execute(
                    "INSERT INTO notification_events VALUES (?,?,?,?)",
                    (event["event_id"], seq, fingerprint(event), canonical(event)),
                )
            self.db.execute("COMMIT")
            self._state = state
            return state.copy()
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def reconcile(self, *, before: int | None = None) -> ContinuousState:
        state: ContinuousState | None = None
        previous = ""
        notices = {}
        post_count = 0
        for seq, key, kind, at, body, ph, prev, chain, sh in self.db.execute(
            "SELECT * FROM journal ORDER BY seq"
        ):
            if before is not None and at >= before:
                break
            if prev != previous or seq != (1 if state is None else state.wallet.version + 1):
                raise PaperError("JOURNAL_DIVERGENCE")
            payload = require_object(json.loads(body))
            if payload.get("at") != at or fingerprint(payload) != ph:
                raise PaperError("JOURNAL_DIVERGENCE")
            old = state
            state, posts = reduce(state, kind, payload)
            raw = [
                {"asset": p.asset, "account": p.account, "quantity": format(p.quantity, "f")}
                for p in posts
            ]
            stored = list(
                self.db.execute(
                    "SELECT asset,account,quantity FROM postings WHERE seq=? ORDER BY idx", (seq,)
                )
            )
            if stored != [(p.asset, p.account, format(p.quantity, "f")) for p in posts]:
                raise PaperError("LEDGER_DIVERGENCE")
            if sh != fingerprint(state.data()) or chain != fingerprint(
                {
                    "seq": seq,
                    "event_key": key,
                    "kind": kind,
                    "payload_hash": ph,
                    "previous_hash": prev,
                    "state_hash": sh,
                    "postings": raw,
                }
            ):
                raise PaperError("JOURNAL_DIVERGENCE")
            event = notice(kind, payload, state, seq, old)
            if event:
                notices[str(event["event_id"])] = (seq, fingerprint(event), canonical(event))
            previous = chain
            post_count += len(posts)
        if state is None:
            raise PaperError("MISSING_GENESIS")
        if before is None:
            self._check(state)
            if self.db.execute("SELECT count(*) FROM postings").fetchone()[0] != post_count:
                raise PaperError("LEDGER_DIVERGENCE")
            actual = {
                eid: (seq, h, data)
                for eid, seq, h, data in self.db.execute("SELECT * FROM notification_events")
            }
            if actual != notices:
                raise PaperError("NOTIFICATION_EVENT_DIVERGENCE")
        return state

    def backup(self, target: Path) -> None:
        if (
            target.exists()
            or target.is_symlink()
            or not target.resolve().is_relative_to(self.directory.parent.resolve())
        ):
            raise PaperError("UNSAFE_BACKUP_PATH")
        connection = sqlite3.connect(target)
        try:
            self.db.backup(connection)
        finally:
            connection.close()

    def freeze_manifest(self) -> None:
        state = self.state()
        if state.t0 is None:
            return
        body = {
            "schema": 1,
            "model": MODEL,
            "context": state.context,
            "t0": state.t0,
            "tend": state.tend,
            "eligible_slots": 672,
            "minimum_valid_slots": 666,
            "rules": state.rules,
            "execution": {
                "ttl_ms": 60000,
                "poll_ms": 5000,
                "participation": "0.10",
                "latency_ms": 1000,
            },
            "clock": {
                "timezone": "UTC",
                "slot_ms": 900000,
                "deadline_ms": 90000,
                "quote_age_ms": 5000,
            },
        }
        raw = (canonical({**body, "manifest_sha256": fingerprint(body)}) + "\n").encode()
        target = self.directory / "pilot-manifest.json"
        if target.is_symlink():
            raise PaperError("UNSAFE_MANIFEST_PATH")
        if target.exists():
            if target.read_bytes() != raw:
                raise PaperError("FROZEN_CONTINUOUS_MANIFEST_CHANGED")
            return
        if self.readonly:
            raise PaperError("PILOT_MANIFEST_MISSING")
        temporary = self.directory / (".pending-manifest-" + str(uuid4()))
        try:
            with temporary.open("xb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.link(temporary, target)
            fd = os.open(self.directory, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)
