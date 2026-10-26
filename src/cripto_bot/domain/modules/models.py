"""Configuracao imutavel e registro minimo da Sprint 04."""

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from cripto_bot.domain.models import PaperSettings
from cripto_bot.domain.portfolio.models import PaperError, fingerprint, number

LIMITS = (
    "max_entry_fraction",
    "max_exposure_fraction",
    "max_daily_loss_fraction",
    "max_drawdown_fraction",
)


@dataclass(frozen=True)
class ModuleDescriptor:
    module_id: str
    version: str
    kind: str = "strategy"
    parameter_schema: int = 1
    state_schema: int = 1
    capabilities: tuple[str, ...] = ("local", "PAPER", "fixture", "backtest", "BTCUSDT", "15m")
    dependencies: tuple[str, ...] = ()
    exclusive_resource: str = "BTCUSDT"


REGISTRY = {"trend_sma": ModuleDescriptor("trend_sma", "1")}


@dataclass(frozen=True)
class ModuleConfig:
    instance_id: str
    module_id: str
    version: str
    enabled: bool
    environments: tuple[str, ...]
    modes: tuple[str, ...]
    evaluation_types: tuple[str, ...]
    symbols: tuple[str, ...]
    capital_weight: str
    execution: str
    on_disable: str
    fast_window: int
    slow_window: int
    interval: str
    depends_on: tuple[str, ...] = ()
    limits: tuple[tuple[str, str], ...] = ()

    @property
    def active(self) -> bool:
        return self.active_for("fixture")

    def active_for(self, evaluation_type: str) -> bool:
        return self.enabled and all(
            (
                "local" in self.environments,
                "PAPER" in self.modes,
                evaluation_type in self.evaluation_types,
                "BTCUSDT" in self.symbols,
            )
        )


@dataclass(frozen=True)
class StrategySettings:
    paper: PaperSettings
    fixture_file: Path
    modules: tuple[ModuleConfig, ...]
    symbol_limits: tuple[tuple[str, str], ...] = ()

    @property
    def data_dir(self) -> Path:
        return self.paper.data_dir

    @property
    def root(self) -> Path:
        return self.paper.root

    def snapshot(self) -> dict[str, object]:
        return {
            "modules": [asdict(item) for item in self.modules],
            "symbol_limits": dict(self.symbol_limits),
            "policy": "exclusive_owner",
        }


def validate_modules(
    modules: tuple[ModuleConfig, ...], *, evaluation_type: str = "fixture"
) -> None:
    by_id = {item.instance_id: item for item in modules}
    if len(by_id) != len(modules):
        raise PaperError("DUPLICATE_INSTANCE")
    total = sum(
        (number(item.capital_weight) for item in modules if item.enabled), start=number("0")
    )
    if total > 1:
        raise PaperError("CAPITAL_WEIGHT_LIMIT")
    for item in modules:
        if not item.enabled:
            continue
        descriptor = REGISTRY.get(item.module_id)
        if descriptor is None or descriptor.version != item.version:
            raise PaperError("UNIMPLEMENTED_MODULE")
        if item.execution != "immediate" or item.on_disable != "hold_and_monitor":
            raise PaperError("UNSUPPORTED_CAPABILITY")
        for values, allowed in (
            (item.environments, {"local"}),
            (item.modes, {"PAPER"}),
            (item.evaluation_types, {evaluation_type}),
            (item.symbols, {"BTCUSDT"}),
        ):
            if set(values) - allowed:
                raise PaperError("UNSUPPORTED_SCOPE")

        def visit(current: str, trail: frozenset[str]) -> None:
            if current in trail:
                raise PaperError("DEPENDENCY_CYCLE")
            node = by_id.get(current)
            if node is None or not node.active_for(evaluation_type):
                raise PaperError("MISSING_DEPENDENCY")
            for dependency in node.depends_on:
                visit(dependency, trail | {current})

        # A flag com listas vazias nao ativa, mas tambem nao resolve dependencias.
        for dependency in item.depends_on:
            visit(dependency, frozenset({item.instance_id}))
    if sum(item.active_for(evaluation_type) for item in modules) > 1:
        raise PaperError("EXCLUSIVE_OWNER_CONFLICT")


class DecisionLike(Protocol):
    @property
    def instance_id(self) -> str: ...
    @property
    def target(self) -> str: ...


def resolve(decisions: tuple[DecisionLike, ...]) -> tuple[DecisionLike, ...]:
    ordered = tuple(sorted(decisions, key=lambda item: item.instance_id))
    if len(ordered) > 1:
        raise PaperError("EXCLUSIVE_OWNER_CONFLICT")
    return ordered


def configuration_hash(snapshot: dict[str, object]) -> str:
    return fingerprint(snapshot)


def monitor_state(enabled: bool, position: bool, pending: tuple[str, ...]) -> str:
    if not enabled:
        return "DRAINING" if position or pending else "DISABLED"
    return (
        "PAUSED" if any(state in ("UNKNOWN", "PARTIALLY_FILLED") for state in pending) else "ACTIVE"
    )
