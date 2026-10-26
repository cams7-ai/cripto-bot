"""Reconciled continuous snapshots; publication delegates to analytics."""

import json
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import cast

from cripto_core.errors import PaperError
from cripto_core.numeric import money, text
from cripto_core.serialization import fingerprint

from cripto_bot.adapters.analytics_dependency import load
from cripto_bot.adapters.continuous_config import ContinuousSettings
from cripto_bot.adapters.storage.continuous_sqlite import ContinuousStore
from cripto_bot.adapters.storage.notification_sqlite import inspect
from cripto_bot.domain.continuous.models import DAY, MODEL
from cripto_bot.domain.risk.limits import ratio


def snapshot(
    store: ContinuousStore,
    now: int,
    *,
    day: str | None = None,
    notification_dir: Path | None = None,
) -> dict[str, object]:
    full = store.reconcile()
    state = full
    day_start = None
    day_end = None
    if day is not None:
        try:
            parsed = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
            if parsed.date().isoformat() != day:
                raise ValueError
            day_start = int(parsed.timestamp()) * 1000
            day_end = day_start + DAY
        except ValueError:
            raise PaperError("INVALID_REPORT_DAY") from None
        if full.t0 is None or full.tend is None or not full.t0 <= day_start < full.tend:
            raise PaperError("DAY_OUTSIDE_PILOT")
        state = store.reconcile(before=day_end)
    end = min(now, day_end) if day_end is not None else now
    cycles = [
        c
        for c in full.cycles.values()
        if day_start is None or day_start <= int(str(c["close"])) < cast(int, day_end)
    ]
    cycles = [
        {**c, "valid": False, "reason": "LATE_COMMIT"}
        if int(str(c["close"])) in full.late_slots
        else c
        for c in cycles
    ]
    valid = sum(c["valid"] is True for c in cycles)
    expected = 96 if day else 672
    completed = full.tend is not None and now >= full.tend
    unknown = any(o.state == "UNKNOWN" for o in full.orders.values())
    candidate = (
        completed
        and len(full.cycles) == 672
        and sum(
            c["valid"] is True and int(str(c["close"])) not in full.late_slots
            for c in full.cycles.values()
        )
        >= 666
        and not unknown
        and full.observation_kind == "binance_public"
    )
    fresh = state.last_mark_at >= 0 and 0 <= end - state.last_mark_at <= 5000
    events = [
        {"event_id": eid, "category": json.loads(body)["category"], "seq": seq}
        for eid, seq, body in store.db.execute(
            "SELECT event_id,seq,data FROM notification_events ORDER BY seq"
        )
    ]
    wallet = state.wallet
    with localcontext() as decimal_context:
        decimal_context.prec = 80
        known_metrics = {
            "unrealized": text(money(wallet.quantity * wallet.mark) - wallet.cost_basis),
            "exposure_fraction": text(
                ratio(wallet.quantity * wallet.mark + wallet.usdt_reserved, wallet.equity)
            ),
            "daily_loss_fraction": text(
                max(Decimal(0), ratio(wallet.baseline - wallet.equity, wallet.baseline))
            ),
            "drawdown_fraction": text(
                max(Decimal(0), ratio(wallet.peak - wallet.equity, wallet.peak))
            ),
        }
    wallet_view = {
        **wallet.data(),
        **{key: value if fresh else None for key, value in known_metrics.items()},
    }
    report = {
        "schema": 1,
        "model": MODEL,
        "pilot_id": state.pilot_id,
        "observation_kind": state.observation_kind,
        "observation_status": "SYNTHETIC_NOT_PILOT"
        if state.observation_kind == "fixture"
        else "OBSERVATION_CANDIDATE"
        if candidate
        else "INCOMPLETE"
        if not completed
        else "FAILED_CRITERIA",
        "pilot_approved": False,
        "approval_note": (
            "Requires seven real days, authorized delivery, full D01-D22 evidence "
            "and review; software never self-approves the pilot."
        ),
        "reconciled": True,
        "status": state.status,
        "t0": state.t0,
        "tend": state.tend,
        "day": day,
        "report_at": now,
        "day_complete": day_end is not None and now >= day_end,
        "elapsed_wall_ms": max(0, min(now, full.tend or now) - (full.t0 or now)),
        "eligible_slots": expected,
        "processed_slots": valid,
        "recorded_slots": len(cycles),
        "missing_or_failed_slots": expected - valid,
        "late_or_failed": [c for c in cycles if c["valid"] is not True],
        "wallet": wallet_view,
        "risk_parameters": state.parameters,
        "metrics_at_last_valid_quote": known_metrics if state.last_mark_at >= 0 else None,
        "orders": state.data()["orders"],
        "position": text(state.wallet.quantity),
        "equity": text(state.wallet.equity) if fresh else None,
        "equity_at_last_valid_quote": text(state.wallet.equity),
        "mark_available": fresh,
        "last_mark_at": state.last_mark_at,
        "clock_and_data": {
            "last_event_at": state.wallet.clock,
            "metadata_at": state.metadata_at,
            "metadata_age_ms": end - state.metadata_at if state.metadata_at >= 0 else None,
            "quote_age_ms": end - state.last_mark_at if state.last_mark_at >= 0 else None,
            "slot_processing_delays_ms": [int(str(c["at"])) - int(str(c["close"])) for c in cycles],
            "uptime_ms": None,
            "restart_count": None,
            "monitoring_note": "Uptime/restarts/resources require independent supervisor evidence.",
        },
        "generation": state.generation,
        "controls": state.controls,
        "notifications": inspect(notification_dir, now)
        if notification_dir
        else {"status": "NOT_CONFIGURED", "as_of": now},
        "notification_events": events,
        "runtime_identity": state.context.get("runtime"),
        "configuration_sha256": fingerprint(state.configuration),
        "financial_sha256": fingerprint(
            {"wallet": state.wallet.data(), "orders": state.data()["orders"]}
        ),
        "limitations": [
            "Local partials use a conservative top-of-book proxy, not exchange fills/queue/impact.",
            "bookTicker has no market timestamp; freshness uses observed local receipt.",
            "Cold cache installation is not claimed; historical final window was not re-evaluated.",
            "Runtime approval and message receipts are separate evidence.",
        ],
    }
    return {**report, "result_sha256": fingerprint(report)}


def export_report(config: ContinuousSettings, report: dict[str, object], run_id: str) -> Path:
    library = load("cripto_analytics.application.publish")
    renderer = load("cripto_analytics.presentation.continuous").document
    # Generic publication already supports consumer-owned models/provenance/renderer.
    return cast(
        Path,
        library.publish_strategy(
            config.data_dir,
            report,
            run_id,
            {"producer": "cripto-bot", "model": MODEL, "result_sha256": report["result_sha256"]},
            model=MODEL,
            renderer=renderer,
        ),
    )
