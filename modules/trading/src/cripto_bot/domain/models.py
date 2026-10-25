"""Modelos puros da base local; sem integracoes externas."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

Mode = Literal["PAPER"]


@dataclass(frozen=True)
class MarketSettings:
    source: Literal["fixture", "binance_public"]
    root: Path
    data_dir: Path
    fixture_file: Path | None = None
    symbol: Literal["BTCUSDT"] = "BTCUSDT"
    interval: Literal["15m"] = "15m"
    page_limit: int = 1000
    max_pages: int = 10


@dataclass(frozen=True)
class Settings:
    mode: Mode = "PAPER"
    market: MarketSettings | None = None


def parse_decimal(value: object) -> Decimal:
    """Aceita exclusivamente texto decimal finito, sem conversao por float."""
    if not isinstance(value, str):
        raise ValueError("decimal exige string")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("decimal invalido") from None
    if not result.is_finite():
        raise ValueError("decimal deve ser finito")
    return result
