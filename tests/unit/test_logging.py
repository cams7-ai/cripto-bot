import json
from datetime import UTC, datetime, timedelta, timezone
from io import StringIO

import pytest

from cripto_bot.observability.logging import emit, utc_now, utc_timestamp


def test_json_and_utc():
    stream = StringIO()
    stamp = utc_timestamp(datetime(2026, 10, 24, tzinfo=timezone(timedelta(hours=-3))))
    emit(stream, {"timestamp": stamp, "event": "test"})
    assert json.loads(stream.getvalue())["timestamp"] == "2026-10-24T03:00:00+00:00"
    assert utc_now().tzinfo == UTC


def test_naive_rejected():
    with pytest.raises(ValueError):
        utc_timestamp(datetime(2026, 10, 24))
