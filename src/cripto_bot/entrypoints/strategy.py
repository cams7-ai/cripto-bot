"""Composicao offline da SMA; nao importa cliente publico/privado."""

import json
import sqlite3

from cripto_bot.adapters.config import ConfigError
from cripto_bot.adapters.storage.market_export import code_revision, source_fingerprint
from cripto_bot.adapters.storage.strategy_report import diagnostic, export_report, summary
from cripto_bot.adapters.storage.strategy_sqlite import StrategyStore
from cripto_bot.adapters.strategy_fixture import load_fixture
from cripto_bot.application.strategy_reconcile import reconcile
from cripto_bot.application.strategy_run import run
from cripto_bot.domain.market import canonical, sequence
from cripto_bot.domain.models import Settings
from cripto_bot.domain.portfolio.models import PaperError, fingerprint, require_object
from cripto_bot.domain.strategy.engine import MODEL, active_module


def execute_strategy(
    settings: Settings, command: str, run_id: str
) -> tuple[int, dict[str, object]]:
    config = settings.strategy
    if config is None:
        if command == "strategy-run":
            return 0, {
                "orders_sent": 0,
                "status": "STRATEGY_NOOP",
                "evaluated_cycles": 0,
                "directional_decisions": 0,
                "simulated_fills": 0,
                "rejected_intents": 0,
                "replayed_actions": 0,
            }
        raise ConfigError("configuracao strategy obrigatoria")
    snapshot = require_object(json.loads(canonical(config.snapshot())))
    snapshot["wallet_id"] = config.paper.wallet_id
    counts = {
        "evaluated_cycles": 0,
        "directional_decisions": 0,
        "simulated_fills": 0,
        "rejected_intents": 0,
        "replayed_actions": 0,
    }
    result: dict[str, object] = {"orders_sent": 0, "wallet_id": config.paper.wallet_id, **counts}
    exists = (config.data_dir / "paper.sqlite3").exists()
    if command == "strategy-run" and not exists and active_module(snapshot) is None:
        return 0, {**result, "status": "STRATEGY_NOOP"}
    fixture = None
    if command == "strategy-run":
        try:
            fixture = load_fixture(config.fixture_file)
        except PaperError:
            raise ConfigError("fixture de estrategia invalida") from None
    try:
        with StrategyStore(
            config.data_dir, create=command == "strategy-run", readonly=command == "strategy-report"
        ) as store:
            try:
                if fixture is not None:
                    parameters = {**config.paper.parameters(), "dataset_id": fixture["dataset_id"]}
                    starts = sequence(fixture["day_starts"])
                    store.initialize(
                        {
                            "model": MODEL,
                            "parameters": parameters,
                            "rules": fixture["rules"],
                            "start_time": require_object(starts[0])["timestamp"],
                            "source": "fixture",
                        }
                    )
                else:
                    parameters = dict(require_object(store.context()["parameters"]))
                    parameters.pop("dataset_id")
                    if parameters != config.paper.parameters():
                        raise PaperError("WALLET_CONTEXT_CONFLICT")
                if command == "strategy-report":
                    report = diagnostic(store)
                    result.update(report)
                    ok = report.get("reconciled") is True and report.get("blocked") is False
                    result["status"] = "STRATEGY_REPORTED" if ok else "STRATEGY_BLOCKED"
                    if not ok:
                        return 3, result
                else:
                    if fixture is not None:
                        state = run(store, fixture, snapshot, counts)
                    else:
                        state = reconcile(store)
                    result.update(counts)
                    result.update(summary(store, state))
                    result["status"] = (
                        "STRATEGY_COMPLETED" if fixture is not None else "STRATEGY_RECONCILED"
                    )
                if command != "strategy-reconcile":
                    provenance = {
                        "code_revision": code_revision(config.root),
                        "source_sha256": source_fingerprint(config.root),
                        "config_sha256": result["config_hash"],
                        "fixture_sha256": fingerprint(fixture) if fixture else None,
                    }
                    manifest = export_report(store, dict(result), run_id, provenance)
                    result["manifest"] = str(manifest)
                    result["html_report"] = str(manifest.parent / "report.html")
                return 0, result
            except (PaperError, OSError, sqlite3.Error) as error:
                result.update(counts)
                result.update(diagnostic(store))
                result.update(
                    status="STRATEGY_FAILED",
                    error_code=str(error)
                    if isinstance(error, PaperError)
                    else "STRATEGY_STORAGE_FAILURE",
                )
                return 3, result
    except (PaperError, OSError, sqlite3.Error) as error:
        result.update(
            status="STRATEGY_FAILED",
            error_code=str(error) if isinstance(error, PaperError) else "STRATEGY_STORAGE_FAILURE",
        )
        return 3, result
