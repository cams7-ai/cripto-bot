import json
from pathlib import Path

import pytest
from cripto_bot.adapters.config import ConfigError, application_root, contained, load_config

from tests.factories import ROOT


def configuration(tmp_path, **changes):
    market = {
        "source": "fixture",
        "fixture_file": str(tmp_path / "sample.json"),
        "data_dir": str(tmp_path / "data"),
    }
    market.update(changes)
    # JSON scalars coincidem com TOML para as entradas usadas neste teste.
    text = 'mode = "PAPER"\n[market]\n' + "\n".join(
        f"{k} = {json.dumps(v)}" for k, v in market.items()
    )
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_market_override_keeps_settings_and_does_not_write(tmp_path):
    path = configuration(tmp_path)
    settings = load_config(path, {"CRIPTO_BOT_MODE": "PAPER"})
    assert settings.market.source == "fixture"
    assert settings.market.root == ROOT
    assert not (tmp_path / "data").exists()
    assert not settings.market.fixture_file.exists()
    assert application_root() == ROOT


@pytest.mark.parametrize(
    "changes",
    [
        {"source": "LIVE"},
        {"source": "binance_public"},
        {"symbol": "ETHUSDT"},
        {"interval": "1m"},
        {"page_limit": True},
        {"page_limit": 0},
        {"page_limit": 1001},
        {"max_pages": 11},
        {"unknown": "SYNTHETIC_SECRET"},
        {"endpoint": "https://SYNTHETIC_SECRET.invalid"},
        {"data_dir": "/tmp/outside"},
        {"fixture_file": "../escape"},
        {"data_dir": ""},
    ],
)
def test_invalid_market(tmp_path, changes):
    path = configuration(tmp_path, **changes)
    with pytest.raises(ConfigError) as caught:
        load_config(path, {})
    assert "SYNTHETIC_SECRET" not in str(caught.value)
    assert not (tmp_path / "data").exists()


def test_missing_fixture_for_fixture_source(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[market]\nsource="fixture"\ndata_dir="data/test"')
    with pytest.raises(ConfigError):
        load_config(path, {})


def test_public_example_and_unknown_environment():
    settings = load_config(ROOT / "config/market.public.example.toml", {})
    assert settings.market.source == "binance_public" and settings.market.fixture_file is None
    with pytest.raises(ConfigError):
        load_config(ROOT / "config/market.fixture.example.toml", {"CRIPTO_BOT_ENDPOINT": "ignored"})


def test_symlink_escape(tmp_path):
    alias = tmp_path / "escape"
    alias.symlink_to("/tmp", target_is_directory=True)
    with pytest.raises(ConfigError):
        contained(str(alias / "data"), ROOT)
    with pytest.raises(ConfigError):
        contained(str(ROOT), ROOT)


def test_manifest_discovery_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    original = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda _: False)
    with pytest.raises(ConfigError):
        application_root()
    monkeypatch.setattr(Path, "is_file", original)
