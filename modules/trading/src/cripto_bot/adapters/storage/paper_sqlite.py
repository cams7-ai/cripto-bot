"""Journal imutavel, projeções e transações independentes do store de mercado."""

import fcntl
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from cripto_bot.domain.market import canonical, iso
from cripto_bot.domain.portfolio.engine import transition
from cripto_bot.domain.portfolio.models import (
    MODEL,
    PaperError,
    Wallet,
    fingerprint,
    record,
    require_object,
)

DDL = (
    "CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK(id=1), "
    "context TEXT NOT NULL, blocked TEXT NOT NULL)",
    "CREATE TABLE projection (id INTEGER PRIMARY KEY CHECK(id=1), "
    "version INTEGER NOT NULL, data TEXT NOT NULL)",
    "CREATE TABLE journal (seq INTEGER PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, "
    "request_hash TEXT NOT NULL, "
    "kind TEXT NOT NULL, payload TEXT NOT NULL, "
    "previous_hash TEXT NOT NULL, chain_hash TEXT NOT NULL)",
    "CREATE TABLE postings (seq INTEGER NOT NULL REFERENCES journal(seq), idx INTEGER NOT NULL, "
    "data TEXT NOT NULL, PRIMARY KEY(seq,idx))",
    "CREATE TABLE intents (action_key TEXT PRIMARY KEY, data TEXT NOT NULL)",
    "CREATE TABLE fills (fill_id TEXT PRIMARY KEY, "
    "action_key TEXT UNIQUE NOT NULL REFERENCES intents(action_key), data TEXT NOT NULL)",
)


