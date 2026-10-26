"""Consumer bridge; all collection rules live in cripto_market_data."""

from cripto_market_data.adapters.config import MarketConfigError
from cripto_market_data.application.collection import CollectionAdapters
from cripto_market_data.application.collection import execute_collection as collect

from cripto_bot.adapters.binance.fixture import fixture_source
from cripto_bot.adapters.binance.public import Budget, PublicSource
from cripto_bot.adapters.config import ConfigError
from cripto_bot.adapters.storage.market_export import (
    code_revision,
    export_market,
    source_fingerprint,
)
from cripto_bot.adapters.storage.market_sqlite import SQLiteMarketStore
from cripto_bot.domain.models import Settings


def execute_collection(
    settings: Settings, start_text: str, end_text: str, allow_network: bool, run_id: str
) -> tuple[int, dict[str, object]]:
    config = settings.market
    if config is None:
        raise ConfigError("configuracao de coleta obrigatoria")
    try:
        return collect(
            config,
            start_text,
            end_text,
            allow_network,
            run_id,
            provenance=lambda: {
                "code_revision": code_revision(config.root),
                "source_sha256": source_fingerprint(config.root),
            },
            adapters=CollectionAdapters(
                PublicSource, fixture_source, SQLiteMarketStore, Budget, export_market
            ),
        )
    except MarketConfigError as error:
        raise ConfigError(str(error)) from None
