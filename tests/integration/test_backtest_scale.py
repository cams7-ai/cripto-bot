"""Escala sintetica de 24 meses: serie integral, estado limitado e custo medido."""

import gzip
import hashlib
import json
import os
import subprocess
import sys

from tests.unit.test_backtest import ROOT
from tests.unit.test_backtest import config as config


def test_70130_candles_bounded_state_and_streaming(config):
    path = config.source_manifests[0].parent
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["count"] = 70130
    manifest["end"] = config.start + 70080 * 900000
    hasher = hashlib.sha256()
    with gzip.GzipFile(path / "candles.jsonl.gz", "wb", mtime=0) as stream:
        for index in range(70130):
            opened = config.start + (index - 50) * 900000
            row = {
                "open_time": opened,
                "close_time": opened + 899999,
                "open": "100",
                "high": "100",
                "low": "100",
                "close": "100",
                "base_volume": "100",
                "quote_volume": "10000",
                "trades": 1,
                "symbol": "BTCUSDT",
                "interval": "15m",
            }
            raw = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
            stream.write(raw)
            hasher.update(raw)
    manifest["data_sha256"] = hasher.hexdigest()
    manifest["files"]["candles.jsonl.gz"] = hashlib.sha256(
        (path / "candles.jsonl.gz").read_bytes()
    ).hexdigest()
    (path / "manifest.json").write_text(json.dumps(manifest))
    config_path = config.root / "config/backtest.toml"
    config_path.write_text(
        config_path.read_text().replace("2026-10-11T00:00:00Z", "2028-09-30T00:00:00Z")
    )
    script = f"""
import json,time,resource
from pathlib import Path
from cripto_bot.adapters.config import load_config
from cripto_bot.adapters.backtest_dataset import prepare
from cripto_bot.adapters.storage.backtest_sqlite import BacktestStore
from cripto_bot.application.backtest_run import protocol,curve_context,execute_curve
from cripto_bot.domain.market import canonical
start=time.perf_counter()
config=load_config(Path({str(config_path)!r}),{{}},root=Path({str(config.root)!r})).backtest
manifest=prepare(config);seal=protocol(config);sizes=[]
with BacktestStore(config.data_dir,create=True) as store:
    store.freeze(seal)
    for segment in ('development','validation','test'):
        state=execute_curve(store,config,curve_context(config,seal,segment,'base','sma'))
        assert state.finished and state.metrics.fills==0 and len(state.window)<=51
        sizes.append(len(canonical(state.data()).encode()))
        assert state.wallet.equity==10000
db_bytes=(config.data_dir/'backtest.sqlite3').stat().st_size
print(json.dumps({{
 'candles':manifest['count'],'score_candles':manifest['score_count'],
 'elapsed_seconds':time.perf_counter()-start,
 'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
 'database_bytes':db_bytes,'max_checkpoint_bytes':max(sizes),'max_window':51,
 'profiles':'SMA/base all three independent segments; synthetic constant prices'}}))
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
        timeout=1200,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    (config.root / "scale-result.json").write_text(json.dumps(result, indent=2))
    assert result["candles"] == 70130 and result["score_candles"] == 70080
    assert result["max_checkpoint_bytes"] < 16000
    print("BACKTEST_SCALE " + proc.stdout.strip())
