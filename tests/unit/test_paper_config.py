import pytest

from cripto_bot.adapters.config import ConfigError, load_config
from tests.paper_factories import write_case


@pytest.mark.parametrize(
    "changes",
    [
        {"initial_usdt": 0.1},
        {"initial_usdt": "0"},
        {"initial_usdt": "NaN"},
        {"fee_bps": "-1"},
        {"fee_bps": "101"},
        {"spread_bps": True},
        {"max_entry_fraction": "0"},
        {"max_exposure_fraction": "1.1"},
        {"max_quote_age_ms": True},
        {"max_quote_age_ms": 5001},
        {"wallet_id": "../escape"},
        {"data_dir": "/tmp/outside"},
        {"scenario_file": "../outside"},
        {"unknown": "SYNTHETIC_SECRET"},
        {"api_key": "SYNTHETIC_SECRET"},
    ],
)
def test_reject_before_financial_effects(tmp_path, changes):
    config = write_case(tmp_path, **changes)
    with pytest.raises(ConfigError) as error:
        load_config(config, {})
    assert "SYNTHETIC_SECRET" not in str(error.value)
    assert not (tmp_path / "data").exists()


def test_defaults_and_override_preserve_paper(tmp_path):
    config = write_case(tmp_path)
    settings = load_config(config, {"CRIPTO_BOT_MODE": "PAPER"})
    assert settings.paper.initial_usdt == "10000.00"
    assert settings.paper.max_quote_age_ms == 5000
    assert not settings.paper.data_dir.exists()
    with pytest.raises(ConfigError):
        load_config(config, {"CRIPTO_BOT_MODE": "LIVE"})


def test_symlink_escape(tmp_path):
    link = tmp_path / "outside"
    link.symlink_to("/tmp", target_is_directory=True)
    config = write_case(tmp_path, data_dir=str(link / "paper"))
    with pytest.raises(ConfigError):
        load_config(config, {})
