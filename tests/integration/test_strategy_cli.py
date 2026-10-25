import copy
import json
from pathlib import Path

import httpx
import pytest
from cripto_bot.adapters.binance.public import PublicSource
from cripto_bot.adapters.config import load_config
from cripto_bot.adapters.storage import strategy_report
from cripto_bot.adapters.storage.strategy_sqlite import StrategyStore

from tests.integration.test_paper_cli import invoke
from tests.strategy_factories import run_case, source, truncated, write_case


def test_default_noop_does_not_read_fixture_or_create_wallet(tmp_path):
    config = write_case(tmp_path, enabled=False)
    (tmp_path / "fixture.json").unlink()
    proc = invoke("strategy-run", config)
    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert result["status"] == "STRATEGY_NOOP" and result["simulated_fills"] == 0
    assert not (tmp_path / "wallet").exists()
    assert json.loads(proc.stderr)["run_id"] == result["run_id"]


def test_full_71_candles_roundtrip_report_reconcile_and_replay(tmp_path):
    config = write_case(tmp_path, compact=False)
    code, result = run_case(config, run_id="first")
    assert code == 0, result
    assert (
        result["evaluated_cycles"] == 71
        and result["total_warmup"] == 50
        and result["total_hold"] == 69
    )
    assert result["simulated_fills"] == 2 and result["total_rejected_intents"] == 0
    assert result["wallet"]["usdt_free"] == "9905.46181258"
    assert (
        result["wallet"]["realized"] == "-94.53818742" and result["wallet"]["fees"] == "1.90536908"
    )
    assert (
        result["wallet"]["btc_free"] == "0.00000000"
        and result["wallet"]["cost_basis"] == "0.00000000"
    )
    financial_hash = result["financial_sha256"]
    before = (tmp_path / "wallet/paper.sqlite3").read_bytes()
    code, reported = run_case(config, "strategy-report", "readonly")
    assert code == 0 and reported["financial_sha256"] == financial_hash
    assert (tmp_path / "wallet/paper.sqlite3").read_bytes() == before
    assert Path(reported["html_report"]).is_file()
    manifest = json.loads(Path(reported["manifest"]).read_text())
    import hashlib

    for name, digest in manifest["files"].items():
        assert (
            hashlib.sha256((Path(reported["manifest"]).parent / name).read_bytes()).hexdigest()
            == digest
        )
    for command, name in [("strategy-reconcile", "check"), ("strategy-run", "replay")]:
        code, again = run_case(config, command, name)
        assert (
            code == 0
            and again["simulated_fills"] == 0
            and again["financial_sha256"] == financial_hash
        )
    assert again["evaluated_cycles"] == 0 and again["replayed_actions"] == 71
    assert len(result["attributions"]) == 2
    for decision in result["decisions"]:
        assert (
            decision["instance_id"] == "trend_btc" and decision["dataset_id"] == "sma-crossings-v1"
        )
        assert decision["config_generation"] == 1 and decision["prefix_sha256"]
    for value in result["wallet"]["intents"].values():
        assert value["plan"]["quantity"] == "9.07275400"


def test_cli_outputs_are_json_and_network_flags_forbidden(tmp_path):
    config = write_case(tmp_path)
    proc = invoke("strategy-run", config)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["orders_sent"] == 0 and result["mode"] == "PAPER"
    assert json.loads(proc.stderr)["run_id"] == result["run_id"]
    denied = invoke("strategy-run", config, "--allow-network")
    assert denied.returncode == 2


