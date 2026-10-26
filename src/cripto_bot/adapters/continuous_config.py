"""Strict continuous configuration; no clients/filesystem effects at validation time."""

import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from cripto_core.errors import PaperError
from cripto_core.numeric import number
from cripto_core.validation import identifier
from pydantic import BaseModel, ConfigDict, Field

from cripto_bot.adapters.strategy_config import CoordinationInput, ModuleInput
from cripto_bot.domain.models import PaperSettings
from cripto_bot.domain.modules.models import LIMITS, ModuleConfig, validate_modules


class NotificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    enabled: bool = False
    integration_mode: Literal["LOCAL"] = "LOCAL"
    mail_sender_url: str | None = None
    mail_to: str | None = None
    mail_content_type: Literal["TEXT", "HTML"] = "TEXT"
    whatsapp_enabled: bool = False
    whatsapp_notify_url: str | None = None
    whatsapp_contact: str | None = None
    whatsapp_timeout_seconds: int = Field(default=3, ge=1, le=3)


class ContinuousInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    environment: Literal["local"] = "local"
    evaluation_type: Literal["continuous"] = "continuous"
    symbol: Literal["BTCUSDT"] = "BTCUSDT"
    interval: Literal["15m"] = "15m"
    enabled: bool = False
    pilot_id: str = "continuous-demo"
    wallet_id: str = "continuous-demo"
    data_dir: str
    market_data_dir: str
    notification_data_dir: str
    source: Literal["fixture", "binance_public"] = "binance_public"
    fixture_file: str | None = None
    initial_usdt: str = "10000.00"
    fee_bps: str = "10"
    slippage_bps: str = "5"
    max_entry_fraction: str = "0.10"
    max_exposure_fraction: str = "0.10"
    max_daily_loss_fraction: str = "0.01"
    max_drawdown_fraction: str = "0.05"
    max_quote_age_ms: int = Field(default=5000, ge=1, le=5000)
    notifications: NotificationInput = Field(default_factory=NotificationInput)


@dataclass(frozen=True)
class ContinuousSettings:
    paper: PaperSettings
    market_data_dir: Path
    notification_data_dir: Path
    source: str
    fixture_file: Path | None
    pilot_id: str
    enabled: bool
    modules: tuple[ModuleConfig, ...]
    notifications: NotificationInput

    @property
    def root(self) -> Path:
        return self.paper.root

    @property
    def data_dir(self) -> Path:
        return self.paper.data_dir

    @property
    def module(self) -> ModuleConfig | None:
        return next((m for m in self.modules if m.active_for("continuous")), None)

    @property
    def active(self) -> bool:
        return self.enabled and self.module is not None

    def configuration(self) -> dict[str, object]:
        # Activation controls may change; financial parameters and ownership stay frozen.
        items = []
        for item in self.modules:
            values = asdict(item)
            values.pop("enabled")
            items.append(values)
        return {"modules": items, "policy": "exclusive_owner"}


def endpoint(value: str | None) -> str:
    if not value:
        raise PaperError("NOTIFICATION_CONFIG_REQUIRED")
    try:
        u = urlsplit(value)
        if (
            u.scheme not in {"http", "https"}
            or u.hostname not in {"localhost", "127.0.0.1", "::1"}
            or u.username
            or u.password
            or u.query
            or u.fragment
            or u.path not in {"", "/"}
            or u.port is None
            or not 1 <= u.port <= 65535
        ):
            raise ValueError
    except ValueError:
        raise PaperError("UNSAFE_NOTIFICATION_URL") from None
    return value.rstrip("/")


