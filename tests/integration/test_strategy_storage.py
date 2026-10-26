import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest

from cripto_bot.adapters.storage.paper_sqlite import PaperStore
from cripto_bot.adapters.storage.strategy_sqlite import StrategyStore
from cripto_bot.application.strategy_reconcile import reconcile
from cripto_bot.domain.portfolio.models import PaperError
from tests.factories import ROOT
from tests.paper_factories import context
from tests.strategy_factories import run_case, write_case


def test_schema_ddl_constraints_immutability_and_legacy_compatibility(tmp_path):
    config = write_case(tmp_path)
    assert run_case(config)[0] == 0
    with StrategyStore(tmp_path / "wallet") as store:
        state = store.replay()
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert store.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {
            row[0] for row in store.db.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        } == store.tables
        assert (
            len(store.db.execute("SELECT name FROM sqlite_schema WHERE type='trigger'").fetchall())
            == 10
        )
        for table in ("configurations", "attributions", "journal", "postings", "fills"):
            with pytest.raises(sqlite3.IntegrityError):
                store.db.execute(f"DELETE FROM {table}")
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute("INSERT INTO attributions VALUES('unknown','unknown','{}')")
        with pytest.raises(PaperError, match="WRITER_BUSY"):
            with StrategyStore(tmp_path / "wallet"):
                pass
        with pytest.raises(PaperError, match="INCOMPATIBLE_PAPER_SCHEMA"):
            with PaperStore(tmp_path / "wallet", readonly=True):
                pass
        assert len(state.attributions) == 2
    legacy = tmp_path / "v1"
    with PaperStore(legacy, create=True) as store:
        store.initialize(context(tmp_path))
    before = (legacy / "paper.sqlite3").read_bytes()
    with pytest.raises(PaperError, match="INCOMPATIBLE_PAPER_SCHEMA"):
        with StrategyStore(legacy, create=True):
            pass
    assert (legacy / "paper.sqlite3").read_bytes() == before


@pytest.mark.parametrize("table", ["projection", "instances", "cycles", "intents"])
def test_divergence_is_readonly_in_report_and_durably_blocked_by_reconcile(tmp_path, table):
    config = write_case(tmp_path)
    assert run_case(config)[0] == 0
    with StrategyStore(tmp_path / "wallet") as store:
        store.db.execute(f"UPDATE {table} SET data='{{}}'")
    before = (tmp_path / "wallet/paper.sqlite3").read_bytes()
    code, report = run_case(config, "strategy-report", "diagnostic")
    assert code == 3 and report["blocked"] and not report["reconciled"]
    assert (tmp_path / "wallet/paper.sqlite3").read_bytes() == before
    code, result = run_case(config, "strategy-reconcile", "block")
    assert code == 3
    with StrategyStore(tmp_path / "wallet") as store:
        assert store.blocked()


def test_cas_and_external_mutation_invalidate_verified_cache(tmp_path):
    config = write_case(tmp_path)
    assert run_case(config)[0] == 0
    with StrategyStore(tmp_path / "wallet") as store:
        state = store.replay()
        snapshot = json.loads(json.dumps(state.configuration))
        snapshot["modules"][0]["enabled"] = False
        updated = store.append("config:2", "CONFIG", {"snapshot": snapshot}, state)
        with pytest.raises(PaperError, match="STALE_WALLET_VERSION"):
            store.append("config:3", "CONFIG", {"snapshot": snapshot}, state)
        store.db.execute("UPDATE instances SET data='{}'")
        with pytest.raises(PaperError, match="DIVERGENCE"):
            store.append("config:4", "CONFIG", {"snapshot": snapshot}, updated)


