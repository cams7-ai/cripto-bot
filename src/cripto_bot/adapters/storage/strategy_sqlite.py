"""Schema v2 novo; mesmo journal transacional da carteira, sem migracao v1."""

from typing import cast

from cripto_bot.adapters.storage.paper_sqlite import DDL as FINANCIAL_DDL
from cripto_bot.adapters.storage.paper_sqlite import PaperStore
from cripto_bot.domain.market import canonical
from cripto_bot.domain.portfolio.models import (
    Intent,
    PaperError,
    Plan,
    Posting,
    Wallet,
    fingerprint,
    record,
)
from cripto_bot.domain.strategy.engine import MODEL, StrategyWallet, reduce

EXTRA_DDL = (
    (
        "CREATE TABLE configurations (generation INTEGER PRIMARY KEY,"
        " config_hash TEXT NOT NULL, data TEXT NOT NULL)"
    ),
    "CREATE TABLE instances (instance_id TEXT PRIMARY KEY, data TEXT NOT NULL)",
    (
        "CREATE TABLE cycles (decision_id TEXT PRIMARY KEY, generatio"
        "n INTEGER NOT NULL REFERENCES configurations(generation), ac"
        "tion_key TEXT UNIQUE REFERENCES intents(action_key), data TE"
        "XT NOT NULL)"
    ),
    (
        "CREATE TABLE attributions (fill_id TEXT PRIMARY KEY REFERENC"
        "ES fills(fill_id), decision_id TEXT NOT NULL REFERENCES cycl"
        "es(decision_id), data TEXT NOT NULL)"
    ),
)
IMMUTABLE_DDL = tuple(
    f"CREATE TRIGGER immutable_{table}_{mutation} BEFORE {mutation} ON {table} "
    "BEGIN SELECT RAISE(ABORT,'immutable modular history'); END"
    for table in ("configurations", "attributions")
    for mutation in ("UPDATE", "DELETE")
)


