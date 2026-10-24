"""Serializacao explicita dos eventos locais."""

import json
from datetime import UTC, datetime
from typing import TextIO


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_timestamp(instant: datetime) -> str:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("timestamp exige timezone")
    return instant.astimezone(UTC).isoformat()


def emit(stream: TextIO, payload: dict[str, object]) -> None:
    stream.write(json.dumps(payload, ensure_ascii=True, allow_nan=False) + "\n")
    stream.flush()
