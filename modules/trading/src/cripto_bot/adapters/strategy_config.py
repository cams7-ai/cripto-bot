"""Schemas TOML modulares; nenhum adapter financeiro ou de rede e construido."""

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cripto_bot.domain.models import PaperSettings
from cripto_bot.domain.modules.models import (
    LIMITS,
    ModuleConfig,
    StrategySettings,
    validate_modules,
)
from cripto_bot.domain.portfolio.models import PaperError, identifier, number


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class StrategyInput(StrictInput):
    environment: Literal["local"] = "local"
    evaluation_type: Literal["fixture"] = "fixture"
    wallet_id: str = "sma-demo"
    data_dir: str
    fixture_file: str
    initial_usdt: str = "10000.00"
    fee_bps: str = "10"
    spread_bps: str = "10"
    slippage_bps: str = "5"
    max_entry_fraction: str = "0.10"
    max_exposure_fraction: str = "0.10"
    max_daily_loss_fraction: str = "0.01"
    max_drawdown_fraction: str = "0.05"
    max_quote_age_ms: int = Field(default=5000, ge=1, le=5000)


class ParamsInput(StrictInput):
    fast_window: int = Field(default=20, ge=1, le=499)
    slow_window: int = Field(default=50, ge=2, le=500)
    interval: Literal["15m"] = "15m"


class ModuleInput(StrictInput):
    module_id: str
    version: str = "1"
    enabled: bool = False
    environments: list[str] = Field(default_factory=list)
    modes: list[str] = Field(default_factory=list)
    evaluation_types: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    capital_weight: str = "0.10"
    execution: str = "immediate"
    on_disable: str = "hold_and_monitor"
    params: ParamsInput = Field(default_factory=ParamsInput)
    depends_on: list[str] = Field(default_factory=list)
    limits: dict[str, str] = Field(default_factory=dict)


class CoordinationInput(StrictInput):
    policy: Literal["exclusive_owner"] = "exclusive_owner"
    symbol_limits: dict[str, dict[str, str]] = Field(default_factory=dict)


def strategy_settings(
    item: StrategyInput,
    supplied: dict[str, ModuleInput],
    coordination: CoordinationInput,
    root: Path,
    contained: Callable[[str, Path], Path],
    *,
    evaluation_type: str = "fixture",
) -> StrategySettings:
    identifier(item.wallet_id)
    if number(item.initial_usdt) <= 0:
        raise PaperError("INVALID_INITIAL")
    for name in ("fee_bps", "spread_bps", "slippage_bps"):
        if number(getattr(item, name)) > 100:
            raise PaperError("INVALID_COST")
    for name in LIMITS:
        if not 0 < number(getattr(item, name)) <= 1:
            raise PaperError("INVALID_LIMIT")

    def limits(values: dict[str, str]) -> tuple[tuple[str, str], ...]:
        for name, value in values.items():
            if name not in LIMITS or not 0 < number(value) <= number(getattr(item, name)):
                raise PaperError("INVALID_LIMIT_OVERRIDE")
        return tuple(sorted(values.items()))

    if set(coordination.symbol_limits) - {"BTCUSDT"}:
        raise PaperError("UNSUPPORTED_SYMBOL")
    modules = []
    for instance_id, module in sorted(supplied.items()):
        identifier(instance_id)
        identifier(module.module_id)
        identifier(module.version)
        if module.params.fast_window >= module.params.slow_window:
            raise PaperError("INVALID_WINDOWS")
        if not 0 < number(module.capital_weight) <= 1:
            raise PaperError("INVALID_WEIGHT")
        for dependency in module.depends_on:
            identifier(dependency)
        modules.append(
            ModuleConfig(
                instance_id,
                module.module_id,
                module.version,
                module.enabled,
                tuple(module.environments),
                tuple(module.modes),
                tuple(module.evaluation_types),
                tuple(module.symbols),
                module.capital_weight,
                module.execution,
                module.on_disable,
                module.params.fast_window,
                module.params.slow_window,
                module.params.interval,
                tuple(module.depends_on),
                limits(module.limits),
            )
        )
    validate_modules(tuple(modules), evaluation_type=evaluation_type)
    fixture = contained(item.fixture_file, root)
    paper = PaperSettings(
        root=root,
        data_dir=contained(item.data_dir, root),
        scenario_file=fixture,
        **item.model_dump(exclude={"environment", "evaluation_type", "data_dir", "fixture_file"}),
    )
    return StrategySettings(
        paper, fixture, tuple(modules), limits(coordination.symbol_limits.get("BTCUSDT", {}))
    )
