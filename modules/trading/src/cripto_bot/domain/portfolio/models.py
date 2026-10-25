"""Valores, estados e postings puramente locais da carteira PAPER."""

import re
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import cast

from cripto_bot.domain.market import EPOCH, canonical, digest, mapping

MODEL = "paper-integral-v1"
Q = Decimal("0.00000001")
ZERO = Decimal("0")


class PaperError(ValueError):
    """Somente codigos locais, sem transportar valores fornecidos."""


def number(value: object, *, signed: bool = False) -> Decimal:
    pattern = r"-?[0-9]{1,18}(?:\.[0-9]{1,8})?" if signed else r"[0-9]{1,18}(?:\.[0-9]{1,8})?"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise PaperError("INVALID_DECIMAL")
    return Decimal(value)


def text(value: Decimal) -> str:
    result = format(value.quantize(Q, rounding=ROUND_HALF_EVEN), "f")
    number(result, signed=True)
    return result


def bounded(value: Decimal) -> Decimal:
    if value != value.quantize(Q) or abs(value) >= Decimal("1000000000000000000"):
        raise PaperError("DECIMAL_RANGE")
    return value


def money(value: Decimal, *, up: bool = False) -> Decimal:
    return bounded(value.quantize(Q, rounding=ROUND_CEILING if up else ROUND_FLOOR))


def record(value: object) -> object:
    if isinstance(value, Decimal):
        return text(value)
    if is_dataclass(value) and not isinstance(value, type):
        return record(asdict(value))
    if isinstance(value, dict):
        return {key: record(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [record(item) for item in value]
    return value


def fingerprint(value: object) -> str:
    return digest(canonical(record(value)).encode())


def identifier(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is None:
        raise PaperError("INVALID_ID")
    return value


def instant(value: object) -> int:
    if not isinstance(value, str):
        raise PaperError("INVALID_TIME")
    try:
        dt = datetime.fromisoformat(value)
        if dt.utcoffset() is None:
            raise ValueError
        delta = dt.astimezone(UTC) - EPOCH
        result = delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000
        if result < 0 or delta.microseconds % 1000:
            raise ValueError
        return result
    except (ValueError, OverflowError):
        raise PaperError("INVALID_TIME") from None


def day_at(milliseconds: int) -> str:
    from datetime import timedelta

    return (EPOCH + timedelta(milliseconds=milliseconds)).date().isoformat()


@dataclass(frozen=True)
class Plan:
    action: str
    side: str
    requested: Decimal
    quantity: Decimal
    price: Decimal
    notional: Decimal
    fee: Decimal
    mark: Decimal
    fill_at: int
    observed_at: int

    @property
    def asset(self) -> str:
        return "USDT" if self.side == "BUY" else "BTC"

    @property
    def reserve(self) -> Decimal:
        return self.notional + self.fee if self.side == "BUY" else self.quantity

    @property
    def fill_id(self) -> str:
        return fingerprint({"model": MODEL, "plan": record(self)})


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


def require_object(value: object) -> dict[str, object]:
    try:
        return mapping(value)
    except ValueError:
        raise PaperError("INVALID_PAYLOAD") from None
