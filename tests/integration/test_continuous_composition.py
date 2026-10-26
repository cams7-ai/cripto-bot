"""Public transport mocks, feature gates, identity and disabled custody."""

import httpx
import pytest
from cripto_core.errors import PaperError
from cripto_core.serialization import fingerprint

from cripto_bot.adapters import continuous_market
from cripto_bot.adapters.config import ConfigError, load_config
from cripto_bot.adapters.continuous_market import PublicFeed, check_clock
from cripto_bot.adapters.storage.continuous_sqlite import ContinuousStore
from cripto_bot.adapters.storage.notification_sqlite import NotificationStore
from cripto_bot.application.continuous_run import sample
from cripto_bot.application.notifications import drain
from cripto_bot.entrypoints.continuous import context, execute_continuous
from tests.continuous_factories import (
    BASE,
    STEP,
    Clock,
    Feed,
    arm_payload,
    config,
    quote,
    rows,
    rules,
)


def test_noop_and_network_send_optins_before_clients_or_stores(tmp_path):
    settings, _ = config(tmp_path, enabled=False, source="binance_public")
    assert execute_continuous(settings, "continuous-run", "noop")[1]["status"] == "CONTINUOUS_NOOP"
    assert not settings.continuous.data_dir.exists()
    other = tmp_path / "active"
    other.mkdir()
    settings, _ = config(other, source="binance_public")
    with pytest.raises(ConfigError, match="network opt-in"):
        execute_continuous(settings, "continuous-run", "blocked")
    assert not settings.continuous.data_dir.exists()
    third = tmp_path / "send"
    third.mkdir()
    settings, _ = config(third, notifications=True)
    with pytest.raises(ConfigError, match="sending requires"):
        execute_continuous(settings, "notify-drain", "blocked", once=True)
    assert not settings.continuous.notification_data_dir.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.replace('capital_weight="0.10"', 'capital_weight="0.5"'),
        lambda s: s.replace('execution="paper_bounded"', 'execution="paper_immediate"'),
        lambda s: s.replace('module_id="trend_sma"', 'module_id="unknown"'),
        lambda s: s.replace('mode="PAPER"', 'mode="LIVE"'),
        lambda s: s.replace('evaluation_types=["continuous"]', 'evaluation_types=["backtest"]'),
    ],
)
def test_invalid_config_has_no_effects(tmp_path, mutation):
    _, path = config(tmp_path)
    path.write_text(mutation(path.read_text()))
    with pytest.raises(ConfigError):
        load_config(path, {}, root=tmp_path)
    assert not (tmp_path / "wallet").exists() and not (tmp_path / "market").exists()


def test_disabled_existing_wallet_keeps_identity_and_cancels(tmp_path):
    settings, path = config(tmp_path)
    clock = Clock(BASE - 2000)
    assert execute_continuous(settings, "continuous-prepare", "prepare", clock=clock)[0] == 0
    with ContinuousStore(settings.continuous.data_dir) as store:
        store.append("arm", "ARM", arm_payload())
        store.append(
            "buy",
            "ADMIT",
            {
                "at": BASE + 2000,
                "order_id": "buy",
                "side": "BUY",
                "quantity": "1",
                "quote": quote(BASE + 2000),
            },
        )
    path.write_text(path.read_text().replace("enabled=true", "enabled=false"))
    disabled = load_config(path, {}, root=tmp_path)
    clock = Clock(BASE + 3000)
    code, result = execute_continuous(disabled, "continuous-run", "disable", clock=clock)
    assert code == 0 and result["status"] == "CUSTODY_PAUSED"
    with ContinuousStore(settings.continuous.data_dir, readonly=True) as store:
        assert store.state().wallet.usdt_reserved == 0 and store.state().disabled


@pytest.mark.parametrize(
    "server,start,end,elapsed",
    [(BASE + 1001, BASE, BASE, 0), (BASE, BASE, BASE + 1001, 1001), (BASE, BASE, BASE + 2000, 0)],
)
def test_rtt_offset_or_clock_jump_rejected(server, start, end, elapsed):
    with pytest.raises(PaperError, match="UNSYNCHRONIZED_CLOCK"):
        check_clock(server, start, end, elapsed)


def test_jump_during_data_fetch_cannot_produce_signal():
    clock = Clock(BASE)

    class Jump(Feed):
        def sample(self, *args, **kwargs):
            clock.at += 2000
            return super().sample(*args, **kwargs)

    with pytest.raises(PaperError, match="CLOCK_DISCONTINUITY"):
        sample(Jump(clock), clock)


def test_four_routes_only_persist_and_refresh_quote_last(tmp_path, monkeypatch):
    settings, _ = config(tmp_path, source="binance_public")
    clock = Clock(BASE + 2000)
    seen = []
    monkeypatch.setattr(continuous_market, "received", lambda raw: clock.now())

    def handle(request):
        seen.append(request.url.path)
        assert request.method == "GET" and request.url.host == "data-api.binance.vision"
        assert "x-mbx-apikey" not in request.headers
        path = request.url.path
        if path.endswith("/time"):
            data = {"serverTime": clock.now()}
        elif path.endswith("/exchangeInfo"):
            data = {"symbols": [rules()]}
        elif path.endswith("/klines"):
            assert request.url.params["startTime"] == str(BASE - 50 * STEP)
            data = rows(BASE, 50)
        else:
            data = {
                "symbol": "BTCUSDT",
                "bidPrice": "100",
                "askPrice": "100",
                "bidQty": "10",
                "askQty": "10",
            }
        return httpx.Response(200, json=data)

    feed = PublicFeed(settings.continuous, clock, transport=httpx.MockTransport(handle))
    try:
        frame = feed.sample(BASE, 50)
        assert len(frame.candles) == 50 and frame.quote["market_timestamp"] is None
        assert seen == [
            "/api/v3/time",
            "/api/v3/exchangeInfo",
            "/api/v3/klines",
            "/api/v3/ticker/bookTicker",
        ]
        feed.sample(quote_only=True)
        assert len(seen) == 5 and seen[-1] == "/api/v3/ticker/bookTicker"
        feed.market.block_until(clock.now() / 1000 + 60)
        with pytest.raises(PaperError, match="MARKET_THROTTLED"):
            feed.sample(quote_only=True)
        assert len(seen) == 5
    finally:
        feed.close()


