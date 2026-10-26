"""Independent worker: durable source import/leases and bounded delivery, no trading."""

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from cripto_core.errors import PaperError
from cripto_core.serialization import canonical, fingerprint
from cripto_core.validation import require_object

from cripto_bot.adapters.continuous_config import ContinuousSettings, NotificationInput
from cripto_bot.adapters.storage.continuous_sqlite import ContinuousStore
from cripto_bot.adapters.storage.notification_sqlite import NotificationStore


class Sender(Protocol):
    def send(
        self, event: dict[str, object], channel: str, *, duplicate_possible: bool = False
    ) -> str: ...


class ProcessSender:
    def __init__(self, settings: NotificationInput, root: Path) -> None:
        self.settings = settings
        self.root = root

    def send(
        self, event: dict[str, object], channel: str, *, duplicate_possible: bool = False
    ) -> str:
        payload = {
            "settings": self.settings.model_dump(),
            "event": event,
            "channel": channel,
            "duplicate_possible": duplicate_possible,
        }
        env = {
            k: v
            for k, v in os.environ.items()
            if k != "PYTHONPATH"
            and not k.startswith(("BINANCE_", "MAIL_", "WHATSAPP_", "CRIPTO_BOT_"))
        }
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-m", "cripto_bot.adapters.notifications.delivery_worker"],
                input=canonical(payload),
                text=True,
                capture_output=True,
                timeout=10,
                cwd=self.root,
                env=env,
            )
            if result.returncode != 0:
                return "UNKNOWN"
            outcome = require_object(json.loads(result.stdout)).get("outcome")
            return str(outcome) if outcome in {"CONFIRMED", "FAILED", "UNKNOWN"} else "UNKNOWN"
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return "UNKNOWN"


def drain(
    settings: ContinuousSettings,
    queue: NotificationStore,
    now: int,
    sender: Sender,
    *,
    clock: Callable[[], int] | None = None,
) -> dict[str, object]:
    # Open a consistent readonly financial snapshot only to import validated immutable events.
    with ContinuousStore(settings.data_dir, readonly=True) as financial:
        state = financial.state()
        if state.pilot_id != settings.pilot_id:
            raise PaperError("PILOT_MISMATCH")
        if state.context["notification_config_sha256"] != fingerprint(
            settings.notifications.model_dump()
        ):
            raise PaperError("NOTIFICATION_CONFIG_CHANGED")
        for body, digest in financial.db.execute(
            "SELECT data,source_hash FROM notification_events ORDER BY seq"
        ):
            queue.enqueue(
                require_object(json.loads(body)),
                digest,
                whatsapp=settings.notifications.whatsapp_enabled,
            )
    claim = queue.claim(now)
    if claim is None:
        return {"processed": 0, **queue.summary()}
    outcome = sender.send(
        require_object(claim["event"]),
        str(claim["channel"]),
        duplicate_possible=claim["duplicate_possible"] is True,
    )
    queue.finish(claim, outcome, max(now, clock()) if clock else now)
    return {"processed": 1, "outcome": outcome, **queue.summary()}
