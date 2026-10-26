"Snapshot reconciliado, CSV integral e HTML limitado sem recursos externos."

import csv
import html
import json
import shutil
import tempfile
from decimal import Decimal, localcontext
from pathlib import Path

from cripto_bot.adapters.backtest_dataset import file_hash
from cripto_bot.adapters.storage.backtest_sqlite import BacktestStore
from cripto_bot.domain.backtest.engine import State
from cripto_bot.domain.backtest.metrics import ratio_text
from cripto_bot.domain.backtest.models import PROFILES, SEGMENTS, VARIANTS, fingerprint
from cripto_bot.domain.market import canonical, iso, sequence
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
    def esc(value: object) -> str:
        return html.escape("N/A" if value is None else str(value), quote=True)

    protocol = require_object(report["protocol"])
    dataset = require_object(protocol["dataset"])
    curves = report["curves"]
    assert isinstance(curves, list)
    rows = []
    charts = []
    for raw in curves:
        curve = require_object(raw)
        m = require_object(curve["metrics"])
        label = " / ".join(str(curve[k]) for k in ("segment", "variant", "profile"))
        cells = [
            curve["segment"],
            curve["variant"],
            curve["profile"],
            m["final_equity"],
            m["net_return"],
            m["maximum_drawdown"],
            m["average_exposure"],
            m["closed_trades"],
            m["fills"],
            m["fees"],
            m["residual_btc"],
        ]
        rows.append("<tr>" + "".join("<td>" + esc(v) + "</td>" for v in cells) + "</tr>")
        # Fixed viewBox; numeric SVG coordinates are derived only from validated Decimal data.
        chart = curve["chart"]
        assert isinstance(chart, list)
        if chart:
            data = [require_object(p) for p in chart]
            values = [Decimal(str(p["equity"])) for p in data]
            low = min(values)
            high = max(values)
            span = high - low or Decimal(1)
            start = int(str(curve["start"]))
            end = int(str(curve["end"]))
            coordinates = " ".join(
                f"{20 + 940 * (int(str(p['at'])) - start) / (end - start):.2f},{180 - float((v - low) / span) * 150:.2f}"  # noqa: E501
                for p, v in zip(data, values, strict=True)
            )
            dd = " ".join(
                f"{20 + 940 * (int(str(p['at'])) - start) / (end - start):.2f},{210 + float(Decimal(str(p['drawdown']))) * 35:.2f}"  # noqa: E501
                for p in data
            )
            notes = [
                (
                    "Estado",
                    "Concluída" if curve["completed"] else "PARCIAL - checkpoint confirmado",
                ),
                ("Janela UTC", iso(start) + " até " + iso(end) + " (fim exclusivo)"),
                (
                    "Decisões / direcionais / HOLD",
                    str(m["decisions"])
                    + " / "
                    + str(m["directional_decisions"])
                    + " / "
                    + str(m["holds"]),
                ),
                ("Recusas / expirações", str(m["rejected"]) + " / " + str(m["expired"])),
                (
                    "PnL realizado / não realizado (USDT)",
                    str(m["realized"]) + " / " + str(m["unrealized"]),
                ),
                ("Custo remanescente (USDT)", m["cost_basis"]),
                ("Impacto de preço (USDT)", m["price_impact"]),
                (
                    "Exposição máxima / tempo em posição",
                    str(m["maximum_exposure"])
                    + " / "
                    + ("N/A" if m["time_in_position"] is None else str(m["time_in_position"])),
                ),
                ("Turnover sobre capital inicial", m["turnover"]),
                (
                    "Trava diária / drawdown",
                    ("sim" if m["daily_blocked"] else "não")
                    + " / "
                    + ("sim" if m["drawdown_blocked"] else "não"),
                ),
            ]
            details = (
                "<dl>"
                + "".join("<dt>" + esc(k) + "</dt><dd>" + esc(v) + "</dd>" for k, v in notes)
                + "</dl>"
            )
            for title, key in (
                ("Resultados de intenção por motivo", "outcomes_by_reason"),
                ("Recusas de coordenação por motivo", "coordination_rejections_by_reason"),
            ):
                reasons = require_object(curve.get(key, {}))
                details += (
                    "<p><b>"
                    + esc(title)
                    + ":</b> "
                    + ("; ".join(esc(k) + " = " + esc(v) for k, v in reasons.items()) or "nenhum")
                    + "</p>"
                )
            charts.append(
                f'<article><h3>{esc(label)}</h3><p>Patrimônio {esc(low)} — {esc(high)} USDT · curva azul; drawdown vermelho.</p>{details}<svg viewBox="0 0 980 250" role="img" aria-label="{esc(label)}"><path d="M20 190H960M20 210H960" stroke="#cbd5e1"/><polyline points="{coordinates}" fill="none" stroke="#167b9b" stroke-width="2"/><polyline points="{dd}" fill="none" stroke="#b84742" stroke-width="1.5"/></svg></article>'  # noqa: E501
            )
    limitations = report["limitations"]
    assert isinstance(limitations, list)
    source = (
        "DADOS SINTÉTICOS — ensaio técnico"
        if dataset["source"] == "fixture"
        else "DADOS PÚBLICOS — avaliação histórica"
    )
    headers = (
        "Segmento",
        "Variante",
        "Perfil",
        "E no checkpoint USDT",
        "Retorno¹",
        "MDD¹",
        "Exposição média¹",
        "Trades fechados",
        "Fills",
        "Taxas USDT",
        "BTC residual",
    )
    ranges = require_object(protocol["segments"])
    schedule = (
        "<h2>Janelas e custos congelados</h2><table><thead>"
        "<tr><th>Segmento</th><th>Início UTC</th><th>Fim ex"
        "clusivo UTC</th><th>Candles de score</th></tr></th"
        "ead><tbody>"
    )
    for segment in SEGMENTS:
        bounds = sequence(ranges[segment])
        a, b = int(str(bounds[0])), int(str(bounds[1]))
        schedule += (
            "<tr>"
            + "".join(
                "<td>" + esc(v) + "</td>" for v in (segment, iso(a), iso(b), (b - a) // 900000)
            )
            + "</tr>"
        )
    schedule += (
        "</tbody></table><p>Aquecimento: "
        + esc(dataset["warmup"])
        + (
            " fechamentos, sem ordens ou PnL. Filtros sintético"
            "s/proxy, sem comprovação de exchangeInfo histórico"
            ".</p>"
        )
    )
    schedule += (
        "<table><thead><tr><th>Variante</th><th>Taxa bps</t"
        "h><th>Spread bps</th><th>Slippage bps</th><th>Atra"
        "so extra (candles)</th></tr></thead><tbody>"
    )
    for row in (
        ("base", 10, 10, 5, 0),
        ("double_costs", 20, 20, 10, 0),
        ("delay_1candle", 10, 10, 5, 1),
        ("double_costs_delay", 20, 20, 10, 1),
    ):
        schedule += "<tr>" + "".join("<td>" + esc(v) + "</td>" for v in row) + "</tr>"
    schedule += (
        "</tbody></table><p>Sinal: abertura agendada +5 s. "
        "Referência observada em +1 ms; fill em +2 ms; fres"
        "hness de cotação separada, até 5 s. Um pendente bl"
        "oqueia outro compromisso.</p>"
    )
    page = (
        (
            '<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta htt'
            'p-equiv="Content-Security-Policy" content="default-src \'none\'; st'
            'yle-src \'unsafe-inline\'"><meta name="viewport" content="width=dev'
            'ice-width"><title>Avaliação histórica · Sprint 05</title><style>b'
            "ody{margin:0;background:#edf2f7;color:#182b43;font:15px system-ui"
            "}main{max-width:1450px;margin:auto;padding:48px}header{background"
            ":#122c46;color:white;padding:36px;border-radius:12px}h1{font-size"
            ":32px}h2{margin-top:40px}p,li{line-height:1.6}code{overflow-wrap:"
            "anywhere}table{border-collapse:collapse;width:100%;font-size:12px"
            ";background:white}th,td{text-align:right;padding:10px;border-bott"
            "om:1px solid #dce4ec}th{background:#dbe8f2}td:first-child,th:firs"
            "t-child{text-align:left}.scroll{overflow-x:auto}article{backgroun"
            "d:white;padding:20px;margin:20px 0;border-radius:10px;break-insid"
            "e:avoid}svg{width:100%;max-height:250px}a{color:#087a95}@media pr"
            "int{main{padding:0}header{background:white;color:#122c46}table{fo"
            "nt-size:9px}}</style><main><header><p>CRIPTO · PAPER / LOCAL / BT"
            "CUSDT / 15m</p><h1>Backtesting e avaliação fora da amostra</h1><p"
            ">"
        )
        + esc(source)
        + (
            "</p></header><h2>Protocolo e leitura dos resultados</h2><p>Capita"
            "l inicial independente: 10000 USDT. SMA20/50 com teto de 10%; ref"
            "erências em caixa, buy-and-hold 100% e 10%. Quatro variantes fixa"
            "das previamente. Retornos, MDD e exposições são razões (0,01 = 1%"
            "). Nenhuma curva é somada às demais.</p><p>Origem: "
        )
        + esc(dataset["source"])
        + " · candles: "
        + esc(dataset["count"])
        + " · janela de score: "
        + esc(iso(int(str(dataset["score_start"]))))
        + " até "
        + esc(iso(int(str(dataset["score_end"]))))
        + " (fim exclusivo).</p><p>Protocolo: <code>"
        + esc(protocol["protocol_hash"])
        + "</code><br>Dataset: <code>"
        + esc(dataset["data_sha256"])
        + "</code><br>Resultado: <code>"
        + esc(report["result_sha256"])
        + (
            "</code></p><p>Teste final é processado somente após desenvolvimen"
            "to e validação completos. Reexportação é leitura do snapshot reco"
            "nciliado; não reabre uma amostra intocada. Os hashes e o registro"
            " local não provam ausência de conhecimento humano prévio.</p><h2>"
            'Comparação por carteira independente</h2><div class="scroll"><tab'
            "le><thead><tr>"
        )
        + "".join("<th>" + esc(h) + "</th>" for h in headers)
        + "</tr></thead><tbody>"
        + "".join(rows)
        + (
            "</tbody></table></div><p>¹ Razões, sem anualização. Trades são ci"
            "clos encerrados; fills incluem entradas e saídas. Saldo em BTC pe"
            "rmanece marcado ao fim.</p><h2>Curvas de patrimônio e drawdown</h"
            "2><p>Redução determinística por buckets, preservando primeiro, mí"
            "nimo, máximo e último patrimônio. Todas as métricas oficiais usam"
            " a série integral de eventos.</p>"
        )
        + "".join(charts)
        + (
            '<h2>Auditoria e limitações</h2><p><a href="report.json">Relatório'
            ' completo e subperíodos mensais</a> · <a href="equity.csv">Equity'
            ' integral</a> · <a href="trades.csv">Fills</a> · <a href="decisio'
            'ns.csv">Decisões</a> · <a href="manifest.json">Manifesto de integ'
            "ridade</a></p><ul>"
        )
        + "".join("<li>" + esc(x) + "</li>" for x in limitations)
        + (
            "</ul><p>Sem projeção de rentabilidade, ordens reais ou autorizaçã"
            "o LIVE.</p></main></html>"
        )
    )

    partial_note = (
        (
            "<p><strong>PARCIAL: métricas até o checkpoint confirmado; "
            "a janela planejada não foi concluída.</strong></p>"
        )
        if any(not require_object(c)["completed"] for c in curves)
        else (
            "<p>Curvas exibidas concluídas; segmentos ainda não processados "
            "não integram esta tabela.</p>"
        )
        if curves
        else "<p>Protocolo congelado; nenhuma curva processada.</p>"
    )
    schedule = partial_note + schedule
    return page.replace(
        "<h2>Comparação por carteira independente</h2>",
        schedule + "<h2>Comparação por carteira independente</h2>",
    )


def export_report(store: BacktestStore, report: dict[str, object], run_id: str) -> Path:
    parent = store.directory / "exports"
    parent.mkdir(exist_ok=True)
    target = parent / run_id
    if target.parent != parent or not run_id.replace("-", "").isalnum():
        raise ValueError("UNSAFE_EXPORT_PATH")
    pending = Path(tempfile.mkdtemp(prefix=".pending-", dir=parent))
    try:
        (pending / "report.json").write_text(canonical(report) + "\n")
        (pending / "report.html").write_text(document(report))
        for name, table in (("equity", "equity"), ("trades", "fills"), ("decisions", "decisions")):
            with (pending / f"{name}.csv").open("w", newline="") as stream:
                if name == "equity":
                    fields = [
                        "run_key",
                        "at",
                        "equity",
                        "usdt_free",
                        "usdt_reserved",
                        "btc",
                        "mark",
                        "cost_basis",
                        "fees",
                        "realized",
                        "unrealized",
                        "exposure",
                        "drawdown",
                    ]
                elif name == "trades":
                    fields = [
                        "run_key",
                        "action_key",
                        "fill_id",
                        "side",
                        "quantity",
                        "price",
                        "notional",
                        "fee",
                        "mark",
                        "fill_at",
                        "realized_delta",
                        "cost_delta",
                        "data_json",
                    ]
                else:
                    fields = [
                        "run_key",
                        "decision_id",
                        "as_of",
                        "target",
                        "reason",
                        "status",
                        "scheduled_at",
                        "valid_until",
                        "budget_cap",
                        "prefix_sha256",
                        "data_json",
                    ]
                writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                for key, body in store.db.execute(
                    f"SELECT run_key,data FROM {table} ORDER BY run_key,rowid"
                ):
                    row = require_object(json.loads(body))
                    flat = {
                        **row,
                        **(require_object(row["plan"]) if name == "trades" else {}),
                        "run_key": key,
                    }
                    if name != "equity":
                        flat["data_json"] = body
                    writer.writerow(flat)
        manifest = {
            "schema": 1,
            "model": report["model"],
            "protocol_hash": require_object(report["protocol"])["protocol_hash"],
            "result_sha256": report["result_sha256"],
            "files": {p.name: file_hash(p) for p in sorted(pending.iterdir())},
        }
        (pending / "manifest.json").write_text(canonical(manifest) + "\n")
        pending.rename(target)
        return target / "manifest.json"
    finally:
        if pending.exists():
            shutil.rmtree(pending)
