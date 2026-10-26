"""Redutor historico causal com janela e intencao ativa limitadas."""

from copy import copy, deepcopy
from dataclasses import asdict, dataclass, field, fields, replace
from decimal import ROUND_FLOOR, Decimal, localcontext

from cripto_bot.domain.backtest.metrics import Metrics
from cripto_bot.domain.backtest.models import fingerprint
from cripto_bot.domain.market import STEP, iso
from cripto_bot.domain.portfolio.engine import transition
from cripto_bot.domain.portfolio.models import (
    PaperError,
    Posting,
    Wallet,
    day_at,
    money,
    number,
    record,
    require_object,
    text,
)
from cripto_bot.domain.risk.execution import execution_price, filters, observation, quantity_step
from cripto_bot.domain.strategy.sma import Snapshot, TrendSMA


class _HistoricalWallet(Wallet):
    """Copia escalares financeiros imutaveis e isola toda arvore mutavel."""

    def __deepcopy__(self, memo: dict[int, object]) -> "_HistoricalWallet":
        result = copy(self)
        memo[id(self)] = result
        for name, value in vars(self).items():
            if not isinstance(value, (str, int, Decimal, type(None))):
                setattr(result, name, deepcopy(value, memo))
        return result


@dataclass
class State:
    wallet: Wallet = field(default_factory=_HistoricalWallet)
    window: list[str] = field(default_factory=list)
    prefix: str = "0" * 64
    pending: dict[str, object] | None = None
    sequence: int = 0
    last_boundary: int = -1
    last_observed: int = -1
    last_closed: int = -1
    finished: bool = False
    metrics: Metrics | None = None
    max_window: int = 0

    def data(self) -> dict[str, object]:
        return {
            "wallet": record(
                {item.name: getattr(self.wallet, item.name) for item in fields(self.wallet)}
            ),
            "window": self.window,
            "prefix": self.prefix,
            "pending": self.pending,
            "sequence": self.sequence,
            "last_boundary": self.last_boundary,
            "last_observed": self.last_observed,
            "last_closed": self.last_closed,
            "finished": self.finished,
            "metrics": self.metrics.data() if self.metrics else None,
            "max_window": self.max_window,
        }


@dataclass
class Effect:
    state: State
    postings: list[Posting] = field(default_factory=list)
    decision: dict[str, object] | None = None
    intent: dict[str, object] | None = None
    fill: dict[str, object] | None = None
    outcome: dict[str, object] | None = None
    equity: dict[str, object] | None = None
    reference: str = ""


def _close(state: State, candle: dict[str, object], slow: int) -> None:
    opened = int(str(candle["open_time"]))
    if state.last_closed >= 0 and opened != state.last_closed + STEP:
        raise PaperError("PREFIX_GAP")
    state.prefix = fingerprint([state.prefix, candle])
    number(candle["close"])
    state.window.append(str(candle["close"]))
    if len(state.window) > slow + 1:
        state.window.pop(0)
    state.max_window = max(state.max_window, len(state.window))
    state.last_closed = opened


def _core(
    effect: Effect, kind: str, payload: dict[str, object], context: dict[str, object]
) -> None:
    wallet, postings = transition(
        effect.state.wallet,
        kind,
        payload,
        require_object(context["parameters"]),
        require_object(context["rules"]),
    )
    effect.state.wallet = wallet
    effect.postings += postings


def _quote(at: int, price: object) -> dict[str, object]:
    return {
        "kind": "REFERENCE",
        "quote_id": "historical-reference",
        "symbol": "BTCUSDT",
        "observed_at": iso(at),
        "price": price,
    }


def _mark(effect: Effect, at: int, price: object, context: dict[str, object]) -> None:
    _core(effect, "MARK", {"timestamp": iso(at), "observation": _quote(at, price)}, context)


