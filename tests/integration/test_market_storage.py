import base64
import gzip
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from cripto_bot.adapters.storage.market_export import export_market
from cripto_bot.adapters.storage.market_sqlite import SQLiteMarketStore
from cripto_bot.domain.market import STEP, MarketError, digest, parse_candle

from tests.factories import BASE, ROOT, raw, sample


def save_first(store, dataset="fixture:test:v1"):
    response = raw(sample()["klines"][:1])
    return store.commit_page(
        dataset, BASE, BASE + STEP, response, [parse_candle(response.json()[0])]
    )


def test_atomic_dedup_conflict_and_isolation(tmp_path):
    directory = tmp_path / "market"
    dataset = "fixture:test:v1"
    with SQLiteMarketStore(directory) as store:
        assert store.checkpoint(dataset, BASE) == BASE
        assert save_first(store) == (1, 0)
        assert store.checkpoint(dataset, BASE) == BASE + STEP
        # Outra janela compartilha candle, mas nao o checkpoint.
        other = BASE - STEP
        store.db.execute("INSERT INTO checkpoints VALUES(?,?,?)", (dataset, other, BASE))
        candle = parse_candle(sample()["klines"][0])
        # Prefixo incompleto nao pode ser confirmado; rollback deixa o bruto intacto.
        before = store.db.execute("SELECT count(*) FROM raw").fetchone()[0]
        with pytest.raises(MarketError, match="CORRUPT_CHECKPOINT"):
            store.commit_page(dataset, other, BASE + STEP, raw([]), [candle])
        assert store.db.execute("SELECT count(*) FROM raw").fetchone()[0] == before
        # Janela nova com mesmo inicio mas checkpoint removido revalida e deduplica.
        store.db.execute("DELETE FROM checkpoints WHERE start=?", (BASE,))
        assert store.commit_page(dataset, BASE, BASE + STEP, raw([]), [candle]) == (0, 1)
        changed = sample()["klines"][0]
        changed[4] = "100.5"
        with pytest.raises(MarketError, match="CONFLICT"):
            store.commit_page(
                dataset, BASE, BASE + 2 * STEP, raw([changed]), [parse_candle(changed)]
            )
        assert store.records(dataset, BASE, BASE + STEP)[0]["close"] == "101"
        assert store.checkpoint("public", BASE) == BASE
        assert store.checkpoint(dataset, BASE + STEP) == BASE + STEP
        with pytest.raises(MarketError, match="NO_PROGRESS"):
            store.commit_page(dataset, BASE, BASE + STEP, raw([]), [])
    with SQLiteMarketStore(directory) as store:
        store.verify_prefix(dataset, BASE, BASE + STEP)


def test_lock_and_rate_limit_survive_restart(tmp_path):
    with SQLiteMarketStore(tmp_path) as first:
        with pytest.raises(MarketError, match="WRITER_BUSY"):
            with SQLiteMarketStore(tmp_path):
                pytest.fail("segundo escritor adquiriu lock")
        first.block_until(1234)
        first.block_until(1000)
    with SQLiteMarketStore(tmp_path) as store:
        assert store.not_before() == 1234


@pytest.mark.parametrize(
    "mutation",
    [
        "DELETE FROM candles",
        "UPDATE candles SET data='{}'",
        "UPDATE raw SET body=x'00'",
        "UPDATE checkpoints SET next_open=next_open+1",
    ],
)
def test_prefix_integrity(tmp_path, mutation):
    with SQLiteMarketStore(tmp_path) as store:
        save_first(store)
        store.db.execute(mutation)
        with pytest.raises(MarketError, match="CORRUPT"):
            store.verify_prefix("fixture:test:v1", BASE, store.checkpoint("fixture:test:v1", BASE))


@pytest.mark.parametrize("kind", ["corrupt", "schema", "foreign"])
def test_do_not_recreate_invalid_database(tmp_path, kind):
    path = tmp_path / "market.sqlite3"
    if kind == "corrupt":
        path.write_bytes(b"not-a-database")
    else:
        db = sqlite3.connect(path)
        db.execute(
            "PRAGMA user_version=99" if kind == "schema" else "CREATE TABLE foreign_table(a)"
        )
        db.close()
    before = path.read_bytes()
    with pytest.raises((MarketError, sqlite3.DatabaseError)):
        with SQLiteMarketStore(tmp_path):
            pytest.fail("banco invalido aceito")
    assert path.read_bytes() == before


