import json
import os
import subprocess
import sys
from copy import deepcopy
from io import StringIO
from pathlib import Path

import httpx
import pytest
from cripto_bot.adapters.binance.public import PublicSource
from cripto_bot.adapters.config import load_config
from cripto_bot.adapters.storage.paper_sqlite import PaperStore
from cripto_bot.entrypoints import cli, paper

from tests.factories import ROOT
from tests.paper_factories import scenario, write_case


def invoke(command, config, *extra):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CRIPTO_BOT_")
        and key not in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "PYTHONPATH")
    }
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(ROOT / "tests/offline_runner.py"),
            str(ROOT / ".venv/bin/cripto-bot"),
            command,
            "--config",
            str(config),
            *extra,
        ],
        env=env,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )


def test_cli_roundtrip_report_reconcile_and_replay(tmp_path):
    config = write_case(tmp_path)
    result = invoke("paper-run", config)
    assert result.returncode == 0, result.stderr + result.stdout
    first = json.loads(result.stdout)
    assert first["wallet"]["usdt_free"] == "10009.58001000"
    assert first["mode"] == "PAPER" and first["orders_sent"] == 0 and first["simulated_fills"] == 2
    assert json.loads(result.stderr)["run_id"] == first["run_id"]
    for command in ("paper-report", "paper-reconcile", "paper-run"):
        proc = invoke(command, config)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        data = json.loads(proc.stdout)
        assert data["financial_sha256"] == first["financial_sha256"]
        assert data["simulated_fills"] == 0
    forbidden = invoke("paper-run", config, "--allow-network")
    assert forbidden.returncode == 2


def test_no_network_adapter_constructed_even_with_market_config(tmp_path, monkeypatch):
    config = write_case(tmp_path)
    config.write_text(
        config.read_text() + '\n[market]\nsource="binance_public"\ndata_dir="data/public-unused"'
    )

    def forbidden(*args, **kwargs):
        pytest.fail("cliente de rede construido no fluxo financeiro")

    monkeypatch.setattr(PublicSource, "__init__", forbidden)
    monkeypatch.setattr(httpx.Client, "__init__", forbidden)
    out, err = StringIO(), StringIO()
    assert cli.main(["paper-run", "--config", str(config)], environ={}, stdout=out, stderr=err) == 0
    assert json.loads(out.getvalue())["reconciled"]


def test_report_does_not_create_wallet_and_config_errors_are_sanitized(tmp_path):
    config = write_case(tmp_path)
    result = invoke("paper-report", config)
    assert result.returncode == 3 and not (tmp_path / "data").exists()
    (tmp_path / "scenario.json").write_text('{"api_key":"SYNTHETIC_SECRET"}')
    result = invoke("paper-run", config)
    assert result.returncode == 2
    assert "SYNTHETIC_SECRET" not in result.stdout + result.stderr
    assert not (tmp_path / "data").exists()
    out = StringIO()
    assert cli.main(["paper-run"], environ={}, stdout=out, stderr=StringIO()) == 2


def test_export_failure_preserves_fills_and_reexports(tmp_path, monkeypatch):
    config = write_case(tmp_path)
    settings = load_config(config, {})

    def failed(*args, **kwargs):
        raise OSError("SYNTHETIC_SECRET")

    with monkeypatch.context() as context:
        context.setattr(paper, "export_report", failed)
        code, result = paper.execute_paper(settings, "paper-run", "failed")
        assert code == 3 and result["total_simulated_fills"] == 2 and result["simulated_fills"] == 2
        assert "SYNTHETIC_SECRET" not in result.get("error_code", "")
    code, result = paper.execute_paper(settings, "paper-run", "reexport")
    assert code == 0 and result["simulated_fills"] == 0
    assert Path(result["manifest"]).exists()


