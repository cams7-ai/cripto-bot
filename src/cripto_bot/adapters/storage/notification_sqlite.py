"""Derived delivery queue, independent lock/leases; contains no financial balances."""

import fcntl
import json
import sqlite3
from pathlib import Path
from typing import BinaryIO

from cripto_core.errors import PaperError
from cripto_core.serialization import canonical, fingerprint
from cripto_core.validation import require_object

DDL = (
    (
        "CREATE TABLE events (event_id TEXT PRIMARY KEY,source_hash TEXT NOT "
        "NULL,data TEXT NOT NULL,status TEXT NOT NULL,preferred TEXT NOT "
        "NULL,retry_at INTEGER NOT NULL,coalesced_into TEXT REFERENCES "
        "events(event_id))"
    ),
    (
        "CREATE TABLE attempts (event_id TEXT NOT NULL REFERENCES "
        "events(event_id),channel TEXT NOT NULL,attempt_no INTEGER NOT "
        "NULL,started_at INTEGER NOT NULL,lease_until INTEGER NOT "
        "NULL,finished_at INTEGER,outcome TEXT NOT NULL,PRIMARY "
        "KEY(event_id,channel,attempt_no))"
    ),
    "CREATE TABLE clock (id INTEGER PRIMARY KEY CHECK(id=1),last_attempt_at INTEGER NOT NULL)",
    "CREATE INDEX queue_ready ON events(status,retry_at)",
    (
        "CREATE TRIGGER immutable_event_payload BEFORE UPDATE OF "
        "event_id,source_hash,data ON events BEGIN SELECT RAISE(ABORT,'immutable "
        "notification event'); END"
    ),
    (
        "CREATE TRIGGER immutable_attempt BEFORE UPDATE ON attempts WHEN "
        "OLD.outcome!='IN_FLIGHT' BEGIN SELECT RAISE(ABORT,'immutable completed "
        "attempt'); END"
    ),
    (
        "CREATE TRIGGER immutable_attempt_delete BEFORE DELETE ON attempts BEGIN "
        "SELECT RAISE(ABORT,'immutable attempt'); END"
    ),
)


class NotificationStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.db: sqlite3.Connection
        self.lock: BinaryIO | None = None

    def __enter__(self) -> "NotificationStore":
        if any(
            (self.directory / n).exists()
            for n in ["continuous.sqlite3", "paper.sqlite3", "backtest.sqlite3", "market.sqlite3"]
        ):
            raise PaperError("SHARED_NOTIFICATION_DIRECTORY")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / "notifications.sqlite3"
        if path.is_symlink() or (self.directory / ".writer.lock").is_symlink():
            raise PaperError("UNSAFE_STORE_PATH")
        self.lock = (self.directory / ".writer.lock").open("ab")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            raise PaperError("WRITER_BUSY") from None
        try:
            exists = path.exists()
            self.db = sqlite3.connect(path, isolation_level=None, timeout=0)
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            if not exists:
                self.db.execute("BEGIN IMMEDIATE")
                for sql in DDL:
                    self.db.execute(sql)
                self.db.execute("INSERT INTO clock VALUES(1,-1000)")
                self.db.execute("PRAGMA user_version=1")
                self.db.execute("COMMIT")
            actual = {
                n: " ".join(sql.split())
                for n, sql in self.db.execute(
                    "SELECT name,sql FROM sqlite_schema WHERE sql IS NOT NULL"
                )
            }
            expected = {s.split()[2]: " ".join(s.split()) for s in DDL}
            if actual != expected or self.db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise PaperError("INCOMPATIBLE_NOTIFICATION_SCHEMA")
            if self.db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise PaperError("NOTIFICATION_CORRUPTION")
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

    def enqueue(self, event: dict[str, object], source_hash: str, *, whatsapp: bool) -> None:
        key = str(event["event_id"])
        if (
            fingerprint(event) != source_hash
            or fingerprint({k: v for k, v in event.items() if k != "event_id"}) != key
        ):
            raise PaperError("NOTIFICATION_SOURCE_DIVERGENCE")
        existing = self.db.execute(
            "SELECT source_hash,data FROM events WHERE event_id=?", (key,)
        ).fetchone()
        if existing:
            if existing != (source_hash, canonical(event)):
                raise PaperError("NOTIFICATION_EVENT_CONFLICT")
            return
        parent = None
        if event["category"] == "DATA_UNAVAILABLE":
            bucket = int(str(event["at"])) // 300000
            for oldkey, raw in self.db.execute(
                "SELECT event_id,data FROM events WHERE coalesced_into IS NULL ORDER BY "
                "rowid DESC LIMIT 128"
            ):
                old = require_object(json.loads(raw))
                if (
                    old["category"] == event["category"]
                    and old["pilot_id"] == event["pilot_id"]
                    and old.get("reason") == event.get("reason")
                    and int(str(old["at"])) // 300000 == bucket
                ):
                    parent = oldkey
                    break
        self.db.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?)",
            (
                key,
                source_hash,
                canonical(event),
                "COALESCED" if parent else "PENDING",
                "whatsapp" if whatsapp else "mail",
                0,
                parent,
            ),
        )

    def recover(self, now: int) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for key, channel, no, started in self.db.execute(
                (
                    "SELECT event_id,channel,attempt_no,started_at FROM attempts WHERE "
                    "outcome='IN_FLIGHT' AND lease_until<=?"
                ),
                (now,),
            ).fetchall():
                self.db.execute(
                    (
                        "UPDATE attempts SET outcome='UNKNOWN',finished_at=? WHERE event_id=? "
                        "AND channel=? AND attempt_no=?"
                    ),
                    (now, key, channel, no),
                )
                status = "RETRY_WAIT" if no < 3 or channel == "whatsapp" else "FAILED"
                self.db.execute(
                    "UPDATE events SET status=?,preferred=?,retry_at=? WHERE event_id=?",
                    (
                        status,
                        "mail" if channel == "whatsapp" else channel,
                        max(now, started + (60000 if no == 1 or channel == "whatsapp" else 300000)),
                        key,
                    ),
                )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def claim(self, now: int) -> dict[str, object] | None:
        self.recover(now)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            last = self.db.execute("SELECT last_attempt_at FROM clock WHERE id=1").fetchone()[0]
            if now < last + 1000:
                self.db.execute("ROLLBACK")
                return None
            row = self.db.execute(
                (
                    "SELECT event_id,data,preferred FROM events WHERE status IN "
                    "('PENDING','RETRY_WAIT','UNKNOWN') AND retry_at<=? ORDER BY rowid LIMIT "
                    "1"
                ),
                (now,),
            ).fetchone()
            if row is None:
                self.db.execute("ROLLBACK")
                return None
            key, body, channel = row
            no = (
                self.db.execute(
                    "SELECT count(*) FROM attempts WHERE event_id=? AND channel=?", (key, channel)
                ).fetchone()[0]
                + 1
            )
            if no > 3:
                self.db.execute("UPDATE events SET status='FAILED' WHERE event_id=?", (key,))
                self.db.execute("COMMIT")
                return None
            self.db.execute(
                "INSERT INTO attempts VALUES (?,?,?,?,?,?,?)",
                (key, channel, no, now, now + 60000, None, "IN_FLIGHT"),
            )
            self.db.execute("UPDATE events SET status='IN_FLIGHT' WHERE event_id=?", (key,))
            self.db.execute("UPDATE clock SET last_attempt_at=? WHERE id=1", (now,))
            self.db.execute("COMMIT")
            duplicate = (
                self.db.execute(
                    "SELECT count(*) FROM attempts WHERE event_id=? AND outcome='UNKNOWN'", (key,)
                ).fetchone()[0]
                > 0
            )
            return {
                "event": require_object(json.loads(body)),
                "channel": channel,
                "attempt_no": no,
                "duplicate_possible": duplicate,
                "started_at": now,
            }
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def finish(self, claim: dict[str, object], outcome: str, now: int) -> None:
        if outcome not in {"CONFIRMED", "FAILED", "UNKNOWN"}:
            raise PaperError("INVALID_DELIVERY_OUTCOME")
        event = require_object(claim["event"])
        key = str(event["event_id"])
        channel = str(claim["channel"])
        no = int(str(claim["attempt_no"]))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT outcome FROM attempts WHERE event_id=? AND channel=? AND attempt_no=?",
                (key, channel, no),
            ).fetchone()
            if row is None:
                raise PaperError("UNKNOWN_ATTEMPT")
            if row[0] != "IN_FLIGHT":
                if row[0] != outcome:
                    raise PaperError("ATTEMPT_CONFLICT")
                self.db.execute("ROLLBACK")
                return
            self.db.execute(
                (
                    "UPDATE attempts SET outcome=?,finished_at=? WHERE event_id=? AND "
                    "channel=? AND attempt_no=?"
                ),
                (outcome, now, key, channel, no),
            )
            preferred = channel
            if outcome == "CONFIRMED":
                status = "CONFIRMED"
                retry = now
            elif channel == "whatsapp":
                status = "RETRY_WAIT"
                preferred = "mail"
                retry = now + 1000
            elif no >= 3:
                status = "FAILED"
                retry = now
            else:
                status = "UNKNOWN" if outcome == "UNKNOWN" else "RETRY_WAIT"
                retry = now + (60000 if no == 1 else 300000)
            self.db.execute(
                "UPDATE events SET status=?,preferred=?,retry_at=? WHERE event_id=?",
                (status, preferred, retry, key),
            )
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def summary(self) -> dict[str, object]:
        return {
            "states": {
                str(k): int(v)
                for k, v in self.db.execute("SELECT status,count(*) FROM events GROUP BY status")
            },
            "attempts": self.db.execute("SELECT count(*) FROM attempts").fetchone()[0],
            "unknown_attempts": self.db.execute(
                "SELECT count(*) FROM attempts WHERE outcome='UNKNOWN'"
            ).fetchone()[0],
        }

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


def inspect(directory: Path, at: int) -> dict[str, object]:
    path = directory / "notifications.sqlite3"
    if not path.exists():
        return {"status": "NOT_STARTED", "states": {}, "attempts": [], "as_of": at}
    if path.is_symlink():
        raise PaperError("UNSAFE_NOTIFICATION_PATH")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise PaperError("INCOMPATIBLE_NOTIFICATION_SCHEMA")
        states = {
            str(k): int(n)
            for k, n in db.execute("SELECT status,count(*) FROM events GROUP BY status")
        }
        rows = [
            {
                "event_id": e,
                "channel": c,
                "attempt_no": n,
                "started_at": s,
                "lease_until": lease,
                "finished_at": f,
                "outcome": o,
            }
            for e, c, n, s, lease, f, o in db.execute(
                "SELECT * FROM attempts ORDER BY started_at,event_id,channel,attempt_no"
            )
        ]
        return {
            "status": "AVAILABLE",
            "states": states,
            "attempts": rows,
            "as_of": at,
            "unknown_attempts": sum(r["outcome"] == "UNKNOWN" for r in rows),
        }
    finally:
        db.close()
