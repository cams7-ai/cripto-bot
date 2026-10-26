"""Deterministic financial reducer: every partial fill and control is a journal event."""

from dataclasses import asdict
from decimal import Decimal, localcontext

from cripto_core.errors import PaperError
from cripto_core.execution import filters
from cripto_core.numeric import ZERO, money, number, text
from cripto_core.serialization import fingerprint
from cripto_core.sizing import sized_quantity
from cripto_core.strategy.sma import Snapshot, TrendSMA
from cripto_core.validation import identifier, require_object, sequence

from cripto_bot.domain.continuous.execution import (
    integer,
    partial_plan,
    plan_for,
    quote_at,
    signature,
)
from cripto_bot.domain.continuous.models import ACTIVE, DAY, STEP, ContinuousState, Order
from cripto_bot.domain.market import parse_candle
from cripto_bot.domain.portfolio.models import Posting, Wallet, day_at, validate_postings
from cripto_bot.domain.risk.limits import admit, update_risk


def candles(value: object) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for raw in sequence(value):
        row = require_object(raw)
        if row.get("symbol") != "BTCUSDT" or row.get("interval") != "15m":
            raise PaperError("INVALID_CANDLE_SCOPE")
        c = parse_candle(
            [
                row["open_time"],
                row["open"],
                row["high"],
                row["low"],
                row["close"],
                row["base_volume"],
                row["close_time"],
                row["quote_volume"],
                row["trades"],
                "0",
                "0",
                "0",
            ]
        )
        if result and c.open_time != int(str(result[-1]["open_time"])) + STEP:
            raise PaperError("DATA_GAP")
        result.append(c.record())
    return result


def mark(state: ContinuousState, quote: dict[str, object], at: int) -> None:
    quote = quote_at(quote, at, int(str(state.parameters["max_quote_age_ms"])))
    wallet = state.wallet
    wallet.mark = number(quote["bid"])
    wallet.clock = at
    state.last_mark_at = at
    if wallet.day != day_at(at):
        wallet.day = day_at(at)
        wallet.baseline = wallet.equity
        wallet.daily_blocked = False
    update_risk(wallet, state.parameters)


def release(
    state: ContinuousState, order: Order, terminal: str, *, resolve_unknown: bool = False
) -> None:
    w = state.wallet
    if order.state not in ACTIVE or (order.state == "UNKNOWN" and not resolve_unknown):
        return
    if order.side == "BUY":
        w.usdt_reserved -= order.reserved
        w.usdt_free += order.reserved
    else:
        w.btc_reserved -= order.reserved
        w.btc_free += order.reserved
    order.reserved = ZERO
    order.state = terminal


def add_order(state: ContinuousState, payload: dict[str, object], at: int) -> str:
    if state.pending() is not None:
        raise PaperError("PENDING_INTENT")
    if state.paused or state.disabled or state.killed:
        raise PaperError("CONTINUOUS_PAUSED")
    if state.t0 is None or state.tend is None or not state.t0 <= at < state.tend:
        raise PaperError("OUTSIDE_PILOT")
    if payload.get("kind") == "LIMIT" and state.observation_kind != "fixture":
        raise PaperError("DRILL_ONLY_LIMIT")
    key = identifier(payload["order_id"])
    side = str(payload["side"])
    quote = quote_at(payload["quote"], at)
    if key in state.orders:
        raise PaperError("DUPLICATE_ORDER")
    mark(state, quote, at)
    decision = integer(payload.get("decision_at", at - 1))
    plan = plan_for(
        side,
        number(payload["quantity"]),
        quote,
        at + 1000,
        decision,
        state.parameters,
        state.rules,
        key,
    )
    # The initial complete plan bounds capital and uses the existing admission policy.
    admit(state.wallet, plan, state.parameters)
    cap = plan.price
    if payload.get("kind", "MARKET_PROXY") == "LIMIT":
        cap = number(payload["limit_price"])
        if cap <= 0:
            raise PaperError("INVALID_PRICE")
        from cripto_core.execution import range_check

        supplied = filters(state.rules)
        tick = number(supplied["PRICE_FILTER"]["tickSize"])
        if tick and cap % tick:
            raise PaperError("FILTER_TICK")
        range_check(cap, supplied["PRICE_FILTER"], "minPrice", "maxPrice")
        if (side == "BUY" and cap > plan.price) or (side == "SELL" and cap < plan.price):
            raise PaperError("LIMIT_WIDENS_CAP")
    order = Order(
        key,
        state.owner,
        side,
        plan.quantity,
        cap,
        at,
        at + 1000,
        at + 60000,
        kind=str(payload.get("kind", "MARKET_PROXY")),
        reserved=plan.reserve,
    )
    if order.kind not in {"MARKET_PROXY", "LIMIT"}:
        raise PaperError("UNSUPPORTED_ORDER_KIND")
    if side == "BUY":
        state.wallet.usdt_free -= plan.reserve
        state.wallet.usdt_reserved += plan.reserve
    else:
        state.wallet.btc_free -= plan.reserve
        state.wallet.btc_reserved += plan.reserve
    state.orders[key] = order
    return key