class StrategyStore(PaperStore):
    schema_version = 2
    model = MODEL
    ddl = FINANCIAL_DDL + EXTRA_DDL + IMMUTABLE_DDL
    tables = PaperStore.tables | {"configurations", "instances", "cycles", "attributions"}

    def __enter__(self) -> "StrategyStore":
        self._verified: tuple[int, int, int, int] | None = None
        super().__enter__()
        try:
            expected = list(self.ddl) + [
                f"CREATE TRIGGER immutable_{table}_{mutation} BEFORE {mutation} ON {table} "
                "BEGIN SELECT RAISE(ABORT,'immutable paper history'); END"
                for table in ("journal", "postings", "fills")
                for mutation in ("UPDATE", "DELETE")
            ]
            expected_sql = {
                statement.split()[2]: " ".join(statement.split()) for statement in expected
            }
            actual = {
                name: " ".join(sql.split())
                for name, sql in self.db.execute(
                    "SELECT name,sql FROM sqlite_schema WHERE sql IS NOT NULL"
                )
            }
            if actual != expected_sql:
                raise PaperError("INCOMPATIBLE_STRATEGY_SCHEMA")
            return self
        except BaseException:
            self.close()
            raise

    def empty_wallet(self) -> StrategyWallet:
        return StrategyWallet()

    def reduce(
        self,
        wallet: Wallet,
        kind: str,
        payload: dict[str, object],
        parameters: dict[str, object],
        rules: dict[str, object],
    ) -> tuple[Wallet, list[Posting]]:
        return reduce(wallet, kind, payload, parameters, rules)

    def intent_record(self, action: str, intent: Intent, wallet: Wallet) -> object:
        state = cast(StrategyWallet, wallet)
        return {"intent": record(intent), "envelope": state.cycles[action]}

    def fill_record(self, plan: Plan, wallet: Wallet) -> object:
        state = cast(StrategyWallet, wallet)
        return {"plan": record(plan), "envelope": state.cycles[plan.action]}

    def posting_records(
        self,
        postings: object,
        wallet: Wallet,
        context: dict[str, object],
        key: str,
        payload: dict[str, object],
    ) -> list[dict[str, object]]:
        rows = super().posting_records(postings, wallet, context, key, payload)
        state = cast(StrategyWallet, wallet)
        action = str(payload.get("action_key", ""))
        if action in state.cycles:
            for row in rows:
                row["envelope"] = state.cycles[action]
        return rows

    def _write(
        self,
        key: str,
        kind: str,
        payload: dict[str, object],
        wallet: Wallet,
        context: dict[str, object],
    ) -> Wallet:
        state = cast(StrategyWallet, super()._write(key, kind, payload, wallet, context))
        for item in state.generations:
            self.db.execute(
                "INSERT OR IGNORE INTO configurations VALUES(?,?,?)",
                (item["generation"], item["config_hash"], canonical(item)),
            )
        for instance, data in state.instances.items():
            self.db.execute(
                "INSERT INTO instances VALUES(?,?) ON CONFLICT(instance_id) "
                "DO UPDATE SET data=excluded.data",
                (instance, canonical(data)),
            )
        for decision, data in state.cycles.items():
            self.db.execute(
                "INSERT INTO cycles VALUES(?,?,?,?) ON CONFLICT(decision_id) "
                "DO UPDATE SET data=excluded.data",
                (
                    decision,
                    data["config_generation"],
                    decision if decision in state.intents else None,
                    canonical(data),
                ),
            )
        for fill, data in state.attributions.items():
            self.db.execute(
                "INSERT OR IGNORE INTO attributions VALUES(?,?,?)",
                (fill, data["action_key"], canonical(data)),
            )
        return state

    def _signature(self, version: int) -> tuple[int, int, int, int]:
        return (
            version,
            self.db.total_changes,
            self.db.execute("PRAGMA data_version").fetchone()[0],
            self.db.execute("PRAGMA schema_version").fetchone()[0],
        )

    def append(self, key: str, kind: str, payload: dict[str, object], wallet: Wallet) -> Wallet:
        if self.blocked():
            raise PaperError("WALLET_BLOCKED")
        with self.transaction():
            existing = self.db.execute(
                "SELECT request_hash FROM journal WHERE event_key=?", (key,)
            ).fetchone()
            if existing is not None:
                if existing[0] != fingerprint({"kind": kind, "payload": payload}):
                    raise PaperError("EVENT_ID_CONFLICT")
                return self.replay()
            # Escritor exclusivo: entre commits verificados, CAS e assinatura do
            # SQLite evitam repetir toda a reconstrucao em cada candle. Qualquer
            # escrita fora do caminho normal invalida o cache e exige replay.
            if self._verified != self._signature(wallet.version):
                current = self.replay()
                if current.data() != wallet.data():
                    raise PaperError("STALE_WALLET_VERSION")
            projection = self.db.execute(
                "SELECT version,data FROM projection WHERE id=1"
            ).fetchone()
            if projection != (wallet.version, canonical(wallet.data())):
                raise PaperError("STALE_WALLET_VERSION")
            updated = self._write(key, kind, payload, wallet, self.context())
        self._verified = self._signature(updated.version)
        return updated

    def replay(self, *, check_projection: bool = True) -> StrategyWallet:
        state = cast(StrategyWallet, super().replay(check_projection=check_projection))
        if check_projection:
            expected = {
                "configurations": [
                    (x["generation"], x["config_hash"], canonical(x)) for x in state.generations
                ],
                "instances": [
                    (key, canonical(value)) for key, value in sorted(state.instances.items())
                ],
                "cycles": [
                    (
                        key,
                        value["config_generation"],
                        key if key in state.intents else None,
                        canonical(value),
                    )
                    for key, value in sorted(state.cycles.items())
                ],
                "attributions": [
                    (key, value["action_key"], canonical(value))
                    for key, value in sorted(state.attributions.items())
                ],
            }
            for table, values in expected.items():
                if list(self.db.execute(f"SELECT * FROM {table} ORDER BY 1")) != values:
                    raise PaperError("MODULAR_PROJECTION_DIVERGENCE")
        if check_projection:
            self._verified = self._signature(state.version)
        return state
