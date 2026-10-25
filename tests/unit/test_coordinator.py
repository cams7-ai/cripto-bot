from dataclasses import dataclass

import pytest
from cripto_bot.domain.modules.models import monitor_state, resolve
from cripto_bot.domain.portfolio.models import PaperError


@dataclass(frozen=True)
class FakeDecision:
    instance_id: str
    target: str


@pytest.mark.parametrize("targets", [("LONG", "CASH"), ("LONG", "LONG"), ("HOLD", "LONG")])
def test_conflicts_are_rejected_regardless_of_arrival(targets):
    items = (FakeDecision("a", targets[0]), FakeDecision("b", targets[1]))
    for ordered in (items, items[::-1]):
        with pytest.raises(PaperError, match="EXCLUSIVE_OWNER_CONFLICT"):
            resolve(ordered)
    assert resolve(()) == ()
    assert resolve((items[0],)) == (items[0],)


@pytest.mark.parametrize("pending", [("RESERVED",), ("PARTIALLY_FILLED",), ("UNKNOWN",)])
def test_disabled_module_retains_pending_monitoring_contract(pending):
    assert monitor_state(False, False, pending) == "DRAINING"
    assert monitor_state(False, True, ()) == "DRAINING"
    assert monitor_state(False, False, ()) == "DISABLED"
    assert monitor_state(True, False, ("UNKNOWN",)) == "PAUSED"