class PaperStore:
    def __init__(self, directory: Path, *, create: bool = False, readonly: bool = False) -> None:
        self.directory = directory
        self.create = create
        self.readonly = readonly
        self.db: sqlite3.Connection
        self.lock: BinaryIO | None = None

    def __enter__(self) -> "PaperStore":
        path = self.directory / "paper.sqlite3"
        if not self.create and not path.is_file():
            raise PaperError("WALLET_NOT_FOUND")
        if self.create:
            self.directory.mkdir(parents=True, exist_ok=True)
        if any((self.directory / name).is_symlink() for name in ("paper.sqlite3", ".paper.lock")):
            raise PaperError("UNSAFE_PAPER_PATH")
        if not self.readonly:
            self.lock = (self.directory / ".paper.lock").open("ab")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.close()
                raise PaperError("WRITER_BUSY") from None
        try:
            self.db = sqlite3.connect(
                path.as_uri() + "?mode=ro" if self.readonly else path,
                uri=self.readonly,
                isolation_level=None,
                timeout=1,
            )
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA synchronous=FULL")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not version and not tables and self.create and not self.readonly:
                with self.transaction():
                    for statement in DDL:
                        self.db.execute(statement)
                    for table in ("journal", "postings", "fills"):
                        for mutation in ("UPDATE", "DELETE"):
                            self.db.execute(
                                f"CREATE TRIGGER immutable_{table}_{mutation} "
                                f"BEFORE {mutation} ON {table} "
                                "BEGIN SELECT RAISE(ABORT,'immutable paper history'); END"
                            )
                    self.db.execute("PRAGMA user_version=1")
            elif version != 1 or tables != {
                "metadata",
                "projection",
                "journal",
                "postings",
                "intents",
                "fills",
            }:
                raise PaperError("INCOMPATIBLE_PAPER_SCHEMA")
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise PaperError("CORRUPT_PAPER_DATABASE")
            if self.readonly:
                self.db.execute("BEGIN")
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if hasattr(self, "db"):
            self.db.close()
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def context(self) -> dict[str, object]:
        row = self.db.execute("SELECT context FROM metadata WHERE id=1").fetchone()
        if row is None:
            raise PaperError("WALLET_NOT_FOUND")
        try:
            return require_object(json.loads(row[0]))
        except (ValueError, TypeError):
            raise PaperError("CORRUPT_CONTEXT") from None

    def blocked(self) -> str:
        row = self.db.execute("SELECT blocked FROM metadata WHERE id=1").fetchone()
        return "" if row is None else str(row[0])

    def block(self) -> None:
        if not self.readonly:
            with self.transaction():
                self.db.execute("UPDATE metadata SET blocked='RECONCILIATION_FAILED' WHERE id=1")

    def initialize(self, context: dict[str, object]) -> None:
        if self.db.execute("SELECT count(*) FROM metadata").fetchone()[0]:
            if self.context() != context:
                raise PaperError("WALLET_CONTEXT_CONFLICT")
            return
        with self.transaction():
            self.db.execute("INSERT INTO metadata VALUES(1,?,'')", (canonical(context),))
            self.db.execute("INSERT INTO projection VALUES(1,0,?)", (canonical(Wallet().data()),))
            self._write(
                "GENESIS", "GENESIS", {"timestamp": context["start_time"]}, Wallet(), context
            )

    def posting_records(
        self,
        postings: object,
        wallet: Wallet,
        context: dict[str, object],
        key: str,
        payload: dict[str, object],
    ) -> list[dict[str, object]]:
        encoded = record(postings)
        assert isinstance(encoded, list)
        return [
            {
                **require_object(item),
                "wallet_id": require_object(context["parameters"])["wallet_id"],
                "timestamp": iso(wallet.clock),
                "reference": payload.get("action_key", key),
                "fill_id": payload.get("fill_id"),
            }
            for item in encoded
        ]

    def _write(
        self,
        key: str,
        kind: str,
        payload: dict[str, object],
        wallet: Wallet,
        context: dict[str, object],
    ) -> Wallet:
        parameters = require_object(context["parameters"])
        rules = require_object(context["rules"])
        updated, postings = transition(wallet, kind, payload, parameters, rules)
        ledger = self.posting_records(postings, updated, context, key, payload)
        row = self.db.execute("SELECT chain_hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
        previous = fingerprint(context) if row is None else str(row[0])
        request = fingerprint({"kind": kind, "payload": payload})
        chain = fingerprint(
            {
                "previous": previous,
                "key": key,
                "request": request,
                "state": updated.data(),
                "postings": ledger,
            }
        )
        self.db.execute(
            "INSERT INTO journal VALUES(?,?,?,?,?,?,?)",
            (updated.version, key, request, kind, canonical(payload), previous, chain),
        )
        for index, posting in enumerate(ledger):
            self.db.execute(
                "INSERT INTO postings VALUES(?,?,?)",
                (updated.version, index, canonical(posting)),
            )
        for action, intent in updated.intents.items():
            self.db.execute(
                "INSERT INTO intents VALUES(?,?) ON CONFLICT(action_key) "
                "DO UPDATE SET data=excluded.data",
                (action, canonical(record(intent))),
            )
        if kind == "FILL":
            intent = updated.intents[str(payload["action_key"])]
            assert intent.plan is not None
            self.db.execute(
                "INSERT INTO fills VALUES(?,?,?)",
                (intent.plan.fill_id, intent.plan.action, canonical(record(intent.plan))),
            )
        cursor = self.db.execute(
            "UPDATE projection SET data=?,version=? WHERE id=1 AND version=?",
            (canonical(updated.data()), updated.version, wallet.version),
        )
        if cursor.rowcount != 1:
            raise PaperError("STALE_WALLET_VERSION")
        return updated

    def append(self, key: str, kind: str, payload: dict[str, object], wallet: Wallet) -> Wallet:
        if self.blocked():
            raise PaperError("WALLET_BLOCKED")
        with self.transaction():
            row = self.db.execute(
                "SELECT request_hash FROM journal WHERE event_key=?", (key,)
            ).fetchone()
            if row is not None:
                if row[0] != fingerprint({"kind": kind, "payload": payload}):
                    raise PaperError("EVENT_ID_CONFLICT")
                return self.replay()
            current = self.replay()
            if current.version != wallet.version or current.data() != wallet.data():
                raise PaperError("STALE_WALLET_VERSION")
            return self._write(key, kind, payload, wallet, self.context())

    def seen(self, key: str, kind: str, payload: dict[str, object]) -> bool:
        row = self.db.execute(
            "SELECT request_hash FROM journal WHERE event_key=?", (key,)
        ).fetchone()
        if row is None:
            return False
        if row[0] != fingerprint({"kind": kind, "payload": payload}):
            raise PaperError("EVENT_ID_CONFLICT")
        return True

    def replay(self, *, check_projection: bool = True) -> Wallet:
        context = self.context()
        if context.get("model") != MODEL:
            raise PaperError("MODEL_CONFLICT")
        wallet = Wallet()
        previous = fingerprint(context)
        expected_fills: dict[str, tuple[str, str]] = {}
        for seq, key, request, kind, payload_text, prior, chain in self.db.execute(
            "SELECT * FROM journal ORDER BY seq"
        ):
            if wallet.version == 0 and kind != "GENESIS":
                raise PaperError("JOURNAL_DIVERGENCE")
            payload = require_object(json.loads(payload_text))
            if (
                seq != wallet.version + 1
                or prior != previous
                or request != fingerprint({"kind": kind, "payload": payload})
            ):
                raise PaperError("JOURNAL_DIVERGENCE")
            wallet, postings = transition(
                wallet,
                kind,
                payload,
                require_object(context["parameters"]),
                require_object(context["rules"]),
            )
            ledger = self.posting_records(postings, wallet, context, key, payload)
            stored = list(
                self.db.execute("SELECT idx,data FROM postings WHERE seq=? ORDER BY idx", (seq,))
            )
            if stored != [(i, canonical(posting)) for i, posting in enumerate(ledger)]:
                raise PaperError("LEDGER_DIVERGENCE")
            expected = fingerprint(
                {
                    "previous": previous,
                    "key": key,
                    "request": request,
                    "state": wallet.data(),
                    "postings": ledger,
                }
            )
            if chain != expected:
                raise PaperError("JOURNAL_DIVERGENCE")
            previous = chain
            if kind == "FILL":
                intent = wallet.intents[str(payload["action_key"])]
                assert intent.plan is not None
                expected_fills[intent.plan.fill_id] = (
                    intent.plan.action,
                    canonical(record(intent.plan)),
                )
        if not wallet.version:
            raise PaperError("MISSING_GENESIS")
        if not check_projection:
            return wallet
        projection = self.db.execute("SELECT version,data FROM projection WHERE id=1").fetchone()
        if (
            not wallet.version
            or projection is None
            or projection != (wallet.version, canonical(wallet.data()))
        ):
            raise PaperError("PROJECTION_DIVERGENCE")
        stored_intents = dict(self.db.execute("SELECT action_key,data FROM intents"))
        if stored_intents != {key: canonical(record(item)) for key, item in wallet.intents.items()}:
            raise PaperError("INTENT_DIVERGENCE")
        if {
            row[0]: (row[1], row[2])
            for row in self.db.execute("SELECT fill_id,action_key,data FROM fills")
        } != expected_fills:
            raise PaperError("FILL_DIVERGENCE")
        return wallet
