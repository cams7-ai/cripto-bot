"""Cross-package identity, frozen protocol and market manifest consumer contracts."""

import base64
import hashlib
import importlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

import pytest
from cripto_core.errors import PaperError
from cripto_market_data.adapters.storage.sqlite import SQLiteMarketStore as DataStore
from cripto_market_data.application.ports import RawResponse as DataRaw
from cripto_market_data.domain.market import Candle as DataCandle
from cripto_market_data.domain.settings import MarketSettings as DataSettings

from cripto_bot.adapters import provenance
from cripto_bot.adapters.backtest_dataset import prepare
from cripto_bot.adapters.storage.backtest_sqlite import BacktestStore
from cripto_bot.adapters.storage.market_sqlite import SQLiteMarketStore
from cripto_bot.application.backtest_run import protocol, verify_protocol
from cripto_bot.application.ports import RawResponse
from cripto_bot.domain.market import Candle, MarketError
from cripto_bot.domain.models import MarketSettings, Settings
from cripto_bot.entrypoints.market import execute_collection
from tests.factories import BASE, fixture_file
from tests.unit.test_backtest import config as config


def test_shared_types_and_store_have_single_identity():
    assert Candle is DataCandle and MarketSettings is DataSettings
    assert RawResponse is DataRaw and SQLiteMarketStore is DataStore
    from cripto_core.errors import MarketError as CoreError

    assert MarketError is CoreError


def test_new_market_export_is_accepted_by_historical_prepare(config):
    root = config.root
    fixture = fixture_file(root / "market.json", 1010)
    source = json.loads(fixture.read_text())
    for row in source["klines"]:
        row[0] -= 50 * 900000
        row[6] -= 50 * 900000
    source["server_time"]["serverTime"] -= 50 * 900000
    fixture.write_text(json.dumps(source))
    market = MarketSettings(
        source="fixture", root=root, data_dir=root / "market", fixture_file=fixture
    )
    start = datetime.fromtimestamp((BASE - 50 * 900000) / 1000, UTC).isoformat()
    end = datetime.fromtimestamp((BASE + 960 * 900000) / 1000, UTC).isoformat()
    code, result = execute_collection(Settings(market=market), start, end, False, "new-source")
    assert code == 0 and result["inserted"] == 1010
    manifest = json.loads(Path(result["manifest"]).read_text())
    assert "producer" not in manifest and manifest["package_version"] == "0.1.0"
    assert set(manifest["files"]) == {
        "candles.jsonl.gz",
        "raw.jsonl.gz",
        "snapshots.json",
        "provenance.json",
    }
    historical = replace(
        config,
        source_manifests=(Path(result["manifest"]),),
        start=BASE,
        end=BASE + 960 * 900000,
    )
    dataset = prepare(historical)
    assert dataset["count"] == 1010 and dataset["score_count"] == 960
    assert dataset["gaps"] == 0 and dataset["symbol"] == "BTCUSDT"


@pytest.fixture
def installed_market(tmp_path, monkeypatch):
    package = tmp_path / "cripto_market_data"
    package.mkdir()
    source = package / "__init__.py"
    source.write_text("# original market fixture\n")
    info = tmp_path / "cripto_market_data-0.1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.3\nName: cripto-market-data\nVersion: 0.1.0\n"
    )

    def seal():
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(source.read_bytes()).digest())
            .rstrip(b"=")
            .decode()
        )
        (info / "RECORD").write_text(f"cripto_market_data/__init__.py,sha256={digest},\n")

    seal()
    dist = metadata.distribution
    loader = importlib.import_module
    monkeypatch.setattr(
        provenance.metadata,
        "distribution",
        lambda n: metadata.PathDistribution(info) if n == "cripto-market-data" else dist(n),
    )
    monkeypatch.setattr(
        provenance,
        "import_module",
        lambda n: SimpleNamespace(__file__=str(source)) if n == "cripto_market_data" else loader(n),
    )
    return source, info, seal


def test_market_only_change_blocks_frozen_resume(config, installed_market):
    source, info, seal = installed_market
    prepare(config)
    before = provenance.source_fingerprint(config.root)
    with BacktestStore(config.data_dir, create=True) as store:
        store.freeze(protocol(config))
        source.write_text("# new market package; unchanged bot/core/config/data\n")
        seal()
        assert provenance.source_fingerprint(config.root) != before
        with pytest.raises(PaperError, match="FROZEN_PROTOCOL_CHANGED"):
            verify_protocol(store, config)


def test_market_record_version_and_editable_rejected(installed_market):
    source, info, seal = installed_market
    with pytest.raises(PaperError, match="PACKAGE_VERSION_MISMATCH"):
        provenance.package_identity("cripto_market_data", "9.0.0")
    source.write_text("# modified without updating RECORD\n")
    with pytest.raises(PaperError, match="PACKAGE_RECORD_MISMATCH"):
        provenance.package_identity("cripto_market_data", "0.1.0")
    seal()
    (info / "direct_url.json").write_text(
        json.dumps({"url": source.parent.parent.as_uri(), "dir_info": {"editable": True}})
    )
    with pytest.raises(PaperError, match="UNSUPPORTED_PACKAGE_INSTALLATION"):
        provenance.package_identity("cripto_market_data", "0.1.0")
