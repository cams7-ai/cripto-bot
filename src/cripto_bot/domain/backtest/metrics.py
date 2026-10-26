"Metricas online na serie completa; geometria do grafico nao interfere no calculo."

from dataclasses import dataclass, fields
from decimal import ROUND_HALF_EVEN, Decimal

from cripto_bot.domain.portfolio.models import ZERO, Wallet, money, text


def ratio_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.0000000000000001"), rounding=ROUND_HALF_EVEN), "f")


@dataclass
class Metrics:
    initial: Decimal
    start: int
    last_at: int
    peak: Decimal
    maximum_drawdown: Decimal = ZERO
    maximum_exposure: Decimal = ZERO
    exposure_area: Decimal = ZERO
    position_ms: int = 0
    last_exposure: Decimal = ZERO
    last_position: bool = False
    turnover_notional: Decimal = ZERO
    fills: int = 0
    closed_trades: int = 0
    decisions: int = 0
    directional: int = 0
    holds: int = 0
    rejected: int = 0
    expired: int = 0
    price_impact: Decimal = ZERO

    def update(self, wallet: Wallet, at: int) -> dict[str, object]:
        elapsed = at - self.last_at
        if elapsed < 0:
            raise ValueError("NON_MONOTONIC_METRIC_TIME")
        self.exposure_area += self.last_exposure * elapsed
        self.position_ms += elapsed if self.last_position else 0
        self.last_at = at
        equity = wallet.equity
        exposure = (
            (wallet.quantity * wallet.mark + wallet.usdt_reserved) / equity if equity else ZERO
        )
        self.peak = max(self.peak, equity)
        dd = (self.peak - equity) / self.peak if self.peak else ZERO
        self.maximum_drawdown = max(self.maximum_drawdown, dd)
        self.maximum_exposure = max(self.maximum_exposure, exposure)
        self.last_exposure = exposure
        self.last_position = wallet.quantity > 0
        return {
            "at": at,
            "mark": text(wallet.mark),
            "equity": text(equity),
            "usdt_free": text(wallet.usdt_free),
            "usdt_reserved": text(wallet.usdt_reserved),
            "btc": text(wallet.quantity),
            "cost_basis": text(wallet.cost_basis),
            "fees": text(wallet.fees),
            "realized": text(wallet.realized),
            "unrealized": text(money(wallet.quantity * wallet.mark) - wallet.cost_basis),
            "exposure": ratio_text(exposure),
            "drawdown": ratio_text(dd),
        }

    def data(self) -> dict[str, object]:
        return {
            f.name: format(getattr(self, f.name), "f")
            if isinstance(getattr(self, f.name), Decimal)
            else getattr(self, f.name)
            for f in fields(self)
        }

    def summary(self, wallet: Wallet) -> dict[str, object]:
        duration = self.last_at - self.start
        return {
            "initial_equity": text(self.initial),
            "final_equity": text(wallet.equity),
            "net_return": ratio_text(wallet.equity / self.initial - 1),
            "realized": text(wallet.realized),
            "unrealized": text(money(wallet.quantity * wallet.mark) - wallet.cost_basis),
            "fees": text(wallet.fees),
            "price_impact": text(self.price_impact),
            "maximum_drawdown": ratio_text(self.maximum_drawdown),
            "maximum_exposure": ratio_text(self.maximum_exposure),
            "average_exposure": ratio_text(self.exposure_area / duration) if duration else None,
            "time_in_position": ratio_text(Decimal(self.position_ms) / duration)
            if duration
            else None,
            "turnover": ratio_text(self.turnover_notional / self.initial),
            "fills": self.fills,
            "closed_trades": self.closed_trades,
            "decisions": self.decisions,
            "directional_decisions": self.directional,
            "holds": self.holds,
            "rejected": self.rejected,
            "expired": self.expired,
            "residual_btc": text(wallet.quantity),
            "cost_basis": text(wallet.cost_basis),
            "terminal_policy": "mark_to_market",
            "daily_blocked": wallet.daily_blocked,
            "drawdown_blocked": wallet.drawdown_blocked,
        }
