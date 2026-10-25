import copy
import json
import tomllib

import pytest
from cripto_bot.adapters.config import ConfigError, load_config
from cripto_bot.adapters.strategy_config import (
    CoordinationInput,
    ModuleInput,
    StrategyInput,
    strategy_settings,
)
from cripto_bot.domain.modules.models import REGISTRY

from tests.factories import ROOT
from tests.strategy_factories import write_case


@pytest.mark.parametrize(
    "old,new",
    [
        ("fast_window = 1", "fast_window = true"),
        ("fast_window = 1", "fast_window = 2"),
        ("slow_window = 2", "slow_window = 501"),
        ('capital_weight = "0.10"', 'capital_weight = "-0.1"'),
        ('capital_weight = "0.10"', "capital_weight = 0.1"),
        ('version = "1"', 'version = "2"'),
        ('module_id = "trend_sma"', 'module_id = "grid"'),
        ('policy = "exclusive_owner"', 'policy = "weighted_targets"'),
        ('execution = "immediate"', 'execution = "twap"'),
        ('on_disable = "hold_and_monitor"', 'on_disable = "cancel_and_monitor"'),
        ('environments = ["local"]', 'environments = ["aws"]'),
        ('modes = ["PAPER"]', 'modes = ["LIVE"]'),
        ('evaluation_types = ["fixture"]', 'evaluation_types = ["backtest"]'),
        ('symbols = ["BTCUSDT"]', 'symbols = ["ETHUSDT"]'),
        ('mode = "PAPER"', 'mode = "TESTNET"'),
        ("max_quote_age_ms = 5000", "max_quote_age_ms = true"),
        ('initial_usdt = "10000.00"', 'initial_usdt = "NaN"'),
        ('fee_bps = "10"', 'fee_bps = "101"'),
        ("enabled = true", 'enabled = "true"'),
        ('interval = "15m"', 'interval = "1h"'),
    ],
)
def test_configuration_rejected_before_wallet_creation(tmp_path, old, new):
    config = write_case(tmp_path)
    assert old in config.read_text()
    config.write_text(config.read_text().replace(old, new))
    with pytest.raises(ConfigError):
        load_config(config, {})
    assert not (tmp_path / "wallet").exists()


@pytest.mark.parametrize("field", ["environments", "modes", "evaluation_types", "symbols"])
def test_missing_scope_never_activates(tmp_path, field):
    config = write_case(tmp_path)
    lines = [line for line in config.read_text().splitlines() if not line.startswith(field + " =")]
    config.write_text("\n".join(lines))
    assert not load_config(config, {}).strategy.modules[0].active


def test_disabled_future_module_does_not_load_or_block(tmp_path):
    config = write_case(tmp_path, enabled=False)
    config.write_text(config.read_text().replace('module_id = "trend_sma"', 'module_id = "grid"'))
    assert not load_config(config, {}).strategy.modules[0].active
    assert set(REGISTRY) == {"trend_sma"}


def test_dependency_cycle_missing_and_owner_conflict(tmp_path):
    config = write_case(tmp_path)
    data = tomllib.loads(config.read_text())

    def validate(modules):
        return strategy_settings(
            StrategyInput.model_validate(data["strategy"]),
            {key: ModuleInput.model_validate(value) for key, value in modules.items()},
            CoordinationInput(),
            ROOT,
            lambda path, root: root / path,
        )

    original = data["modules"]
    for dependency in ("absent", "trend_btc"):
        modules = copy.deepcopy(original)
        modules["trend_btc"]["depends_on"] = [dependency]
        with pytest.raises(ValueError):
            validate(modules)
    modules = copy.deepcopy(original)
    modules["second"] = copy.deepcopy(modules["trend_btc"])
    with pytest.raises(ValueError):
        validate(modules)
    modules["second"]["capital_weight"] = "1"
    with pytest.raises(ValueError):
        validate(modules)


@pytest.mark.parametrize(
    "table,field,value",
    [
        ("modules.trend_btc.limits", "max_entry_fraction", "0.11"),
        ("modules.trend_btc.limits", "max_drawdown_fraction", "0"),
        ("coordination.symbol_limits.BTCUSDT", "max_daily_loss_fraction", "NaN"),
        ("coordination.symbol_limits.ETHUSDT", "max_exposure_fraction", "0.01"),
        ("modules.trend_btc.limits", "disable_risk", "true"),
    ],
)
def test_override_cannot_bypass_global_risk(tmp_path, table, field, value):
    config = write_case(tmp_path)
    config.write_text(config.read_text() + f"\n[{table}]\n{field}={json.dumps(value)}\n")
    with pytest.raises(ConfigError):
        load_config(config, {})


def test_override_paths_and_shared_directory(tmp_path):
    config = write_case(tmp_path)
    config.write_text(
        config.read_text() + '\n[modules.trend_btc.limits]\nmax_entry_fraction="0.02"\n'
    )
    assert dict(load_config(config, {}).strategy.modules[0].limits)["max_entry_fraction"] == "0.02"
    config.write_text(
        config.read_text() + f'\n[paper]\ndata_dir="{tmp_path / "wallet"}"\n'
        f'scenario_file="{ROOT / "config/fixtures/paper-roundtrip.json"}"\n'
    )
    with pytest.raises(ConfigError):
        load_config(config, {})


@pytest.mark.parametrize(
    "env",
    [{"CRIPTO_BOT_RISK": "off"}, {"BINANCE_API_KEY": "SYNTHETIC"}, {"CRIPTO_BOT_MODE": "LIVE"}],
)
def test_unknown_env_and_private_mode_rejected(tmp_path, env):
    with pytest.raises(ConfigError):
        load_config(write_case(tmp_path), env)
