"""Ciclos finitos: prefixo fechado, recuperacao antes de novas decisoes."""

from typing import Protocol, cast

from cripto_bot.application.strategy_reconcile import reconcile
from cripto_bot.domain.market import sequence
from cripto_bot.domain.portfolio.models import (
    PaperError,
    Wallet,
    fingerprint,
    instant,
    require_object,
)
from cripto_bot.domain.strategy.engine import StrategyWallet, cycle_identity


class StrategyStorePort(Protocol):
    def replay(self, *, check_projection: bool = True) -> StrategyWallet: ...
    def block(self) -> None: ...
    def blocked(self) -> str: ...
    def append(self, key: str, kind: str, payload: dict[str, object], wallet: Wallet) -> Wallet: ...


def run(
    store: StrategyStorePort,
    fixture: dict[str, object],
    snapshot: dict[str, object],
    counts: dict[str, int],
) -> StrategyWallet:
    state = reconcile(store)

    def finish(current: StrategyWallet) -> StrategyWallet:
        for action, item in tuple(current.intents.items()):
            if item.state == "RESERVED":
                assert item.plan is not None
                current = cast(
                    StrategyWallet,
                    store.append(
                        action + ":fill",
                        "FILL",
                        {"action_key": action, "fill_id": item.plan.fill_id},
                        current,
                    ),
                )
                counts["simulated_fills"] += 1
        return current

    starts = {
        instant(require_object(item)["timestamp"]): require_object(item)
        for item in sequence(fixture["day_starts"])
    }
    executions = cast(dict[int, dict[str, object]], fixture["executions"])
    historic = {int(str(c["candle"])): c for c in state.cycles.values()}
    incoming = {
        int(str(require_object(c)["open_time"])): require_object(c)
        for c in sequence(fixture["candles"])
    }
    for old in state.candles:
        opened = int(str(old["open_time"]))
        if incoming.get(opened) != old or historic[opened]["execution_sha256"] != fingerprint(
            executions.get(opened)
        ):
            raise PaperError("DATASET_PREFIX_CONFLICT")
    for historic_day, payload in state.day_starts.items():
        if starts.get(int(historic_day)) != payload:
            raise PaperError("DATASET_PREFIX_CONFLICT")
    state = finish(state)
    if fingerprint(snapshot) != state.config_hash:
        state = cast(
            StrategyWallet,
            store.append(f"config:{state.generation + 1}", "CONFIG", {"snapshot": snapshot}, state),
        )
    for value in sequence(fixture["candles"]):
        candle = require_object(value)
        opened = int(str(candle["open_time"]))
        if opened <= state.watermark:
            counts["replayed_actions"] += 1
            continue
        for baseline_at, baseline in sorted(starts.items()):
            if (
                baseline_at <= int(str(candle["available_at"]))
                and str(baseline_at) not in state.day_starts
            ):
                state = cast(
                    StrategyWallet, store.append(f"day:{baseline_at}", "DAY_START", baseline, state)
                )
        action = cycle_identity(state, candle, str(fixture["dataset_id"]))
        state = cast(
            StrategyWallet,
            store.append(
                action,
                "CYCLE",
                {"action_key": action, "candle": candle, "execution": executions.get(opened)},
                state,
            ),
        )
        counts["evaluated_cycles"] += 1
        decision = state.cycles[action]
        if decision["target"] != "HOLD":
            counts["directional_decisions"] += 1
        if decision["status"] in ("RISK_REJECTED", "NOT_EXECUTED"):
            counts["rejected_intents"] += 1
        state = finish(state)
    return reconcile(store)
