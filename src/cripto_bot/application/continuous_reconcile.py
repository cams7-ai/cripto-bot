"""Continuous financial replay has no price transport, reset or repair operation."""

from cripto_bot.adapters.storage.continuous_sqlite import ContinuousStore
from cripto_bot.domain.continuous.models import ContinuousState


def reconcile(store: ContinuousStore) -> ContinuousState:
    return store.reconcile()
