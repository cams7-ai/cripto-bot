"""Valores, estados e postings puramente locais da carteira PAPER."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import cast

from cripto_core.contracts import (
    Plan as Plan,
)
from cripto_core.errors import (
    PaperError as PaperError,
)
from cripto_core.numeric import (
    ZERO as ZERO,
)
from cripto_core.numeric import (
    Q as Q,
)
from cripto_core.numeric import (
    bounded as bounded,
)
from cripto_core.numeric import (
    money as money,
)
from cripto_core.numeric import (
    number as number,
)
from cripto_core.numeric import (
    text as text,
)
from cripto_core.serialization import (
    fingerprint as fingerprint,
)
from cripto_core.serialization import (
    record as record,
)
from cripto_core.validation import (
    identifier as identifier,
)
from cripto_core.validation import (
    instant as instant,
)
from cripto_core.validation import (
    require_object as require_object,
)

from cripto_bot.domain.market import EPOCH

MODEL = "paper-integral-v1"


def day_at(milliseconds: int) -> str:
    from datetime import timedelta

    return (EPOCH + timedelta(milliseconds=milliseconds)).date().isoformat()


@dataclass
class Intent:
    state: str
    payload_hash: str
    plan: Plan | None = None
    reason: str = ""
    reserved: Decimal = ZERO


@dataclass
class Wallet:
    usdt_free: Decimal = ZERO
    usdt_reserved: Decimal = ZERO
    btc_free: Decimal = ZERO
    btc_reserved: Decimal = ZERO
    cost_basis: Decimal = ZERO
    fees: Decimal = ZERO
    realized: Decimal = ZERO
    mark: Decimal = ZERO
    peak: Decimal = ZERO
    baseline: Decimal = ZERO
    day: str = ""
    daily_blocked: bool = False
    drawdown_blocked: bool = False
    clock: int = 0
    version: int = 0
    cursor: int = 0
    intents: dict[str, Intent] = field(default_factory=dict)

    @property
    def quantity(self) -> Decimal:
        return self.btc_free + self.btc_reserved

    @property
    def equity(self) -> Decimal:
        return self.usdt_free + self.usdt_reserved + money(self.quantity * self.mark)

    def data(self) -> dict[str, object]:
        return cast(dict[str, object], record(self))

    def validate(self) -> None:
        for value in (
            self.usdt_free,
            self.usdt_reserved,
            self.btc_free,
            self.btc_reserved,
            self.cost_basis,
            self.fees,
            self.peak,
            self.baseline,
        ):
            if value < 0:
                raise PaperError("NEGATIVE_BALANCE")
            bounded(value)
        bounded(self.realized)
        if (self.quantity == 0 and self.cost_basis != 0) or self.equity < 0:
            raise PaperError("POSITION_DIVERGENCE")
        pending = [item for item in self.intents.values() if item.state == "RESERVED"]
        if len(pending) > 1:
            raise PaperError("RESERVATION_DIVERGENCE")
        for asset in ("BTC", "USDT"):
            reserved = sum(
                (item.reserved for item in pending if item.plan and item.plan.asset == asset), ZERO
            )
            if reserved != (self.btc_reserved if asset == "BTC" else self.usdt_reserved):
                raise PaperError("RESERVATION_DIVERGENCE")


@dataclass(frozen=True)
class Posting:
    asset: str
    account: str
    quantity: Decimal


def validate_postings(postings: list[Posting]) -> None:
    for asset in ("BTC", "USDT"):
        if sum((p.quantity for p in postings if p.asset == asset), ZERO) != 0:
            raise PaperError("UNBALANCED_LEDGER")
    if any(p.asset not in ("BTC", "USDT") for p in postings):
        raise PaperError("INVALID_ASSET")
