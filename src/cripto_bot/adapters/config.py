"""Fronteira TOML/ambiente: validar antes de executar."""

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from cripto_market_data.adapters.config import MarketConfigError, market_settings
from cripto_market_data.adapters.config import MarketInput as MarketInput
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cripto_bot.adapters.backtest_config import BacktestInput, backtest_settings
from cripto_bot.adapters.continuous_config import ContinuousInput
from cripto_bot.adapters.continuous_config import settings as continuous_settings
from cripto_bot.adapters.strategy_config import (
    CoordinationInput,
    ModuleInput,
    StrategyInput,
    strategy_settings,
)
from cripto_bot.domain.models import PaperSettings, Settings
from cripto_bot.domain.portfolio.models import PaperError, identifier, number


class ConfigError(ValueError):
    """Diagnostico fixo, sem valores recebidos."""


def application_root() -> Path:
    for candidate in (Path.cwd(), *Path.cwd().parents):
        manifest = candidate / "pyproject.toml"
        if manifest.is_file():
            try:
                if (
                    tomllib.loads(manifest.read_text()).get("project", {}).get("name")
                    == "cripto-bot"
                ):
                    return candidate.resolve()
            except (OSError, ValueError):
                pass
    raise ConfigError("raiz da aplicacao indisponivel")


def contained(path: str, root: Path) -> Path:
    if not path:
        raise ConfigError("caminho invalido")
    try:
        result = (Path.cwd() / path).resolve()
        if not result.is_relative_to(root.resolve()) or result == root.resolve():
            raise ConfigError("caminho fora do modulo")
        return result
    except (OSError, ValueError, RuntimeError):
        raise ConfigError("caminho invalido") from None


class PaperInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    wallet_id: str = "paper-demo"
    data_dir: str
    scenario_file: str
    initial_usdt: str = "10000.00"
    fee_bps: str = "10"
    spread_bps: str = "10"
    slippage_bps: str = "5"
    max_entry_fraction: str = "0.10"
    max_exposure_fraction: str = "0.10"
    max_daily_loss_fraction: str = "0.01"
    max_drawdown_fraction: str = "0.05"
    max_quote_age_ms: int = Field(default=5000, ge=1, le=5000)


class ConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    mode: Literal["PAPER"] = "PAPER"
    market: MarketInput | None = None
    paper: PaperInput | None = None
    strategy: StrategyInput | None = None
    backtest: BacktestInput | None = None
    continuous: ContinuousInput | None = None
    modules: dict[str, ModuleInput] = Field(default_factory=dict)
    coordination: CoordinationInput = Field(default_factory=CoordinationInput)


def load_config(
    path: Path | None, environ: Mapping[str, str], *, root: Path | None = None
) -> Settings:
    data: dict[str, object] = {}
    if path is not None:
        try:
            with path.open("rb") as stream:
                data = tomllib.load(stream)
        except (OSError, ValueError):
            raise ConfigError("arquivo de configuracao invalido ou inacessivel") from None
    try:
        parsed = ConfigInput.model_validate(data)
    except ValidationError:
        raise ConfigError("configuracao nao suportada") from None
    # Inspecionar nomes; nao ler conteudo de credenciais.
    if any(key in environ for key in ("BINANCE_API_KEY", "BINANCE_API_SECRET")):
        raise ConfigError("credenciais Binance nao permitidas")
    if any(key.startswith("CRIPTO_BOT_") and key != "CRIPTO_BOT_MODE" for key in environ):
        raise ConfigError("variavel de configuracao nao suportada")
    if "CRIPTO_BOT_MODE" in environ:
        try:
            ConfigInput.model_validate({"mode": environ["CRIPTO_BOT_MODE"]})
        except ValidationError:
            raise ConfigError("modo nao permitido") from None
    market = None
    if parsed.market is not None:
        item = parsed.market
        module_root = application_root() if root is None else root.resolve()
        try:
            market = market_settings(item, module_root, contained)
        except MarketConfigError:
            raise ConfigError("fixture inconsistente") from None

    paper = None
    if parsed.paper is not None:
        item_paper = parsed.paper
        try:
            identifier(item_paper.wallet_id)
            if number(item_paper.initial_usdt) <= 0:
                raise PaperError("INVALID_INITIAL")
            for value in (item_paper.fee_bps, item_paper.spread_bps, item_paper.slippage_bps):
                if number(value) > 100:
                    raise PaperError("INVALID_COST")
            for value in (
                item_paper.max_entry_fraction,
                item_paper.max_exposure_fraction,
                item_paper.max_daily_loss_fraction,
                item_paper.max_drawdown_fraction,
            ):
                if not 0 < number(value) <= 1:
                    raise PaperError("INVALID_LIMIT")
        except PaperError:
            raise ConfigError("configuracao PAPER invalida") from None
        module_root = application_root() if root is None else root.resolve()
        values = item_paper.model_dump(exclude={"data_dir", "scenario_file"})
        paper = PaperSettings(
            root=module_root,
            data_dir=contained(item_paper.data_dir, module_root),
            scenario_file=contained(item_paper.scenario_file, module_root),
            **values,
        )
    strategy = None
    if parsed.strategy is not None:
        module_root = application_root() if root is None else root.resolve()
        try:
            strategy = strategy_settings(
                parsed.strategy, parsed.modules, parsed.coordination, module_root, contained
            )
        except (PaperError, ValueError):
            raise ConfigError("configuracao modular invalida") from None
        if paper is not None and paper.data_dir == strategy.data_dir:
            raise ConfigError("diretorio financeiro compartilhado")
    elif (
        parsed.backtest is None
        and parsed.continuous is None
        and (parsed.modules or parsed.coordination.model_dump() != CoordinationInput().model_dump())
    ):
        raise ConfigError("configuracao strategy obrigatoria")
    backtest = None
    if parsed.backtest is not None:
        if strategy is not None:
            raise ConfigError("avaliacoes concorrentes nao suportadas")
        module_root = application_root() if root is None else root.resolve()
        try:
            backtest = backtest_settings(
                parsed.backtest, parsed.modules, parsed.coordination, module_root, contained
            )
        except (ValueError, PaperError):
            raise ConfigError("configuracao historica invalida") from None
        if paper is not None and paper.data_dir == backtest.data_dir:
            raise ConfigError("diretorio financeiro compartilhado")
        if market is not None and market.data_dir == backtest.data_dir:
            raise ConfigError("diretorio de mercado compartilhado")
    continuous = None
    if parsed.continuous is not None:
        if paper is not None or strategy is not None or backtest is not None:
            raise ConfigError("continuous envelope cannot share financial evaluation")
        try:
            module_root = application_root() if root is None else root.resolve()
            continuous = continuous_settings(
                parsed.continuous,
                parsed.modules,
                parsed.coordination,
                module_root,
                contained,
                environ,
            )
            if market is not None and any(
                market.data_dir.is_relative_to(p) or p.is_relative_to(market.data_dir)
                for p in [continuous.data_dir, continuous.notification_data_dir]
            ):
                raise PaperError("SHARED_CONTINUOUS_DIRECTORY")
        except (ValueError, PaperError):
            raise ConfigError("invalid continuous configuration") from None
    return Settings(
        mode=parsed.mode,
        market=market,
        paper=paper,
        strategy=strategy,
        backtest=backtest,
        continuous=continuous,
    )