def test_symlink_database_rejected(tmp_path):
    target = tmp_path / "outside"
    target.write_bytes(b"untouched")
    directory = tmp_path / "market"
    directory.mkdir()
    (directory / "market.sqlite3").symlink_to(target)
    with pytest.raises(MarketError, match="UNSAFE"):
        with SQLiteMarketStore(directory):
            pytest.fail("symlink aceito")
    assert target.read_bytes() == b"untouched"


def test_export_hashes_raw_and_replay(tmp_path, monkeypatch):
    with SQLiteMarketStore(tmp_path) as store:
        save_first(store)
        path = export_market(store, "fixture:test:v1", BASE, BASE + STEP, "first", {})
        manifest = json.loads(path.read_text())
        for name, expected in manifest["files"].items():
            assert digest((path.parent / name).read_bytes()) == expected
        content = gzip.decompress((path.parent / "candles.jsonl.gz").read_bytes())
        assert digest(content) == manifest["data_sha256"]
        raw_record = json.loads(gzip.decompress((path.parent / "raw.jsonl.gz").read_bytes()))
        assert digest(base64.b64decode(raw_record["body_base64"])) == raw_record["sha256"]
        second = export_market(store, "fixture:test:v1", BASE, BASE + STEP, "second", {})
        assert json.loads(second.read_text())["data_sha256"] == manifest["data_sha256"]

        def failed(*args, **kwargs):
            raise OSError("SYNTHETIC_SECRET")

        with monkeypatch.context() as context:
            context.setattr(Path, "rename", failed)
            with pytest.raises(OSError):
                export_market(store, "fixture:test:v1", BASE, BASE + STEP, "failed", {})
        assert not (tmp_path / "exports/failed").exists()
        assert not list((tmp_path / "exports").glob(".pending-*"))
        assert store.checkpoint("fixture:test:v1", BASE) == BASE + STEP
        export_market(store, "fixture:test:v1", BASE, BASE + STEP, "recovered", {})


@pytest.mark.parametrize("commit", [False, True])
def test_process_death_transaction_and_lock_release(tmp_path, commit):
    directory = tmp_path / "db"
    program = f"""
import os
from pathlib import Path
from cripto_bot.adapters.storage.market_sqlite import SQLiteMarketStore
from cripto_bot.application.ports import RawResponse
from cripto_bot.domain.market import parse_candle
store=SQLiteMarketStore(Path({str(directory)!r})).__enter__()
row=[{BASE},'100','102','99','101','1.5',{BASE + STEP - 1},'151.5',5,'0.5','50.5','0']
if not {commit!r}: store.verify_prefix=lambda *args:os._exit(17)
raw=RawResponse('fixture',{{}},b'[]','test')
store.commit_page('crash', {BASE}, {BASE + STEP}, raw, [parse_candle(row)])
os._exit(17)
"""
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "tests/offline_runner.py"), "--code", program],
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 17, proc.stderr
    with SQLiteMarketStore(directory) as store:
        assert store.checkpoint("crash", BASE) == (BASE + STEP if commit else BASE)
        assert len(store.records("crash", BASE, BASE + STEP)) == (1 if commit else 0)
        assert store.db.execute("SELECT count(*) FROM raw").fetchone()[0] == (1 if commit else 0)


def test_export_unsafe_paths_and_failed_manifest(tmp_path, monkeypatch):
    with SQLiteMarketStore(tmp_path) as store:
        save_first(store)
        with pytest.raises(MarketError, match="UNSAFE"):
            export_market(store, "fixture:test:v1", BASE, BASE + STEP, "../escape", {})
        original = Path.open

        def fail_manifest(path, *args, **kwargs):
            if path.name == "manifest.json":
                raise OSError("full disk")
            return original(path, *args, **kwargs)

        with monkeypatch.context() as context:
            context.setattr(Path, "open", fail_manifest)
            with pytest.raises(OSError):
                export_market(store, "fixture:test:v1", BASE, BASE + STEP, "manifest-failed", {})
        assert not list((tmp_path / "exports").iterdir())
        store.close()
        store.close()


def test_record_with_inconsistent_open_time(tmp_path):
    from cripto_bot.domain.market import canonical

    with SQLiteMarketStore(tmp_path) as store:
        save_first(store)
        content = canonical({"open_time": BASE + STEP})
        store.db.execute("UPDATE candles SET data=?,sha256=?", (content, digest(content.encode())))
        with pytest.raises(MarketError, match="CORRUPT_RECORD"):
            store.verify_prefix("fixture:test:v1", BASE, BASE + STEP)
