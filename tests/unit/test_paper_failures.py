from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path

import pytest

from cripto_bot.adapters.config import load_config
from cripto_bot.adapters.storage.paper_report import export_report, summary
from cripto_bot.adapters.storage.paper_sqlite import PaperStore
from cripto_bot.application.paper_reconcile import reconcile
from cripto_bot.domain.portfolio.engine import transition
from cripto_bot.domain.portfolio.models import PaperError
from cripto_bot.domain.risk.execution import make_plan
from cripto_bot.domain.risk.limits import admit
from cripto_bot.entrypoints import paper
from tests.paper_factories import context, initialized, order, scenario, write_case


def test_lifecycle_guards_and_sell_release(tmp_path):
    ctx, wallet = initialized(tmp_path)
    params, rules = ctx["parameters"], ctx["rules"]
    wallet, _ = transition(wallet, "ORDER", order(), params, rules)
    plan = wallet.intents[order()["action_key"]].plan
    with pytest.raises(PaperError, match="PENDING"):
        admit(wallet, plan, params)
    bad = deepcopy(wallet)
    bad.intents[plan.action].reserved -= 1
    bad.usdt_reserved -= 1
    with pytest.raises(PaperError, match="INSUFFICIENT_RESERVE"):
        transition(bad, "FILL", {"action_key": plan.action, "fill_id": plan.fill_id}, params, rules)
    wallet, _ = transition(
        wallet, "FILL", {"action_key": plan.action, "fill_id": plan.fill_id}, params, rules
    )
    duplicate = order(timestamp="2026-10-01T00:16:01Z")
    with pytest.raises(PaperError, match="ACTION_CONFLICT"):
        transition(wallet, "ORDER", duplicate, params, rules)
    with pytest.raises(PaperError, match="MONOTONIC"):
        transition(wallet, "MARK", scenario()["events"][0], params, rules)
    with pytest.raises(PaperError, match="UNKNOWN_INTENT"):
        transition(wallet, "CANCEL", {"action_key": "missing"}, params, rules)
    with pytest.raises(PaperError, match="UNKNOWN_TRANSITION"):
        transition(wallet, "UNKNOWN", {}, params, rules)
    with pytest.raises(PaperError, match="DUPLICATE_GENESIS"):
        transition(wallet, "GENESIS", {}, params, rules)
    wallet, _ = transition(wallet, "ORDER", order(2), params, rules)
    wallet, postings = transition(
        wallet, "REJECT", {"action_key": order(2)["action_key"]}, params, rules
    )
    assert wallet.btc_free == 1 and wallet.btc_reserved == 0 and wallet.fees == D("0.1001")


def test_mark_loss_and_next_day_with_persistent_position(tmp_path):
    ctx, wallet = initialized(tmp_path)
    params = {
        **ctx["parameters"],
        "max_daily_loss_fraction": "0.00001",
        "max_drawdown_fraction": "0.00001",
    }
    wallet, _ = transition(wallet, "ORDER", order(), params, ctx["rules"])
    plan = wallet.intents[order()["action_key"]].plan
    wallet, _ = transition(
        wallet, "FILL", {"action_key": plan.action, "fill_id": plan.fill_id}, params, ctx["rules"]
    )
    assert wallet.daily_blocked and wallet.drawdown_blocked
    event = deepcopy(scenario()["events"][0])
    event["type"] = "MARK"
    event["timestamp"] = "2026-10-01T00:20:00Z"
    event["observation"]["observed_at"] = event["timestamp"]
    event["observation"]["price"] = "110"
    wallet, _ = transition(wallet, "MARK", event, params, ctx["rules"])
    assert wallet.daily_blocked and wallet.drawdown_blocked
    wallet, _ = transition(wallet, "ORDER", order(2), params, ctx["rules"])
    assert wallet.intents[order(2)["action_key"]].state == "RESERVED"


