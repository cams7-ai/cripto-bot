"""Contratos financeiros e causais historicos, com oraculos independentes."""

import json
import shutil
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path

import pytest
from cripto_bot.adapters.backtest_dataset import file_hash, normalize, prepare, validate
from cripto_bot.adapters.config import load_config
from cripto_bot.adapters.storage.backtest_report import document, export_report, snapshot
from cripto_bot.adapters.storage.backtest_sqlite import BacktestStore
from cripto_bot.application.backtest_run import (
    curve_context,
    execute_curve,
    protocol,
    run_segment,
    verify_protocol,
)
from cripto_bot.domain.backtest.engine import State, reduce
from cripto_bot.domain.backtest.metrics import Metrics
from cripto_bot.domain.backtest.models import VARIANTS, split
from cripto_bot.domain.market import STEP, canonical, iso
from cripto_bot.domain.portfolio.models import PaperError, Wallet

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("pyproject.toml", "uv.lock", ".python-version"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    shutil.copytree(
        ROOT / "config/fixtures/backtest-sma", tmp_path / "config/fixtures/backtest-sma"
    )
    config_file = tmp_path / "config/backtest.toml"
    config_file.write_text((ROOT / "config/backtest.sma.fixture.toml").read_text())
    result = load_config(config_file, {}, root=tmp_path).backtest
    assert result is not None
    return result


def sealed(config):
    prepare(config)
    return protocol(config)


def context(config, variant="base", profile="sma"):
    return curve_context(config, sealed(config), "development", variant, profile)


def candle(at, price="100"):
    return normalize(
        dict(
            open_time=at,
            close_time=at + STEP - 1,
            open=price,
            high=price,
            low=price,
            close=price,
            base_volume="100",
            quote_volume="10000",
            trades=1,
            symbol="BTCUSDT",
            interval="15m",
        )
    )


def initial(ctx):
    start = ctx["start"]
    warmup = [candle(start + (i - 50) * STEP) for i in range(50)]
    return reduce(State(), "INIT", {"warmup": warmup, "baseline_price": "100"}, ctx).state


def signal(ctx):
    state = initial(ctx)
    start = ctx["start"]
    state = reduce(state, "BOUNDARY", {"at": start, "closed": candle(start - STEP)}, ctx).state
    state = reduce(state, "OBSERVE", {"bar_open": start, "price": "100"}, ctx).state
    return reduce(state, "BOUNDARY", {"at": start + STEP, "closed": candle(start, "110")}, ctx)


def test_split_and_defaults(config):
    assert [int((b - a) / STEP) for a, b in split(config.start, config.end, 50).values()] == [
        576,
        192,
        192,
    ]
    assert config.module.active is False
    assert config.module.active_for("backtest") is True
    assert load_config(None, {}, root=config.root).backtest is None
    with pytest.raises(PaperError):
        split(0, 100 * STEP, 50)


@pytest.mark.parametrize(
    "field,value",
    [
        ("open", 100.0),
        ("open", True),
        ("open", "NaN"),
        ("open", "1e2"),
        ("open", "-1"),
        ("available_at", True),
        ("trades", True),
        ("symbol", "ETHUSDT"),
        ("interval", "1h"),
        ("high", "99"),
    ],
)
def test_reject_candle(field, value):
    row = candle(0)
    row[field] = value
    with pytest.raises((PaperError, ValueError)):
        normalize(row)


def test_prepare_replay_hash_and_corruption(config):
    first = prepare(config)
    assert first == prepare(config) == validate(config)
    assert first["count"] == 1010
    path = config.dataset_dir / "candles.jsonl"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(PaperError, match="HASH"):
        validate(config)


@pytest.mark.parametrize(
    "filename", ["raw.jsonl.gz", "candles.jsonl.gz", "snapshots.json", "provenance.json"]
)
def test_all_source_hashes_checked(config, filename):
    path = config.source_manifests[0].parent / filename
    path.write_bytes(path.read_bytes() + b"corrupt")
    with pytest.raises(PaperError, match="HASH"):
        prepare(config)
    assert not (config.data_dir / "backtest.sqlite3").exists()


@pytest.mark.parametrize("variant", VARIANTS)
def test_schedule_no_future_price_and_no_reserve(config, variant):
    ctx = context(config, variant)
    effect = signal(ctx)
    state = effect.state
    assert effect.decision["target"] == "LONG"
    assert state.wallet.usdt_free == Decimal("10000") and state.wallet.usdt_reserved == 0
    assert state.wallet.quantity == 0 and state.wallet.intents == {}
    assert "quantity" not in state.pending
    at = ctx["start"] + STEP
    due = at + (STEP if "delay" in variant else 0)
    assert state.pending["scheduled_at"] == due
    assert state.pending["valid_until"] == due + 5000
    assert state.max_window == 51
    first = reduce(state, "OBSERVE", {"bar_open": at, "price": "110"}, ctx)
    second = reduce(state, "OBSERVE", {"bar_open": at, "price": "200"}, ctx)
    assert effect.decision == state.pending
    if due == at:
        assert first.state.wallet.intents and second.state.wallet.intents
        assert first.intent != second.intent
    else:
        assert not first.state.wallet.intents


def test_exact_cost_oracle_and_sell(config):
    ctx = context(config)
    effect = signal(ctx)
    at = ctx["start"] + STEP
    admission = reduce(effect.state, "OBSERVE", {"bar_open": at, "price": "110"}, ctx)
    action, intent = next(iter(admission.state.wallet.intents.items()))
    plan = intent.plan
    assert plan.quantity == Decimal("9.072754")
    assert plan.price == Decimal("110.11") and plan.fee == Decimal("0.99900095")
    bought = reduce(
        admission.state, "FILL", {"action_key": action, "fill_id": plan.fill_id}, ctx
    ).state
    # A controlled exit isolates financial arithmetic from crossover generation.
    bought.pending = {
        **effect.decision,
        "decision_id": "controlled-exit",
        "target": "CASH",
        "as_of": iso(at + STEP),
        "scheduled_at": at + STEP,
        "valid_until": at + STEP + 5000,
    }
    bought.last_boundary = at + STEP
    sold_admission = reduce(bought, "OBSERVE", {"bar_open": at + STEP, "price": "100"}, ctx)
    sell = sold_admission.state.wallet.intents["controlled-exit"].plan
    assert sell.price == Decimal("99.90") and sell.fee == Decimal("0.90636813")
    final = reduce(
        sold_admission.state,
        "FILL",
        {"action_key": "controlled-exit", "fill_id": sell.fill_id},
        ctx,
    ).state
    assert final.wallet.usdt_free == Decimal("9905.46181258")
    assert final.wallet.realized == Decimal("-94.53818742") and final.wallet.quantity == 0
    assert final.metrics.closed_trades == 1 and final.metrics.fills == 2


def test_metrics_independent_event_oracle():
    with localcontext() as ctx:
        ctx.prec = 80
        metric = Metrics(Decimal("10000"), 0, 0, Decimal("10000"))
        wallet = Wallet(usdt_free=Decimal("9000"), btc_free=Decimal("10"), mark=Decimal("100"))
        metric.update(wallet, 0)
        wallet.mark = Decimal("90")
        metric.update(wallet, 1000)
        wallet.mark = Decimal("110")
        metric.update(wallet, 2000)
        result = metric.summary(wallet)
        assert result["net_return"] == "0.0100000000000000"
        assert result["maximum_drawdown"] == "0.0100000000000000"
        assert result["average_exposure"] == "0.0954545454545455"
        assert result["time_in_position"] == "1.0000000000000000"


def test_store_replay_lock_cas_freeze_and_readonly_report(config):
    ctx = context(config)
    seal = protocol(config)
    with BacktestStore(config.data_dir, create=True) as store:
        store.freeze(seal)
        store.freeze(seal)
        with pytest.raises(PaperError, match="CONFLICT"):
            store.freeze({**seal, "code_sha256": "changed"})
        with pytest.raises(PaperError, match="WRITER_BUSY"):
            with BacktestStore(config.data_dir):
                pass
        state = execute_curve(store, config, ctx)
        assert state.finished and state.max_window == 51 and not state.wallet.intents
        assert state.data() == store.reconcile(ctx["run_key"]).data()
        total = store.db.total_changes
        again = execute_curve(store, config, ctx)
        assert again.data() == state.data() and store.db.total_changes == total
        assert store.db.execute("SELECT count(*) FROM fills").fetchone()[0] > 0
    digest = file_hash(config.data_dir / "backtest.sqlite3")
    with BacktestStore(config.data_dir, readonly=True) as store:
        report = snapshot(store, store.reconcile_all())
        path = export_report(store, report, "report-test")
        for name, expected in json.loads(path.read_text())["files"].items():
            assert file_hash(path.parent / name) == expected
        assert len(report["curves"][0]["chart"]) <= 800
        assert "script" not in document(report).lower().replace("description", "")
    assert file_hash(config.data_dir / "backtest.sqlite3") == digest


def test_dev_validation_gate_and_changed_config(config):
    seal = sealed(config)
    with BacktestStore(config.data_dir, create=True) as store:
        store.freeze(seal)
        with pytest.raises(PaperError, match="PREVIOUS_SEGMENT"):
            run_segment(store, config, "test")
        with pytest.raises(PaperError, match="CHANGED"):
            verify_protocol(store, replace(config, experiment_id="other"))


def test_corruption_blocks_durably(config):
    ctx = context(config)
    with BacktestStore(config.data_dir, create=True) as store:
        store.freeze(protocol(config))
        execute_curve(store, config, ctx)
        store.db.execute("UPDATE runs SET projection='{}'")
        with pytest.raises(PaperError, match="RECONCILIATION"):
            store.reconcile(ctx["run_key"])
        assert store.blocked()
    with BacktestStore(config.data_dir) as store:
        assert store.blocked()


@pytest.mark.parametrize("profile", ["cash", "buy_hold_100pct", "buy_hold_10pct"])
def test_benchmarks_independent_capital_terminal(config, profile):
    ctx = context(config, profile=profile)
    with BacktestStore(config.data_dir, create=True) as store:
        store.freeze(protocol(config))
        state = execute_curve(store, config, ctx)
        assert state.metrics.initial == 10000
        assert state.wallet.quantity == 0 if profile == "cash" else state.wallet.quantity > 0
        assert state.metrics.fills == (0 if profile == "cash" else 1)
        assert state.metrics.closed_trades == 0
        assert state.metrics.maximum_exposure <= (
            1 if profile == "buy_hold_100pct" else Decimal(".11")
        )


def test_terminal_pending_expires_without_fill(config):
    ctx = context(config, "delay_1candle")
    effect = signal(ctx)
    state = effect.state
    ctx = {**ctx, "end": ctx["start"] + 2 * STEP}
    state = reduce(state, "OBSERVE", {"bar_open": ctx["start"] + STEP, "price": "110"}, ctx).state
    final = reduce(state, "END", {"closed": candle(ctx["start"] + STEP)}, ctx)
    assert final.outcome["status"] == "END_OF_WINDOW"
    assert final.state.metrics.fills == 0 and final.state.wallet.usdt_free == 10000


@pytest.mark.parametrize(
    "old,new",
    [
        ('initial_usdt = "10000.00"', 'initial_usdt = "10001"'),
        ('fee_bps = "10"', 'fee_bps = "9"'),
        ('max_entry_fraction = "0.10"', 'max_entry_fraction = "0.20"'),
        ('capital_weight = "0.10"', 'capital_weight = "0.20"'),
        ("slow_window = 50", "slow_window = true"),
        ('evaluation_types = ["backtest"]', 'evaluation_types = ["continuous"]'),
        ('policy = "exclusive_owner"', 'policy = "weighted_targets"'),
        ('mode = "PAPER"', 'mode = "LIVE"'),
        ("max_quote_age_ms = 5000", "max_quote_age_ms = 5001"),
        ('start = "2026-10-01T00:00:00Z"', 'start = "2026-10-01T00:00:01Z"'),
    ],
)
def test_strict_historical_configuration(config, old, new):
    from cripto_bot.adapters.config import ConfigError

    p = config.root / "config/backtest.toml"
    p.write_text(p.read_text().replace(old, new))
    with pytest.raises(ConfigError):
        load_config(p, {}, root=config.root)


def test_cas_rejects_outdated_in_memory_state(config):
    ctx = context(config)
    start = ctx["start"]
    with BacktestStore(config.data_dir, create=True) as store:
        store.freeze(protocol(config))
        warmup = [candle(start + (i - 50) * STEP) for i in range(50)]
        state = store.create_run(ctx, warmup, "100")
        store.append(ctx["run_key"], "b", "BOUNDARY", {"at": start, "closed": warmup[-1]}, state)
        with pytest.raises(PaperError, match="STALE"):
            store.append(ctx["run_key"], "o", "OBSERVE", {"bar_open": start, "price": "100"}, state)


def test_invalid_source_crc_and_count_are_rejected(config):
    p = config.source_manifests[0]
    m = json.loads(p.read_text())
    source = p.parent / "candles.jsonl.gz"
    source.write_bytes(source.read_bytes()[:-5])
    m["files"]["candles.jsonl.gz"] = file_hash(source)
    p.write_text(canonical(m))
    with pytest.raises((EOFError, OSError, PaperError)):
        prepare(config)
    assert not config.dataset_dir.exists()


def test_readonly_report_escapes_dataset_text(config):
    from cripto_bot.adapters.storage.backtest_report import document

    seal = sealed(config)
    seal["dataset"]["source"] = "<script>alert(1)</script>"
    report = {
        "protocol": seal,
        "curves": [],
        "limitations": ["<img src=x onerror=alert(1)>"],
        "result_sha256": "hash",
    }
    page = document(report)
    assert "<script>" not in page and "<img" not in page
    assert "&lt;script&gt;" in page and "default-src &#" not in page


def test_actual_midnight_baseline_and_no_lookahead(config):
    ctx = context(config)
    effect = signal(ctx)
    state = effect.state
    start = ctx["start"]
    assert state.wallet.day == iso(start)[:10]
    # Changing execution reference cannot mutate the already emitted decision.
    frozen = canonical(effect.decision)
    reduce(state, "OBSERVE", {"bar_open": start + STEP, "price": "1000"}, ctx)
    assert canonical(effect.decision) == frozen
    with pytest.raises(PaperError, match="BOUNDARY"):
        reduce(state, "BOUNDARY", {"at": ctx["end"], "closed": candle(ctx["end"] - STEP)}, ctx)


def test_positive_round_trip_accounting(config):
    ctx = context(config)
    start = ctx["start"]
    effect = signal(ctx)
    admitted = reduce(
        effect.state, "OBSERVE", {"bar_open": start + STEP, "price": "110"}, ctx
    ).state
    action, intent = next(iter(admitted.wallet.intents.items()))
    bought = reduce(
        admitted, "FILL", {"action_key": action, "fill_id": intent.plan.fill_id}, ctx
    ).state
    at = start + 2 * STEP
    bought.last_boundary = at
    bought.pending = {
        **effect.decision,
        "decision_id": "gain-exit",
        "target": "CASH",
        "as_of": iso(at),
        "scheduled_at": at,
        "valid_until": at + 5000,
    }
    sell_state = reduce(bought, "OBSERVE", {"bar_open": at, "price": "120"}, ctx).state
    plan = sell_state.wallet.intents["gain-exit"].plan
    final = reduce(
        sell_state, "FILL", {"action_key": "gain-exit", "fill_id": plan.fill_id}, ctx
    ).state
    assert final.wallet.usdt_free == Decimal("10086.55416388")
    assert final.wallet.realized == Decimal("86.55416388")


@pytest.mark.parametrize("direction", [1, 0, -1])
def test_synthetic_up_down_lateral_regimes(config, direction):
    ctx = context(config)
    ctx["end"] = ctx["start"] + 96 * STEP
    state = initial(ctx)
    closed = candle(ctx["start"] - STEP)
    for i in range(96):
        at = ctx["start"] + i * STEP
        state = reduce(state, "BOUNDARY", {"at": at, "closed": closed}, ctx).state
        price = str(100 + direction * i // 10)
        state = reduce(state, "OBSERVE", {"bar_open": at, "price": price}, ctx).state
        if state.wallet.intents:
            action, intent = next(iter(state.wallet.intents.items()))
            state = reduce(
                state, "FILL", {"action_key": action, "fill_id": intent.plan.fill_id}, ctx
            ).state
        closed = candle(at, price)
    final = reduce(state, "END", {"closed": closed}, ctx).state
    assert final.finished and final.max_window == 51
    if direction == 1:
        assert (
            final.metrics.fills == 1 and final.wallet.quantity > 0 and final.wallet.equity > 10000
        )
    else:
        assert final.metrics.fills == 0 and final.wallet.equity == 10000
