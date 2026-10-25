"""Relatorios derivados: nenhuma operação de execução ou correção de saldo."""

import json
import os
import re
import shutil
import tempfile
from decimal import Decimal, localcontext
from pathlib import Path

from cripto_bot.adapters.storage.paper_sqlite import PaperStore
from cripto_bot.domain.market import canonical, digest
from cripto_bot.domain.portfolio.models import (
    MODEL,
    PaperError,
    Wallet,
    fingerprint,
    money,
    text,
)
from cripto_bot.domain.risk.limits import ratio


def summary(store: PaperStore, wallet: Wallet, *, reconciled: bool = True) -> dict[str, object]:
    with localcontext() as context:
        context.prec = 80
        state = wallet.data()
        state.update(
            cost_average=text(ratio(wallet.cost_basis, wallet.quantity)),
            unrealized=text(money(wallet.quantity * wallet.mark) - wallet.cost_basis),
            equity=text(wallet.equity),
            exposure_fraction=text(
                ratio(wallet.quantity * wallet.mark + wallet.usdt_reserved, wallet.equity)
            ),
            daily_loss_fraction=text(
                max(Decimal(0), ratio(wallet.baseline - wallet.equity, wallet.baseline))
            ),
            drawdown_fraction=text(
                max(Decimal(0), ratio(wallet.peak - wallet.equity, wallet.peak))
            ),
        )
        data: dict[str, object] = {
            "wallet": state,
            "model": MODEL,
            "context": store.context(),
            "reconciled": reconciled,
            "blocked": bool(store.blocked()) or not reconciled,
            "total_simulated_fills": sum(i.state == "FILLED" for i in wallet.intents.values()),
            "total_rejected_intents": sum(
                i.state in ("RISK_REJECTED", "REJECTED") for i in wallet.intents.values()
            ),
        }
        rows = list(
            store.db.execute(
                "SELECT seq,event_key,kind,payload,previous_hash,chain_hash "
                "FROM journal ORDER BY seq"
            )
        )
        ledger = [
            {"seq": seq, "index": idx, "posting": json.loads(body)}
            for seq, idx, body in store.db.execute(
                "SELECT seq,idx,data FROM postings ORDER BY seq,idx"
            )
        ]
        data["journal"] = [
            {
                "seq": row[0],
                "event_key": row[1],
                "kind": row[2],
                "payload": json.loads(row[3]),
                "previous_hash": row[4],
                "chain_hash": row[5],
            }
            for row in rows
        ]
        data["ledger"] = ledger
        data["financial_sha256"] = fingerprint(
            {"wallet": state, "ledger": ledger, "journal": data["journal"]}
        )
        return data


def diagnostic(store: PaperStore) -> dict[str, object]:
    try:
        wallet = store.replay()
        return summary(store, wallet)
    except (PaperError, ValueError, KeyError, TypeError):
        try:
            return summary(store, store.replay(check_projection=False), reconciled=False)
        except (PaperError, ValueError, KeyError, TypeError):
            return {"reconciled": False, "blocked": True, "report_available": False}


def export_report(
    store: PaperStore, report: dict[str, object], run_id: str, provenance: dict[str, object]
) -> Path:
    parent = store.directory / "reports"
    if not parent.resolve().is_relative_to(store.directory.resolve()) or not re.fullmatch(
        r"[A-Za-z0-9-]{1,64}", run_id
    ):
        raise PaperError("UNSAFE_REPORT_PATH")
    parent.mkdir(exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=parent))
    try:
        body = (canonical(report) + "\n").encode()
        with (temporary / "report.json").open("wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        manifest = {
            **provenance,
            "model": MODEL,
            "financial_sha256": report["financial_sha256"],
            "run_id": run_id,
            "complete": True,
            "files": {"report.json": digest(body)},
        }
        with (temporary / "manifest.json").open("w") as stream:
            stream.write(canonical(manifest) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        target = parent / run_id
        temporary.rename(target)
        return target / "manifest.json"
    except BaseException:
        shutil.rmtree(temporary)
        raise
