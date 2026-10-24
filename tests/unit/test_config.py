from pathlib import Path

import pytest
from cripto_bot.adapters.config import ConfigError, load_config


@pytest.mark.parametrize("content", [None, "", 'mode = "PAPER"'])
def test_defaults(tmp_path, content):
    path = None
    if content is not None:
        path = tmp_path / "config.toml"
        path.write_text(content)
    assert load_config(path, {}).mode == "PAPER"


@pytest.mark.parametrize(
    "mode",
    ['"LIVE"', '"TESTNET"', '"other"', '""', '"paper"', '" PAPER"', '"PAPER "', "1", "true", "[]"],
)
def test_file_mode_rejected_before_override(tmp_path, mode):
    path = tmp_path / "config.toml"
    path.write_text(f"mode = {mode}")
    with pytest.raises(ConfigError):
        load_config(path, {"CRIPTO_BOT_MODE": "PAPER"})


@pytest.mark.parametrize("mode", ["LIVE", "TESTNET", "unknown", "", "paper", "PAPER "])
def test_override_rejected(tmp_path, mode):
    path = tmp_path / "config.toml"
    path.write_text('mode = "PAPER"')
    with pytest.raises(ConfigError):
        load_config(path, {"CRIPTO_BOT_MODE": mode})


def test_valid_override_and_unrelated_environment():
    assert load_config(None, {"CRIPTO_BOT_MODE": "PAPER", "OTHER": "ignored"}).mode == "PAPER"


@pytest.mark.parametrize("key", ["symbol", "endpoint", "api_key", "SYNTHETIC_SECRET"])
def test_extra_field(tmp_path, key):
    path = tmp_path / "config.toml"
    path.write_text(f'{key} = "SYNTHETIC_SECRET"')
    with pytest.raises(ConfigError) as caught:
        load_config(path, {})
    assert "SYNTHETIC_SECRET" not in str(caught.value)


@pytest.mark.parametrize(
    "key", ["CRIPTO_BOT_API_KEY", "CRIPTO_BOT_UNKNOWN", "BINANCE_API_KEY", "BINANCE_API_SECRET"]
)
@pytest.mark.parametrize("value", ["", "SYNTHETIC_SECRET"])
def test_forbidden_environment(key, value):
    with pytest.raises(ConfigError) as caught:
        load_config(None, {key: value})
    assert "SYNTHETIC_SECRET" not in str(caught.value)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing", {})


@pytest.mark.parametrize("content", ["mode = [", 'mode = "SYNTHETIC_SECRET', "\udcff"])
def test_malformed_or_invalid_encoding(tmp_path, content):
    path = tmp_path / "config.toml"
    path.write_bytes(content.encode("utf-8", errors="surrogateescape"))
    with pytest.raises(ConfigError):
        load_config(path, {})


def test_unreadable_file(monkeypatch, tmp_path):
    def denied(*args, **kwargs):
        raise PermissionError("SYNTHETIC_SECRET")

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(ConfigError) as caught:
        load_config(tmp_path / "denied", {})
    assert "SYNTHETIC_SECRET" not in str(caught.value)
