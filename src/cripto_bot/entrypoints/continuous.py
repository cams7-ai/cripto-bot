"""Composition for local continuous commands; real network and send gates are separate."""

import signal
import sqlite3
from collections.abc import Callable
from dataclasses import asdict
from uuid import uuid4

from cripto_core.errors import PaperError
from cripto_core.serialization import fingerprint
from cripto_core.validation import require_object

from cripto_bot.adapters.config import ConfigError
from cripto_bot.adapters.continuous_config import ContinuousSettings
from cripto_bot.adapters.continuous_market import Clock, Feed, FixtureFeed, PublicFeed, SystemClock
from cripto_bot.adapters.provenance import runtime_identity
from cripto_bot.adapters.storage.continuous_sqlite import ContinuousStore
from cripto_bot.adapters.storage.notification_sqlite import NotificationStore
from cripto_bot.application.continuous_control import apply_pending, submit
from cripto_bot.application.continuous_run import run
from cripto_bot.application.notifications import ProcessSender, drain
from cripto_bot.domain.models import Settings


def context(config: ContinuousSettings) -> dict[str, object]:
    modules = config.modules
    if not modules:
        raise PaperError("CONTINUOUS_MODULE_REQUIRED")
    module = asdict(modules[0])
    module.pop("enabled")
    return {
        "pilot_id": config.pilot_id,
        "owner": modules[0].instance_id,
        "source": config.source,
        "parameters": config.paper.parameters(),
        "configuration": {"module": module, **config.configuration()},
        "runtime": runtime_identity(config.root),
        "fixture_sha256": fingerprint(config.fixture_file.read_bytes().hex())
        if config.fixture_file
        else None,
        "notification_config_sha256": fingerprint(config.notifications.model_dump()),
    }


def execute_continuous(
    settings: Settings,
    command: str,
    run_id: str,
    *,
    allow_network: bool = False,
    once: bool = False,
    allow_send: bool = False,
    action: str | None = None,
    reason: str | None = None,
    order_id: str | None = None,
    request_id: str | None = None,
    day: str | None = None,
    clock: Clock | None = None,
    feed_factory: Callable[[ContinuousSettings, Clock], Feed] | None = None,
) -> tuple[int, dict[str, object]]:
    config = settings.continuous
    clock = clock or SystemClock()
    result: dict[str, object] = {"orders_sent": 0, "simulated_fills": 0}
    if config is None:
        raise ConfigError("continuous configuration required")
    exists = (config.data_dir / "continuous.sqlite3").exists()
    if command in {"continuous-run", "continuous-prepare"} and not config.active and not exists:
        return 0, {**result, "status": "CONTINUOUS_NOOP"}
    if command == "continuous-run" and config.source == "fixture" and allow_network:
        raise ConfigError("fixture rejects network")
    if (
        command == "continuous-run"
        and config.source == "binance_public"
        and not allow_network
        and config.active
    ):
        raise ConfigError("public data requires explicit network opt-in")
    if command == "notify-drain" and config.notifications.enabled and not allow_send:
        raise ConfigError("sending requires explicit opt-in")
    if command == "notify-drain" and not config.notifications.enabled:
        return 0, {**result, "status": "NOTIFICATIONS_DISABLED"}
    try:
        if command == "notify-drain":
            with NotificationStore(config.notification_data_dir) as queue:
                sender = ProcessSender(config.notifications, config.root)
                while True:
                    delivered = drain(config, queue, clock.now(), sender, clock=clock.now)
                    if once:
                        return 0, {**result, "status": "NOTIFICATIONS_DRAINED", **delivered}
                    clock.sleep(1)
        if command == "continuous-control":
            if action in {"resume", "reload"} and not config.active:
                raise ConfigError("resume requires active explicit module flags")
            with ContinuousStore(config.data_dir, readonly=True) as store:
                store.verify_context(context(config))
                state = store.state()
            submitted = submit(
                config.data_dir,
                state.pilot_id,
                str(action),
                reason or "",
                request_id or str(uuid4()),
                clock.now(),
                fingerprint(state.context),
                order_id,
            )
            return 0, {**result, **submitted}
        prepared_context = None
        if command == "continuous-prepare":
            prepared_context = context(config)
            if config.source == "binance_public":
                packages = require_object(require_object(prepared_context["runtime"])["packages"])
                if any(require_object(v).get("installation") != "wheel" for v in packages.values()):
                    raise PaperError("PILOT_REQUIRES_INSTALLED_WHEELS")
        readonly = command in {"continuous-reconcile", "continuous-report"}
        with ContinuousStore(
            config.data_dir, create=command == "continuous-prepare", readonly=readonly
        ) as store:
            if command == "continuous-prepare":
                assert prepared_context is not None
                state = store.initialize(prepared_context, clock.now())
                return 0, {
                    **result,
                    "status": state.status,
                    "pilot_id": state.pilot_id,
                    "observation_status": "PILOT_PENDING",
                }
            if command == "continuous-reconcile":
                state = store.reconcile()
                pending = state.pending()
                return 0, {
                    **result,
                    "status": "CONTINUOUS_RECONCILED",
                    "version": state.wallet.version,
                    "pending_order": pending.order_id if pending else None,
                }
            if command == "continuous-report":
                from cripto_bot.adapters.storage.continuous_report import export_report, snapshot

                report = snapshot(
                    store, clock.now(), day=day, notification_dir=config.notification_data_dir
                )
                manifest = export_report(config, report, run_id)
                return 0, {
                    **result,
                    "status": "CONTINUOUS_REPORTED",
                    "manifest": str(manifest),
                    "html_report": str(manifest.parent / "report.html"),
                    "observation_status": report["observation_status"],
                }
            store.verify_context(context(config))
            apply_pending(store, clock.now())
            if not config.active:
                state = store.state()
                if not state.disabled:
                    store.append(
                        fingerprint({"disable": state.wallet.version, "run_id": run_id}),
                        "CONTROL",
                        {
                            "at": max(clock.now(), state.wallet.clock),
                            "request_id": fingerprint({"disable": run_id}),
                            "action": "disable",
                            "reason": "CONFIG_DISABLED",
                        },
                    )
                return 0, {
                    **result,
                    "status": "CUSTODY_PAUSED",
                    "position": format(store.state().wallet.quantity, "f"),
                    "price_available": False,
                }
            feed = (
                feed_factory(config, clock)
                if feed_factory
                else FixtureFeed(config.fixture_file, clock)
                if config.source == "fixture" and config.fixture_file
                else PublicFeed(config, clock)
            )
            stopped = False

            def stop(signum: int, frame: object) -> None:
                nonlocal stopped
                stopped = True

            previous = None
            try:
                if not once:
                    previous = signal.signal(signal.SIGTERM, stop)
                output = run(store, feed, clock, once=once, stop=lambda: stopped)
            finally:
                feed.close()
                if previous is not None:
                    signal.signal(signal.SIGTERM, previous)
            return 0, {
                **result,
                **output,
                "pilot_id": config.pilot_id,
                "observation_status": "PILOT_PENDING",
            }
    except ConfigError:
        raise
    except (PaperError, OSError, ValueError, sqlite3.Error) as error:
        return 3, {
            **result,
            "status": "CONTINUOUS_FAILED",
            "error_code": str(error)
            if isinstance(error, PaperError)
            else "CONTINUOUS_DATA_OR_STORAGE_FAILURE",
        }
