"""JSON/HTML do mesmo snapshot; publicacao nunca avalia ou executa estrategia."""

from pathlib import Path

from cripto_bot.adapters.storage.paper_report import summary as financial_summary
from cripto_bot.adapters.storage.strategy_sqlite import StrategyStore
from cripto_bot.domain.portfolio.models import PaperError
from cripto_bot.domain.strategy.engine import MODEL, StrategyWallet


def summary(
    store: StrategyStore, state: StrategyWallet, *, reconciled: bool = True
) -> dict[str, object]:
    result = financial_summary(store, state, reconciled=reconciled)
    cycles = list(state.cycles.values())
    result.update(
        model=MODEL,
        decisions=cycles,
        instances=state.instances,
        attributions=list(state.attributions.values()),
        config_generation=state.generation,
        config_hash=state.config_hash,
        total_evaluated_cycles=len(cycles),
        total_warmup=sum(c["reason"] == "WARMUP" for c in cycles),
        total_hold=sum(c["target"] == "HOLD" for c in cycles),
        total_directional_decisions=sum(c["target"] != "HOLD" for c in cycles),
        total_rejected_intents=sum(
            c["status"] in ("RISK_REJECTED", "NOT_EXECUTED", "REJECTED") for c in cycles
        ),
    )
    return result


def diagnostic(store: StrategyStore) -> dict[str, object]:
    try:
        return summary(store, store.replay())
    except (PaperError, ValueError, KeyError, TypeError):
        try:
            return summary(store, store.replay(check_projection=False), reconciled=False)
        except (PaperError, ValueError, KeyError, TypeError):
            return {"reconciled": False, "blocked": True, "report_available": False}


def render_html(report: dict[str, object]) -> str:
    from typing import cast

    from cripto_bot.adapters.analytics_dependency import load

    return cast(str, load("cripto_analytics.presentation.strategy").render_html(report))


def export_report(
    store: StrategyStore, report: dict[str, object], run_id: str, provenance: dict[str, object]
) -> Path:
    from typing import cast

    from cripto_bot.adapters.analytics_dependency import load

    return cast(
        Path,
        load("cripto_analytics.application.publish").publish_strategy(
            store.directory, report, run_id, provenance, model=MODEL, renderer=render_html
        ),
    )