def fill(state: ContinuousState, order: Order, quote: dict[str, object], at: int) -> list[Posting]:
    if order.state == "UNKNOWN":
        return []
    mark(state, quote, at)
    if order.side == "BUY":
        exposure = state.wallet.quantity * state.wallet.mark + state.wallet.usdt_reserved
        if (
            state.wallet.daily_blocked
            or state.wallet.drawdown_blocked
            or exposure > state.wallet.equity * number(state.parameters["max_exposure_fraction"])
        ):
            release(state, order, "CANCELED")
            return []
    plan = partial_plan(order, quote, at, state.parameters, state.rules)
    order.last_poll = at
    if plan is None:
        return []
    w = state.wallet
    postings = []

    def post(asset: str, debit: str, credit: str, amount: Decimal) -> None:
        postings.extend([Posting(asset, debit, -amount), Posting(asset, credit, amount)])

    if order.side == "BUY":
        spent = plan.notional + plan.fee
        if spent > order.reserved:
            raise PaperError("INSUFFICIENT_RESERVED")
        w.usdt_reserved -= spent
        order.reserved -= spent
        w.btc_free += plan.quantity
        w.cost_basis += spent
        order.spent += spent
        post("USDT", "wallet_reserved", "counterparty", plan.notional)
        post("USDT", "wallet_reserved", "fees", plan.fee)
        post("BTC", "counterparty", "wallet_free", plan.quantity)
        cost_delta = spent
        realized = ZERO
    else:
        if plan.quantity > order.reserved:
            raise PaperError("INSUFFICIENT_RESERVED")
        old_quantity = w.quantity
        cost = money(w.cost_basis * plan.quantity / old_quantity)
        if plan.quantity == old_quantity:
            cost = w.cost_basis
        w.btc_reserved -= plan.quantity
        order.reserved -= plan.quantity
        w.cost_basis -= cost
        w.usdt_free += plan.notional - plan.fee
        realized = plan.notional - plan.fee - cost
        w.realized += realized
        cost_delta = -cost
        post("BTC", "wallet_reserved", "counterparty", plan.quantity)
        post("USDT", "counterparty", "wallet_free", plan.notional)
        post("USDT", "wallet_free", "fees", plan.fee)
    order.filled += plan.quantity
    order.notional += plan.notional
    order.fees += plan.fee
    w.fees += plan.fee
    fid = fingerprint({"order": order.order_id, "quote": quote["quote_id"]})
    if fid in order.fills:
        raise PaperError("DUPLICATE_FILL")
    order.fills[fid] = {
        "fill_id": fid,
        "order_id": order.order_id,
        "owner": order.owner,
        "side": order.side,
        "quantity": text(plan.quantity),
        "price": text(plan.price),
        "notional": text(plan.notional),
        "fee": text(plan.fee),
        "cost_delta": text(cost_delta),
        "realized_delta": text(realized),
        "at": at,
        "quote_id": quote["quote_id"],
    }
    order.quote_ids.append(str(quote["quote_id"]))
    order.last_signature = signature(quote)
    order.book_signatures.append(order.last_signature)
    if order.filled == order.requested:
        release(state, order, "FILLED")
    else:
        order.state = "PARTIALLY_FILLED"
    update_risk(w, state.parameters)
    validate_postings(postings)
    return postings


