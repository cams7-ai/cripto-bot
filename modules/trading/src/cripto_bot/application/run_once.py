"""Ciclo vazio: nao representa uma simulacao financeira."""

from dataclasses import dataclass, field
from typing import Literal

from cripto_bot.domain.models import Mode, Settings


@dataclass(frozen=True)
class RunResult:
    mode: Mode
    status: Literal["NOOP"] = field(default="NOOP", init=False)
    orders_sent: Literal[0] = field(default=0, init=False)


def run_once(settings: Settings) -> RunResult:
    return RunResult(mode=settings.mode)
