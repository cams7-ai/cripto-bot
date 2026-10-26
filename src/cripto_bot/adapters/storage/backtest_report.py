"Snapshot reconciliado, CSV integral e HTML limitado sem recursos externos."

import json
from decimal import Decimal, localcontext
from pathlib import Path

from cripto_bot.adapters.storage.backtest_analytics import export_analytics
from cripto_bot.adapters.storage.backtest_sqlite import BacktestStore
from cripto_bot.domain.backtest.engine import State
from cripto_bot.domain.backtest.metrics import ratio_text
from cripto_bot.domain.backtest.models import PROFILES, SEGMENTS, VARIANTS, fingerprint
from cripto_bot.domain.market import canonical, iso
from cripto_bot.domain.portfolio.models import require_object, text


def _series(
    store: BacktestStore, key: str
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    count = store.db.execute("SELECT count(*) FROM equity WHERE run_key=?", (key,)).fetchone()[0]
    bucket = max(1, (count + 159) // 160)
    points: list[dict[str, object]] = []
    group: list[dict[str, object]] = []
    months: dict[str, list[dict[str, object]]] = {}

    def retain() -> None:
        if group:
            selected = {
                int(str(p["at"])): p
                for p in (
                    group[0],
                    min(group, key=lambda r: Decimal(str(r["equity"]))),
                    max(group, key=lambda r: Decimal(str(r["equity"]))),
                    group[-1],
                    max(group, key=lambda r: Decimal(str(r["drawdown"]))),
                )
            }
            points.extend(selected[k] for k in sorted(selected))
            group.clear()

    for index, (raw,) in enumerate(
        store.db.execute("SELECT data FROM equity WHERE run_key=? ORDER BY seq", (key,))
    ):
        row = require_object(json.loads(raw))
        group.append(row)
        month = iso(int(str(row["at"])))[:7]
        if month not in months:
            months[month] = [row, row]
        else:
            months[month][1] = row
        if (index + 1) % bucket == 0:
            retain()
    retain()
    monthly: list[dict[str, object]] = []
    previous: Decimal | None = None
    for month, (first, last) in months.items():
        initial = previous if previous is not None else Decimal(str(first["equity"]))
        final = Decimal(str(last["equity"]))
        monthly.append(
            {
                "month": month,
                "opening_equity": text(initial),
                "closing_equity": text(final),
                "net_return": ratio_text(final / initial - 1) if initial else None,
                "market_return": ratio_text(
                    Decimal(str(last["mark"])) / Decimal(str(first["mark"])) - 1
                ),
                "market_direction": (
                    "alta"
                    if Decimal(str(last["mark"])) > Decimal(str(first["mark"]))
                    else "baixa"
                    if Decimal(str(last["mark"])) < Decimal(str(first["mark"]))
                    else "lateral"
                ),
            }
        )
        previous = final
    return points, monthly


def snapshot(store: BacktestStore, states: dict[str, State]) -> dict[str, object]:
    curves = []
    with localcontext() as ctx:
        ctx.prec = 80
        for key, state in states.items():
            context = require_object(
                json.loads(
                    store.db.execute("SELECT context FROM runs WHERE run_key=?", (key,)).fetchone()[
                        0
                    ]
                )
            )
            assert state.metrics is not None
            points, months = _series(store, key)
            curves.append(
                {
                    "run_key": key,
                    "segment": context["segment"],
                    "variant": context["variant"],
                    "profile": context["profile"],
                    "start": context["start"],
                    "end": context["end"],
                    "completed": state.finished,
                    "measured_until": state.wallet.clock,
                    "metrics": state.metrics.summary(state.wallet),
                    "outcomes_by_reason": {
                        str(status) + ":" + str(reason): count
                        for status, reason, count in store.db.execute(
                            "SELECT json_extract(data,'$.status'),"
                            "json_extract(data,'$.reason'),count(*) "
                            "FROM outcomes WHERE run_key=? GROUP BY 1,2",
                            (key,),
                        )
                    },
                    "coordination_rejections_by_reason": {
                        str(reason): count
                        for reason, count in store.db.execute(
                            (
                                "SELECT json_extract(data,'$.execution_reason'),cou"
                                "nt(*) FROM decisions "
                            )
                            + (
                                "WHERE run_key=? AND json_extract(data,'$.status')="
                                "'COORDINATOR_REJECTED' "
                            )
                            + "GROUP BY 1",
                            (key,),
                        )
                    },
                    "monthly": months,
                    "chart": points,
                    "max_window": state.max_window,
                    "checkpoint_bytes": len(canonical(state.data()).encode()),
                }
            )
    curves.sort(
        key=lambda row: (
            SEGMENTS.index(str(row["segment"])),
            VARIANTS.index(str(row["variant"])),
            PROFILES.index(str(row["profile"])),
        )
    )
    body = {
        "schema": 1,
        "model": "paper-backtest-v1",
        "protocol": store.protocol(),
        "curves": curves,
        "reconciled": True,
        "final_test_status": "CONSUMED"
        if any(row["segment"] == "test" for row in curves)
        else "NOT_PROCESSED_IN_THIS_STORE",
        "blocked": store.blocked(),
        "limitations": [
            "OHLC open is a synthetic execution proxy; no liquidity guarantee.",
            "Filters are synthetic, not historical exchangeInfo. Fees paid in USDT.",
            ("Independent capital per curve; balances and returns must not be summed."),
            "Terminal mark to market leaves residual BTC; no implicit liquidation.",
            "Software cannot prove absence of human knowledge of the final period.",
            "No annualization or LIVE authorization; quote-currency risk remains.",
        ],
        "presentation": (
            "Up to 160 buckets per curve: first/min/max/last plus "
            "maximum drawdown. Metrics use every event."
        ),
    }
    return {**body, "result_sha256": fingerprint(body)}


def document(report: dict[str, object]) -> str:
    from typing import cast

    from cripto_bot.adapters.analytics_dependency import load

    return cast(
        str, load("cripto_analytics.presentation.backtest").document(report, segments=SEGMENTS)
    )


def export_report(store: BacktestStore, report: dict[str, object], run_id: str) -> Path:
    from typing import cast

    from cripto_bot.adapters.analytics_dependency import load
    from cripto_bot.adapters.storage.analytics_source import AnalyticsRows

    return cast(
        Path,
        load("cripto_analytics.application.publish").publish_backtest(
            report,
            AnalyticsRows(store),
            store.directory,
            run_id,
            segments=SEGMENTS,
            renderer=document,
            analytics_exporter=lambda rows, snapshot, path: export_analytics(store, snapshot, path),
        ),
    )
