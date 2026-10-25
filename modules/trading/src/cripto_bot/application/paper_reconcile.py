"""Reconciliação antes de novos efeitos; sem correção silenciosa."""

from typing import Protocol

from cripto_bot.domain.portfolio.models import PaperError, Wallet


class ReconciliationStore(Protocol):
    def replay(self) -> Wallet: ...
    def block(self) -> None: ...
    def blocked(self) -> str: ...


def reconcile(store: ReconciliationStore) -> Wallet:
    try:
        wallet = store.replay()
    except Exception:
        store.block()
        raise PaperError("RECONCILIATION_FAILED") from None
    if store.blocked():
        raise PaperError("WALLET_BLOCKED")
    return wallet
