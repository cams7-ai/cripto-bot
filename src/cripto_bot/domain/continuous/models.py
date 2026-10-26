"""Versioned local continuous state; legacy wallet transitions are not changed."""

from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import cast

from cripto_core.errors import PaperError
from cripto_core.numeric import ZERO, bounded, number
from cripto_core.serialization import record

from cripto_bot.domain.portfolio.models import Wallet

MODEL = "paper-continuous-v1"
STEP = 900000
DAY = 86400000
ACTIVE = frozenset({"RESERVED", "OPEN", "PARTIALLY_FILLED", "UNKNOWN"})


@dataclass
class Order:
    order_id: str
    owner: str
    side: str
    requested: Decimal
    cap: Decimal
    created_at: int
    earliest_at: int
    expires_at: int
    kind: str = "MARKET_PROXY"
    state: str = "OPEN"
    filled: Decimal = ZERO
    notional: Decimal = ZERO
    fees: Decimal = ZERO
    reserved: Decimal = ZERO
    spent: Decimal = ZERO
    last_signature: str = ""
    quote_ids: list[str] = field(default_factory=list)
    book_signatures: list[str] = field(default_factory=list)
    fills: dict[str, dict[str, object]] = field(default_factory=dict)
    last_poll: int = 0


@dataclass
class ContinuousState:
    wallet: Wallet = field(default_factory=Wallet)
    status: str = "PREPARED"
    pilot_id: str = ""
    owner: str = ""
    t0: int | None = None
    tend: int | None = None
    observation_kind: str = "fixture"
    context: dict[str, object] = field(default_factory=dict)
    parameters: dict[str, object] = field(default_factory=dict)
    configuration: dict[str, object] = field(default_factory=dict)
    rules: dict[str, object] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    cycles: dict[str, dict[str, object]] = field(default_factory=dict)
    controls: dict[str, dict[str, object]] = field(default_factory=dict)
    generations: dict[str, dict[str, object]] = field(default_factory=dict)
    daily: dict[str, dict[str, object]] = field(default_factory=dict)
    candles: list[dict[str, object]] = field(default_factory=list)
    late_slots: list[int] = field(default_factory=list)
    last_slot: int = -1
    last_mark_at: int = -1
    generation: int = 0
    epoch: int = 0
    disabled: bool = False
    killed: bool = False
    paused: bool = False
    metadata_at: int = -1

    def data(self) -> dict[str, object]:
        return cast(dict[str, object], record(self))

    def copy(self) -> "ContinuousState":
        return deepcopy(self)

    def pending(self) -> Order | None:
        return next((o for o in self.orders.values() if o.state in ACTIVE), None)

    def validate(self) -> None:
        w = self.wallet
        for value in [
            w.usdt_free,
            w.usdt_reserved,
            w.btc_free,
            w.btc_reserved,
            w.cost_basis,
            w.fees,
            w.peak,
            w.baseline,
        ]:
            if value < 0:
                raise PaperError("NEGATIVE_BALANCE")
            bounded(value)
        bounded(w.realized)
        if w.quantity == 0 and w.cost_basis != 0:
            raise PaperError("POSITION_DIVERGENCE")
        pending = [o for o in self.orders.values() if o.state in ACTIVE]
        if len(pending) > 1:
            raise PaperError("EXCLUSIVE_OWNER_CONFLICT")
        for asset in ["BUY", "SELL"]:
            reserve = sum((o.reserved for o in pending if o.side == asset), ZERO)
            if reserve != (w.usdt_reserved if asset == "BUY" else w.btc_reserved):
                raise PaperError("RESERVATION_DIVERGENCE")
        for order in self.orders.values():
            if not ZERO <= order.filled <= order.requested or order.reserved < 0:
                raise PaperError("ORDER_DIVERGENCE")
            if sum((number(f["quantity"]) for f in order.fills.values()), ZERO) != order.filled:
                raise PaperError("FILL_DIVERGENCE")
        if w.equity < 0:
            raise PaperError("NEGATIVE_EQUITY")
