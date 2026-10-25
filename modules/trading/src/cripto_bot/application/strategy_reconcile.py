"""Porta de reconciliacao modular, sem reparo financeiro implicito."""

from typing import Protocol

from cripto_bot.domain.portfolio.models import PaperError
from cripto_bot.domain.strategy.engine import StrategyWallet


class StrategyReadStore(Protocol):
    def replay(self, *, check_projection: bool = True) -> StrategyWallet: ...
    def block(self) -> None: ...
    def blocked(self) -> str: ...


def reconcile(store: StrategyReadStore) -> StrategyWallet:
    try:
        state = store.replay()
    except Exception:
        store.block()
        raise PaperError("RECONCILIATION_FAILED") from None
    if store.blocked():
        raise PaperError("WALLET_BLOCKED")
    return state
