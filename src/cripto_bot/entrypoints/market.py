"""Composicao do comando de coleta; opt-in antes de quaisquer efeitos."""

import sqlite3
import time
from dataclasses import asdict

from cripto_bot.adapters.binance.fixture import fixture_source
from cripto_bot.adapters.binance.public import HOST, Budget, PublicSource
from cripto_bot.adapters.config import ConfigError
from cripto_bot.adapters.storage.market_export import (
    code_revision,
    export_market,
    source_fingerprint,
)
from cripto_bot.adapters.storage.market_sqlite import SQLiteMarketStore
from cripto_bot.application.collect_market import Progress, collect_market
from cripto_bot.domain.market import MarketError, canonical, digest, timestamp, window
from cripto_bot.domain.models import Settings


def execute_collection(
    settings: Settings, start_text: str, end_text: str, allow_network: bool, run_id: str
) -> tuple[int, dict[str, object]]:
    config = settings.market
    if config is None:
        raise ConfigError("configuracao de coleta obrigatoria")
    if allow_network != (config.source == "binance_public"):
        raise ConfigError("opt-in de rede inconsistente")
    try:
        start, end = timestamp(start_text), timestamp(end_text)
        window(start, end, config.page_limit * config.max_pages)
    except MarketError:
        raise ConfigError("janela invalida") from None
    progress = Progress(start)
    budget = Budget()
    budget.remaining()
    dataset = "binance_public:" + HOST + ":v1"
    result: dict[str, object] = {
        "dataset": None,
        "source": config.source,
        "symbol": config.symbol,
        "interval": config.interval,
        "start": start,
        "end": end,
        "orders_sent": 0,
    }
    source: PublicSource | None = None
    try:
        if config.source == "fixture":
            assert config.fixture_file is not None
            dataset, source = fixture_source(config.fixture_file, budget)
        result["dataset"] = dataset
        with SQLiteMarketStore(config.data_dir) as store:
            checkpoint = store.checkpoint(dataset, start)
            store.verify_prefix(dataset, start, checkpoint)
            progress.next_open_time = min(checkpoint, end)
            if config.source == "binance_public":
                if store.not_before() > time.time():
                    raise MarketError("SOURCE_BLOCKED")
                source = PublicSource(budget=budget, blocked_until=store.block_until)
            assert source is not None
            collect_market(
                source,
                store,
                dataset,
                start,
                end,
                config.page_limit,
                config.max_pages,
                progress,
                budget.remaining,
            )
            budget.remaining()
            context: dict[str, object] = {
                "source": config.source,
                "code_revision": code_revision(config.root),
                "source_sha256": source_fingerprint(config.root),
                "fixture_sha256": dataset.split(":")[1] if config.source == "fixture" else None,
                "config_sha256": digest(
                    canonical(
                        {
                            "mode": settings.mode,
                            "source": config.source,
                            "symbol": config.symbol,
                            "interval": config.interval,
                            "page_limit": config.page_limit,
                            "max_pages": config.max_pages,
                            "data_dir": str(config.data_dir),
                            "fixture_file": str(config.fixture_file),
                        }
                    ).encode()
                ),
            }
            manifest = export_market(store, dataset, start, end, run_id, context)
            budget.remaining()
            result.update(status="COLLECTED", complete=True, manifest=str(manifest))
        code = 0
    except (MarketError, OSError, sqlite3.Error) as error:
        result.update(
            status="COLLECTION_FAILED",
            complete=False,
            error_code=str(error) if isinstance(error, MarketError) else "STORAGE_FAILURE",
        )
        code = 3
    finally:
        if source is not None:
            source.close()
    result.update(asdict(progress))
    return code, result
