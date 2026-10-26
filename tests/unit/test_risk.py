from decimal import Decimal as D

import pytest

from cripto_bot.domain.portfolio.engine import transition
from cripto_bot.domain.portfolio.models import PaperError, Wallet, day_at, instant
from cripto_bot.domain.risk.execution import make_plan
from cripto_bot.domain.risk.limits import admit, update_risk
from tests.paper_factories import context, initialized, order, scenario


def test_entry_boundary_and_exposure(tmp_path):
    ctx, wallet = initialized(tmp_path)
    params = {**ctx["parameters"], "fee_bps": "0", "spread_bps": "0", "slippage_bps": "0"}
    plan = make_plan(order(quantity="10"), "exact", params, ctx["rules"])
    admit(wallet, plan, params)
    plan = make_plan(order(quantity="10.000001"), "over", params, ctx["rules"])
    with pytest.raises(PaperError, match="ENTRY_LIMIT"):
        admit(wallet, plan, params)
    params["max_entry_fraction"] = "1"
    with pytest.raises(PaperError, match="EXPOSURE_LIMIT"):
        admit(wallet, plan, params)


def test_daily_drawdown_thresholds_and_recovery_does_not_reset(tmp_path):
    ctx = context(tmp_path)
    clock = instant("2026-10-01T00:30:00Z")
    wallet = Wallet(
        usdt_free=D("9900"), peak=D("10000"), baseline=D("10000"), day=day_at(clock), clock=clock
    )
    update_risk(wallet, ctx["parameters"])
    assert wallet.daily_blocked and not wallet.drawdown_blocked
    wallet.usdt_free = D("9500")
    update_risk(wallet, ctx["parameters"])
    assert wallet.drawdown_blocked
    wallet.usdt_free = D("10000")
    update_risk(wallet, ctx["parameters"])
    assert wallet.daily_blocked and wallet.drawdown_blocked
    day = scenario()["events"][0]
    day["timestamp"] = "2026-10-02T00:00:00Z"
    day["observation"]["observed_at"] = day["timestamp"]
    wallet, _ = transition(wallet, "DAY_START", day, ctx["parameters"], ctx["rules"])
    assert not wallet.daily_blocked and wallet.drawdown_blocked and wallet.peak == D("10000")
    with pytest.raises(PaperError, match="DUPLICATE"):
        transition(wallet, "DAY_START", day, ctx["parameters"], ctx["rules"])


def test_sell_under_lock_and_no_short(tmp_path):
    ctx, wallet = initialized(tmp_path)
    plan = make_plan(order(2), "sell", ctx["parameters"], ctx["rules"])
    with pytest.raises(PaperError, match="INSUFFICIENT_BTC"):
        admit(wallet, plan, ctx["parameters"])
    wallet.btc_free = D(1)
    wallet.cost_basis = D(100)
    wallet.daily_blocked = True
    wallet.drawdown_blocked = True
    admit(wallet, plan, ctx["parameters"])
    buy = make_plan(order(), "buy", ctx["parameters"], ctx["rules"])
    with pytest.raises(PaperError, match="RISK_LOCKED"):
        admit(wallet, buy, ctx["parameters"])
    wallet.daily_blocked = wallet.drawdown_blocked = False
    with pytest.raises(PaperError, match="POSITION_ALREADY_OPEN"):
        admit(wallet, buy, ctx["parameters"])


def test_missing_day_and_insufficient_cash(tmp_path):
    ctx, wallet = initialized(tmp_path)
    plan = make_plan(order(), "buy", ctx["parameters"], ctx["rules"])
    wallet.day = ""
    with pytest.raises(PaperError, match="MISSING_DAY"):
        admit(wallet, plan, ctx["parameters"])
    wallet.day = day_at(plan.observed_at)
    wallet.usdt_free = D(1)
    with pytest.raises(PaperError, match="INSUFFICIENT_USDT"):
        admit(wallet, plan, ctx["parameters"])
