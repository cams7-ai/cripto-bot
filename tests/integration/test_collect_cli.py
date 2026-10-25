import json
import os
import subprocess
import sys

from tests.factories import ROOT, fixture_file


def invoke(path, run_args):
    clean = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CRIPTO_BOT_")
        and k not in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "PYTHONPATH")
    }
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(ROOT / "tests/offline_runner.py"),
            str(ROOT / ".venv/bin/cripto-bot"),
            "collect-market",
            "--config",
            str(path),
            "--start",
            "2026-10-01T00:00:00Z",
            "--end",
            "2026-10-01T00:30:00Z",
            *run_args,
        ],
        cwd=ROOT,
        env=clean,
        text=True,
        capture_output=True,
        timeout=15,
    )


def test_offline_cli_repeated_and_opt_in_rejected(tmp_path):
    fixture = fixture_file(tmp_path / "fixture.json")
    config = tmp_path / "config.toml"
    config.write_text(
        f'mode="PAPER"\n[market]\nsource="fixture"\n'
        f'fixture_file="{fixture}"\ndata_dir="{tmp_path / "data"}"'
    )
    first = invoke(config, [])
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["inserted"] == 2
    second = invoke(config, [])
    assert second.returncode == 0 and json.loads(second.stdout)["inserted"] == 0
    rejected = invoke(config, ["--allow-network"])
    assert rejected.returncode == 2


def test_public_without_opt_in_creates_nothing(tmp_path):
    config = tmp_path / "config.toml"
    directory = tmp_path / "data"
    config.write_text(f'[market]\nsource="binance_public"\ndata_dir="{directory}"')
    result = invoke(config, [])
    assert result.returncode == 2 and not directory.exists()
