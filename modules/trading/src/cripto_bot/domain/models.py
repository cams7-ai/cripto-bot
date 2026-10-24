"""Modelos puros da base local; sem integracoes externas."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

Mode = Literal["PAPER"]


@dataclass(frozen=True)
class Settings:
    mode: Mode = "PAPER"


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