def test_no_http_adapter_constructed_and_html_escapes_data(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("NETWORK_CONSTRUCTION")

    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(PublicSource, "__init__", forbidden)
    config = write_case(tmp_path)
    code, result = run_case(config)
    assert code == 0
    result["decisions"][0]["reason"] = '<script>alert("x")</script>'
    body = strategy_report.render_html(result)
    assert "<script>" not in body and "&lt;script&gt;" in body
    assert "Content-Security-Policy" in body and "http://" not in body


@pytest.mark.parametrize(
    "mutation",
    [
        "gap",
        "unordered",
        "duplicate",
        "future",
        "naive",
        "ohlc",
        "float",
        "bool",
        "unknown",
        "rules",
        "schema",
        "duplicate_quote",
        "wrong_symbol",
        "day",
        "oversized",
    ],
)
def test_invalid_fixture_has_no_financial_effect(tmp_path, mutation):
    data = source()
    if mutation == "gap":
        data["candles"].pop(1)
    elif mutation == "unordered":
        data["candles"].reverse()
    elif mutation == "duplicate":
        data["candles"].insert(1, copy.deepcopy(data["candles"][0]))
    elif mutation == "future":
        data["as_of"] = "2026-10-01T00:01:00Z"
    elif mutation == "naive":
        data["candles"][0]["available_at"] = "2026-10-01T00:15:00"
    elif mutation == "ohlc":
        data["candles"][0]["high"] = "1"
    elif mutation == "float":
        data["candles"][0]["close"] = 100.0
    elif mutation == "bool":
        data["candles"][0]["trades"] = True
    elif mutation == "unknown":
        data["candles"][0]["api_key"] = "SYNTHETIC_SECRET"
    elif mutation == "rules":
        data["rules"]["filters"][0]["filterType"] = "UNKNOWN_FILTER"
    elif mutation == "schema":
        data["schema_version"] = True
    elif mutation == "duplicate_quote":
        data["executions"][1]["observation"]["quote_id"] = data["executions"][0]["observation"][
            "quote_id"
        ]
    elif mutation == "wrong_symbol":
        data["executions"][0]["observation"]["symbol"] = "ETHUSDT"
    elif mutation == "day":
        data["day_starts"] = []
    config = write_case(tmp_path, data=data)
    if mutation == "oversized":
        (tmp_path / "fixture.json").write_text(" " * (4 * 1024 * 1024 + 1))
    with pytest.raises(ValueError):
        run_case(config)
    assert not (tmp_path / "wallet").exists()


@pytest.mark.parametrize("kind", ["absent", "stale", "predecision", "expired"])
def test_missing_or_unusable_quote_does_not_invent_fill_or_retry_cross(tmp_path, kind):
    data = source()
    if kind == "absent":
        data["executions"] = []
    elif kind == "stale":
        data["executions"][0]["fill_at"] = "2026-10-01T00:45:07Z"
    elif kind == "predecision":
        data["executions"][0]["observation"]["observed_at"] = "2026-10-01T00:44:59Z"
    else:
        data["executions"][0]["observation"]["observed_at"] = "2026-10-01T00:45:06Z"
    config = write_case(tmp_path, data=data)
    code, result = run_case(config)
    assert code == 0 and result["total_simulated_fills"] == 0
    decisions = result["decisions"]
    assert sum(d["target"] == "LONG" for d in decisions) == 1
    assert result["total_rejected_intents"] == 1
    assert result["wallet"]["usdt_free"] == "10000.00000000"


def test_prefix_append_preserves_past_identity_and_conflicting_history_is_rejected(tmp_path):
    original = source()
    config = write_case(tmp_path, data=truncated(original, 3))
    code, first = run_case(config, run_id="prefix")
    assert code == 0 and first["simulated_fills"] == 1
    original_decisions = copy.deepcopy(first["decisions"])
    (tmp_path / "fixture.json").write_text(json.dumps(original))
    code, full = run_case(config, run_id="extended")
    assert code == 0 and full["simulated_fills"] == 1
    assert full["decisions"][:3] == original_decisions
    original["candles"][0]["quote_volume"] = "999"
    (tmp_path / "fixture.json").write_text(json.dumps(original))
    code, conflict = run_case(config, run_id="conflict")
    assert code == 3 and conflict["error_code"] == "DATASET_PREFIX_CONFLICT"


def test_publication_failure_preserves_effects_and_report_reexports(tmp_path, monkeypatch):
    config = write_case(tmp_path)
    original = Path.rename

    def fail(self, *args, **kwargs):
        raise OSError("SYNTHETIC_SECRET")

    with monkeypatch.context() as m:
        m.setattr(Path, "rename", fail)
        code, result = run_case(config)
    assert code == 3 and result["total_simulated_fills"] == 2
    assert "SYNTHETIC_SECRET" not in str(result.get("error_code", ""))
    assert Path.rename is original
    code, result = run_case(config, "strategy-report", "reexport")
    assert code == 0 and result["simulated_fills"] == 0
    assert Path(result["html_report"]).exists()
    assert not list((tmp_path / "wallet/reports").glob(".pending-*"))


def test_lower_risk_limits_latch_but_allow_reduction(tmp_path):
    config = write_case(tmp_path)
    config.write_text(
        config.read_text() + '\n[modules.trend_btc.limits]\nmax_daily_loss_fraction="0.001"\n'
        'max_drawdown_fraction="0.001"\n'
    )
    code, result = run_case(config)
    assert code == 0 and result["simulated_fills"] == 2
    assert result["wallet"]["daily_blocked"] and result["wallet"]["drawdown_blocked"]
    assert result["wallet"]["btc_free"] == "0.00000000"
    assert result["decisions"][2]["risk_limits"]["max_drawdown_fraction"] == "0.001"


def test_profitable_synthetic_case_is_valid_without_becoming_gate_for_profit(tmp_path):
    data = source(prices=(100, 110))
    config = write_case(tmp_path, data=data)
    code, result = run_case(config)
    assert code == 0 and result["simulated_fills"] == 2
    from decimal import Decimal

    assert Decimal(result["wallet"]["realized"]) > 0


def test_report_does_not_initialize_missing_wallet(tmp_path):
    config = write_case(tmp_path)
    code, result = run_case(config, "strategy-report")
    assert code == 3 and result["error_code"] == "WALLET_NOT_FOUND"
    assert not (tmp_path / "wallet").exists()


def test_symlink_fixture_and_report_destinations_cannot_escape(tmp_path):
    config = write_case(tmp_path)
    (tmp_path / "fixture.json").unlink()
    (tmp_path / "fixture.json").symlink_to("/etc/hosts")
    with pytest.raises(ValueError):
        load_config(config, {})
    (tmp_path / "fixture.json").unlink()
    (tmp_path / "fixture.json").write_text(json.dumps(source()))
    (tmp_path / "wallet").mkdir()
    (tmp_path / "wallet/reports").symlink_to("/tmp")
    code, result = run_case(config)
    assert code == 3 and result["error_code"] == "UNSAFE_REPORT_PATH"


def test_atomic_reload_disable_drain_and_reactivation_without_old_signals(tmp_path):
    original = source()
    config = write_case(tmp_path, data=truncated(original, 3))
    code, bought = run_case(config, run_id="buy")
    assert code == 0 and bought["simulated_fills"] == 1
    active_text = config.read_text()
    config.write_text(
        active_text.replace("fast_window = 1", "fast_window = 2").replace(
            "slow_window = 2", "slow_window = 3"
        )
    )
    code, rejected = run_case(config, run_id="invalidchange")
    assert code == 3 and rejected["error_code"] == "DRAIN_REQUIRED"
    assert rejected["config_generation"] == 1
    config.write_text(active_text.replace("enabled = true", "enabled = false"))
    (tmp_path / "fixture.json").write_text(json.dumps(original))
    code, disabled = run_case(config, run_id="disabled")
    assert code == 0 and disabled["simulated_fills"] == 0
    assert disabled["instances"]["trend_btc"]["status"] == "DRAINING"
    assert disabled["wallet"]["btc_free"] == bought["wallet"]["btc_free"]
    assert disabled["wallet"]["fees"] == bought["wallet"]["fees"]
    assert disabled["decisions"][-1]["reason"] == "DISABLED"
    config.write_text(active_text)
    code, denied = run_case(config, run_id="reactivate-position")
    assert code == 3 and denied["error_code"] == "DRAIN_REQUIRED"


def test_in_cash_generation_changes_do_not_replay_confirmed_candles(tmp_path):
    config = write_case(tmp_path)
    code, first = run_case(config, run_id="first")
    assert code == 0
    config.write_text(config.read_text().replace("enabled = true", "enabled = false"))
    code, disabled = run_case(config, run_id="disable")
    assert code == 0 and disabled["config_generation"] == 2 and disabled["simulated_fills"] == 0
    config.write_text(config.read_text().replace("enabled = false", "enabled = true"))
    code, active = run_case(config, run_id="reactivate")
    assert code == 0 and active["config_generation"] == 3 and active["evaluated_cycles"] == 0
    assert active["wallet"]["fees"] == first["wallet"]["fees"]
    assert active["wallet"]["baseline"] == first["wallet"]["baseline"]
    assert active["instances"]["trend_btc"]["epoch"] == 2


def test_confirmed_execution_quote_conflict_is_rejected_before_generation_change(tmp_path):
    config = write_case(tmp_path)
    assert run_case(config)[0] == 0
    data = json.loads((tmp_path / "fixture.json").read_text())
    data["executions"][0]["observation"]["price"] = "109"
    (tmp_path / "fixture.json").write_text(json.dumps(data))
    config.write_text(config.read_text().replace("enabled = true", "enabled = false"))
    code, result = run_case(config, run_id="changedquote")
    assert code == 3 and result["error_code"] == "DATASET_PREFIX_CONFLICT"
    assert result["config_generation"] == 1


def test_disabled_evaluation_and_module_failure_have_no_financial_effect(tmp_path, monkeypatch):
    from io import StringIO

    from cripto_bot.domain.strategy.sma import TrendSMA
    from cripto_bot.entrypoints.cli import main

    config = write_case(tmp_path, enabled=False)

    def fail(*args, **kwargs):
        raise RuntimeError("SYNTHETIC_SECRET")

    with monkeypatch.context() as m:
        m.setattr(TrendSMA, "evaluate", fail)
        assert run_case(config)[1]["status"] == "STRATEGY_NOOP"
        config.write_text(config.read_text().replace("enabled = false", "enabled = true"))
        out, err = StringIO(), StringIO()
        code = main(["strategy-run", "--config", str(config)], environ={}, stdout=out, stderr=err)
        assert code == 1 and not out.getvalue() and "SYNTHETIC_SECRET" not in err.getvalue()
    with StrategyStore(tmp_path / "wallet") as store:
        state = store.replay()
        assert not state.intents and not state.cycles
    assert run_case(config, run_id="afterfailure")[0] == 0


def test_pending_local_cancel_releases_once_and_prevents_later_fill(tmp_path, monkeypatch):
    from cripto_bot.domain.portfolio.models import PaperError

    config = write_case(tmp_path)
    original = StrategyStore.append

    def stop(self, key, kind, payload, wallet):
        if kind == "FILL":
            raise PaperError("INJECTED_INTERRUPTION")
        return original(self, key, kind, payload, wallet)

    with monkeypatch.context() as m:
        m.setattr(StrategyStore, "append", stop)
        code, result = run_case(config)
    assert code == 3 and result["wallet"]["usdt_reserved"] == "999.99994389"
    with StrategyStore(tmp_path / "wallet") as store:
        state = store.replay()
        action = next(iter(state.intents))
        plan = state.intents[action].plan
        canceled = store.append("cancel", "CANCEL", {"action_key": action}, state)
        assert canceled.usdt_reserved == 0 and str(canceled.usdt_free) == "10000.00000000"
        with pytest.raises(PaperError, match="INTENT_NOT_RESERVED"):
            store.append(
                "forbiddenfill", "FILL", {"action_key": action, "fill_id": plan.fill_id}, canceled
            )
        assert store.replay().data() == canceled.data()


def test_symbol_and_instance_limits_combine_conservatively(tmp_path):
    config = write_case(tmp_path)
    config.write_text(
        config.read_text()
        + '\n[modules.trend_btc.limits]\nmax_entry_fraction="0.04"\n'
        + '\n[coordination.symbol_limits.BTCUSDT]\nmax_entry_fraction="0.02"\n'
    )
    code, result = run_case(config)
    assert code == 0
    assert result["decisions"][2]["risk_limits"]["max_entry_fraction"] == "0.02"
    from decimal import Decimal

    assert (
        Decimal(
            result["wallet"]["intents"][result["decisions"][2]["decision_id"]]["plan"]["notional"]
        )
        <= 200
    )


def test_installation_without_configuration_is_noop():
    from io import StringIO

    from cripto_bot.entrypoints.cli import main

    output = StringIO()
    assert main(["strategy-run"], environ={}, stdout=output, stderr=StringIO()) == 0
    assert json.loads(output.getvalue())["status"] == "STRATEGY_NOOP"


def test_midnight_close_initializes_new_utc_day_before_admission(tmp_path):
    from cripto_bot.domain.market import iso
    from cripto_bot.domain.portfolio.models import instant

    data = source()
    shift = 23 * 3600000 + 15 * 60000
    for candle in data["candles"]:
        candle["open_time"] += shift
        candle["close_time"] += shift
        candle["available_at"] = iso(instant(candle["available_at"]) + shift)
    for execution in data["executions"]:
        execution["candle_open"] += shift
        execution["fill_at"] = iso(instant(execution["fill_at"]) + shift)
        quote = execution["observation"]
        quote["observed_at"] = iso(instant(quote["observed_at"]) + shift)
    data["as_of"] = "2026-10-02T01:00:00Z"
    data["day_starts"].append(
        {
            "quote_id": "day-2",
            "kind": "REFERENCE",
            "symbol": "BTCUSDT",
            "observed_at": "2026-10-02T00:00:00Z",
            "price": "110",
        }
    )
    config = write_case(tmp_path, data=data)
    code, result = run_case(config)
    assert code == 0 and result["simulated_fills"] == 2
    assert result["wallet"]["day"] == "2026-10-02"
    assert result["wallet"]["baseline"] == "10000.00000000"