def reduce(
    original: ContinuousState | None, kind: str, payload: dict[str, object]
) -> tuple[ContinuousState, list[Posting]]:
    with localcontext() as context:
        context.prec = 80
        state = original.copy() if original else ContinuousState()
        at = integer(payload["at"])
        posts: list[Posting] = []
        if at < state.wallet.clock:
            raise PaperError("NON_MONOTONIC_TIME")
        if kind == "PREPARE":
            if original is not None:
                raise PaperError("ALREADY_PREPARED")
            state.context = require_object(payload["context"])
            state.parameters = require_object(state.context["parameters"])
            state.configuration = require_object(state.context["configuration"])
            state.pilot_id = identifier(state.context["pilot_id"])
            state.owner = identifier(state.context["owner"])
            state.observation_kind = str(state.context["source"])
            state.generations["0"] = {
                "configuration": state.configuration,
                "config_hash": fingerprint(state.configuration),
            }
        elif original is None:
            raise PaperError("MISSING_GENESIS")
        elif kind == "ARM":
            if state.t0 is not None:
                raise PaperError("ALREADY_ARMED")
            state.rules = require_object(payload["rules"])
            filters(state.rules)
            state.candles = candles(payload["candles"])
            if len(state.candles) != 50:
                raise PaperError("WARMUP_REQUIRED")
            t0 = integer(payload["t0"])
            if t0 % STEP or at >= t0:
                raise PaperError("INVALID_PILOT_WINDOW")
            if int(str(state.candles[-1]["close_time"])) >= at:
                raise PaperError("FUTURE_WARMUP")
            state.t0 = t0
            state.tend = t0 + 7 * DAY
            state.status = "ARMED"
            state.metadata_at = integer(payload["metadata_at"])
            if not at - 3600000 <= state.metadata_at <= at:
                raise PaperError("STALE_METADATA")
            state.wallet.usdt_free = number(state.parameters["initial_usdt"])
            mark(state, require_object(payload["quote"]), at)
            state.wallet.peak = state.wallet.equity
            state.wallet.baseline = state.wallet.equity
            state.last_slot = t0 - STEP
        elif kind in {"CYCLE", "MISSED"}:
            close = integer(payload["close"])
            if (
                state.t0 is None
                or state.tend is None
                or not state.t0 <= close < state.tend
                or close % STEP
            ):
                raise PaperError("INVALID_SLOT")
            if close != state.last_slot + STEP:
                raise PaperError("SLOT_GAP")
            state.last_slot = close
            item: dict[str, object] = {
                "close": close,
                "at": at,
                "decision_id": fingerprint(
                    {"pilot": state.pilot_id, "close": close, "generation": state.generation}
                ),
                "generation": state.generation,
                "target": "HOLD",
                "reason": "MISSED",
                "valid": False,
            }
            if kind == "MISSED":
                item["reason"] = str(payload.get("reason", "MISSED"))
            else:
                if not close + 2000 <= at <= close + 90000:
                    raise PaperError("SLOT_DEADLINE")
                incoming = candles(payload["candles"])
                if not incoming or int(str(incoming[-1]["open_time"])) != close - STEP:
                    raise PaperError("CANDLE_NOT_CLOSED")
                merged = {int(str(c["open_time"])): c for c in [*state.candles, *incoming]}
                state.candles = candles([merged[k] for k in sorted(merged)][-51:])
                if len(state.candles) != 51:
                    raise PaperError("WARMUP_REQUIRED")
                if not at - 3600000 <= state.metadata_at <= at:
                    raise PaperError("STALE_METADATA")
                mark(state, require_object(payload["quote"]), at)
                decision = TrendSMA().evaluate(
                    Snapshot(
                        tuple(number(c["close"]) for c in state.candles),
                        close,
                        state.wallet.quantity,
                    ),
                    20,
                    50,
                    state.owner,
                )
                item.update(asdict(decision))
                item["valid"] = True
                if state.paused or state.disabled or state.killed:
                    item["reason"] = "CUSTODY"
                    item["target"] = "HOLD"
                elif state.pending():
                    item["reason"] = "PENDING_ORDER"
                    item["target"] = "HOLD"
                elif decision.target != "HOLD":
                    quote = require_object(payload["quote"])
                    module = require_object(state.configuration["module"])
                    qty = (
                        state.wallet.btc_free
                        if decision.target == "CASH"
                        else sized_quantity(
                            state.wallet.usdt_free,
                            state.wallet.equity,
                            module,
                            quote,
                            at,
                            state.parameters,
                            state.rules,
                        )
                    )
                    try:
                        item["order_id"] = add_order(
                            state,
                            {
                                "order_id": item["decision_id"],
                                "side": "SELL" if decision.target == "CASH" else "BUY",
                                "quantity": text(qty),
                                "quote": quote,
                                "decision_at": close,
                            },
                            at,
                        )
                    except PaperError as error:
                        item["execution_reason"] = str(error)
                        item["reason"] = "RISK_REJECTED"
            state.cycles[str(close)] = item
            if not state.paused and not state.killed:
                state.status = "DRAINING" if state.disabled else "RUNNING"
        elif kind == "ADMIT":
            if state.t0 is None:
                raise PaperError("NOT_ARMED")
            add_order(state, payload, at)
        elif kind == "POLL":
            order = state.pending()
            if order is None and payload.get("quote") is not None:
                mark(state, require_object(payload["quote"]), at)
            if order:
                if order.state == "UNKNOWN":
                    pass
                elif at >= order.expires_at:
                    release(state, order, "EXPIRED")
                elif at - order.last_poll >= 5000:
                    if state.paused or state.killed or state.disabled:
                        release(state, order, "CANCELED")
                    else:
                        posts = fill(state, order, require_object(payload["quote"]), at)
        elif kind == "LATE_COMMIT":
            close = integer(payload["close"])
            if str(close) not in state.cycles:
                raise PaperError("UNKNOWN_SLOT")
            if close not in state.late_slots:
                state.late_slots.append(close)
            state.paused = True
            state.status = "PAUSED"
            order = state.pending()
            if order:
                release(state, order, "CANCELED")
        elif kind == "METADATA":
            rules = require_object(payload["rules"])
            filters(rules)
            if state.rules and fingerprint(rules) != fingerprint(state.rules):
                state.paused = True
                state.status = "PAUSED"
                order = state.pending()
                if order:
                    release(state, order, "CANCELED")
            else:
                state.rules = rules
                state.metadata_at = integer(payload.get("metadata_at", at))
        elif kind == "CONTROL":
            request = identifier(payload["request_id"])
            action = str(payload["action"])
            if request in state.controls:
                raise PaperError("DUPLICATE_CONTROL")
            if action in {"pause", "kill", "disable"}:
                state.paused = True
                state.killed = state.killed or action == "kill"
                state.disabled = state.disabled or action == "disable"
                order = state.pending()
                if order:
                    release(state, order, "CANCELED")
                state.status = (
                    "KILLED"
                    if state.killed
                    else "DRAINING"
                    if state.disabled and state.wallet.quantity
                    else "PAUSED"
                )
            elif action in {"resume", "reload"}:
                pending = state.pending()
                if pending and pending.state == "UNKNOWN":
                    raise PaperError("UNRESOLVED_ORDER")
                if state.wallet.daily_blocked or state.wallet.drawdown_blocked:
                    raise PaperError("RISK_LOCKED")
                state.generation += 1
                state.epoch += 1
                state.paused = False
                state.killed = False
                state.disabled = False
                state.status = "RUNNING" if state.t0 else "PREPARED"
                state.generations[str(state.generation)] = {
                    "configuration": state.configuration,
                    "config_hash": fingerprint(state.configuration),
                }
            elif action == "cancel":
                key = identifier(payload["order_id"])
                if key not in state.orders:
                    raise PaperError("UNKNOWN_ORDER")
                release(state, state.orders[key], "CANCELED", resolve_unknown=True)
            else:
                raise PaperError("INVALID_CONTROL")
            state.controls[request] = {
                "action": action,
                "at": at,
                "generation": state.generation,
                "epoch": state.epoch,
            }
        elif kind == "UNKNOWN":
            order = state.pending()
            if order:
                order.state = "UNKNOWN"
                state.paused = True
                state.status = "PAUSED"
        elif kind == "DAILY":
            key = str(payload["day"])
            if key in state.daily:
                raise PaperError("DUPLICATE_DAY")
            state.daily[key] = {"at": at, "day": key}
        elif kind == "END":
            if state.tend is None or at < state.tend:
                raise PaperError("PILOT_NOT_COMPLETE")
            order = state.pending()
            if order and order.state != "UNKNOWN":
                release(state, order, "EXPIRED")
            state.status = "END_BLOCKED" if order and order.state == "UNKNOWN" else "ENDED"
            state.paused = True
        else:
            raise PaperError("UNKNOWN_TRANSITION")
        state.wallet.clock = at
        state.wallet.version += 1
        state.wallet.cursor += 1
        state.validate()
        before = original.wallet if original else Wallet()
        posts = []
        for asset, names in [
            ("USDT", ("usdt_free", "usdt_reserved")),
            ("BTC", ("btc_free", "btc_reserved")),
        ]:
            total = ZERO
            for name in names:
                delta = getattr(state.wallet, name) - getattr(before, name)
                if delta:
                    posts.append(Posting(asset, name, delta))
                    total += delta
            if asset == "USDT":
                fee = state.wallet.fees - before.fees
                if fee:
                    posts.append(Posting(asset, "fees", fee))
                    total += fee
            if total:
                posts.append(Posting(asset, "clearing", -total))
        validate_postings(posts)
        return state, posts
