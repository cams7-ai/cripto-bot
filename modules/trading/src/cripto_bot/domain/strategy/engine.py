"""Um journal reconstrói juntos carteira, geracoes, ciclos e atribuicoes."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from math import lcm
from typing import cast

from cripto_bot.domain.market import STEP, iso, sequence
from cripto_bot.domain.modules.models import LIMITS, monitor_state, resolve
from cripto_bot.domain.portfolio.engine import transition
from cripto_bot.domain.portfolio.models import (
    ZERO,
    PaperError,
    Posting,
    Q,
    Wallet,
    fingerprint,
    instant,
    number,
    require_object,
    text,
)
from cripto_bot.domain.risk.execution import filters, observation
from cripto_bot.domain.strategy.sma import Snapshot, TrendSMA

MODEL = "paper-modular-v1"


@dataclass
class StrategyWallet(Wallet):
    generation: int = 0
    config_hash: str = ""
    configuration: dict[str, object] = field(default_factory=dict)
    generations: list[dict[str, object]] = field(default_factory=list)
    instances: dict[str, dict[str, object]] = field(default_factory=dict)
    candles: list[dict[str, object]] = field(default_factory=list)
    day_starts: dict[str, dict[str, object]] = field(default_factory=dict)
    cycles: dict[str, dict[str, object]] = field(default_factory=dict)
    attributions: dict[str, dict[str, object]] = field(default_factory=dict)
    watermark: int = -1
    owner: str = ""


def active_module(configuration: dict[str, object]) -> dict[str, object] | None:
    active = []
    for value in sequence(configuration.get("modules", [])):
        module = require_object(value)
        if module["enabled"] and all(
            wanted in sequence(module[field_name])
            for field_name, wanted in (
                ("environments", "local"),
                ("modes", "PAPER"),
                ("evaluation_types", "fixture"),
                ("symbols", "BTCUSDT"),
            )
        ):
            active.append(module)
    if len(active) > 1:
        raise PaperError("EXCLUSIVE_OWNER_CONFLICT")
    return active[0] if active else None


def effective_parameters(
    wallet: StrategyWallet, parameters: dict[str, object]
) -> dict[str, object]:
    effective = dict(parameters)
    module = active_module(wallet.configuration)
    if module is None and wallet.owner:
        module = next(
            (
                require_object(m)
                for m in sequence(wallet.configuration.get("modules", []))
                if require_object(m)["instance_id"] == wallet.owner
            ),
            None,
        )
    overrides = [require_object(wallet.configuration.get("symbol_limits", {}))]
    if module is not None:
        overrides.append(dict(cast(list[tuple[str, str]], module["limits"])))
    for limit in LIMITS:
        effective[limit] = format(
            min(
                [number(parameters[limit])]
                + [number(item[limit]) for item in overrides if limit in item]
            ),
            "f",
        )
    if module is not None:
        effective["max_entry_fraction"] = format(
            min(number(effective["max_entry_fraction"]), number(module["capital_weight"])), "f"
        )
    return effective


def _account() -> dict[str, object]:
    return {
        "status": "READY",
        "epoch": 0,
        "watermark": -1,
        "quantity": "0.00000000",
        "cost": "0.00000000",
        "fees": "0.00000000",
        "realized": "0.00000000",
    }


def cycle_identity(wallet: StrategyWallet, candle: dict[str, object], dataset: str) -> str:
    module = active_module(wallet.configuration)
    instance = str(module["instance_id"]) if module else wallet.owner or "disabled"
    state = wallet.instances.get(instance, _account())
    return fingerprint(
        {
            "environment": "local",
            "mode": "PAPER",
            "wallet": wallet.configuration.get("wallet_id"),
            "instance": instance,
            "version": module["version"] if module else "1",
            "generation": wallet.generation,
            "epoch": state["epoch"],
            "dataset": dataset,
            "symbol": "BTCUSDT",
            "interval": "15m",
            "candle": candle["open_time"],
        }
    )


def _configure(wallet: StrategyWallet, snapshot: dict[str, object]) -> None:
    module = active_module(snapshot)
    old = active_module(wallet.configuration)
    if wallet.quantity or any(i.state == "RESERVED" for i in wallet.intents.values()):
        # Desativar apenas deixa de gerar novas entradas; propriedade permanece.
        if module is not None and (old is None or module != old):
            raise PaperError("DRAIN_REQUIRED")
        if old is not None and module is None:
            old_modules = sequence(wallet.configuration["modules"])
            new_modules = sequence(snapshot["modules"])

            def material(values: list[object]) -> object:
                return [
                    {k: v for k, v in require_object(x).items() if k != "enabled"} for x in values
                ]

            if (
                material(old_modules) != material(new_modules)
                or snapshot["symbol_limits"] != wallet.configuration["symbol_limits"]
            ):
                raise PaperError("DRAIN_REQUIRED")
    if module is not None:
        instance = str(module["instance_id"])
        if wallet.owner and wallet.owner != instance and wallet.quantity:
            raise PaperError("INVENTORY_OWNER_CONFLICT")
        state = wallet.instances.setdefault(instance, _account())
        if old is None or old["instance_id"] != instance:
            state["epoch"] = int(str(state["epoch"])) + 1
        if not wallet.quantity:
            wallet.owner = instance
    wallet.generation += 1
    wallet.config_hash = fingerprint(snapshot)
    wallet.configuration = deepcopy(snapshot)
    wallet.generations.append(
        {
            "generation": wallet.generation,
            "config_hash": wallet.config_hash,
            "snapshot": deepcopy(snapshot),
        }
    )


def _refresh(wallet: StrategyWallet) -> None:
    module = active_module(wallet.configuration)
    for instance, state in wallet.instances.items():
        pending = tuple(
            i.state
            for action, i in wallet.intents.items()
            if i.state == "RESERVED" and wallet.cycles[action]["instance_id"] == instance
        )
        state["status"] = monitor_state(
            module is not None and module["instance_id"] == instance,
            number(state["quantity"]) > 0,
            pending,
        )
    for name, total in (
        ("quantity", wallet.quantity),
        ("cost", wallet.cost_basis),
        ("fees", wallet.fees),
        ("realized", wallet.realized),
    ):
        if (
            sum((number(state[name], signed=True) for state in wallet.instances.values()), ZERO)
            != total
        ):
            raise PaperError("ATTRIBUTION_DIVERGENCE")


def _sized_quantity(
    wallet: StrategyWallet,
    module: dict[str, object],
    quote: dict[str, object],
    at: int,
    parameters: dict[str, object],
    rules: dict[str, object],
) -> Decimal:
    _, mark = observation(quote, at, int(str(parameters["max_quote_age_ms"])))
    supplied = filters(rules)
    # Mesmo modelo de preco da 03; make_plan/admit revalidam o plano definitivo.
    slip = number(parameters["slippage_bps"]) / 10000
    price = (
        mark * (1 + number(parameters["spread_bps"]) / 20000 + slip)
        if quote["kind"] == "REFERENCE"
        else number(quote["ask"]) * (1 + slip)
    )
    tick = number(supplied["PRICE_FILTER"]["tickSize"]) or Q
    price = (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    if price <= 0:
        raise PaperError("INVALID_PRICE")
    budget = min(
        wallet.usdt_free,
        wallet.equity * number(module["capital_weight"]),
        wallet.equity * number(parameters["max_entry_fraction"]),
        wallet.equity * number(parameters["max_exposure_fraction"]),
    )
    step_units = [
        int(number(supplied[name]["stepSize"]) / Q)
        for name in ("LOT_SIZE", "MARKET_LOT_SIZE")
        if number(supplied[name]["stepSize"])
    ]
    step = Decimal(lcm(*step_units)) * Q if step_units else Q
    return (
        budget / (price * (1 + number(parameters["fee_bps"]) / 10000)) / step
    ).to_integral_value(rounding=ROUND_FLOOR) * step


def reduce(
    previous: Wallet,
    kind: str,
    payload: dict[str, object],
    parameters: dict[str, object],
    rules: dict[str, object],
) -> tuple[Wallet, list[Posting]]:
    if not isinstance(previous, StrategyWallet):
        raise PaperError("MODEL_CONFLICT")
    with localcontext() as context:
        context.prec = 80
        wallet = deepcopy(previous)
        effective = effective_parameters(wallet, parameters)
        postings: list[Posting] = []
        if kind == "CONFIG":
            _configure(wallet, require_object(payload["snapshot"]))
        elif kind == "CYCLE":
            candle = deepcopy(require_object(payload["candle"]))
            opened = int(str(candle["open_time"]))
            at = int(str(candle["available_at"]))
            if opened <= wallet.watermark or (wallet.candles and opened != wallet.watermark + STEP):
                raise PaperError("CYCLE_WATERMARK_CONFLICT")
            if at != opened + STEP or at < wallet.clock:
                raise PaperError("INVALID_CANDLE_TIME")
            if payload["action_key"] != cycle_identity(
                wallet, candle, str(parameters["dataset_id"])
            ):
                raise PaperError("DECISION_ID_CONFLICT")
            wallet.candles.append(candle)
            key = str(payload["action_key"])
            module = active_module(wallet.configuration)
            instance = str(module["instance_id"]) if module else wallet.owner or "disabled"
            # A marcacao e conhecida no fechamento; nao e preco de execucao.
            marked, postings = transition(
                wallet,
                "MARK",
                {
                    "timestamp": iso(at),
                    "observation": {
                        "quote_id": "close-mark",
                        "kind": "REFERENCE",
                        "symbol": "BTCUSDT",
                        "observed_at": iso(at),
                        "price": candle["close"],
                    },
                },
                effective,
                rules,
            )
            wallet = cast(StrategyWallet, marked)
            state = wallet.instances.get(instance, _account())
            decision = (
                TrendSMA().evaluate(
                    Snapshot(
                        tuple(number(c["close"]) for c in wallet.candles),
                        at,
                        number(state["quantity"]),
                    ),
                    int(str(module["fast_window"])),
                    int(str(module["slow_window"])),
                    instance,
                )
                if module is not None
                else None
            )
            if decision is not None:
                resolve((decision,))
            envelope: dict[str, object] = {
                **(
                    asdict(decision)
                    if decision
                    else {"instance_id": instance, "target": "HOLD", "reason": "DISABLED"}
                ),
                "decision_id": key,
                "module_id": module["module_id"] if module else "trend_sma",
                "implementation_version": "1",
                "schema_version": 1,
                "config_hash": wallet.config_hash,
                "config_generation": wallet.generation,
                "state_version": previous.version,
                "activation_epoch": state["epoch"],
                "environment": "local",
                "mode": "PAPER",
                "evaluation_type": "fixture",
                "wallet_id": parameters["wallet_id"],
                "symbol": "BTCUSDT",
                "interval": "15m",
                "dataset_id": parameters["dataset_id"],
                "prefix_sha256": fingerprint(wallet.candles),
                "execution_sha256": fingerprint(payload.get("execution")),
                "close": candle["close"],
                "as_of": iso(at),
                "valid_until": iso(at + int(str(parameters["max_quote_age_ms"]))),
                "candle": opened,
                "status": "HOLD",
                "execution_reason": "",
                "risk_limits": {name: effective[name] for name in LIMITS},
            }
            wallet.cycles[key] = envelope
            wallet.watermark = opened
            if instance in wallet.instances:
                wallet.instances[instance]["watermark"] = opened
            if decision is not None and decision.target != "HOLD":
                quote_data = payload.get("execution")
                try:
                    if quote_data is None:
                        raise PaperError("MISSING_QUOTE")
                    execution = require_object(quote_data)
                    quote = require_object(execution["observation"])
                    fill_at = instant(execution["fill_at"])
                    if max(instant(quote["observed_at"]), fill_at) > instant(
                        envelope["valid_until"]
                    ):
                        raise PaperError("EXPIRED_DECISION")
                    assert module is not None
                    quantity = (
                        _sized_quantity(wallet, module, quote, fill_at, effective, rules)
                        if decision.target == "LONG"
                        else number(state["quantity"])
                    )
                    if decision.target == "CASH" and wallet.owner != instance:
                        raise PaperError("INVENTORY_OWNER_CONFLICT")
                    event = {
                        "timestamp": quote["observed_at"],
                        "decision_at": iso(at),
                        "fill_at": execution["fill_at"],
                        "observation": quote,
                        "quantity": text(quantity),
                        "side": "BUY" if decision.target == "LONG" else "SELL",
                        "action_key": key,
                        "envelope": deepcopy(envelope),
                    }
                    ordered, reserved = transition(wallet, "ORDER", event, effective, rules)
                    wallet = cast(StrategyWallet, ordered)
                    postings += reserved
                    item = wallet.intents[key]
                    wallet.cycles[key]["status"] = item.state
                    wallet.cycles[key]["execution_reason"] = item.reason
                except PaperError as error:
                    wallet.cycles[key]["status"] = "NOT_EXECUTED"
                    wallet.cycles[key]["execution_reason"] = str(error)
        else:
            if kind in ("FILL", "CANCEL", "REJECT"):
                key = str(payload["action_key"])
                envelope = wallet.cycles[key]
                effective.update(require_object(envelope["risk_limits"]))
            updated, postings = transition(wallet, kind, payload, effective, rules)
            wallet = cast(StrategyWallet, updated)
            if kind == "DAY_START":
                wallet.day_starts[str(instant(payload["timestamp"]))] = deepcopy(payload)
            if kind in ("FILL", "CANCEL", "REJECT"):
                key = str(payload["action_key"])
                envelope = wallet.cycles[key]
                instance = str(envelope["instance_id"])
                account = wallet.instances[instance]
                account.update(
                    quantity=text(wallet.quantity),
                    cost=text(wallet.cost_basis),
                    fees=text(number(account["fees"]) + wallet.fees - previous.fees),
                    realized=text(
                        number(account["realized"], signed=True)
                        + wallet.realized
                        - previous.realized
                    ),
                )
                envelope["status"] = wallet.intents[key].state
                envelope["equity_after"] = text(wallet.equity)
                if kind == "FILL":
                    plan = wallet.intents[key].plan
                    assert plan is not None
                    wallet.attributions[plan.fill_id] = {
                        "fill_id": plan.fill_id,
                        "action_key": key,
                        "instance_id": instance,
                        "envelope": deepcopy(envelope),
                        "quantity": text(plan.quantity),
                        "fee": text(plan.fee),
                        "cost_delta": text(wallet.cost_basis - previous.cost_basis),
                        "realized_delta": text(wallet.realized - previous.realized),
                    }
        if kind == "CYCLE":
            wallet.cycles[str(payload["action_key"])]["equity_after"] = text(wallet.equity)
        elif kind in ("FILL", "CANCEL", "REJECT"):
            wallet.cycles[str(payload["action_key"])]["equity_after"] = text(wallet.equity)
        wallet.version = previous.version + 1
        _refresh(wallet)
        wallet.validate()
        return wallet, postings
