import json
import socket
from datetime import UTC, datetime
from io import StringIO

import pytest
from cripto_bot.entrypoints import cli


def invoke(args, env=None):
    out, err = StringIO(), StringIO()
    code = cli.main(
        args,
        environ={} if env is None else env,
        stdout=out,
        stderr=err,
        clock=lambda: datetime(2026, 10, 24, tzinfo=UTC),
        new_id=lambda: "fixed-id",
    )
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("command,status", [("check-config", "VALID"), ("run-once", "NOOP")])
def test_success_and_correlation(command, status, monkeypatch):
    if command == "check-config":

        def forbidden(*args):
            pytest.fail("check-config nao deve chamar ciclo")

        monkeypatch.setattr(cli, "run_once", forbidden)
    code, out, err = invoke([command])
    result, event = json.loads(out), json.loads(err)
    assert code == 0
    assert result["status"] == status
    assert result["mode"] == event["mode"] == "PAPER"
    assert result["run_id"] == event["run_id"] == "fixed-id"
    assert result["timestamp"] == event["timestamp"] == "2026-10-24T00:00:00+00:00"
    if command == "run-once":
        assert result["orders_sent"] == 0


@pytest.mark.parametrize(
    "env", [{"CRIPTO_BOT_MODE": "LIVE"}, {"BINANCE_API_KEY": "SYNTHETIC_SECRET"}]
)
def test_rejected_before_cycle(env, monkeypatch):
    def forbidden(*args):
        pytest.fail("configuracao invalida alcancou ciclo")

    monkeypatch.setattr(cli, "run_once", forbidden)
    code, out, err = invoke(["run-once"], env)
    assert code == 2 and out == ""
    assert "mode" not in json.loads(err)
    assert "SYNTHETIC_SECRET" not in err


def test_internal_failure_sanitized(monkeypatch):
    def broken(*args):
        raise RuntimeError("SYNTHETIC_SECRET")

    monkeypatch.setattr(cli, "run_once", broken)
    code, out, err = invoke(["run-once"])
    assert code == 1 and out == ""
    assert json.loads(err)["error_code"] == "INTERNAL_ERROR"
    assert "SYNTHETIC_SECRET" not in err


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["SYNTHETIC_SECRET"],
        ["run-once", "--api-key", "SYNTHETIC_SECRET"],
        ["run-once", "--config"],
    ],
)
def test_argument_errors_do_not_echo(args):
    code, out, err = invoke(args)
    assert code == 2 and out == ""
    assert "SYNTHETIC_SECRET" not in err


def test_network_guard():
    with pytest.raises(RuntimeError, match="NETWORK_BLOCKED"):
        socket.socket()