def test_report_of_divergent_projection_is_readonly_and_reconcile_blocks(tmp_path):
    config = write_case(tmp_path)
    settings = load_config(config, {})
    assert paper.execute_paper(settings, "paper-run", "first")[0] == 0
    with PaperStore(tmp_path / "data") as store:
        store.db.execute("UPDATE projection SET data='{}'")
    before = (tmp_path / "data/paper.sqlite3").read_bytes()
    code, report = paper.execute_paper(settings, "paper-report", "read")
    assert code == 3 and report["blocked"] and not report["reconciled"]
    assert report["wallet"]["usdt_free"] == "10009.58001000"
    assert (tmp_path / "data/paper.sqlite3").read_bytes() == before
    code, result = paper.execute_paper(settings, "paper-reconcile", "audit")
    assert code == 3 and result["blocked"]
    with PaperStore(tmp_path / "data") as store:
        assert store.blocked()


def test_scenario_and_configuration_conflict_do_not_create_another_wallet(tmp_path):
    config = write_case(tmp_path)
    settings = load_config(config, {})
    assert paper.execute_paper(settings, "paper-run", "first")[0] == 0
    path = tmp_path / "scenario.json"
    changed = json.loads(path.read_text())
    changed["events"][1]["quantity"] = "2"
    path.write_text(json.dumps(changed))
    code, result = paper.execute_paper(settings, "paper-run", "conflict")
    assert code == 3 and result["error_code"] == "WALLET_CONTEXT_CONFLICT"
    assert result["total_simulated_fills"] == 2


def test_risk_rejection_is_not_a_fill(tmp_path):
    source = deepcopy(scenario())
    source["events"] = source["events"][:2]
    source["events"][1]["quantity"] = "100"
    config = write_case(tmp_path, source)
    code, result = paper.execute_paper(load_config(config, {}), "paper-run", "rejected")
    assert code == 0 and result["simulated_fills"] == 0 and result["rejected_intents"] == 1
    assert result["wallet"]["usdt_free"] == "10000.00000000"
    assert next(iter(result["wallet"]["intents"].values()))["state"] == "RISK_REJECTED"


def test_risk_latches_persist_through_recovery_and_new_day(tmp_path):
    source = scenario()
    source["events"][1]["quantity"] = "9"
    source["events"][2]["quantity"] = "9"
    marks = []
    for name, stamp, price in [("loss", "00:20:00", "40"), ("recovery", "00:25:00", "150")]:
        event = deepcopy(source["events"][0])
        event.update(event_id=name, type="MARK", timestamp="2026-10-01T" + stamp + "Z")
        event["observation"].update(quote_id=name, observed_at=event["timestamp"], price=price)
        marks.append(event)
    source["events"][2:2] = marks
    next_day = deepcopy(source["events"][0])
    next_day.update(event_id="day2", timestamp="2026-10-02T00:00:00Z")
    next_day["observation"].update(quote_id="day2", observed_at=next_day["timestamp"], price="110")
    buy = deepcopy(source["events"][1])
    buy.update(
        event_id="blocked-buy",
        action_id="blocked-buy",
        quantity="1",
        timestamp="2026-10-02T00:15:01Z",
        decision_at="2026-10-02T00:15:00Z",
        fill_at="2026-10-02T00:15:01.001Z",
    )
    buy["observation"].update(quote_id="blocked-buy", observed_at=buy["timestamp"])
    source["events"].extend([next_day, buy])
    config = write_case(tmp_path, source)
    settings = load_config(config, {})
    code, first = paper.execute_paper(settings, "paper-run", "risk")
    assert code == 0 and first["simulated_fills"] == 2 and first["rejected_intents"] == 1
    assert first["wallet"]["drawdown_blocked"] is True and first["wallet"]["daily_blocked"] is False
    assert first["wallet"]["peak"] == "10448.19910000"
    assert first["wallet"]["btc_free"] == "0.00000000"
    with PaperStore(tmp_path / "data") as store:
        from cripto_bot.application.paper_reconcile import reconcile

        assert reconcile(store).drawdown_blocked
    code, repeated = paper.execute_paper(settings, "paper-run", "repeat-risk")
    assert code == 0 and repeated["simulated_fills"] == 0
    assert repeated["financial_sha256"] == first["financial_sha256"]
