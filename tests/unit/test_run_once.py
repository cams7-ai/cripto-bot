from cripto_bot.application.run_once import run_once
from cripto_bot.domain.models import Settings


def test_noop_repeated():
    settings = Settings()
    first = run_once(settings)
    assert first == run_once(settings)
    assert first.mode == "PAPER"
    assert first.status == "NOOP"
    assert first.orders_sent == 0
