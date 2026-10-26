"""Morte real antes/depois do rename de uma exportacao em leitura."""

import json
import os
import subprocess
import sys

import pytest

from cripto_bot.adapters.backtest_dataset import file_hash
from cripto_bot.adapters.storage.backtest_report import export_report, snapshot
from cripto_bot.adapters.storage.backtest_sqlite import BacktestStore
from cripto_bot.application.backtest_run import execute_curve, protocol
from tests.unit.test_backtest import ROOT, context
from tests.unit.test_backtest import config as config


@pytest.mark.parametrize("stage", ["before", "after"])
def test_death_at_publication_preserves_wallet_and_retry(config, stage):
    ctx = context(config)
    with BacktestStore(config.data_dir, create=True) as store:
        store.freeze(protocol(config))
        execute_curve(store, config, ctx)
    original = file_hash(config.data_dir / "backtest.sqlite3")
    script = f"""
import os
from pathlib import Path
from cripto_bot.adapters.storage.backtest_sqlite import BacktestStore
from cripto_bot.adapters.storage.backtest_report import snapshot,export_report
original=Path.rename
def rename(self,target):
    if {stage!r}=='before':os._exit(72)
    original(self,target)
    os._exit(72)
Path.rename=rename
with BacktestStore(Path({str(config.data_dir)!r}),readonly=True) as store:
    export_report(store,snapshot(store,store.reconcile_all()),'death-publication')
"""
    env = {
        k: v for k, v in os.environ.items() if k != "PYTHONPATH" and not k.startswith("CRIPTO_BOT_")
    }
    proc = subprocess.run(
        [sys.executable, "-I", str(ROOT / "tests/offline_runner.py"), "--code", script],
        cwd=config.root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 72, proc.stderr
    assert file_hash(config.data_dir / "backtest.sqlite3") == original
    target = config.data_dir / "exports/death-publication"
    assert target.exists() == (stage == "after")
    if target.exists():
        for name, digest in json.loads((target / "manifest.json").read_text())["files"].items():
            assert file_hash(target / name) == digest
    with BacktestStore(config.data_dir, readonly=True) as store:
        report = snapshot(store, store.reconcile_all())
        manifest = export_report(store, report, "retry-after-death")
        assert json.loads(manifest.read_text())["result_sha256"] == report["result_sha256"]
    assert file_hash(config.data_dir / "backtest.sqlite3") == original
