"""Journal historico unico com checkpoint limitado e commits financeiros separados."""

import fcntl
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from cripto_bot.domain.backtest.engine import Effect, State, reduce
from cripto_bot.domain.backtest.models import MODEL, build_context, fingerprint
from cripto_bot.domain.market import canonical, iso
from cripto_bot.domain.portfolio.models import PaperError, record, require_object

DDL = (
    (
        "CREATE TABLE protocol (id INTEGER PRIMARY KEY CHECK(id=1), sealed"
        " TEXT NOT NULL, blocked TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE runs (run_key TEXT PRIMARY KEY, context TEXT NOT NUL"
        "L, status TEXT NOT NULL, version INTEGER NOT NULL, projection TEX"
        "T NOT NULL, last_hash TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE journal (run_key TEXT NOT NULL REFERENCES runs(run_k"
        "ey), seq INTEGER NOT NULL, event_key TEXT NOT NULL, kind TEXT NOT"
        " NULL, payload TEXT NOT NULL, request_hash TEXT NOT NULL, previou"
        "s_hash TEXT NOT NULL, chain_hash TEXT NOT NULL, PRIMARY KEY(run_k"
        "ey,seq), UNIQUE(run_key,event_key))"
    ),
    (
        "CREATE TABLE postings (run_key TEXT NOT NULL, seq INTEGER NOT NUL"
        "L, idx INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(run_key,"
        "seq,idx), FOREIGN KEY(run_key,seq) REFERENCES journal(run_key,seq"
        "))"
    ),
    (
        "CREATE TABLE decisions (run_key TEXT NOT NULL, decision_id TEXT N"
        "OT NULL, seq INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(ru"
        "n_key,decision_id), FOREIGN KEY(run_key,seq) REFERENCES journal(r"
        "un_key,seq))"
    ),
    (
        "CREATE TABLE intents (run_key TEXT NOT NULL, action_key TEXT NOT "
        "NULL, seq INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(run_k"
        "ey,action_key), FOREIGN KEY(run_key,action_key) REFERENCES decisi"
        "ons(run_key,decision_id), FOREIGN KEY(run_key,seq) REFERENCES jou"
        "rnal(run_key,seq))"
    ),
    (
        "CREATE TABLE fills (run_key TEXT NOT NULL, fill_id TEXT NOT NULL,"
        " action_key TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(run_ke"
        "y,fill_id), UNIQUE(run_key,action_key), FOREIGN KEY(run_key,actio"
        "n_key) REFERENCES intents(run_key,action_key))"
    ),
    (
        "CREATE TABLE outcomes (run_key TEXT NOT NULL, action_key TEXT NOT"
        " NULL, seq INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(run_"
        "key,action_key), FOREIGN KEY(run_key,action_key) REFERENCES decis"
        "ions(run_key,decision_id), FOREIGN KEY(run_key,seq) REFERENCES jo"
        "urnal(run_key,seq))"
    ),
    (
        "CREATE TABLE attributions (run_key TEXT NOT NULL, fill_id TEXT NO"
        "T NULL, instance_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KE"
        "Y(run_key,fill_id), FOREIGN KEY(run_key,fill_id) REFERENCES fills"
        "(run_key,fill_id))"
    ),
    (
        "CREATE TABLE equity (run_key TEXT NOT NULL, seq INTEGER NOT NULL,"
        " at INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(run_key,seq"
        "), FOREIGN KEY(run_key,seq) REFERENCES journal(run_key,seq))"
    ),
    "CREATE INDEX decisions_sequence ON decisions(run_key,seq)",
    "CREATE INDEX intents_sequence ON intents(run_key,seq)",
    "CREATE INDEX outcomes_sequence ON outcomes(run_key,seq)",
)
IMMUTABLE = (
    "journal",
    "postings",
    "decisions",
    "intents",
    "fills",
    "outcomes",
    "attributions",
    "equity",
)
TRIGGERS = tuple(
    f"CREATE TRIGGER immutable_{table}_{op} BEFORE {op} ON {table} "
    "BEGIN SELECT RAISE(ABORT,'immutable backtest history'); END"
    for table in IMMUTABLE
    for op in ("UPDATE", "DELETE")
) + (
    (
        "CREATE TRIGGER immutable_protocol BEFORE UPDATE OF sealed ON prot"
        "ocol BEGIN SELECT RAISE(ABORT,'immutable protocol'); END"
    ),
    (
        "CREATE TRIGGER immutable_run_context BEFORE UPDATE OF context ON "
        "runs BEGIN SELECT RAISE(ABORT,'immutable run context'); END"
    ),
)


