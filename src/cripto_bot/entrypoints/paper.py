"""Composicao financeira offline: nenhum cliente de rede e nenhum reset."""

import sqlite3

from cripto_bot.adapters.config import ConfigError
from cripto_bot.adapters.execution.paper import PaperExecutor
from cripto_bot.adapters.paper_scenario import load_scenario
from cripto_bot.adapters.storage.market_export import code_revision, source_fingerprint
from cripto_bot.adapters.storage.paper_report import diagnostic, export_report, summary
from cripto_bot.adapters.storage.paper_sqlite import PaperStore
from cripto_bot.application.paper_reconcile import reconcile
from cripto_bot.application.paper_run import run_scenario
from cripto_bot.domain.market import sequence
from cripto_bot.domain.models import Settings
from cripto_bot.domain.portfolio.models import MODEL, PaperError, fingerprint, require_object


def execute_paper(settings: Settings, command: str, run_id: str) -> tuple[int, dict[str, object]]:
    config = settings.paper
    if config is None:
        raise ConfigError("configuracao PAPER obrigatoria")
    scenario: dict[str, object] | None = None
    context: dict[str, object] | None = None
    if command == "paper-run":
        try:
            scenario = load_scenario(config.scenario_file)
        except PaperError:
            raise ConfigError("roteiro PAPER invalido") from None
        first = require_object(sequence(scenario["events"])[0])
        context = {
            "model": MODEL,
            "parameters": config.parameters(),
            "rules": scenario["rules"],
            "scenario_id": scenario["scenario_id"],
            "scenario_sha256": fingerprint(scenario),
            "start_time": first["timestamp"],
        }
    result: dict[str, object] = {
        "orders_sent": 0,
        "wallet_id": config.wallet_id,
        "simulated_fills": 0,
        "rejected_intents": 0,
        "replayed_actions": 0,
    }
    counts = {"simulated_fills": 0, "rejected_intents": 0, "replayed_actions": 0}
    try:
        with PaperStore(
            config.data_dir, create=command == "paper-run", readonly=command == "paper-report"
        ) as store:
            try:
                if context is not None:
                    store.initialize(context)
                else:
                    saved = store.context()
                    if (
                        saved.get("model") != MODEL
                        or saved.get("parameters") != config.parameters()
                    ):
                        raise PaperError("WALLET_CONTEXT_CONFLICT")
                if command == "paper-report":
                    report = diagnostic(store)
                    result.update(report)
                    ok = report.get("reconciled") is True and report.get("blocked") is False
                    result["status"] = "PAPER_REPORT" if ok else "PAPER_BLOCKED"
                    return (0 if ok else 3), result
                if command == "paper-run":
                    assert scenario is not None
                    wallet, counts = run_scenario(
                        store, scenario, config.wallet_id, PaperExecutor(), counts
                    )
                    result.update(counts)
                else:
                    wallet = reconcile(store)
                result.update(summary(store, wallet))
                result["status"] = (
                    "PAPER_COMPLETED" if command == "paper-run" else "PAPER_RECONCILED"
                )
                if command == "paper-run":
                    provenance = {
                        "code_revision": code_revision(config.root),
                        "source_sha256": source_fingerprint(config.root),
                        "config_sha256": fingerprint(config.parameters()),
                        "scenario_sha256": store.context()["scenario_sha256"],
                    }
                    manifest = export_report(store, dict(result), run_id, provenance)
                    result["manifest"] = str(manifest)
                return 0, result
            except (PaperError, OSError, sqlite3.Error) as error:
                if isinstance(error, PaperError) and str(error) == "CORRUPT_CONTEXT":
                    store.block()
                result.update(counts)
                result.update(diagnostic(store))
                result.update(
                    status="PAPER_FAILED",
                    error_code=str(error)
                    if isinstance(error, PaperError)
                    else "PAPER_STORAGE_FAILURE",
                )
                return 3, result
    except (PaperError, OSError, sqlite3.Error) as error:
        result.update(
            status="PAPER_FAILED",
            error_code=str(error) if isinstance(error, PaperError) else "PAPER_STORAGE_FAILURE",
        )
        return 3, result
