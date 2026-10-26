"""Contratos publicos: NOOP, codigos de erro e publicacao automatica sem rede."""

import json
from dataclasses import replace
from io import StringIO

from cripto_bot.adapters.backtest_dataset import file_hash
from cripto_bot.adapters.binance.public import PublicSource
from cripto_bot.domain.models import Settings
from cripto_bot.entrypoints.backtest import execute_backtest
from cripto_bot.entrypoints.cli import main

from tests.unit.test_backtest import config as config


def test_disabled_commands_do_not_read_source_or_create_wallet(config, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP client must not be constructed")

    monkeypatch.setattr(PublicSource, "__init__", forbidden)
    disabled = replace(config, modules=tuple(replace(m, enabled=False) for m in config.modules))
    config.source_manifests[0].unlink()
    code, result = execute_backtest(Settings(backtest=disabled), "backtest-run", "disabled")
    assert code == 0 and result["status"] == "BACKTEST_NOOP" and result["orders_sent"] == 0
    assert not config.data_dir.exists()
    assert execute_backtest(Settings(), "backtest-run", "absent")[1]["status"] == "BACKTEST_NOOP"
    assert execute_backtest(Settings(), "backtest-prepare", "absent")[0] == 2


def test_missing_store_and_invalid_dataset_have_distinct_exit_codes(config):
    settings = Settings(backtest=config)
    code, result = execute_backtest(settings, "backtest-reconcile", "missing")
    assert code == 3 and result["error_code"] == "BACKTEST_NOT_FOUND"
    path = config.source_manifests[0].parent / "raw.jsonl.gz"
    path.write_bytes(b"changed")
    code, result = execute_backtest(settings, "backtest-prepare", "bad-data")
    assert code == 2 and result["simulated_fills"] == 0 and not config.data_dir.exists()


def test_command_flow_publishes_report_and_replays_without_effect(config, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected public transport")

    monkeypatch.setattr(PublicSource, "__init__", forbidden)
    settings = Settings(backtest=config)
    for command in ("backtest-prepare", "backtest-freeze"):
        code, result = execute_backtest(settings, command, command)
        assert code == 0, result
    code, result = execute_backtest(settings, "backtest-run", "premature", "test")
    assert code == 3 and result["error_code"] == "PREVIOUS_SEGMENT_REQUIRED"
    code, first = execute_backtest(settings, "backtest-run", "development", "development")
    assert code == 0, first
    assert first["simulated_fills"] == 56 and first["reproduction"] is False
    from pathlib import Path

    assert Path(first["html_report"]).is_file()
    digest = file_hash(config.data_dir / "backtest.sqlite3")
    for command in ("backtest-report", "backtest-reconcile"):
        code, result = execute_backtest(settings, command, command)
        assert code == 0, result
        assert file_hash(config.data_dir / "backtest.sqlite3") == digest
    code, again = execute_backtest(settings, "backtest-run", "reproduction", "development")
    assert code == 0 and again["simulated_fills"] == 0 and again["reproduction"] is True
    assert again["result_sha256"] == first["result_sha256"]
    assert file_hash(config.data_dir / "backtest.sqlite3") == digest
    changed = replace(config, experiment_id="other")
    code, result = execute_backtest(Settings(backtest=changed), "backtest-freeze", "conflict")
    assert code == 3 and result["error_code"] == "PROTOCOL_CONFLICT"


def test_cli_json_and_forbidden_network_argument(config):
    out, err = StringIO(), StringIO()
    assert main(["backtest-run"], environ={}, stdout=out, stderr=err) == 0
    result = json.loads(out.getvalue())
    log = json.loads(err.getvalue())
    assert result["status"] == "BACKTEST_NOOP" and result["orders_sent"] == 0
    assert result["run_id"] == log["run_id"] and result["mode"] == "PAPER"
    out, err = StringIO(), StringIO()
    assert main(["backtest-run", "--allow-network"], environ={}, stdout=out, stderr=err) == 2
    assert out.getvalue() == "" and json.loads(err.getvalue())["error_code"] == "INVALID_CONFIG"
