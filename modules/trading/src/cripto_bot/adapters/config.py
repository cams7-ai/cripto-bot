"""Fronteira TOML/ambiente: validar antes de executar."""

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cripto_bot.domain.models import MarketSettings, Settings


class ConfigError(ValueError):
    """Diagnostico fixo, sem valores recebidos."""


class MarketInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    source: Literal["fixture", "binance_public"]
    symbol: Literal["BTCUSDT"] = "BTCUSDT"
    interval: Literal["15m"] = "15m"
    fixture_file: str | None = None
    data_dir: str
    page_limit: int = Field(default=1000, ge=1, le=1000)
    max_pages: int = Field(default=10, ge=1, le=10)


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


class ConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    mode: Literal["PAPER"] = "PAPER"
    market: MarketInput | None = None


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
        if (item.source == "fixture") != (item.fixture_file is not None):
            raise ConfigError("fixture inconsistente")
        market = MarketSettings(
            source=item.source,
            root=module_root,
            data_dir=contained(item.data_dir, module_root),
            fixture_file=None
            if item.fixture_file is None
            else contained(item.fixture_file, module_root),
            symbol=item.symbol,
            interval=item.interval,
            page_limit=item.page_limit,
            max_pages=item.max_pages,
        )
    return Settings(mode=parsed.mode, market=market)
