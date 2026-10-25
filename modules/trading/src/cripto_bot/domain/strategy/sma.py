"""SMA pura: recebe somente fechamentos disponiveis, sem conhecer execucao."""

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Protocol

from cripto_bot.domain.portfolio.models import PaperError


@dataclass(frozen=True)
class Snapshot:
    closes: tuple[Decimal, ...]
    as_of: int
    position: Decimal


@dataclass(frozen=True)
class Decision:
    instance_id: str
    target: str
    reason: str
    previous_fast: str = ""
    previous_slow: str = ""
    fast: str = ""
    slow: str = ""


class Strategy(Protocol):
    def evaluate(self, snapshot: Snapshot, fast: int, slow: int, instance: str) -> Decision: ...


class TrendSMA:
    def evaluate(self, snapshot: Snapshot, fast: int, slow: int, instance: str) -> Decision:
        if not 1 <= fast < slow <= 500:
            raise PaperError("INVALID_WINDOWS")
        if any(not value.is_finite() or value <= 0 for value in snapshot.closes):
            raise PaperError("INVALID_CLOSE")
        if len(snapshot.closes) <= slow:
            return Decision(instance, "HOLD", "WARMUP")
        with localcontext() as context:
            context.prec = 80
            values = snapshot.closes
            pf = sum(values[-fast - 1 : -1]) / fast
            ps = sum(values[-slow - 1 : -1]) / slow
            f = sum(values[-fast:]) / fast
            s = sum(values[-slow:]) / slow
            target, reason = "HOLD", "NO_CROSS"
            if pf <= ps and f > s:
                target, reason = (
                    ("LONG", "CROSS_UP") if snapshot.position == 0 else ("HOLD", "ALREADY_LONG")
                )
            elif pf >= ps and f < s:
                target, reason = (
                    ("CASH", "CROSS_DOWN") if snapshot.position > 0 else ("HOLD", "NO_INVENTORY")
                )
            return Decision(instance, target, reason, *[format(x, "f") for x in (pf, ps, f, s)])