def reduce(
    previous: State, kind: str, payload: dict[str, object], context: dict[str, object]
) -> Effect:
    with localcontext() as decimal_context:
        decimal_context.prec = 80
        state = replace(
            previous,
            window=previous.window.copy(),
            metrics=replace(previous.metrics) if previous.metrics else None,
        )
        effect = Effect(state)
        params = require_object(context["parameters"])
        module = require_object(context["module"])
        slow = int(str(module["slow_window"]))
        start = int(str(context["start"]))
        end = int(str(context["end"]))
        profile = str(context["profile"])
        delay = int(str(context["delay_candles"]))
        if state.finished:
            raise PaperError("RUN_ALREADY_FINISHED")
        if kind == "INIT":
            if state.sequence:
                raise PaperError("DUPLICATE_INIT")
            warmup = payload["warmup"]
            if not isinstance(warmup, list) or len(warmup) != slow:
                raise PaperError("INVALID_WARMUP")
            for raw in warmup:
                _close(state, require_object(raw), slow)
            if state.last_closed != start - STEP:
                raise PaperError("INVALID_WARMUP")
            midnight = start // 86400000 * 86400000
            _core(effect, "GENESIS", {"timestamp": iso(midnight)}, context)
            _core(
                effect,
                "DAY_START",
                {
                    "timestamp": iso(midnight),
                    "observation": _quote(midnight, payload["baseline_price"]),
                },
                context,
            )
            _mark(effect, start, state.window[-1], context)
            initial = number(params["initial_usdt"])
            state.metrics = Metrics(initial, start, start, initial)
        elif kind == "BOUNDARY":
            at = int(str(payload["at"]))
            closed = require_object(payload["closed"])
            if (
                not start <= at < end
                or at <= state.last_boundary
                or int(str(closed["available_at"])) != at
            ):
                raise PaperError("INVALID_BOUNDARY")
            if state.last_boundary >= 0 and at != state.last_boundary + STEP:
                raise PaperError("BOUNDARY_GAP")
            if state.wallet.intents:
                raise PaperError("UNRESOLVED_FINANCIAL_INTENT")
            if at != start:
                _close(state, closed, slow)
            elif (
                closed["close"] != state.window[-1]
                or int(str(closed["open_time"])) != state.last_closed
            ):
                raise PaperError("PREFIX_CONFLICT")
            if state.wallet.day != day_at(at):
                if at % 86400000:
                    raise PaperError("MISSING_DAY_START")
                _core(
                    effect,
                    "DAY_START",
                    {"timestamp": iso(at), "observation": _quote(at, closed["close"])},
                    context,
                )
            _mark(effect, at, closed["close"], context)
            state.last_boundary = at
            target, reason = "HOLD", "REFERENCE_HOLD"
            details: dict[str, object] = {}
            if profile == "sma" and at > start:
                decision = TrendSMA().evaluate(
                    Snapshot(tuple(Decimal(c) for c in state.window), at, state.wallet.quantity),
                    int(str(module["fast_window"])),
                    slow,
                    str(module["instance_id"]),
                )
                details = asdict(decision)
                target = decision.target
                reason = decision.reason
            elif profile.startswith("buy_hold") and at == start:
                target, reason = "LONG", "BUY_HOLD_ENTRY"
            if profile != "sma" or at > start:
                assert state.metrics is not None
                state.metrics.decisions += 1
                state.metrics.holds += target == "HOLD"
                state.metrics.directional += target != "HOLD"
                content_id = fingerprint(
                    {
                        "module": module,
                        "profile": profile,
                        "prefix": state.prefix,
                        "at": at,
                        "position": text(state.wallet.quantity),
                        "target": target,
                    }
                )
                action = fingerprint([context["run_key"], content_id])
                envelope = {
                    "decision_id": action,
                    "content_id": content_id,
                    "module_id": "trend_sma" if profile == "sma" else profile,
                    "instance_id": module["instance_id"],
                    "implementation_version": "1",
                    "schema_version": 1,
                    "config_hash": context["config_hash"],
                    "config_generation": 1,
                    "state_version": previous.sequence,
                    "environment": "local",
                    "mode": "PAPER",
                    "evaluation_type": "backtest",
                    "wallet_id": params["wallet_id"],
                    "dataset_id": context["dataset_id"],
                    "prefix_sha256": state.prefix,
                    "symbol": "BTCUSDT",
                    "interval": "15m",
                    "as_of": iso(at),
                    "target": target,
                    "reason": reason,
                    "candle": closed["open_time"],
                    "close": closed["close"],
                    "risk_limits": {k: v for k, v in params.items() if k.startswith("max_")},
                    **{
                        k: v
                        for k, v in details.items()
                        if k not in ("target", "reason", "instance_id")
                    },
                }
                if target != "HOLD":
                    if state.pending is not None:
                        envelope.update(
                            status="COORDINATOR_REJECTED", execution_reason="PENDING_DECISION"
                        )
                        state.metrics.rejected += 1
                    else:
                        cap = min(
                            state.wallet.usdt_free,
                            state.wallet.equity * number(context["capital_weight"]),
                            state.wallet.equity * number(params["max_entry_fraction"]),
                            state.wallet.equity * number(params["max_exposure_fraction"]),
                        )
                        due = at + delay * STEP
                        envelope.update(
                            status="SCHEDULED",
                            execution_reason="",
                            scheduled_at=due,
                            valid_until=due + 5000,
                            budget_cap=text(money(cap)),
                        )
                        state.pending = deepcopy(envelope)
                else:
                    envelope.update(status="HOLD", execution_reason="")
                effect.decision = envelope
        elif kind == "OBSERVE":
            opened = int(str(payload["bar_open"]))
            at = opened + 1
            if opened != state.last_boundary or opened <= state.last_observed or at >= end:
                raise PaperError("INVALID_OBSERVATION_TIME")
            _mark(effect, at, payload["price"], context)
            state.last_observed = opened
            if state.pending is not None and int(str(state.pending["scheduled_at"])) <= opened:
                pending = state.pending
                action = str(pending["decision_id"])
                effect.reference = action
                if int(str(pending["scheduled_at"])) != opened or opened + 2 > int(
                    str(pending["valid_until"])
                ):
                    raise PaperError("MISSED_OR_EXPIRED_SCHEDULE")
                side = "BUY" if pending["target"] == "LONG" else "SELL"
                quote = _quote(at, payload["price"])
                try:
                    if side == "BUY":
                        _, mark = observation(
                            quote, opened + 2, int(str(params["max_quote_age_ms"]))
                        )
                        supplied = filters(require_object(context["rules"]))
                        price = execution_price(side, quote, mark, params, supplied)
                        step = quantity_step(supplied)
                        cap = min(
                            number(pending["budget_cap"]),
                            state.wallet.usdt_free,
                            state.wallet.equity * number(context["capital_weight"]),
                            state.wallet.equity * number(params["max_entry_fraction"]),
                            state.wallet.equity * number(params["max_exposure_fraction"]),
                        )
                        quantity = (
                            cap / (price * (1 + number(params["fee_bps"]) / 10000)) / step
                        ).to_integral_value(rounding=ROUND_FLOOR) * step
                    else:
                        quantity = state.wallet.btc_free
                    event = {
                        "timestamp": iso(at),
                        "decision_at": pending["as_of"],
                        "fill_at": iso(opened + 2),
                        "observation": quote,
                        "quantity": text(quantity),
                        "side": side,
                        "action_key": action,
                        "envelope": pending,
                    }
                    _core(effect, "ORDER", event, context)
                    intent = state.wallet.intents[action]
                    effect.intent = {
                        "action_key": action,
                        "intent": record(intent),
                        "envelope": deepcopy(pending),
                    }
                    if intent.state != "RESERVED":
                        effect.outcome = {
                            "action_key": action,
                            "status": intent.state,
                            "reason": intent.reason,
                            "envelope": deepcopy(pending),
                        }
                        assert state.metrics is not None
                        state.metrics.rejected += 1
                        state.wallet.intents.clear()
                        state.pending = None
                except PaperError as error:
                    effect.outcome = {
                        "action_key": action,
                        "status": "NOT_EXECUTED",
                        "reason": str(error),
                        "envelope": deepcopy(pending),
                    }
                    assert state.metrics is not None
                    state.metrics.rejected += 1
                    state.wallet.intents.clear()
                    state.pending = None
        elif kind == "FILL":
            if state.pending is None:
                raise PaperError("MISSING_SCHEDULED_DECISION")
            action = str(state.pending["decision_id"])
            reserved = state.wallet.intents.get(action)
            if reserved is None or reserved.plan is None:
                raise PaperError("MISSING_RESERVED_PLAN")
            plan = reserved.plan
            if payload != {"action_key": action, "fill_id": plan.fill_id}:
                raise PaperError("FILL_CONFLICT")
            _core(effect, "FILL", payload, context)
            assert state.metrics is not None
            state.metrics.fills += 1
            state.metrics.turnover_notional += plan.notional
            state.metrics.closed_trades += plan.side == "SELL" and state.wallet.quantity == 0
            state.metrics.price_impact += plan.quantity * (
                plan.price - plan.mark if plan.side == "BUY" else plan.mark - plan.price
            )
            effect.reference = action
            effect.fill = {
                "fill_id": plan.fill_id,
                "action_key": action,
                "plan": record(plan),
                "envelope": deepcopy(state.pending),
                "cost_delta": text(state.wallet.cost_basis - previous.wallet.cost_basis),
                "realized_delta": text(state.wallet.realized - previous.wallet.realized),
            }
            effect.outcome = {
                "action_key": action,
                "status": "FILLED",
                "reason": "",
                "envelope": deepcopy(state.pending),
            }
            state.wallet.intents.clear()
            state.pending = None
        elif kind == "END":
            candle = require_object(payload["closed"])
            if int(str(candle["available_at"])) != end or state.last_observed != end - STEP:
                raise PaperError("INVALID_TERMINAL_BOUNDARY")
            if state.wallet.intents:
                raise PaperError("UNRESOLVED_FINANCIAL_INTENT")
            _close(state, candle, slow)
            _mark(effect, end, candle["close"], context)
            if state.pending:
                effect.outcome = {
                    "action_key": state.pending["decision_id"],
                    "status": "END_OF_WINDOW",
                    "reason": "OUTSIDE_SEGMENT",
                    "envelope": deepcopy(state.pending),
                }
                assert state.metrics is not None
                state.metrics.expired += 1
                state.pending = None
            state.finished = True
        else:
            raise PaperError("UNKNOWN_BACKTEST_EVENT")
        state.sequence += 1
        state.wallet.validate()
        assert state.metrics is not None
        effect.equity = state.metrics.update(state.wallet, state.wallet.clock)
        return effect
