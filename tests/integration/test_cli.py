import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tests/offline_runner.py"
SCRIPT = Path(sys.executable).parent / "cripto-bot"


def invoke(args, env=None):
    clean = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CRIPTO_BOT_")
        and key not in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "PYTHONPATH")
    }
    clean.update(env or {})
    return subprocess.run(
        [sys.executable, "-I", str(RUNNER), str(SCRIPT), *args],
        env=clean,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=15,
    )


@pytest.mark.parametrize("command,status", [("check-config", "VALID"), ("run-once", "NOOP")])
def test_installed_script(command, status):
    proc = invoke([command, "--config", str(ROOT / "config/paper.example.toml")])
    assert proc.returncode == 0, proc.stderr
    result, event = json.loads(proc.stdout), json.loads(proc.stderr)
    assert result["status"] == status and result["mode"] == "PAPER"
    assert result["run_id"] == event["run_id"]
    if command == "run-once":
        assert result["orders_sent"] == 0


@pytest.mark.parametrize(
    "env",
    [
        {"CRIPTO_BOT_MODE": "LIVE"},
        {"CRIPTO_BOT_MODE": "TESTNET"},
        {"BINANCE_API_SECRET": "SYNTHETIC_SECRET"},
        {"CRIPTO_BOT_UNKNOWN": ""},
    ],
)
def test_reject_environment(env):
    proc = invoke(["run-once"], env)
    assert proc.returncode == 2 and proc.stdout == ""
    assert "SYNTHETIC_SECRET" not in proc.stderr
    assert json.loads(proc.stderr)["event"] == "configuration_rejected"


@pytest.mark.parametrize(
    "content",
    ['api_key = "SYNTHETIC_SECRET"', 'mode = "SYNTHETIC_SECRET', '"SYNTHETIC_SECRET" = 1'],
)
def test_secret_file_not_echoed(tmp_path, content):
    path = tmp_path / "secret.toml"
    path.write_text(content)
    proc = invoke(["run-once", "--config", str(path)])
    assert proc.returncode == 2 and proc.stdout == ""
    assert "SYNTHETIC_SECRET" not in proc.stderr


def test_secret_argument_not_echoed():
    proc = invoke(["run-once", "--api-key", "SYNTHETIC_SECRET"])
    assert proc.returncode == 2 and proc.stdout == ""
    assert "SYNTHETIC_SECRET" not in proc.stderr


def test_help():
    proc = invoke(["--help"])
    assert proc.returncode == 0 and "PAPER" in proc.stdout


def test_child_guard():
    proc = subprocess.run(
        [sys.executable, "-I", str(RUNNER), "--probe"], text=True, capture_output=True, timeout=15
    )
    assert proc.returncode != 0
    assert "NETWORK_BLOCKED" in proc.stderr
