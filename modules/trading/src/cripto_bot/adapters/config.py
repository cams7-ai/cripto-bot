"""Fronteira TOML/ambiente: validar antes de executar."""

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from cripto_bot.domain.models import Settings


class ConfigError(ValueError):
    """Diagnostico fixo, sem valores recebidos."""


class ConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    mode: Literal["PAPER"] = "PAPER"


def load_config(path: Path | None, environ: Mapping[str, str]) -> Settings:
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
            parsed = ConfigInput.model_validate({"mode": environ["CRIPTO_BOT_MODE"]})
        except ValidationError:
            raise ConfigError("modo nao permitido") from None
    return Settings(mode=parsed.mode)
