import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from decimal import Decimal as D

import pytest

from cripto_bot.adapters.execution.paper import PaperExecutor
from cripto_bot.adapters.storage.paper_report import summary
from cripto_bot.adapters.storage.paper_sqlite import PaperStore
from cripto_bot.application.paper_reconcile import reconcile
from cripto_bot.application.paper_run import run_scenario
from cripto_bot.domain.portfolio.models import PaperError
from tests.factories import ROOT
from tests.paper_factories import context, order, scenario, write_case


def run(store):
    return run_scenario(store, scenario(), "paper-demo", PaperExecutor())


def test_persistence_idempotence_journal_and_ledger(tmp_path):
    ctx = context(tmp_path)
    directory = tmp_path / "data"
    with PaperStore(directory, create=True) as store:
        store.initialize(ctx)
        wallet, counts = run(store)
        first = summary(store, wallet)
        assert counts["simulated_fills"] == 2 and first["total_simulated_fills"] == 2
        assert wallet.usdt_free == D("10009.58001")
        for (seq,) in store.db.execute("SELECT seq FROM journal"):
            postings = [
                json.loads(row[0])
                for row in store.db.execute("SELECT data FROM postings WHERE seq=?", (seq,))
            ]
            for asset in ("BTC", "USDT"):
                assert sum((D(p["quantity"]) for p in postings if p["asset"] == asset), D(0)) == 0
            assert all(
                p["wallet_id"] == "paper-demo" and p["timestamp"].endswith("+00:00")
                for p in postings
            )
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute("UPDATE postings SET data='{}'")
        with pytest.raises(PaperError, match="CONTEXT_CONFLICT"):
            store.initialize({**ctx, "scenario_sha256": "different"})
        assert store.seen("paper-demo:roundtrip-v1:buy", "ORDER", order())
        with pytest.raises(PaperError, match="EVENT_ID_CONFLICT"):
            store.seen("paper-demo:roundtrip-v1:buy", "ORDER", order(quantity="2"))
        # Mesmo evento devolve histórico, mesmo com token de versão antigo.
        assert (
            store.append("paper-demo:roundtrip-v1:buy", "ORDER", order(), wallet).data()
            == wallet.data()
        )
    with PaperStore(directory) as store:
        store.initialize(ctx)
        wallet, counts = run(store)
        assert counts == {"simulated_fills": 0, "rejected_intents": 0, "replayed_actions": 3}
        assert summary(store, wallet)["financial_sha256"] == first["financial_sha256"]
        assert store.db.execute("SELECT count(*) FROM fills").fetchone()[0] == 2


def test_version_cas_and_writer_lock(tmp_path):
    with PaperStore(tmp_path, create=True) as store:
        store.initialize(context(tmp_path))
        old = store.replay()
        day = scenario()["events"][0]
        current = store.append("day", "DAY_START", day, old)
        with pytest.raises(PaperError, match="STALE_WALLET_VERSION"):
            store.append("buy", "ORDER", order(), old)
        assert store.replay().data() == current.data()
        with pytest.raises(PaperError, match="WRITER_BUSY"):
            with PaperStore(tmp_path):
                pytest.fail("segundo escritor aceito")


@pytest.mark.parametrize("mutation", ["projection", "intents", "fills", "journal", "postings"])
def test_divergence_is_blocked_and_never_fixed(tmp_path, mutation):
    with PaperStore(tmp_path, create=True) as store:
        store.initialize(context(tmp_path))
        wallet, _ = run(store)
        count = store.db.execute("SELECT count(*) FROM journal").fetchone()[0]
        if mutation == "projection":
            store.db.execute("UPDATE projection SET data='{}'")
        elif mutation == "intents":
            store.db.execute("UPDATE intents SET data='{}'")
        elif mutation == "fills":
            store.db.execute("DROP TRIGGER immutable_fills_UPDATE")
            store.db.execute("UPDATE fills SET data='{}'")
        elif mutation == "journal":
            store.db.execute("DROP TRIGGER immutable_journal_UPDATE")
            store.db.execute("UPDATE journal SET chain_hash='bad' WHERE seq=2")
        else:
            store.db.execute("DROP TRIGGER immutable_postings_DELETE")
            store.db.execute("DELETE FROM postings WHERE seq=1 AND idx=0")
        with pytest.raises(PaperError, match="RECONCILIATION_FAILED"):
            reconcile(store)
        assert store.blocked() == "RECONCILIATION_FAILED"
        assert store.db.execute("SELECT count(*) FROM journal").fetchone()[0] == count
        with pytest.raises(PaperError, match="WALLET_BLOCKED"):
            store.append("another", "MARK", {}, wallet)
    with PaperStore(tmp_path, readonly=True) as store:
        assert store.blocked()


