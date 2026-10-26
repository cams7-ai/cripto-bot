"""Transicoes deterministicas: a mesma funcao reconstrói o historico."""

from copy import deepcopy
from decimal import Decimal, localcontext

from cripto_bot.domain.portfolio.models import (
    ZERO,
    Intent,
    PaperError,
    Posting,
    Wallet,
    day_at,
    fingerprint,
    instant,
    money,
    number,
    validate_postings,
)
from cripto_bot.domain.risk.execution import make_plan, observation
from cripto_bot.domain.risk.limits import admit, update_risk


def transition(
    previous: Wallet,
    kind: str,
    payload: dict[str, object],
    parameters: dict[str, object],
    rules: dict[str, object],
) -> tuple[Wallet, list[Posting]]:
    with localcontext() as context:
        context.prec = 80
        wallet = deepcopy(previous)
        postings: list[Posting] = []

        def post(asset: str, account: str, quantity: Decimal) -> None:
            amount = quantity
            postings.append(Posting(asset, account, amount))

        if kind == "GENESIS":
            if previous.version:
                raise PaperError("DUPLICATE_GENESIS")
            initial = number(parameters["initial_usdt"])
            wallet.clock = instant(payload["timestamp"])
            wallet.usdt_free = initial
            wallet.peak = initial
            post("USDT", "free", initial)
            post("USDT", "capital", -initial)
        elif kind in ("DAY_START", "MARK", "ORDER"):
            at = instant(payload.get("timestamp"))
            if at < wallet.clock:
                raise PaperError("NON_MONOTONIC_REPLAY")
            if any(item.state == "RESERVED" for item in wallet.intents.values()):
                raise PaperError("PENDING_INTENT")
            wallet.clock = at
            wallet.cursor += 1
            if kind in ("DAY_START", "MARK"):
                _, wallet.mark = observation(
                    payload.get("observation"), at, int(str(parameters["max_quote_age_ms"]))
                )
                if kind == "DAY_START":
                    if at % 86400000 or wallet.day == day_at(at):
                        raise PaperError("DUPLICATE_OR_INVALID_DAY")
                    wallet.day = day_at(at)
                    wallet.baseline = wallet.equity
                    wallet.daily_blocked = False
                update_risk(wallet, parameters)
            else:
                action = str(payload["action_key"])
                if action in wallet.intents:
                    raise PaperError("ACTION_CONFLICT")
                item = Intent("RISK_REJECTED", fingerprint(payload))
                try:
                    plan = make_plan(payload, action, parameters, rules)
                    wallet.mark = plan.mark
                    update_risk(wallet, parameters)
                    admit(wallet, plan, parameters)
                except PaperError as error:
                    item.reason = str(error)
                else:
                    item.state = "RESERVED"
                    item.plan = plan
                    item.reserved = plan.reserve
                    if plan.asset == "USDT":
                        wallet.usdt_free -= plan.reserve
                        wallet.usdt_reserved += plan.reserve
                    else:
                        wallet.btc_free -= plan.reserve
                        wallet.btc_reserved += plan.reserve
                    post(plan.asset, "free", -plan.reserve)
                    post(plan.asset, "reserved", plan.reserve)
                wallet.intents[action] = item
        elif kind in ("FILL", "CANCEL", "REJECT"):
            action = str(payload["action_key"])
            if action not in wallet.intents:
                raise PaperError("UNKNOWN_INTENT")
            intent = wallet.intents[action]
            if intent.state != "RESERVED" or intent.plan is None:
                raise PaperError("INTENT_NOT_RESERVED")
            plan = intent.plan
            if kind == "FILL":
                if payload.get("fill_id") != plan.fill_id or wallet.clock > plan.fill_at:
                    raise PaperError("FILL_CONFLICT")
                wallet.clock = plan.fill_at
                remainder = intent.reserved - plan.reserve
                if remainder < 0:
                    raise PaperError("INSUFFICIENT_RESERVE")
                if plan.side == "BUY":
                    wallet.usdt_reserved -= intent.reserved
                    wallet.usdt_free += remainder
                    wallet.btc_free += plan.quantity
                    wallet.cost_basis += plan.notional + plan.fee
                    post("USDT", "reserved", -intent.reserved)
                    post("USDT", "free", remainder)
                    post("USDT", "market", plan.notional)
                    post("USDT", "fees", plan.fee)
                    post("BTC", "free", plan.quantity)
                    post("BTC", "market", -plan.quantity)
                else:
                    old_quantity = wallet.quantity
                    cost = (
                        wallet.cost_basis
                        if plan.quantity == old_quantity
                        else money(wallet.cost_basis * plan.quantity / old_quantity)
                    )
                    wallet.btc_reserved -= intent.reserved
                    wallet.btc_free += remainder
                    wallet.usdt_free += plan.notional - plan.fee
                    wallet.cost_basis -= cost
                    wallet.realized += plan.notional - plan.fee - cost
                    post("BTC", "reserved", -intent.reserved)
                    post("BTC", "free", remainder)
                    post("BTC", "market", plan.quantity)
                    post("USDT", "free", plan.notional - plan.fee)
                    post("USDT", "market", -plan.notional)
                    post("USDT", "fees", plan.fee)
                wallet.fees += plan.fee
                intent.state = "FILLED"
            else:
                intent.state = "CANCELED" if kind == "CANCEL" else "REJECTED"
                if plan.asset == "USDT":
                    wallet.usdt_reserved -= intent.reserved
                    wallet.usdt_free += intent.reserved
                else:
                    wallet.btc_reserved -= intent.reserved
                    wallet.btc_free += intent.reserved
                post(plan.asset, "reserved", -intent.reserved)
                post(plan.asset, "free", intent.reserved)
            intent.reserved = ZERO
            update_risk(wallet, parameters)
        else:
            raise PaperError("UNKNOWN_TRANSITION")
        wallet.version += 1
        validate_postings(postings)
        wallet.validate()
        return wallet, postings
