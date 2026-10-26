"""Risco de admissao e marcacao, sempre em Decimal."""

from decimal import Decimal

from cripto_bot.domain.portfolio.models import PaperError, Plan, Wallet, day_at, number


def update_risk(wallet: Wallet, parameters: dict[str, object]) -> None:
    wallet.peak = max(wallet.peak, wallet.equity)
    if wallet.day == day_at(wallet.clock) and wallet.baseline > 0:
        if wallet.baseline - wallet.equity >= wallet.baseline * number(
            parameters["max_daily_loss_fraction"]
        ):
            wallet.daily_blocked = True
    if wallet.peak > 0 and wallet.peak - wallet.equity >= wallet.peak * number(
        parameters["max_drawdown_fraction"]
    ):
        wallet.drawdown_blocked = True


def admit(wallet: Wallet, plan: Plan, parameters: dict[str, object]) -> None:
    if any(item.state == "RESERVED" for item in wallet.intents.values()):
        raise PaperError("PENDING_INTENT")
    if wallet.day != day_at(plan.observed_at) or wallet.day != day_at(plan.fill_at):
        raise PaperError("MISSING_DAY_START")
    if plan.side == "SELL":
        if plan.quantity > wallet.btc_free:
            raise PaperError("INSUFFICIENT_BTC")
        return
    if wallet.daily_blocked or wallet.drawdown_blocked:
        raise PaperError("RISK_LOCKED")
    if wallet.quantity > 0:
        raise PaperError("POSITION_ALREADY_OPEN")
    if plan.reserve > wallet.usdt_free:
        raise PaperError("INSUFFICIENT_USDT")
    if wallet.equity <= 0 or plan.reserve > wallet.equity * number(
        parameters["max_entry_fraction"]
    ):
        raise PaperError("ENTRY_LIMIT")
    exposure = max(plan.quantity * plan.mark, plan.reserve)
    if exposure > wallet.equity * number(parameters["max_exposure_fraction"]):
        raise PaperError("EXPOSURE_LIMIT")


def ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    return numerator / denominator if denominator else Decimal(0)
