"""Validacao do wheel instalado; executar com Python do ambiente principal."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
STAGE = os.environ.get("WHEEL_SMOKE_STAGE")
if STAGE not in {"layout", "final", "06b", "06c"}:
    raise ValueError("WHEEL_SMOKE_STAGE must be layout, final, 06b or 06c")
PYTHON = ROOT / (
    f".venv-wheel-{STAGE}/bin/python"
    if STAGE in {"06b", "06c"}
    else f".venv-wheel-06a-{STAGE}/bin/python"
)
SCRIPT = PYTHON.parent / "cripto-bot"
RUNNER = ROOT / "tests/offline_runner.py"
NEUTRAL = ROOT / ".wheel-smoke" / (STAGE if STAGE in {"06b", "06c"} else f"06a-{STAGE}")
NEUTRAL.mkdir(parents=True, exist_ok=True)
# Isolated application root keeps fixture test usage away from the real registry.
for name in ("pyproject.toml", "uv.lock", ".python-version"):
    shutil.copyfile(ROOT / name, NEUTRAL / name)
shutil.copytree(ROOT / "config/fixtures", NEUTRAL / "config/fixtures", dirs_exist_ok=True)
env = {
    key: value
    for key, value in os.environ.items()
    if not key.startswith("CRIPTO_BOT_")
    and key not in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "PYTHONPATH")
}
(wheel,) = (ROOT / "dist" / (f"{STAGE}-final" if STAGE in {"06b", "06c"} else f"06a-{STAGE}")).glob(
    "*.whl"
)
with ZipFile(wheel) as archive:
    assert not any(name.startswith("tests/") for name in archive.namelist())
origin = subprocess.run(
    [str(PYTHON), "-I", str(RUNNER), "--code", "import cripto_bot; print(cripto_bot.__file__)"],
    cwd=NEUTRAL,
    env=env,
    text=True,
    capture_output=True,
    check=True,
)
assert "site-packages/cripto_bot/__init__.py" in origin.stdout
core_origin = None
if STAGE in {"final", "06b", "06c"}:
    proof = subprocess.run(
        [
            str(PYTHON),
            "-I",
            str(RUNNER),
            "--code",
            "import json,cripto_core;from importlib.metadata import version;"
            "print(json.dumps({'origin':cripto_core.__file__,'version':version('cripto-core')}))",
        ],
        cwd=NEUTRAL,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    core_origin = json.loads(proof.stdout)
    assert "site-packages/cripto_core/__init__.py" in core_origin["origin"]
    assert core_origin["version"] == "0.1.0"
market_origin = None
if STAGE in {"06b", "06c"}:
    proof = subprocess.run(
        [
            str(PYTHON),
            "-I",
            str(RUNNER),
            "--code",
            "import json,cripto_market_data;from importlib.metadata import version;"
            "print(json.dumps({'origin':cripto_market_data.__file__,"
            "'version':version('cripto-market-data')}))",
        ],
        cwd=NEUTRAL,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    market_origin = json.loads(proof.stdout)
    assert "site-packages/cripto_market_data/__init__.py" in market_origin["origin"]
    assert market_origin["version"] == "0.1.0"
analytics_origin = None
if STAGE == "06c":
    proof = subprocess.run(
        [
            str(PYTHON),
            "-I",
            str(RUNNER),
            "--code",
            "import json,cripto_analytics;from importlib.metadata import version;"
            "print(json.dumps({'origin':cripto_analytics.__file__,'version':version('cripto-analytics')}))",
        ],
        cwd=NEUTRAL,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    analytics_origin = json.loads(proof.stdout)
    assert "site-packages/cripto_analytics/__init__.py" in analytics_origin["origin"]
    assert analytics_origin["version"] == "0.1.0"
checks = []
for command in ("check-config", "run-once"):
    proc = subprocess.run(
        [
            str(PYTHON),
            "-I",
            str(RUNNER),
            str(SCRIPT),
            command,
            "--config",
            str(ROOT / "config/paper.example.toml"),
        ],
        cwd=NEUTRAL,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["mode"] == "PAPER"
    assert result["status"] == ("NOOP" if command == "run-once" else "VALID")
    if command == "run-once":
        assert result["orders_sent"] == 0
    assert json.loads(proc.stderr)["run_id"] == result["run_id"]
    checks.append({"command": command, "exit_code": proc.returncode, "result": result})
# Diretorio de dados novo por smoke test: nao confundir repeticao com primeira coleta.

scratch = Path(tempfile.mkdtemp(prefix="market-", dir=NEUTRAL))
config = scratch / "fixture.toml"
config.write_text(
    'mode="PAPER"\n[market]\nsource="fixture"\n'
    f'fixture_file="{NEUTRAL / "config/fixtures/market-sample.json"}"\n'
    f'data_dir="{scratch / "data"}"\n'
)
for expected in (2, 0):
    collected = subprocess.run(
        [
            str(PYTHON),
            "-I",
            str(RUNNER),
            str(SCRIPT),
            "collect-market",
            "--config",
            str(config),
            "--start",
            "2026-10-01T00:00:00Z",
            "--end",
            "2026-10-01T00:30:00Z",
        ],
        cwd=NEUTRAL,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    result = json.loads(collected.stdout)
    assert result["inserted"] == expected and result["complete"] and result["orders_sent"] == 0
    manifest = json.loads(Path(result["manifest"]).read_text())
    assert manifest["count"] == 2
    checks.append(
        {"command": "collect-market", "exit_code": collected.returncode, "result": result}
    )
paper_config = scratch / "paper.toml"
paper_config.write_text(
    'mode="PAPER"\n[paper]\n'
    f'scenario_file="{NEUTRAL / "config/fixtures/paper-roundtrip.json"}"\n'
    f'data_dir="{scratch / "paper-wallet"}"\n'
)
financial_hash = None
for command, expected in (
    ("paper-run", 2),
    ("paper-reconcile", 0),
    ("paper-report", 0),
    ("paper-run", 0),
):
    paper = subprocess.run(
        [str(PYTHON), "-I", str(RUNNER), str(SCRIPT), command, "--config", str(paper_config)],
        cwd=NEUTRAL,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert paper.returncode == 0, paper.stdout + paper.stderr
    result = json.loads(paper.stdout)
    assert result["simulated_fills"] == expected and result["reconciled"]
    assert result["wallet"]["usdt_free"] == "10009.58001000"
    assert result["wallet"]["btc_free"] == "0.00000000"
    assert result["wallet"]["fees"] == "0.20999000" and result["orders_sent"] == 0
    if financial_hash is None:
        financial_hash = result["financial_sha256"]
    assert result["financial_sha256"] == financial_hash
    checks.append(
        {
            "command": command,
            "exit_code": paper.returncode,
            "result": {
                key: result[key]
                for key in (
                    "status",
                    "simulated_fills",
                    "total_simulated_fills",
                    "reconciled",
                    "financial_sha256",
                    "wallet",
                )
            },
        }
    )

# SMA em fixture: default desabilitado e carteira modular nova por smoke.
sma_template = (ROOT / "config/strategy.sma.example.toml").read_text()
sma_template = sma_template.replace(
    "config/fixtures/strategy-sma-crossings.json",
    str(NEUTRAL / "config/fixtures/strategy-sma-crossings.json"),
)
sma_config = scratch / "strategy.toml"
sma_config.write_text(sma_template.replace("data/sma-demo", str(scratch / "strategy-wallet")))
sma_hash = None
for command, expected in (
    ("strategy-run", 0),
    ("strategy-run", 2),
    ("strategy-reconcile", 0),
    ("strategy-report", 0),
    ("strategy-run", 0),
):
    if expected == 2:
        sma_config.write_text(sma_config.read_text().replace("enabled = false", "enabled = true"))
    proc_sma = subprocess.run(
        [str(PYTHON), "-I", str(RUNNER), str(SCRIPT), command, "--config", str(sma_config)],
        cwd=NEUTRAL,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert proc_sma.returncode == 0, proc_sma.stdout + proc_sma.stderr
    result = json.loads(proc_sma.stdout)
    assert result["simulated_fills"] == expected and result["orders_sent"] == 0
    if result["status"] == "STRATEGY_NOOP":
        assert not (scratch / "strategy-wallet").exists()
    else:
        assert result["wallet"]["usdt_free"] == "9905.46181258"
        assert result["total_evaluated_cycles"] == 71 and result["reconciled"]
        if sma_hash is None:
            sma_hash = result["financial_sha256"]
        assert result["financial_sha256"] == sma_hash
        if "manifest" in result:
            manifest = json.loads(Path(result["manifest"]).read_text())
            for name, digest in manifest["files"].items():
                body = (Path(result["manifest"]).parent / name).read_bytes()
                assert hashlib.sha256(body).hexdigest() == digest
    checks.append(
        {
            "command": command,
            "exit_code": proc_sma.returncode,
            "result": {
                k: result[k]
                for k in (
                    "status",
                    "simulated_fills",
                    "orders_sent",
                    "financial_sha256",
                    "total_evaluated_cycles",
                    "wallet",
                    "manifest",
                    "html_report",
                )
                if k in result
            },
        }
    )

# Historico: mesmo console instalado em site-packages, cwd neutro, guard ativo.
bt_config = scratch / "backtest.toml"
bt_template = (ROOT / "config/backtest.sma.example.toml").read_text()
bt_template = bt_template.replace(
    "config/fixtures/backtest-sma/manifest.json",
    str(NEUTRAL / "config/fixtures/backtest-sma/manifest.json"),
)
bt_template = bt_template.replace(
    "data/backtest-fixture-dataset", str(scratch / "backtest-dataset")
)
bt_template = bt_template.replace("data/backtest-example-v1", str(scratch / "backtest-wallet"))
bt_config.write_text(bt_template)
bt_hash = None
for command, segment in (
    ("backtest-run", "development"),
    ("backtest-prepare", None),
    ("backtest-freeze", None),
    ("backtest-run", "development"),
    ("backtest-run", "validation"),
    ("backtest-run", "test"),
    ("backtest-reconcile", None),
    ("backtest-report", None),
    ("backtest-run", "test"),
):
    if command == "backtest-prepare":
        bt_config.write_text(bt_template.replace("enabled = false", "enabled = true"))
    argv = [str(PYTHON), "-I", str(RUNNER), str(SCRIPT), command, "--config", str(bt_config)]
    if segment:
        argv += ["--segment", segment, "--variant", "all"]
    bt = subprocess.run(argv, cwd=NEUTRAL, env=env, text=True, capture_output=True, timeout=180)
    assert bt.returncode == 0, bt.stdout + bt.stderr
    result = json.loads(bt.stdout)
    assert result["orders_sent"] == 0
    if result["status"] == "BACKTEST_NOOP":
        assert not (scratch / "backtest-wallet").exists()
    if "manifest" in result:
        manifest = json.loads(Path(result["manifest"]).read_text())
        assert {
            "equity.parquet",
            "trades.parquet",
            "decisions.parquet",
            "analytics.duckdb",
            "analytics.json",
        } <= manifest["files"].keys()
        assert manifest["analytics"]["result_sha256"] == result["result_sha256"]
        for name, digest in manifest["files"].items():
            assert (
                hashlib.sha256((Path(result["manifest"]).parent / name).read_bytes()).hexdigest()
                == digest
            )
    if segment == "test":
        if bt_hash is None:
            bt_hash = result["result_sha256"]
        else:
            assert bt_hash == result["result_sha256"] and result["simulated_fills"] == 0
    checks.append(
        {"command": command, "segment": segment, "exit_code": bt.returncode, "result": result}
    )

proc = subprocess.run(
    [str(PYTHON), "-I", str(RUNNER), str(SCRIPT), "run-once"],
    cwd=NEUTRAL,
    env={**env, "CRIPTO_BOT_MODE": "LIVE"},
    text=True,
    capture_output=True,
    timeout=15,
)
assert proc.returncode == 2 and proc.stdout == ""
assert json.loads(proc.stderr)["event"] == "configuration_rejected"
probe = subprocess.run(
    [str(PYTHON), "-I", str(RUNNER), "--probe"],
    cwd=NEUTRAL,
    env=env,
    text=True,
    capture_output=True,
    timeout=15,
)
assert probe.returncode != 0 and "NETWORK_BLOCKED" in probe.stderr
print(
    json.dumps(
        {
            "stage": STAGE,
            "wheel": str(wheel.relative_to(ROOT)),
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "origin": origin.stdout.strip(),
            "core": core_origin,
            "market_data": market_origin,
            "analytics": analytics_origin,
            "checks": checks,
            "live_exit_code": proc.returncode,
            "socket_probe_blocked": True,
        },
        indent=2,
    )
)
