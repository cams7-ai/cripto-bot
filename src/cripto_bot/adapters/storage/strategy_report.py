"""JSON/HTML do mesmo snapshot; publicacao nunca avalia ou executa estrategia."""

import html
import json
import os
import re
import shutil
import tempfile
from decimal import Decimal, localcontext
from pathlib import Path

from cripto_bot.adapters.storage.paper_report import summary as financial_summary
from cripto_bot.adapters.storage.strategy_sqlite import StrategyStore
from cripto_bot.domain.market import canonical, digest, sequence
from cripto_bot.domain.portfolio.models import PaperError, require_object
from cripto_bot.domain.strategy.engine import MODEL, StrategyWallet


def summary(
    store: StrategyStore, state: StrategyWallet, *, reconciled: bool = True
) -> dict[str, object]:
    result = financial_summary(store, state, reconciled=reconciled)
    cycles = list(state.cycles.values())
    result.update(
        model=MODEL,
        decisions=cycles,
        instances=state.instances,
        attributions=list(state.attributions.values()),
        config_generation=state.generation,
        config_hash=state.config_hash,
        total_evaluated_cycles=len(cycles),
        total_warmup=sum(c["reason"] == "WARMUP" for c in cycles),
        total_hold=sum(c["target"] == "HOLD" for c in cycles),
        total_directional_decisions=sum(c["target"] != "HOLD" for c in cycles),
        total_rejected_intents=sum(
            c["status"] in ("RISK_REJECTED", "NOT_EXECUTED", "REJECTED") for c in cycles
        ),
    )
    return result


def diagnostic(store: StrategyStore) -> dict[str, object]:
    try:
        return summary(store, store.replay())
    except (PaperError, ValueError, KeyError, TypeError):
        try:
            return summary(store, store.replay(check_projection=False), reconciled=False)
        except (PaperError, ValueError, KeyError, TypeError):
            return {"reconciled": False, "blocked": True, "report_available": False}


