"""Validacao do wheel instalado; executar com Python do ambiente principal."""

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv-wheel/bin/python"
SCRIPT = ROOT / ".venv-wheel/bin/cripto-bot"
RUNNER = ROOT / "tests/offline_runner.py"
NEUTRAL = ROOT / ".wheel-smoke"
NEUTRAL.mkdir(exist_ok=True)
env = {
    key: value
    for key, value in os.environ.items()
    if not key.startswith("CRIPTO_BOT_")
    and key not in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "PYTHONPATH")
}
(wheel,) = (ROOT / "dist").glob("*.whl")
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
    f'fixture_file="{ROOT / "config/fixtures/market-sample.json"}"\n'
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
            "wheel": wheel.name,
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "origin": origin.stdout.strip(),
            "checks": checks,
            "live_exit_code": proc.returncode,
            "socket_probe_blocked": True,
        },
        indent=2,
    )
)