class BacktestStore:
    batch_size = 64

    def __init__(self, directory: Path, *, create: bool = False, readonly: bool = False) -> None:
        self.directory = directory
        self.create = create
        self.readonly = readonly
        self.lock: BinaryIO | None = None
        self.db: sqlite3.Connection
        self.pending_events = 0
        self.verified: tuple[str, int, int, int, int] | None = None

    def __enter__(self) -> "BacktestStore":
        path = self.directory / "backtest.sqlite3"
        if any((self.directory / name).exists() for name in ("paper.sqlite3", "market.sqlite3")):
            raise PaperError("INCOMPATIBLE_BACKTEST_DIRECTORY")
        if not path.is_file() and not self.create:
            raise PaperError("BACKTEST_NOT_FOUND")
        if self.create:
            self.directory.mkdir(parents=True, exist_ok=True)
        if any(
            (self.directory / name).is_symlink() for name in ("backtest.sqlite3", ".backtest.lock")
        ):
            raise PaperError("UNSAFE_BACKTEST_PATH")
        if not self.readonly:
            self.lock = (self.directory / ".backtest.lock").open("ab")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.lock.close()
                self.lock = None
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
            objects = dict(
                self.db.execute("SELECT name,sql FROM sqlite_schema WHERE sql IS NOT NULL")
            )
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if not objects and not version and self.create and not self.readonly:
                with self.transaction():
                    for statement in (*DDL, *TRIGGERS):
                        self.db.execute(statement)
                    self.db.execute("PRAGMA user_version=1")
                objects = dict(
                    self.db.execute("SELECT name,sql FROM sqlite_schema WHERE sql IS NOT NULL")
                )
                version = 1
            expected = {s.split()[2]: " ".join(s.split()) for s in (*DDL, *TRIGGERS)}
            if version != 1 or {n: " ".join(s.split()) for n, s in objects.items()} != expected:
                raise PaperError("INCOMPATIBLE_BACKTEST_SCHEMA")
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise PaperError("CORRUPT_BACKTEST_DATABASE")
            if not self.readonly:
                self.db.execute("PRAGMA journal_mode=WAL")
            if self.readonly:
                self.db.execute("BEGIN")
            return self
        except BaseException:
            self.close(rollback=True)
            raise

    def __exit__(self, *args: object) -> None:
        self.close(rollback=bool(args and args[0]))

    def close(self, *, rollback: bool = False) -> None:
        if hasattr(self, "db"):
            if rollback:
                self.db.rollback()
            elif not self.readonly:
                self.flush()
            self.db.close()
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.flush()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            self.verified = None
            raise

    def flush(self) -> None:
        if self.pending_events:
            self.db.commit()
            self.pending_events = 0

    def rollback(self) -> None:
        self.db.rollback()
        self.pending_events = 0
        self.verified = None

    def blocked(self) -> bool:
        row = self.db.execute("SELECT blocked FROM protocol WHERE id=1").fetchone()
        return bool(row and row[0])

    def block(self) -> None:
        if not self.readonly:
            self.rollback()
            with self.transaction():
                self.db.execute("UPDATE protocol SET blocked='RECONCILIATION_FAILED' WHERE id=1")

    def protocol(self) -> dict[str, object]:
        row = self.db.execute("SELECT sealed FROM protocol WHERE id=1").fetchone()
        if row is None:
            raise PaperError("PROTOCOL_NOT_FROZEN")
        sealed = require_object(json.loads(row[0]))
        if sealed.get("model") != MODEL:
            raise PaperError("BACKTEST_MODEL_CONFLICT")
        if sealed.get("protocol_hash") != fingerprint(
            {k: v for k, v in sealed.items() if k != "protocol_hash"}
        ):
            raise PaperError("PROTOCOL_DIVERGENCE")
        return sealed

    def freeze(self, sealed: dict[str, object]) -> None:
        existing = self.db.execute("SELECT sealed FROM protocol WHERE id=1").fetchone()
        if existing:
            if existing[0] != canonical(sealed):
                raise PaperError("PROTOCOL_CONFLICT")
            if self.blocked():
                raise PaperError("BACKTEST_BLOCKED")
            return
        with self.transaction():
            self.db.execute("INSERT INTO protocol VALUES(1,?,'')", (canonical(sealed),))

    def _signature(self, key: str, seq: int) -> tuple[str, int, int, int, int]:
        return (
            key,
            seq,
            self.db.total_changes,
            self.db.execute("PRAGMA data_version").fetchone()[0],
            self.db.execute("PRAGMA schema_version").fetchone()[0],
        )

    def create_run(
        self, context: dict[str, object], warmup: list[dict[str, object]], baseline: object
    ) -> State:
        if self.readonly or self.blocked():
            raise PaperError("BACKTEST_BLOCKED")
        expected = build_context(
            self.protocol(),
            str(context["segment"]),
            str(context["variant"]),
            str(context["profile"]),
        )
        if canonical(context) != canonical(expected):
            raise PaperError("FROZEN_CURVE_CONTEXT_CONFLICT")
        self.flush()
        key = str(context["run_key"])
        row = self.db.execute("SELECT context FROM runs WHERE run_key=?", (key,)).fetchone()
        if row:
            if row[0] != canonical(context):
                raise PaperError("RUN_CONTEXT_CONFLICT")
            return self.reconcile(key)
        with self.transaction():
            self.db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?)",
                (
                    key,
                    canonical(context),
                    "RUNNING",
                    0,
                    canonical(State().data()),
                    fingerprint(context),
                ),
            )
            result = self._write(
                key,
                "init",
                "INIT",
                {"warmup": warmup, "baseline_price": baseline},
                State(),
                context,
            )
        self.verified = self._signature(key, result.sequence)
        return result

    def _rows(
        self, key: str, seq: int, event_key: str, effect: Effect
    ) -> dict[str, list[tuple[object, ...]]]:
        rows: dict[str, list[tuple[object, ...]]] = {t: [] for t in IMMUTABLE if t != "journal"}
        reference = effect.reference or event_key
        envelope = (effect.intent or effect.fill or {}).get("envelope")
        for idx, posting in enumerate(effect.postings):
            body = {
                "posting": record(posting),
                "wallet_id": key,
                "reference": reference,
                "timestamp": iso(effect.state.wallet.clock),
                "envelope": envelope,
            }
            rows["postings"].append((key, seq, idx, canonical(body)))
        if effect.decision:
            d = effect.decision
            rows["decisions"].append((key, d["decision_id"], seq, canonical(d)))
        if effect.intent:
            i = effect.intent
            rows["intents"].append((key, i["action_key"], seq, canonical(i)))
        if effect.fill:
            f = effect.fill
            rows["fills"].append((key, f["fill_id"], f["action_key"], canonical(f)))
            rows["attributions"].append(
                (key, f["fill_id"], require_object(f["envelope"])["instance_id"], canonical(f))
            )
        if effect.outcome:
            o = effect.outcome
            rows["outcomes"].append((key, o["action_key"], seq, canonical(o)))
        if effect.equity:
            rows["equity"].append((key, seq, effect.equity["at"], canonical(effect.equity)))
        return rows

    def _write(
        self,
        key: str,
        event_key: str,
        kind: str,
        payload: dict[str, object],
        state: State,
        context: dict[str, object],
        effect: Effect | None = None,
    ) -> State:
        effect = reduce(state, kind, payload, context) if effect is None else effect
        updated = effect.state
        seq = updated.sequence
        row = self.db.execute(
            "SELECT version,last_hash FROM runs WHERE run_key=?", (key,)
        ).fetchone()
        if row is None or row[0] != state.sequence:
            raise PaperError("STALE_RUN_VERSION")
        request = fingerprint({"kind": kind, "payload": payload})
        rows = self._rows(key, seq, event_key, effect)
        updated_data = updated.data()
        chain = fingerprint(
            {
                "previous": row[1],
                "key": event_key,
                "request": request,
                "state": updated_data,
                "rows": rows,
            }
        )
        self.db.execute(
            "INSERT INTO journal VALUES(?,?,?,?,?,?,?,?)",
            (key, seq, event_key, kind, canonical(payload), request, row[1], chain),
        )
        for table, values in rows.items():
            for value in values:
                self.db.execute(
                    f"INSERT INTO {table} VALUES(" + ",".join("?" for _ in value) + ")", value
                )
        cursor = self.db.execute(
            (
                "UPDATE runs SET status=?,version=?,projection=?,last_hash=? WHERE"
                " run_key=? AND version=?"
            ),
            (
                "COMPLETED" if updated.finished else "RUNNING",
                seq,
                canonical(updated_data),
                chain,
                key,
                state.sequence,
            ),
        )
        if cursor.rowcount != 1:
            raise PaperError("STALE_RUN_VERSION")
        return updated

    def append(
        self, key: str, event_key: str, kind: str, payload: dict[str, object], state: State
    ) -> State:
        if self.readonly or self.blocked():
            raise PaperError("BACKTEST_BLOCKED")
        context = require_object(
            json.loads(
                self.db.execute("SELECT context FROM runs WHERE run_key=?", (key,)).fetchone()[0]
            )
        )
        existing = self.db.execute(
            "SELECT request_hash FROM journal WHERE run_key=? AND event_key=?", (key, event_key)
        ).fetchone()
        if existing:
            if existing[0] != fingerprint({"kind": kind, "payload": payload}):
                raise PaperError("EVENT_CONFLICT")
            self.flush()
            return self.reconcile(key)
        if self.verified != self._signature(key, state.sequence):
            self.flush()
            current = self.reconcile(key)
            if current.data() != state.data():
                raise PaperError("STALE_RUN_VERSION")
        # Uma projecao confirmada e uma versao identificam o estado de trabalho.
        projection = self.db.execute(
            "SELECT version,projection FROM runs WHERE run_key=?", (key,)
        ).fetchone()
        if projection != (state.sequence, canonical(state.data())):
            raise PaperError("STALE_RUN_VERSION")
        effect = reduce(state, kind, payload, context)
        financial = bool(
            effect.postings
            or effect.intent
            or kind in ("FILL", "END")
            or (effect.decision is not None and effect.decision.get("status") == "SCHEDULED")
        )
        try:
            if financial:
                if self.pending_events:
                    # A marcacao anterior ainda nao publicada acompanha a decisao/A.
                    # A decisao e seu checkpoint continuam num unico commit; B e separado.
                    updated = self._write(key, event_key, kind, payload, state, context, effect)
                    self.flush()
                else:
                    with self.transaction():
                        updated = self._write(key, event_key, kind, payload, state, context, effect)
            else:
                if not self.pending_events:
                    self.db.execute("BEGIN IMMEDIATE")
                updated = self._write(key, event_key, kind, payload, state, context, effect)
                self.pending_events += 1
                if self.pending_events >= self.batch_size:
                    self.flush()
            self.verified = self._signature(key, updated.sequence)
            return updated
        except BaseException:
            self.rollback()
            raise

    def replay(self, key: str) -> State:
        run = self.db.execute(
            "SELECT context,status,version,projection,last_hash FROM runs WHERE run_key=?", (key,)
        ).fetchone()
        if run is None:
            raise PaperError("RUN_NOT_FOUND")
        context = require_object(json.loads(run[0]))
        expected_context = build_context(
            self.protocol(),
            str(context["segment"]),
            str(context["variant"]),
            str(context["profile"]),
        )
        if canonical(context) != canonical(expected_context):
            raise PaperError("FROZEN_CURVE_CONTEXT_CONFLICT")
        state = State()
        previous = fingerprint(context)
        counts = {t: 0 for t in IMMUTABLE if t != "journal"}
        for seq, event_key, kind, body, request, prior, chain in self.db.execute(
            (
                "SELECT seq,event_key,kind,payload,request_hash,previous_hash,chai"
                "n_hash FROM journal WHERE run_key=? ORDER BY seq"
            ),
            (key,),
        ):
            payload = require_object(json.loads(body))
            if (
                seq != state.sequence + 1
                or prior != previous
                or request != fingerprint({"kind": kind, "payload": payload})
            ):
                raise PaperError("JOURNAL_DIVERGENCE")
            effect = reduce(state, kind, payload, context)
            rows = self._rows(key, seq, event_key, effect)
            expected = fingerprint(
                {
                    "previous": previous,
                    "key": event_key,
                    "request": request,
                    "state": effect.state.data(),
                    "rows": rows,
                }
            )
            if chain != expected:
                raise PaperError("JOURNAL_DIVERGENCE")
            for table, values in rows.items():
                for value in values:
                    if table in ("fills", "attributions"):
                        stored = self.db.execute(
                            f"SELECT * FROM {table} WHERE run_key=? AND fill_id=?", (key, value[1])
                        ).fetchone()
                        if stored != value:
                            raise PaperError("FILL_OR_ATTRIBUTION_DIVERGENCE")
                    counts[table] += 1
                if table not in ("fills", "attributions"):
                    order = "idx" if table == "postings" else "rowid"
                    stored = list(
                        self.db.execute(
                            f"SELECT * FROM {table} WHERE run_key=? AND seq=? ORDER BY {order}",
                            (key, seq),
                        )
                    )
                    if stored != values:
                        raise PaperError("PROJECTION_DIVERGENCE")
            state = effect.state
            previous = chain
        if not state.sequence or run[1:] != (
            "COMPLETED" if state.finished else "RUNNING",
            state.sequence,
            canonical(state.data()),
            previous,
        ):
            raise PaperError("CHECKPOINT_DIVERGENCE")
        for table, count in counts.items():
            if (
                self.db.execute(f"SELECT count(*) FROM {table} WHERE run_key=?", (key,)).fetchone()[
                    0
                ]
                != count
            ):
                raise PaperError("HISTORY_DIVERGENCE")
        self.verified = self._signature(key, state.sequence)
        return state

    def reconcile(self, key: str) -> State:
        self.flush()
        try:
            state = self.replay(key)
        except Exception:
            self.block()
            raise PaperError("RECONCILIATION_FAILED") from None
        if self.blocked():
            raise PaperError("BACKTEST_BLOCKED")
        return state

    def reconcile_all(self) -> dict[str, State]:
        # Numero de curvas e fixo: 3 segmentos x 4 variantes x 4 perfis.
        return {
            str(row[0]): self.reconcile(str(row[0]))
            for row in self.db.execute("SELECT run_key FROM runs ORDER BY run_key")
        }
