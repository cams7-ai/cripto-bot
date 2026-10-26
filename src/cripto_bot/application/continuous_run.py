"""Causal clock-driven continuous scheduler. Synthetic observations never pass a pilot."""

import shutil
from collections.abc import Callable
from datetime import UTC, datetime

from cripto_core.errors import PaperError
from cripto_core.serialization import fingerprint

from cripto_bot.adapters.continuous_market import Clock, Feed, Frame
from cripto_bot.adapters.storage.continuous_sqlite import ContinuousStore
from cripto_bot.application.continuous_control import apply_pending
from cripto_bot.domain.continuous.models import DAY, STEP


def next_midnight(now: int) -> int:
    start = (now // DAY + 1) * DAY
    return start if start - now >= 900000 else start + DAY


def _pause(store: ContinuousStore, at: int, reason: str) -> None:
    state = store.state()
    if state.paused:
        return
    store.append(
        fingerprint({"pause": reason, "at": at, "version": state.wallet.version}),
        "CONTROL",
        {
            "at": max(at, state.wallet.clock),
            "request_id": fingerprint({"reason": reason, "version": state.wallet.version}),
            "action": "pause",
            "reason": reason,
        },
    )


def frame_rules(store: ContinuousStore, frame: Frame) -> bool:
    state = store.state()
    changed = fingerprint(state.rules) != fingerprint(frame.rules)
    if changed and state.paused:
        return False
    if (
        fingerprint(state.rules) != fingerprint(frame.rules)
        or frame.metadata_at > state.metadata_at
    ):
        state = store.append(
            fingerprint({"metadata": frame.rules, "at": frame.metadata_at, "event_at": frame.at}),
            "METADATA",
            {
                "at": max(frame.at, state.wallet.clock),
                "rules": frame.rules,
                "metadata_at": frame.metadata_at,
                "reason": "METADATA_CHANGED" if changed else "METADATA_REFRESHED",
            },
        )
    if fingerprint(state.rules) != fingerprint(frame.rules):
        return False
    return True


def sample(
    feed: Feed, clock: Clock, close: int | None = None, count: int = 50, *, quote_only: bool = False
) -> Frame:
    wall, mono = clock.now(), clock.monotonic()
    frame = feed.sample(close, count, quote_only=quote_only)
    if abs((clock.now() - wall) - (clock.monotonic() - mono) * 1000) > 1000:
        raise PaperError("CLOCK_DISCONTINUITY")
    return frame


def arm(store: ContinuousStore, feed: Feed, clock: Clock) -> None:
    frame = sample(feed, clock, count=50)
    # Freeze the calendar after a valid current-data preflight; final T0 warmup is
    # re-fetched causally at the first slot, never requested from the future.
    t0 = next_midnight(clock.now())
    store.append(
        "arm",
        "ARM",
        {
            "at": frame.at,
            "t0": t0,
            "rules": frame.rules,
            "quote": frame.quote,
            "candles": frame.candles,
            "metadata_at": frame.metadata_at,
            "preflight_sha256": frame.source_sha256,
        },
    )
    store.freeze_manifest()


def tick(
    store: ContinuousStore, feed: Feed, clock: Clock, *, free_bytes: Callable[[], int] | None = None
) -> dict[str, object]:
    now = clock.now()
    apply_pending(store, now)
    state = store.state()
    if state.t0 is not None:
        store.freeze_manifest()
    disk = (free_bytes or (lambda: shutil.disk_usage(store.directory).free))()
    if disk < 1024**3 and not state.paused:
        _pause(store, now, "LOW_DISK")
        state = store.state()
    if state.t0 is None:
        if state.paused or state.disabled or state.killed:
            return {"status": "PREPARED_PAUSED", "simulated_fills": 0}
        if disk < 5 * 1024**3:
            raise PaperError("PILOT_DISK_RESERVE")
        arm(store, feed, clock)
        return {"status": "ARMED", "simulated_fills": 0}
    assert state.t0 is not None and state.tend is not None
    t0, tend = state.t0, state.tend
    if now < state.wallet.clock:
        _pause(store, now, "CLOCK_DISCONTINUITY")
        raise PaperError("CLOCK_DISCONTINUITY")
    before = sum(len(o.fills) for o in state.orders.values())
    order = state.pending()
    # Local expiry/cancel has no price dependency; UNKNOWN is never released blindly.
    if order and now >= order.expires_at and order.state != "UNKNOWN":
        store.append(fingerprint({"expire": order.order_id, "at": now}), "POLL", {"at": now})
        state = store.state()
    # Every missed close is persisted and remains in the fixed denominator.
    expected = state.last_slot + STEP
    while expected < tend and expected + 90000 < now:
        store.append(
            "cycle:" + str(expected),
            "MISSED",
            {"at": max(now, store.state().wallet.clock), "close": expected, "reason": "MISSED"},
        )
        expected += STEP
    state = store.state()
    if t0 <= now < tend and expected <= now - 2000:
        try:
            frame = sample(feed, clock, expected, 51)
            apply_pending(store, clock.now())
            if not frame_rules(store, frame):
                raise PaperError("METADATA_CHANGED")
            at = clock.now()
            if at > expected + 90000:
                raise PaperError("SLOT_DEADLINE")
            store.append(
                "cycle:" + str(expected),
                "CYCLE",
                {
                    "at": at,
                    "close": expected,
                    "quote": frame.quote,
                    "candles": frame.candles,
                    "source_sha256": frame.source_sha256,
                },
            )
            if clock.now() > expected + 90000:
                store.append(
                    "late:" + str(expected), "LATE_COMMIT", {"at": clock.now(), "close": expected}
                )
        except (PaperError, ValueError, OSError) as error:
            # A failure is durable, never substituted with HOLD or replayed as an old trade.
            if isinstance(error, PaperError) and str(error) in {
                "CLOCK_DISCONTINUITY",
                "UNSYNCHRONIZED_CLOCK",
            }:
                _pause(store, max(clock.now(), store.state().wallet.clock), str(error))
            if str(expected) in store.state().cycles:
                # The financial commit succeeded. Never rewrite it as MISSED.
                _pause(store, max(clock.now(), store.state().wallet.clock), "POST_COMMIT_FAILURE")
                raise
            store.append(
                "cycle:" + str(expected),
                "MISSED",
                {
                    "at": max(clock.now(), store.state().wallet.clock),
                    "close": expected,
                    "reason": str(error) if isinstance(error, PaperError) else "MARKET_UNAVAILABLE",
                },
            )
    state = store.state()
    order = state.pending()
    if (
        now >= t0
        and now < tend
        and (order is not None or state.wallet.quantity > 0)
        and not state.killed
    ):
        last = order.last_poll if order else state.last_mark_at
        if now - last >= 5000:
            try:
                frame = sample(feed, clock, quote_only=True)
                apply_pending(store, clock.now())
                if frame_rules(store, frame):
                    store.append(
                        fingerprint({"poll": state.wallet.version, "at": frame.at}),
                        "POLL",
                        {"at": max(frame.at, store.state().wallet.clock), "quote": frame.quote},
                    )
            except (PaperError, ValueError, OSError):
                _pause(store, max(clock.now(), store.state().wallet.clock), "QUOTE_UNAVAILABLE")
    state = store.state()
    if state.t0 is not None and state.tend is not None:
        for offset in range(7):
            day_end = state.t0 + (offset + 1) * DAY
            day = datetime.fromtimestamp((day_end - DAY) / 1000, UTC).date().isoformat()
            if now >= day_end and day not in state.daily:
                store.append(
                    "daily:" + day,
                    "DAILY",
                    {"at": max(now, store.state().wallet.clock), "day": day},
                )
        if now >= state.tend and state.status not in {"ENDED", "END_BLOCKED"}:
            store.append("end", "END", {"at": max(now, store.state().wallet.clock)})
    state = store.state()
    after = sum(len(o.fills) for o in state.orders.values())
    return {
        "status": state.status,
        "simulated_fills": after - before,
        "low_disk_warning": disk < 2 * 1024**3,
    }


def run(
    store: ContinuousStore, feed: Feed, clock: Clock, *, once: bool, stop: Callable[[], bool]
) -> dict[str, object]:
    last_wall = clock.now()
    last_mono = clock.monotonic()
    result: dict[str, object] = {"simulated_fills": 0}
    while not stop():
        wall = clock.now()
        mono = clock.monotonic()
        if abs((wall - last_wall) - (mono - last_mono) * 1000) > 1000:
            _pause(store, max(wall, store.state().wallet.clock), "CLOCK_DISCONTINUITY")
            raise PaperError("CLOCK_DISCONTINUITY")
        last_wall = wall
        last_mono = mono
        step = tick(store, feed, clock)
        total = int(str(result["simulated_fills"])) + int(str(step["simulated_fills"]))
        result = {**step, "simulated_fills": total}
        if once or store.state().status in {"ENDED", "END_BLOCKED"}:
            return result
        clock.sleep(1)
    _pause(store, max(clock.now(), store.state().wallet.clock), "SIGTERM")
    return {**result, "status": "PAUSED"}