def test_disabled_and_duplicate_filters(tmp_path):
    ctx = context(tmp_path)
    rules = ctx["rules"]
    for f in rules["filters"]:
        for key in list(f):
            if key not in ("filterType", "applyToMarket"):
                f[key] = "0"
    make_plan(order(), "disabled", ctx["parameters"], rules)
    rules["filters"].append(deepcopy(rules["filters"][0]))
    with pytest.raises(PaperError, match="DUPLICATE_FILTER"):
        make_plan(order(), "duplicate", ctx["parameters"], rules)


def test_stale_day_and_invalid_quote_types(tmp_path):
    ctx = context(tmp_path)
    for change in ("zero", "kind", "side"):
        event = order()
        if change == "zero":
            event["observation"]["price"] = "0"
        elif change == "kind":
            event["observation"]["kind"] = "UNKNOWN"
        else:
            event["side"] = "SHORT"
        with pytest.raises(PaperError):
            make_plan(event, "invalid", ctx["parameters"], ctx["rules"])


def test_actual_export_failure_leaves_no_manifest(tmp_path, monkeypatch):
    config = write_case(tmp_path)
    settings = load_config(config, {})
    assert paper.execute_paper(settings, "paper-run", "first")[0] == 0
    with PaperStore(tmp_path / "data") as store:
        report = summary(store, store.replay())
        with pytest.raises(PaperError, match="UNSAFE_REPORT_PATH"):
            export_report(store, report, "../escape", {})
        old = Path.rename

        def fail(*args, **kwargs):
            raise OSError("SYNTHETIC_SECRET")

        monkeypatch.setattr(Path, "rename", fail)
        with pytest.raises(OSError):
            export_report(store, report, "failed", {})
        assert not list((tmp_path / "data/reports").glob(".pending-*"))
        assert not (tmp_path / "data/reports/failed").exists()
        monkeypatch.setattr(Path, "rename", old)
        export_report(store, report, "recovered", {})


def test_context_corruption_and_block_survive_open(tmp_path):
    config = write_case(tmp_path)
    settings = load_config(config, {})
    assert paper.execute_paper(settings, "paper-run", "first")[0] == 0
    with PaperStore(tmp_path / "data") as store:
        store.db.execute("UPDATE metadata SET context='invalid'")
    code, result = paper.execute_paper(settings, "paper-run", "bad-context")
    assert code == 3 and result["blocked"] and not result["reconciled"]
    with PaperStore(tmp_path / "data") as store:
        assert store.blocked()


def test_durable_block_cannot_be_cleared_by_replay(tmp_path):
    config = write_case(tmp_path)
    settings = load_config(config, {})
    assert paper.execute_paper(settings, "paper-run", "first")[0] == 0
    with PaperStore(tmp_path / "data") as store:
        store.block()
        with pytest.raises(PaperError, match="WALLET_BLOCKED"):
            reconcile(store)
    code, result = paper.execute_paper(settings, "paper-run", "blocked")
    assert code == 3 and result["blocked"]


def test_report_config_conflict(tmp_path):
    config = write_case(tmp_path)
    settings = load_config(config, {})
    assert paper.execute_paper(settings, "paper-run", "first")[0] == 0
    changed = write_case(tmp_path, fee_bps="11")
    code, result = paper.execute_paper(load_config(changed, {}), "paper-report", "conflict")
    assert code == 3 and result["error_code"] == "WALLET_CONTEXT_CONFLICT"


def test_missing_history_is_not_reported_as_zero_wallet(tmp_path):
    config = write_case(tmp_path)
    settings = load_config(config, {})
    assert paper.execute_paper(settings, "paper-run", "first")[0] == 0
    with PaperStore(tmp_path / "data") as store:
        store.db.execute("DROP TRIGGER immutable_postings_DELETE")
        store.db.execute("DROP TRIGGER immutable_fills_DELETE")
        store.db.execute("DROP TRIGGER immutable_journal_DELETE")
        store.db.execute("DELETE FROM postings")
        store.db.execute("DELETE FROM fills")
        store.db.execute("DELETE FROM journal")
    code, result = paper.execute_paper(settings, "paper-reconcile", "missing-history")
    assert code == 3 and result["report_available"] is False and result["blocked"] is True
