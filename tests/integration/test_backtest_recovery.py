"""Mortes reais com journal em copias sinteticas, sob guard de socket."""

import os
import subprocess
import sys

import pytest
from cripto_bot.adapters.storage.backtest_sqlite import BacktestStore
from cripto_bot.application.backtest_run import curve_context, execute_curve

from tests.unit.test_backtest import ROOT, sealed
from tests.unit.test_backtest import config as config


@pytest.mark.parametrize(
    "kind,where",
    [
        ("BOUNDARY", "before"),
        ("BOUNDARY", "after"),
        ("BOUNDARY", "after_schedule"),
        ("OBSERVE", "after"),
        ("FILL", "before"),
        ("FILL", "after"),
        ("END", "before"),
        ("END", "after"),
        ("INIT", "inside"),
    ],
)
def test_real_process_death_recovers_exactly_once(config, kind, where):
    seal = sealed(config)
    ctx = curve_context(config, seal, "development", "base", "sma")
    config_path = config.root / "config/backtest.toml"
    script = f"""
import os
from pathlib import Path
from cripto_bot.adapters.config import load_config
from cripto_bot.adapters.storage.backtest_sqlite import BacktestStore
from cripto_bot.application.backtest_run import protocol,curve_context,execute_curve
config=load_config(Path({str(config_path)!r}),{{}},root=Path({str(config.root)!r})).backtest
original=BacktestStore.append
def append(self,key,event_key,kind,payload,state):
    if kind=={kind!r} and {where!r}=='before' and (kind!='OBSERVE' or state.pending):os._exit(71)
    result=original(self,key,event_key,kind,payload,state)
    if kind=={kind!r} and {where!r}=='after':
        if kind!='OBSERVE' or result.wallet.intents:os._exit(71)
    if kind=={kind!r} and {where!r}=='after_schedule' and result.pending:os._exit(71)
    return result
BacktestStore.append=append
if {where!r}=='inside':
    original_write=BacktestStore._write
    def inside(self,*args,**kwargs):
        original_write(self,*args,**kwargs)
        os._exit(71)
    BacktestStore._write=inside
with BacktestStore(config.data_dir,create=True) as store:
    seal=protocol(config);store.freeze(seal)
    execute_curve(store,config,curve_context(config,seal,'development','base','sma'))
"""
    env = {
        k: v for k, v in os.environ.items() if k != "PYTHONPATH" and not k.startswith("CRIPTO_BOT_")
    }
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "tests/offline_runner.py"), "--code", script],
        cwd=config.root,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert proc.returncode == 71, proc.stderr
    with BacktestStore(config.data_dir) as store:
        obtained = execute_curve(store, config, ctx)
        fills = store.db.execute("SELECT count(*) FROM fills").fetchone()[0]
        assert fills == 12
        assert (
            store.db.execute("SELECT count(*) FROM fills").fetchone()
            == store.db.execute("SELECT count(*) FROM attributions").fetchone()
        )
        replay = execute_curve(store, config, ctx)
        assert obtained.data() == replay.data()
    fresh = config.root / "expected"
    with BacktestStore(fresh, create=True) as store:
        store.freeze(seal)
        expected = execute_curve(store, config, ctx)
    assert obtained.data() == expected.data()


def test_sql_checkpoint_cas_and_immutable_history(config):
    seal = sealed(config)
    ctx = curve_context(config, seal, "development", "base", "cash")
    with BacktestStore(config.data_dir, create=True) as store:
        store.freeze(seal)
        state = execute_curve(store, config, ctx)
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute("DELETE FROM journal")
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute("UPDATE protocol SET sealed='{}'")
        store.db.execute("UPDATE runs SET version=version+1")
        with pytest.raises(ValueError):
            store.reconcile(ctx["run_key"])
        assert store.blocked() and state.finished


def test_final_window_overlap_registry(config):
    from cripto_bot.application.backtest_run import consume_test

    seal = sealed(config)
    seal["dataset"]["source"] = "binance_public"
    consume_test(config, seal)
    consume_test(config, seal)
    changed = {**seal, "protocol_hash": "different"}
    with pytest.raises(ValueError, match="ALREADY_CONSUMED"):
        consume_test(config, changed)
    assert (config.root / "data/backtest-usage/test-usage.sqlite3").is_file()
