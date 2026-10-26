"""Modelos puros da base local; sem integracoes externas."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from cripto_bot.domain.backtest.models import BacktestSettings
    from cripto_bot.domain.modules.models import StrategySettings

Mode = Literal["PAPER"]


@dataclass(frozen=True)
class MarketSettings:
    source: Literal["fixture", "binance_public"]
    root: Path
    data_dir: Path
    fixture_file: Path | None = None
    symbol: Literal["BTCUSDT"] = "BTCUSDT"
    interval: Literal["15m"] = "15m"
    page_limit: int = 1000
    max_pages: int = 10


@dataclass(frozen=True)
class PaperSettings:
    root: Path
    data_dir: Path
    scenario_file: Path
    wallet_id: str = "paper-demo"
    initial_usdt: str = "10000.00"
    fee_bps: str = "10"
    spread_bps: str = "10"
    slippage_bps: str = "5"
    max_entry_fraction: str = "0.10"
    max_exposure_fraction: str = "0.10"
    max_daily_loss_fraction: str = "0.01"
    max_drawdown_fraction: str = "0.05"
    max_quote_age_ms: int = 5000

    def parameters(self) -> dict[str, object]:
        from dataclasses import asdict

        return {
            key: value
            for key, value in asdict(self).items()
            if key not in ("root", "data_dir", "scenario_file")
        }


@dataclass(frozen=True)
class Settings:
    mode: Mode = "PAPER"
    market: MarketSettings | None = None
    paper: PaperSettings | None = None
    strategy: StrategySettings | None = None
    backtest: BacktestSettings | None = None


def parse_decimal(value: object) -> Decimal:
    """Aceita exclusivamente texto decimal finito, sem conversao por float."""
    if not isinstance(value, str):
        raise ValueError("decimal exige string")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("decimal invalido") from None
    if not result.is_finite():
        raise ValueError("decimal deve ser finito")
    return result