@pytest.mark.parametrize(
    "phase", ["before-A", "after-A", "before-B", "after-B", "before-HOLD", "after-HOLD"]
)
def test_real_process_death_preserves_cursors_reservations_and_unique_fills(tmp_path, phase):
    config = write_case(tmp_path)
    program = f"""
import os
from pathlib import Path
from cripto_bot.adapters.config import load_config
from cripto_bot.adapters.storage.strategy_sqlite import StrategyStore
from cripto_bot.entrypoints.strategy import execute_strategy
phase={phase!r}
original_write=StrategyStore._write
original_append=StrategyStore.append
def write(self,key,kind,payload,wallet,context):
    result=original_write(self,key,kind,payload,wallet,context)
    directional=kind=='CYCLE' and result.cycles[payload['action_key']]['target']!='HOLD'
    hold=kind=='CYCLE' and not directional
    if ((phase=='before-A' and directional) or (phase=='before-B' and kind=='FILL')
        or (phase=='before-HOLD' and hold)):
        os._exit(19)
    return result
def append(self,key,kind,payload,wallet):
    result=original_append(self,key,kind,payload,wallet)
    directional=kind=='CYCLE' and result.cycles[payload['action_key']]['target']!='HOLD'
    hold=kind=='CYCLE' and not directional
    if ((phase=='after-A' and directional) or (phase=='after-B' and kind=='FILL')
        or (phase=='after-HOLD' and hold)):
        os._exit(19)
    return result
StrategyStore._write=write
StrategyStore.append=append
execute_strategy(load_config(Path({str(config)!r}),{{}}),'strategy-run','crash')
"""
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "tests/offline_runner.py"), "--code", program],
        cwd=ROOT,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 19, proc.stderr + proc.stdout
    with StrategyStore(tmp_path / "wallet") as store:
        state = reconcile(store)
        if phase in ("after-A", "before-B"):
            assert str(state.usdt_reserved) == "999.99994389" and len(state.cycles) == 3
        elif phase == "before-HOLD":
            assert len(state.cycles) == 0
        elif phase == "after-HOLD":
            assert len(state.cycles) == 1
        elif phase == "before-A":
            assert len(state.cycles) == 2 and not state.intents
        else:
            assert len(state.cycles) == 3 and state.quantity > 0
    code, result = run_case(config, run_id="recover")
    assert code == 0 and result["total_simulated_fills"] == 2
    assert result["wallet"]["usdt_free"] == "9905.46181258"
    code, repeated = run_case(config, run_id="repeat")
    assert code == 0 and repeated["simulated_fills"] == 0
    assert repeated["financial_sha256"] == result["financial_sha256"]


@pytest.mark.parametrize("change", ["version", "view", "trigger", "table"])
def test_unexpected_v2_schema_objects_are_rejected_without_rewrite(tmp_path, change):
    config = write_case(tmp_path)
    assert run_case(config)[0] == 0
    path = tmp_path / "wallet/paper.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        if change == "version":
            connection.execute("PRAGMA user_version=3")
        elif change == "view":
            connection.execute("CREATE VIEW unexpected AS SELECT * FROM cycles")
        elif change == "trigger":
            connection.execute("DROP TRIGGER immutable_attributions_UPDATE")
        else:
            connection.execute("CREATE TABLE unexpected (id INTEGER)")
    before = path.read_bytes()
    with pytest.raises(PaperError, match="INCOMPATIBLE_"):
        with StrategyStore(tmp_path / "wallet", readonly=True):
            pass
    assert path.read_bytes() == before


def test_corrupt_attribution_is_blocked_after_deliberate_harness_injection(tmp_path):
    config = write_case(tmp_path)
    assert run_case(config)[0] == 0
    with StrategyStore(tmp_path / "wallet") as store:
        # Somente harness: a API normal proibe essa alteracao por trigger.
        store.db.execute("DROP TRIGGER immutable_attributions_UPDATE")
        store.db.execute("UPDATE attributions SET data='{}'")
        with pytest.raises(PaperError, match="RECONCILIATION_FAILED"):
            reconcile(store)
        assert store.blocked()