def test_notification_failure_does_not_repeat_financial_event(tmp_path):
    settings, _ = config(tmp_path, notifications=True)
    cfg = settings.continuous
    with ContinuousStore(cfg.data_dir, create=True) as financial:
        financial.initialize(context(cfg), BASE - 2000)
        financial.append("arm", "ARM", arm_payload())
        before = fingerprint(financial.state().data())

        class Failed:
            def send(self, *args, **kwargs):
                with ContinuousStore(cfg.data_dir, readonly=True) as reader:
                    assert fingerprint(reader.state().data()) == before
                return "UNKNOWN"

        with NotificationStore(cfg.notification_data_dir) as queue:
            assert drain(cfg, queue, BASE, Failed())["processed"] == 1
            assert drain(cfg, queue, BASE + 1000, Failed())["processed"] == 0
        assert fingerprint(financial.reconcile().data()) == before


def test_disabling_tighter_module_preserves_risk_without_activation(tmp_path):
    _, path = config(tmp_path)
    path.write_text(path.read_text().replace('capital_weight="0.10"', 'capital_weight="0.05"'))
    active = load_config(path, {}, root=tmp_path)
    path.write_text(path.read_text().replace("enabled=true", "enabled=false"))
    disabled = load_config(path, {}, root=tmp_path)
    assert disabled.continuous.active is False
    assert disabled.continuous.module is None
    assert all(not m.enabled for m in disabled.continuous.modules)
    # Flags may only stop effects; they cannot change a frozen financial protocol.
    assert disabled.continuous.paper.parameters() == active.continuous.paper.parameters()


def test_public_prepare_rejects_editable_before_creating_store(tmp_path):
    settings, _ = config(tmp_path, source="binance_public")
    code, result = execute_continuous(settings, "continuous-prepare", "blocked")
    assert code == 3 and result["error_code"] == "PILOT_REQUIRES_INSTALLED_WHEELS"
    assert not settings.continuous.data_dir.exists()


def test_clock_failure_persists_pause_before_once_returns(tmp_path):
    from cripto_bot.application.continuous_run import _pause, tick

    settings, _ = config(tmp_path)
    clock = Clock(BASE + 2000)

    class Jump(Feed):
        def sample(self, *args, **kwargs):
            clock.at += 2000
            return super().sample(*args, **kwargs)

    with ContinuousStore(settings.continuous.data_dir, create=True) as store:
        store.initialize(context(settings.continuous), BASE - 2000)
        store.append("arm", "ARM", arm_payload())
        tick(store, Jump(clock), clock, free_bytes=lambda: 10 * 1024**3)
        assert store.state().paused and not store.state().orders
        assert store.state().cycles[str(BASE)]["reason"] == "CLOCK_DISCONTINUITY"
        version = store.state().wallet.version
        _pause(store, clock.now(), "CLOCK_DISCONTINUITY")
        assert store.state().wallet.version == version


def test_repeated_changed_metadata_does_not_conflict_or_flood(tmp_path):
    from dataclasses import replace

    from cripto_bot.adapters.continuous_market import Frame
    from cripto_bot.application.continuous_run import frame_rules

    settings, _ = config(tmp_path)
    with ContinuousStore(settings.continuous.data_dir, create=True) as store:
        store.initialize(context(settings.continuous), BASE - 2000)
        store.append("arm", "ARM", arm_payload())
        changed = rules()
        changed["filters"][0]["tickSize"] = "0.1"
        frame = Frame(
            BASE + 2000, BASE + 2000, changed, quote(BASE + 2000), [], BASE + 2000, "fake"
        )
        assert frame_rules(store, frame) is False
        version = store.state().wallet.version
        assert frame_rules(store, replace(frame, at=BASE + 3000)) is False
        assert store.state().wallet.version == version
        store.append(
            "resume", "CONTROL", {"at": BASE + 4000, "request_id": "resume", "action": "resume"}
        )
        assert frame_rules(store, replace(frame, at=BASE + 5000)) is False
        assert store.reconcile().paused


def test_market_budget_never_extends_remaining_slot_deadline(tmp_path, monkeypatch):
    settings, _ = config(tmp_path, source="binance_public")
    clock = Clock(BASE + 89000)
    seen = []
    monkeypatch.setattr(continuous_market, "received", lambda raw: clock.now())

    def handle(request):
        seen.append(request.url.path)
        clock.advance(400)
        if request.url.path.endswith("/time"):
            data = {"serverTime": clock.now()}
        elif request.url.path.endswith("/exchangeInfo"):
            data = {"symbols": [rules()]}
        elif request.url.path.endswith("/klines"):
            data = rows(BASE, 51)
        else:
            data = {
                "symbol": "BTCUSDT",
                "bidPrice": "100",
                "askPrice": "100",
                "bidQty": "10",
                "askQty": "10",
            }
        return httpx.Response(200, json=data)

    feed = PublicFeed(settings.continuous, clock, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(PaperError, match="DEADLINE_EXCEEDED"):
            feed.sample(BASE, 51)
        assert feed.source.budget.seconds == 1
        assert "/api/v3/ticker/bookTicker" not in seen
    finally:
        feed.close()
