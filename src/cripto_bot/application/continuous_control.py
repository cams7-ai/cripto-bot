"""Durable control inbox: no concurrent write to the financial daemon's database."""

import json
import os
from pathlib import Path
from uuid import uuid4

from cripto_core.errors import PaperError
from cripto_core.serialization import canonical, fingerprint
from cripto_core.validation import identifier, require_object

from cripto_bot.adapters.storage.continuous_sqlite import ContinuousStore

ACTIONS = frozenset({"pause", "resume", "kill", "disable", "reload", "cancel"})


def directory(data_dir: Path) -> Path:
    path = data_dir / "control-requests"
    if path.is_symlink() or not path.resolve().is_relative_to(data_dir.resolve()):
        raise PaperError("UNSAFE_CONTROL_PATH")
    return path


def submit(
    data_dir: Path,
    pilot_id: str,
    action: str,
    reason: str,
    request_id: str,
    at: int,
    context_hash: str,
    order_id: str | None = None,
) -> dict[str, object]:
    identifier(request_id)
    identifier(pilot_id)
    if action not in ACTIONS or not reason or len(reason) > 256 or any(ord(c) < 32 for c in reason):
        raise PaperError("INVALID_CONTROL")
    if action == "cancel":
        identifier(order_id)
    elif order_id is not None:
        raise PaperError("UNEXPECTED_ORDER_ID")
    body: dict[str, object] = {
        "pilot_id": pilot_id,
        "action": action,
        "reason": reason,
        "request_id": request_id,
        "context_hash": context_hash,
        "order_id": order_id,
    }
    parent = directory(data_dir)
    parent.mkdir(exist_ok=True)
    target = parent / (request_id + ".json")
    if target.exists() or target.is_symlink():
        if target.is_symlink():
            raise PaperError("UNSAFE_CONTROL_PATH")
        old = require_object(json.loads(target.read_text()))
        oldbody = {k: v for k, v in old.items() if k not in {"requested_at", "sha256"}}
        if body != oldbody:
            raise PaperError("CONTROL_CONFLICT")
        return {"request_id": request_id, "status": "ACCEPTED", "application": "PENDING"}
    body["requested_at"] = at
    raw = {**body, "sha256": fingerprint(body)}
    temporary = parent / (".pending-" + str(uuid4()))
    try:
        with temporary.open("xb") as f:
            os.chmod(temporary, 0o600)
            f.write((canonical(raw) + "\n").encode())
            f.flush()
            os.fsync(f.fileno())
        # link is atomic and refuses replacement, unlike rename on an existing regular file.
        try:
            os.link(temporary, target)
        except FileExistsError:
            actual = require_object(json.loads(target.read_text()))
            if {k: v for k, v in actual.items() if k not in {"requested_at", "sha256"}} != {
                k: v for k, v in body.items() if k != "requested_at"
            }:
                raise PaperError("CONTROL_CONFLICT") from None
        fd = os.open(parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)
    return {"request_id": request_id, "status": "ACCEPTED", "application": "PENDING"}


def apply_pending(store: ContinuousStore, at: int) -> int:
    parent = directory(store.directory)
    if not parent.exists():
        return 0
    requests = []
    for path in parent.glob("*.json"):
        if path.is_symlink() or path.stat().st_size > 4096:
            raise PaperError("CORRUPT_CONTROL_REQUEST")
        item = require_object(json.loads(path.read_text()))
        body = {k: v for k, v in item.items() if k != "sha256"}
        if (
            fingerprint(body) != item.get("sha256")
            or path.name != str(item.get("request_id")) + ".json"
        ):
            raise PaperError("CORRUPT_CONTROL_REQUEST")
        requests.append(item)
    done = 0
    for item in sorted(requests, key=lambda r: (int(str(r["requested_at"])), str(r["request_id"]))):
        state = store.state()
        key = identifier(item["request_id"])
        if item["pilot_id"] != state.pilot_id or item["context_hash"] != fingerprint(state.context):
            raise PaperError("CONTROL_CONTEXT_CONFLICT")
        if key in state.controls:
            continue
        payload = {
            "at": max(at, state.wallet.clock),
            "request_id": key,
            "action": item["action"],
            "reason": item["reason"],
        }
        if item["order_id"] is not None:
            payload["order_id"] = item["order_id"]
        store.append("control:" + key, "CONTROL", payload)
        done += 1
    return done
