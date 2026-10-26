"""Console script exclusivamente PAPER e offline."""

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Never, TextIO
from uuid import uuid4

from cripto_bot.adapters.config import ConfigError, load_config
from cripto_bot.application.run_once import run_once
from cripto_bot.entrypoints.backtest import execute_backtest
from cripto_bot.entrypoints.continuous import execute_continuous
from cripto_bot.entrypoints.market import execute_collection
from cripto_bot.entrypoints.paper import execute_paper
from cripto_bot.entrypoints.strategy import execute_strategy
from cripto_bot.observability.logging import emit, utc_now, utc_timestamp


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        # argparse pode incluir argumentos/valores secretos na mensagem original.
        raise ConfigError("argumentos invalidos")


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    clock: Callable[[], datetime] = utc_now,
    new_id: Callable[[], str] = lambda: str(uuid4()),
) -> int:
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    env = os.environ if environ is None else environ
    run_id = new_id()
    try:
        timestamp = utc_timestamp(clock())
        parser = SafeParser(prog="cripto-bot", description="Base PAPER offline")
        sub = parser.add_subparsers(dest="command", required=True)
        for name in (
            "check-config",
            "run-once",
            "collect-market",
            "paper-run",
            "paper-report",
            "paper-reconcile",
            "strategy-run",
            "strategy-report",
            "strategy-reconcile",
            "backtest-prepare",
            "backtest-freeze",
            "backtest-run",
            "backtest-report",
            "backtest-reconcile",
            "continuous-prepare",
            "continuous-run",
            "continuous-reconcile",
            "continuous-report",
            "continuous-control",
            "notify-drain",
        ):
            command = sub.add_parser(name)
            command.add_argument("--config", type=Path)
            if name in {"continuous-run", "notify-drain"}:
                command.add_argument("--once", action="store_true")
            if name == "continuous-run":
                command.add_argument("--allow-network", action="store_true")
            if name == "notify-drain":
                command.add_argument("--allow-send", action="store_true")
            if name == "continuous-report":
                command.add_argument("--day")
            if name == "continuous-control":
                command.add_argument(
                    "--action",
                    choices=("pause", "resume", "kill", "disable", "reload", "cancel"),
                    required=True,
                )
                command.add_argument("--reason", required=True)
                command.add_argument("--order-id")
                command.add_argument("--request-id")
            if name == "backtest-run":
                command.add_argument(
                    "--segment",
                    choices=("development", "validation", "test"),
                    default="development",
                )
                command.add_argument("--variant", choices=("all",), default="all")
            if name == "collect-market":
                command.add_argument("--start", required=True)
                command.add_argument("--end", required=True)
                command.add_argument("--allow-network", action="store_true")
        args = parser.parse_args(argv)
        settings = load_config(args.config, env)
        common: dict[str, object] = {
            "run_id": run_id,
            "timestamp": timestamp,
            "mode": settings.mode,
        }
        code = 0
        if args.command == "check-config":
            result = {**common, "status": "VALID"}
        elif args.command == "run-once":
            result = {**common, **asdict(run_once(settings))}
        elif args.command.startswith("continuous-") or args.command == "notify-drain":
            code, continuous = execute_continuous(
                settings,
                args.command,
                run_id,
                allow_network=getattr(args, "allow_network", False),
                once=getattr(args, "once", False),
                allow_send=getattr(args, "allow_send", False),
                action=getattr(args, "action", None),
                reason=getattr(args, "reason", None),
                order_id=getattr(args, "order_id", None),
                request_id=getattr(args, "request_id", None),
                day=getattr(args, "day", None),
            )
            result = {**common, **continuous}
        elif args.command.startswith("backtest-"):
            code, historical = execute_backtest(
                settings, args.command, run_id, getattr(args, "segment", "development")
            )
            result = {**common, **historical}
        elif args.command.startswith("strategy-"):
            code, strategy = execute_strategy(settings, args.command, run_id)
            result = {**common, **strategy}
        elif args.command.startswith("paper-"):
            code, paper = execute_paper(settings, args.command, run_id)
            result = {**common, **paper}
        else:
            code, collected = execute_collection(
                settings, args.start, args.end, args.allow_network, run_id
            )
            result = {**common, **collected}
        emit(err, {**common, "level": "INFO" if code == 0 else "ERROR", "event": args.command})
        emit(out, result)
        return code
    except ConfigError:
        emit(
            err,
            {
                "timestamp": utc_timestamp(clock()),
                "level": "ERROR",
                "event": "configuration_rejected",
                "run_id": run_id,
                "error_code": "INVALID_CONFIG",
            },
        )
        return 2
    except Exception:
        # Falhas inesperadas nao se tornam NOOP e nao vazam o input original.
        emit(
            err,
            {
                "timestamp": utc_timestamp(utc_now()),
                "level": "ERROR",
                "event": "internal_failure",
                "run_id": run_id,
                "error_code": "INTERNAL_ERROR",
            },
        )
        return 1