@pytest.mark.parametrize("phase", ["before-A", "after-A", "before-B", "after-B"])
def test_process_death_two_commits_and_recovery(tmp_path, phase):
    config = write_case(tmp_path)
    program = f"""
import os
from pathlib import Path
from cripto_bot.adapters.config import load_config
from cripto_bot.adapters.storage.paper_sqlite import PaperStore
from cripto_bot.entrypoints.paper import execute_paper
original_write=PaperStore._write
original_append=PaperStore.append
phase={phase!r}
def write(self,key,kind,payload,wallet,context):
    result=original_write(self,key,kind,payload,wallet,context)
    if (phase=='before-A' and kind=='ORDER') or (phase=='before-B' and kind=='FILL'):
        os._exit(19)
    return result
def append(self,key,kind,payload,wallet):
    result=original_append(self,key,kind,payload,wallet)
    if (phase=='after-A' and kind=='ORDER') or (phase=='after-B' and kind=='FILL'):
        os._exit(19)
    return result
PaperStore._write=write
PaperStore.append=append
execute_paper(load_config(Path({str(config)!r}),{{}}),'paper-run','crash')
"""
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "tests/offline_runner.py"), "--code", program],
        cwd=ROOT,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert proc.returncode == 19, proc.stderr
    from cripto_bot.adapters.config import load_config
    from cripto_bot.entrypoints.paper import execute_paper

    with PaperStore(tmp_path / "data") as store:
        wallet = reconcile(store)
        if phase == "before-A":
            assert wallet.usdt_reserved == 0 and not wallet.intents
        elif phase in ("after-A", "before-B"):
            assert wallet.usdt_reserved == D("100.2001") and wallet.btc_free == 0
        else:
            assert wallet.usdt_reserved == 0 and wallet.btc_free == 1
    settings = load_config(config, {})
    code, result = execute_paper(settings, "paper-run", "recovered")
    assert code == 0 and result["total_simulated_fills"] == 2
    assert result["wallet"]["usdt_free"] == "10009.58001000"
    code, repeat = execute_paper(settings, "paper-run", "repeat")
    assert code == 0 and repeat["simulated_fills"] == 0
    assert repeat["financial_sha256"] == result["financial_sha256"]


@pytest.mark.parametrize("kind", ["corrupt", "schema"])
def test_invalid_database_is_preserved(tmp_path, kind):
    path = tmp_path / "paper.sqlite3"
    if kind == "corrupt":
        path.write_bytes(b"corrupt")
    else:
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA user_version=99")
    before = path.read_bytes()
    with pytest.raises((PaperError, sqlite3.Error)):
        with PaperStore(tmp_path):
            pytest.fail("banco invalido aceito")
    assert path.read_bytes() == before


def test_report_does_not_initialize_missing_wallet(tmp_path):
    directory = tmp_path / "missing"
    with pytest.raises(PaperError, match="WALLET_NOT_FOUND"):
        with PaperStore(directory, readonly=True):
            pytest.fail("carteira ausente aceita")
    assert not directory.exists()


def test_conflicting_append_cannot_modify_confirmed_history(tmp_path):
    with PaperStore(tmp_path, create=True) as store:
        store.initialize(context(tmp_path))
        wallet, _ = run(store)
        before = store.db.execute("SELECT count(*) FROM journal").fetchone()[0]
        with pytest.raises(PaperError, match="EVENT_ID_CONFLICT"):
            store.append("paper-demo:roundtrip-v1:buy", "ORDER", order(quantity="2"), wallet)
        assert store.db.execute("SELECT count(*) FROM journal").fetchone()[0] == before
        assert store.replay().data() == wallet.data()