def settings(
    item: ContinuousInput,
    supplied: dict[str, ModuleInput],
    coordination: CoordinationInput,
    root: Path,
    contained: Callable[[str, Path], Path],
    environ: Mapping[str, str],
) -> ContinuousSettings:
    identifier(item.pilot_id)
    identifier(item.wallet_id)
    if number(item.initial_usdt) <= 0:
        raise PaperError("INVALID_INITIAL")
    if number(item.fee_bps) != 10 or not 0 <= number(item.slippage_bps) <= 5:
        raise PaperError("INVALID_CONTINUOUS_COST")
    caps = {
        "max_entry_fraction": "0.10",
        "max_exposure_fraction": "0.10",
        "max_daily_loss_fraction": "0.01",
        "max_drawdown_fraction": "0.05",
    }
    for name in LIMITS:
        if not 0 < number(getattr(item, name)) <= number(caps[name]):
            raise PaperError("INVALID_LIMIT")
    if coordination.policy != "exclusive_owner" or set(coordination.symbol_limits) - {"BTCUSDT"}:
        raise PaperError("UNSUPPORTED_COORDINATION")
    global_limits = {name: getattr(item, name) for name in LIMITS}
    for name, value in coordination.symbol_limits.get("BTCUSDT", {}).items():
        if name not in LIMITS or not 0 < number(value) <= number(global_limits[name]):
            raise PaperError("INVALID_LIMIT_OVERRIDE")
        global_limits[name] = value
    modules = []
    for key, m in sorted(supplied.items()):
        identifier(key)
        if m.params.fast_window != 20 or m.params.slow_window != 50:
            raise PaperError("CONTINUOUS_SMA_WINDOWS")
        if not 0 < number(m.capital_weight) <= number("0.10"):
            raise PaperError("CAPITAL_WEIGHT_LIMIT")
        for name, value in m.limits.items():
            if name not in LIMITS or not 0 < number(value) <= number(global_limits[name]):
                raise PaperError("INVALID_LIMIT_OVERRIDE")
        modules.append(
            ModuleConfig(
                key,
                m.module_id,
                m.version,
                m.enabled,
                tuple(m.environments),
                tuple(m.modes),
                tuple(m.evaluation_types),
                tuple(m.symbols),
                m.capital_weight,
                m.execution,
                m.on_disable,
                20,
                50,
                m.params.interval,
                tuple(m.depends_on),
                tuple(sorted(m.limits.items())),
            )
        )
    if len(modules) > 1:
        raise PaperError("EXCLUSIVE_OWNER_CONFLICT")
    validate_modules(tuple(modules), evaluation_type="continuous")
    paths = [
        contained(value, root)
        for value in [item.data_dir, item.market_data_dir, item.notification_data_dir]
    ]
    protected = root / "data/backtest-usage"
    for index, path in enumerate(paths):
        if path.is_relative_to(protected) or protected.is_relative_to(path):
            raise PaperError("PROTECTED_DATA_DIRECTORY")
        for other in paths[index + 1 :]:
            if path.is_relative_to(other) or other.is_relative_to(path):
                raise PaperError("SHARED_CONTINUOUS_DIRECTORY")
    if item.source == "binance_public" and item.fixture_file is not None:
        raise PaperError("INVALID_FIXTURE_SOURCE")
    fixture = contained(item.fixture_file, root) if item.fixture_file else None
    if item.source == "fixture" and item.enabled and fixture is None:
        raise PaperError("FIXTURE_REQUIRED")
    values = item.model_dump(
        exclude={
            "environment",
            "evaluation_type",
            "symbol",
            "interval",
            "enabled",
            "pilot_id",
            "data_dir",
            "market_data_dir",
            "notification_data_dir",
            "source",
            "fixture_file",
            "notifications",
        }
    )
    values.update(global_limits)
    module = next((m for m in modules if m.active_for("continuous")), None)
    if module:
        for name, value in module.limits:
            values[name] = value
        for name in ["max_entry_fraction", "max_exposure_fraction"]:
            values[name] = format(
                min(number(str(values[name])), number(module.capital_weight)), "f"
            )
    if module is None and len(modules) == 1:
        # Custody only: preserve the tighter financial identity while all execution
        # guards still see module=None and active=False. Never select/enable it.
        for name, value in modules[0].limits:
            values[name] = value
        for name in ["max_entry_fraction", "max_exposure_fraction"]:
            values[name] = format(
                min(number(str(values[name])), number(modules[0].capital_weight)), "f"
            )
    paper = PaperSettings(
        root=root,
        data_dir=paths[0],
        scenario_file=fixture or paths[0] / "unused",
        spread_bps="0",
        **values,
    )
    n = item.notifications.model_dump()
    env_keys = {
        "INTEGRATION_MODE": "integration_mode",
        "MAIL_SENDER_URL": "mail_sender_url",
        "MAIL_TO": "mail_to",
        "MAIL_CONTENT_TYPE": "mail_content_type",
        "WHATSAPP_NOTIFY_URL": "whatsapp_notify_url",
        "WHATSAPP_CONTACT": "whatsapp_contact",
    }
    if item.notifications.enabled:
        for key, target in env_keys.items():
            if key in environ:
                n[target] = environ[key]
        if "WHATSAPP_ENABLED" in environ:
            if environ["WHATSAPP_ENABLED"] not in {"true", "false"}:
                raise PaperError("INVALID_WHATSAPP_FLAG")
            n["whatsapp_enabled"] = environ["WHATSAPP_ENABLED"] == "true"
        if "WHATSAPP_TIMEOUT_SECONDS" in environ:
            try:
                n["whatsapp_timeout_seconds"] = int(environ["WHATSAPP_TIMEOUT_SECONDS"])
            except ValueError:
                raise PaperError("INVALID_TIMEOUT") from None
    notifications = NotificationInput.model_validate(n)
    if notifications.enabled:
        endpoint(notifications.mail_sender_url)
        if (
            not notifications.mail_to
            or re.fullmatch(r"[^\s@]{1,64}@[^\s@]{1,190}", notifications.mail_to) is None
        ):
            raise PaperError("INVALID_RECIPIENT")
        if notifications.whatsapp_enabled:
            endpoint(notifications.whatsapp_notify_url)
            if (
                not notifications.whatsapp_contact
                or len(notifications.whatsapp_contact) > 128
                or any(ord(c) < 32 for c in notifications.whatsapp_contact)
            ):
                raise PaperError("INVALID_CONTACT")
    elif notifications.whatsapp_enabled:
        raise PaperError("NOTIFICATIONS_DISABLED")
    return ContinuousSettings(
        paper,
        paths[1],
        paths[2],
        item.source,
        fixture,
        item.pilot_id,
        item.enabled,
        tuple(modules),
        notifications,
    )
