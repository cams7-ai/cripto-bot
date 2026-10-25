"""Roteiro sequencial; transação A separada da confirmação do fill."""

from typing import Protocol

from cripto_bot.application.paper_reconcile import reconcile
from cripto_bot.domain.market import sequence
from cripto_bot.domain.portfolio.models import Plan, Wallet, require_object


class PaperRunStore(Protocol):
    def replay(self) -> Wallet: ...
    def block(self) -> None: ...
    def blocked(self) -> str: ...
    def append(self, key: str, kind: str, payload: dict[str, object], wallet: Wallet) -> Wallet: ...
    def seen(self, key: str, kind: str, payload: dict[str, object]) -> bool: ...


class PaperExecution(Protocol):
    def fill(self, plan: Plan) -> dict[str, object]: ...


def run_scenario(
    store: PaperRunStore,
    scenario: dict[str, object],
    wallet_id: str,
    executor: PaperExecution,
    counts: dict[str, int] | None = None,
) -> tuple[Wallet, dict[str, int]]:
    execution = executor
    wallet = reconcile(store)
    counts = (
        {"simulated_fills": 0, "rejected_intents": 0, "replayed_actions": 0}
        if counts is None
        else counts
    )

    def finish_pending(current: Wallet) -> Wallet:
        for action, intent in current.intents.items():
            if intent.state == "RESERVED":
                assert intent.plan is not None
                current = store.append(
                    action + ":FILL", "FILL", execution.fill(intent.plan), current
                )
                counts["simulated_fills"] += 1
        return current

    wallet = finish_pending(wallet)
    prefix = wallet_id + ":" + str(scenario["scenario_id"]) + ":"
    for raw in sequence(scenario["events"]):
        payload = dict(require_object(raw))
        key = prefix + str(payload["event_id"])
        kind = str(payload["type"])
        if kind == "ORDER":
            payload["action_key"] = prefix + str(payload["action_id"])
        if store.seen(key, kind, payload):
            counts["replayed_actions"] += 1
            continue
        wallet = store.append(key, kind, payload, wallet)
        if kind == "ORDER" and wallet.intents[str(payload["action_key"])].state == "RISK_REJECTED":
            counts["rejected_intents"] += 1
        wallet = finish_pending(wallet)
    return reconcile(store), counts