def render_html(report: dict[str, object]) -> str:
    def esc(value: object) -> str:
        return html.escape(str(value), quote=True)

    def table(headers: list[str], rows: list[list[object]]) -> str:
        return (
            '<div class="scroll"><table><thead><tr>'
            + "".join("<th>" + esc(x) + "</th>" for x in headers)
            + "</tr></thead><tbody>"
            + "".join(
                "<tr>" + "".join("<td>" + esc(x) + "</td>" for x in row) + "</tr>" for row in rows
            )
            + "</tbody></table></div>"
        )

    wallet = require_object(report["wallet"])
    cycles = [require_object(c) for c in sequence(report["decisions"])]
    context = require_object(report["context"])
    parameters = require_object(context["parameters"])

    def cycle_row(c: dict[str, object]) -> list[object]:
        return [
            c["as_of"],
            c["close"],
            c["reason"],
            c.get("fast", "-"),
            c.get("slow", "-"),
            c["target"],
            c["status"],
            c["execution_reason"] or "-",
        ]

    intents = require_object(wallet["intents"])
    trades = []
    for action, value in intents.items():
        intent = require_object(value)
        plan = intent.get("plan")
        if plan:
            p = require_object(plan)
            trades.append(
                [p["side"], p["quantity"], p["price"], p["fee"], intent["state"], action[:16]]
            )
    with localcontext() as decimal_context:
        decimal_context.prec = 80
        equities = [Decimal(str(c["equity_after"])) for c in cycles]
        graph = ""
        if equities:
            lo, hi = min(equities), max(equities)
            span = hi - lo or Decimal(1)
            points = " ".join(
                f"{Decimal(35) + Decimal(i) * 630 / max(1, len(equities) - 1):.2f},"
                f"{Decimal(125) - (v - lo) * 90 / span:.2f}"
                for i, v in enumerate(equities)
            )
            graph = (
                '<svg viewBox="0 0 700 160" role="img" aria-label="Patrimônio sintético por ciclo">'
                '<path d="M35 20 V125 H675" fill="none" stroke="#9aabb9"/>'
                f'<polyline points="{points}" fill="none" stroke="#087f82" stroke-width="3"/>'
                '<text x="35" y="151">Ciclo 1</text>'
                f'<text x="590" y="151">Ciclo {len(equities)}</text>'
                "</svg>"
                f'<p class="small">Faixa observada nos ciclos: {esc(lo)} a {esc(hi)} USDT. '
                "Marcação "
                "sintética, incluindo o custo do fill quando ocorrido.</p>"
            )
    css = (
        "body{margin:0;background:#edf2f6;color:#132d46;font:16px/1.5"
        " system-ui,sans-serif}main{max-width:1100px;margin:30px auto"
        ";background:white;padding:40px;border-top:5px solid #087f82}"
        "h1{font-size:34px;margin:12px 0}h2{color:#087f82;margin-top:"
        "30px}h3{font-size:18px}.small{font-size:13px;color:#536575;o"
        "verflow-wrap:anywhere}.badge{color:#087f82;font-weight:bold}"
        ".scroll{overflow-x:auto}table{width:100%;border-collapse:col"
        "lapse;font-size:13px;margin:15px 0}th,td{padding:10px;text-a"
        "lign:left;vertical-align:top;overflow-wrap:anywhere}th{backg"
        "round:#132d46;color:white}tr:nth-child(even){background:#edf"
        "4f7}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-siz"
        "e:12px}svg{width:100%;max-height:220px}svg text{font-size:12"
        "px;fill:#536575}details{margin:20px 0}.notice{background:#ed"
        "f4f7;padding:15px;border-left:4px solid #087f82}@media(max-w"
        "idth:600px){main{margin:0;padding:18px}h1{font-size:28px}}@m"
        "edia print{body{background:white}main{margin:0;padding:0;bor"
        "der:0}.scroll{overflow:visible}table{font-size:10px}th{color"
        ":#132d46;background:#edf4f7}tr{break-inside:avoid}h2{break-a"
        "fter:avoid}details{display:block}}"
    )
    sections = [
        (
            '<!doctype html><html lang="pt-BR"><head><meta charset="utf-8'
            '"><meta name="viewport" content="width=device-width,initial-'
            'scale=1"><meta http-equiv="Content-Security-Policy" content='
            "\"default-src 'none'; style-src 'unsafe-inline'\"><title>Sprin"
            "t 04 | Relatório da SMA</title><style>"
        )
        + css
        + "</style></head><body><main>",
        (
            '<p class="badge">CRIPTO BOT / SPRINT 04 / PAPER OFFLINE</p><'
            "h1>Processamento da estratégia SMA</h1>"
        ),
        (
            '<p class="notice">Teste funcional com dados sintéticos e dec'
            "isões automáticas por cruzamento. Não é backtest fora da amo"
            "stra ou evidência de rentabilidade real. Fill integral deter"
            "minístico; taxas somente em USDT. Ordens externas: 0.</p>"
        ),
        '<p class="small">Carteira '
        + esc(parameters["wallet_id"])
        + " • Dataset "
        + esc(parameters["dataset_id"])
        + " • Modelo "
        + esc(MODEL)
        + " • Geração "
        + esc(report["config_generation"])
        + "</p>",
        "<h2>Resumo do processamento</h2>",
        table(
            ["Indicador", "Total histórico", "Nesta execução"],
            [
                [
                    "Ciclos avaliados",
                    report["total_evaluated_cycles"],
                    report.get("evaluated_cycles", 0),
                ],
                ["HOLD (inclui warm-up)", report["total_hold"], "-"],
                ["Warm-up", report["total_warmup"], "-"],
                [
                    "Decisões direcionais",
                    report["total_directional_decisions"],
                    report.get("directional_decisions", 0),
                ],
                [
                    "Fills simulados",
                    report["total_simulated_fills"],
                    report.get("simulated_fills", 0),
                ],
                [
                    "Recusas / não executadas",
                    report["total_rejected_intents"],
                    report.get("rejected_intents", 0),
                ],
                ["Ciclos reconhecidos como replay", "-", report.get("replayed_actions", 0)],
            ],
        ),
        "<h2>Carteira e resultado</h2>",
        table(
            ["Métrica", "Valor"],
            [
                [label, wallet[name]]
                for name, label in (
                    ("usdt_free", "USDT disponível"),
                    ("usdt_reserved", "USDT reservado"),
                    ("btc_free", "BTC disponível"),
                    ("btc_reserved", "BTC reservado"),
                    ("cost_basis", "Custo da posição (USDT)"),
                    ("fees", "Taxas acumuladas (USDT)"),
                    ("realized", "Resultado realizado (USDT)"),
                    ("unrealized", "Não realizado (USDT)"),
                    ("equity", "Patrimônio final (USDT)"),
                    ("peak", "Pico de patrimônio (USDT)"),
                    ("baseline", "Patrimônio no início do dia (USDT)"),
                    ("exposure_fraction", "Exposição final (fração)"),
                    ("daily_loss_fraction", "Perda diária (fração)"),
                    ("drawdown_fraction", "Drawdown final (fração)"),
                )
            ],
        ),
        (
            '<p class="small">Valores monetários em USDT; saldos BTC em B'
            "TC. Exposição, perda e drawdown são frações (multiplicar por 100 "
            "para porcentagem). Drawdown é o estado final relativo ao pic"
            "o, não o máximo de toda a série.</p>"
        ),
        graph,
        "<h2>Por que houve decisão?</h2>",
        table(
            [
                "Disponível em UTC",
                "Fechamento",
                "Motivo",
                "SMA curta",
                "SMA longa",
                "Alvo",
                "Estado",
                "Motivo de execução",
            ],
            [cycle_row(c) for c in cycles if c["target"] != "HOLD"],
        ),
        "<h2>Planos de execução e custos</h2>",
        table(["Lado", "BTC", "Preço USDT/BTC", "Taxa USDT", "Estado", "ID (prefixo)"], trades),
        "<h2>Risco e reconciliação</h2>",
        table(
            ["Controle", "Valor"],
            [
                [label, parameters[name]]
                for name, label in (
                    ("max_entry_fraction", "Entrada máxima (fração)"),
                    ("max_exposure_fraction", "Exposição máxima (fração)"),
                    ("max_daily_loss_fraction", "Limite de perda diária (fração)"),
                    ("max_drawdown_fraction", "Limite de drawdown (fração)"),
                    ("max_quote_age_ms", "Idade máxima da observação (ms)"),
                    ("fee_bps", "Taxa por operação (bps)"),
                    ("spread_bps", "Spread total (bps)"),
                    ("slippage_bps", "Slippage adverso (bps)"),
                )
            ]
            + [
                ["Reconciliada", "Sim" if report["reconciled"] else "Não"],
                ["Bloqueada", "Sim" if report["blocked"] else "Não"],
                ["Trava diária", "Ativa" if wallet["daily_blocked"] else "Inativa"],
                ["Trava de drawdown", "Ativa" if wallet["drawdown_blocked"] else "Inativa"],
            ],
        ),
        (
            '<p class="small">Limites acima são os globais. Cada decisão '
            "no detalhamento registra o mínimo efetivo global/instância/p"
            "ar. Bps: um ponto-base = 0,01%; spread total dividido por la"
            "do em REFERENCE.</p>"
        ),
        "<details><summary>Todos os ciclos, incluindo HOLD</summary>"
        + table(
            ["UTC", "Fechamento", "Motivo", "SMA curta", "SMA longa", "Alvo", "Estado", "Recusa"],
            [cycle_row(c) for c in cycles],
        )
        + "</details>",
        "<details><summary>Configuração, propriedade e evidências</summary><pre>"
        + esc(
            json.dumps(
                {
                    "context": context,
                    "instances": report["instances"],
                    "config_hash": report["config_hash"],
                    "financial_sha256": report["financial_sha256"],
                    "attributions": report["attributions"],
                    "decisions": cycles,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        + "</pre></details>",
        (
            '<p class="small">Fonte: snapshot reconciliado do journal SQL'
            "ite v2. Relatório derivado: exportar não avalia sinais nem p"
            "roduz fills. O manifesto identifica hashes, código e arquivo"
            "s desta publicação. Casos inválidos, falhas e resultados esp"
            "erados/obtidos da suíte estão no guia de validação da Sprint"
            ".</p></main></body></html>"
        ),
    ]
    return "".join(sections)


def export_report(
    store: StrategyStore, report: dict[str, object], run_id: str, provenance: dict[str, object]
) -> Path:
    parent = store.directory / "reports"
    if not parent.resolve().is_relative_to(store.directory.resolve()) or not re.fullmatch(
        r"[A-Za-z0-9-]{1,64}", run_id
    ):
        raise PaperError("UNSAFE_REPORT_PATH")
    parent.mkdir(exist_ok=True)
    target = parent / run_id
    if target.exists() or target.is_symlink():
        raise PaperError("REPORT_ALREADY_EXISTS")
    temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=parent))
    try:
        bodies = {"report.json": canonical(report) + "\n", "report.html": render_html(report)}
        hashes = {}
        for name, body in bodies.items():
            raw = body.encode("utf-8")
            with (temporary / name).open("wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            hashes[name] = digest(raw)
        manifest = {
            **provenance,
            "model": MODEL,
            "run_id": run_id,
            "complete": True,
            "financial_sha256": report["financial_sha256"],
            "files": hashes,
        }
        with (temporary / "manifest.json").open("w", encoding="utf-8") as stream:
            stream.write(canonical(manifest) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.rename(target)
        return target / "manifest.json"
    except BaseException:
        shutil.rmtree(temporary)
        raise
