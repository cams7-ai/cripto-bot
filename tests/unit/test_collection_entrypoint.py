import json
from dataclasses import replace
from io import StringIO
from pathlib import Path

import httpx
import pytest

from cripto_bot.adapters.binance.public import PublicSource
from cripto_bot.adapters.config import ConfigError
from cripto_bot.adapters.storage.market_sqlite import SQLiteMarketStore
from cripto_bot.domain.market import STEP
from cripto_bot.domain.models import MarketSettings, Settings
from cripto_bot.entrypoints import cli, market
from tests.factories import BASE, ROOT, fixture_file, sample


@pytest.fixture
def settings(tmp_path):
    return Settings(
        market=MarketSettings(
            source="fixture",
            root=ROOT,
            data_dir=tmp_path / "db",
            fixture_file=fixture_file(tmp_path / "fixture.json"),
        )
    )


def execute(settings, run_id="run1"):
    return market.execute_collection(
        settings, "2026-10-01T00:00:00Z", "2026-10-01T00:30:00Z", False, run_id
    )


def test_full_flow_repeat_and_config_hash(settings):
    code, first = execute(settings)
    assert code == 0 and first["inserted"] == 2
    code, second = execute(settings, "run2")
    assert code == 0 and second["inserted"] == 0
    a = json.loads(Path(first["manifest"]).read_text())
    b = json.loads(Path(second["manifest"]).read_text())
    assert a["data_sha256"] == b["data_sha256"] and a["config_sha256"] == b["config_sha256"]
    assert a["fixture_sha256"] in first["dataset"]
    assert a["code_revision"]


def test_opt_in_before_any_effect(settings, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("adapter construido antes do opt-in")

    monkeypatch.setattr(market, "PublicSource", forbidden)
    monkeypatch.setattr(market, "SQLiteMarketStore", forbidden)
    public = replace(
        settings, market=replace(settings.market, source="binance_public", fixture_file=None)
    )
    with pytest.raises(ConfigError):
        execute(public)
    with pytest.raises(ConfigError):
        market.execute_collection(settings, "bad", "bad", True, "run")
    with pytest.raises(ConfigError):
        execute(Settings())
    with pytest.raises(ConfigError):
        market.execute_collection(settings, "bad", "bad", False, "run")


def test_export_failure_recovery_and_no_secret(settings, monkeypatch):
    def failed(*args, **kwargs):
        raise OSError("SYNTHETIC_SECRET")

    with monkeypatch.context() as context:
        context.setattr(market, "export_market", failed)
        code, result = execute(settings)
        assert code == 3 and result["next_open_time"] == BASE + 2 * STEP
        assert "SYNTHETIC_SECRET" not in json.dumps(result)
    code, result = execute(settings, "recovered")
    assert code == 0 and result["inserted"] == 0


@pytest.mark.parametrize("failure", ["missing", "invalid", "too_large", "shape"])
def test_fixture_errors(settings, failure):
    path = settings.market.fixture_file
    if failure == "missing":
        path.unlink()
    elif failure == "invalid":
        path.write_bytes(b"NaN")
    elif failure == "too_large":
        path.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    else:
        path.write_text("{}")
    code, result = execute(settings)
    assert code == 3 and result["complete"] is False
    assert not settings.market.data_dir.exists()


def test_public_adapter_mock_and_persisted_ban(settings, monkeypatch):
    public = replace(
        settings, market=replace(settings.market, source="binance_public", fixture_file=None)
    )
    data = sample()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(418, headers={"Retry-After": "60"})

    def factory(**kwargs):
        return PublicSource(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(market, "PublicSource", factory)

    def invoke(run_id):
        return market.execute_collection(
            public, "2026-10-01T00:00:00Z", "2026-10-01T00:30:00Z", True, run_id
        )

    code, result = invoke("banned")
    assert code == 3 and result["error_code"] == "IP_BANNED"
    with SQLiteMarketStore(settings.market.data_dir) as store:
        assert store.not_before() > 0
    code, result = invoke("blocked")
    assert code == 3 and result["error_code"] == "SOURCE_BLOCKED" and len(calls) == 1
    # Recuperacao apos desbloqueio: o cliente publico ainda usa transporte em memoria.
    with SQLiteMarketStore(settings.market.data_dir) as store:
        store.db.execute("DELETE FROM restrictions")

    def success(request):
        key = {
            "/api/v3/time": "server_time",
            "/api/v3/exchangeInfo": "exchange_info",
            "/api/v3/ticker/bookTicker": "book_ticker",
            "/api/v3/klines": "klines",
        }[request.url.path]
        return httpx.Response(200, json=data[key])

    monkeypatch.setattr(
        market,
        "PublicSource",
        lambda **kwargs: PublicSource(**kwargs, transport=httpx.MockTransport(success)),
    )
    assert invoke("allowed")[0] == 0


def test_cli_json_output_and_noop_with_market(settings, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        f'mode="PAPER"\n[market]\nsource="fixture"\nfixture_file="{settings.market.fixture_file}"\ndata_dir="{settings.market.data_dir}"'
    )
    out, err = StringIO(), StringIO()
    assert (
        cli.main(
            [
                "collect-market",
                "--config",
                str(path),
                "--start",
                "2026-10-01T00:00:00Z",
                "--end",
                "2026-10-01T00:30:00Z",
            ],
            environ={},
            stdout=out,
            stderr=err,
        )
        == 0
    )
    result = json.loads(out.getvalue())
    assert result["orders_sent"] == 0 and result["mode"] == "PAPER"
    assert json.loads(err.getvalue())["run_id"] == result["run_id"]
    out = StringIO()
    assert (
        cli.main(["run-once", "--config", str(path)], environ={}, stdout=out, stderr=StringIO())
        == 0
    )
    assert json.loads(out.getvalue())["status"] == "NOOP"


def test_storage_failure_in_cli_is_sanitized(settings, tmp_path):
    settings.market.data_dir.mkdir()
    (settings.market.data_dir / "market.sqlite3").write_bytes(b"corrupt")
    code, result = execute(settings)
    assert code == 3 and result["error_code"] == "STORAGE_FAILURE"
