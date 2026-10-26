"""Consumer bridge for market export and legacy provenance imports."""

from importlib.metadata import version
from pathlib import Path

from cripto_market_data.adapters.storage.export import export_market as export_data
from cripto_market_data.adapters.storage.sqlite import SQLiteMarketStore

from cripto_bot.adapters.provenance import code_revision as code_revision
from cripto_bot.adapters.provenance import source_fingerprint as source_fingerprint


def export_market(
    store: SQLiteMarketStore,
    dataset: str,
    start: int,
    end: int,
    run_id: str,
    context: dict[str, object],
) -> Path:
    return export_data(
        store, dataset, start, end, run_id, context, package_version=version("cripto-bot")
    )
