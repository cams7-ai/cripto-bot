"Configuracao backtest estrita, reutilizando validacoes financeiras existentes."

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import Field

from cripto_bot.adapters.strategy_config import (
    CoordinationInput,
    ModuleInput,
    StrategyInput,
    StrictInput,
    strategy_settings,
)
from cripto_bot.domain.backtest.models import BENCHMARKS, VARIANTS, BacktestSettings, split
from cripto_bot.domain.market import timestamp
from cripto_bot.domain.portfolio.models import PaperError, identifier, number


class BacktestInput(StrictInput):
    experiment_id: str
    environment: Literal["local"] = "local"
    evaluation_type: Literal["backtest"] = "backtest"
    source_manifests: list[str] = Field(min_length=1, max_length=128)
    dataset_dir: str
    data_dir: str
    start: str
    end: str
    initial_usdt: str = "10000.00"
    fee_bps: str = "10"
    spread_bps: str = "10"
    slippage_bps: str = "5"
    max_entry_fraction: str = "0.10"
    max_exposure_fraction: str = "0.10"
    max_daily_loss_fraction: str = "0.01"
    max_drawdown_fraction: str = "0.05"
    max_quote_age_ms: int = Field(default=5000, ge=1, le=5000)
    split_percentages: list[int] = Field(default_factory=lambda: [60, 20, 20])
    variants: list[str] = Field(default_factory=lambda: list(VARIANTS))
    benchmarks: list[str] = Field(default_factory=lambda: list(BENCHMARKS))
    terminal_policy: Literal["mark_to_market"] = "mark_to_market"


def backtest_settings(
    item: BacktestInput,
    modules: dict[str, ModuleInput],
    coordination: CoordinationInput,
    root: Path,
    contained: Callable[[str, Path], Path],
) -> BacktestSettings:
    identifier(item.experiment_id)
    if (
        number(item.initial_usdt) != 10000
        or number(item.fee_bps) != 10
        or number(item.spread_bps) != 10
        or number(item.slippage_bps) != 5
    ):
        raise PaperError("FIXED_BASE_PROTOCOL")
    for name, ceiling in (
        ("max_entry_fraction", "0.10"),
        ("max_exposure_fraction", "0.10"),
        ("max_daily_loss_fraction", "0.01"),
        ("max_drawdown_fraction", "0.05"),
    ):
        if number(getattr(item, name)) > number(ceiling):
            raise PaperError("HISTORICAL_RISK_CEILING")
    if (
        item.split_percentages != [60, 20, 20]
        or item.variants != list(VARIANTS)
        or item.benchmarks != list(BENCHMARKS)
    ):
        raise PaperError("UNSUPPORTED_PROTOCOL")
    start, end = timestamp(item.start), timestamp(item.end)
    if start >= end:
        raise PaperError("INVALID_WINDOW")
    common = item.model_dump(
        exclude={
            "experiment_id",
            "source_manifests",
            "dataset_dir",
            "start",
            "end",
            "split_percentages",
            "variants",
            "benchmarks",
            "terminal_policy",
            "evaluation_type",
        }
    )
    common.update(
        wallet_id=item.experiment_id, fixture_file=str(Path(item.dataset_dir) / "candles.jsonl")
    )
    validated = strategy_settings(
        StrategyInput.model_validate(common),
        modules,
        coordination,
        root,
        contained,
        evaluation_type="backtest",
    )
    dataset = contained(item.dataset_dir, root)
    if any(number(m.capital_weight) > number("0.10") for m in validated.modules):
        raise PaperError("HISTORICAL_WEIGHT_CEILING")
    if dataset == validated.data_dir:
        raise PaperError("SHARED_DATA_DIRECTORY")
    sources = tuple(contained(p, root) for p in item.source_manifests)
    if len(set(sources)) != len(sources):
        raise PaperError("DUPLICATE_MANIFEST")
    slow = next((m.slow_window for m in validated.modules if m.active_for("backtest")), 50)
    split(start, end, slow)
    return BacktestSettings(
        validated.paper,
        item.experiment_id,
        sources,
        dataset,
        start,
        end,
        validated.modules,
        validated.symbol_limits,
    )
