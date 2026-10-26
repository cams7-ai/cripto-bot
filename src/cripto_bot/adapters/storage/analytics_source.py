"""Consumer-owned stable reconciled view: financial SQL never leaves the bot."""

import json
from collections.abc import Iterator, Mapping

from cripto_core.validation import require_object

from cripto_bot.adapters.storage.backtest_sqlite import BacktestStore

TABLES = {"equity": "equity", "trades": "fills", "decisions": "decisions"}


class AnalyticsRows:
    def __init__(self, store: BacktestStore) -> None:
        self._store = store

    def iter_rows(self, table: str) -> Iterator[Mapping[str, object]]:
        source = TABLES[table]
        for key, body in self._store.db.execute(
            f"SELECT run_key,data FROM {source} ORDER BY run_key,rowid"
        ):
            row = require_object(json.loads(body))
            flat = {
                **row,
                **(require_object(row["plan"]) if table == "trades" else {}),
                "run_key": key,
            }
            if table != "equity":
                flat["data_json"] = body
            yield flat

    def counts(self, table: str) -> dict[str, int]:
        source = TABLES[table]
        return {
            str(key): int(count)
            for key, count in self._store.db.execute(
                f"SELECT run_key,count(*) FROM {source} GROUP BY run_key"
            )
        }
