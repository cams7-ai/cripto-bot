import copy
import json
from pathlib import Path

from cripto_bot.application.ports import RawResponse
from cripto_bot.domain.market import STEP, canonical

ROOT = Path(__file__).resolve().parents[1]
BASE = 1790812800000


def sample(count=2):
    data = json.loads((ROOT / "config/fixtures/market-sample.json").read_text())
    first = data["klines"][0]
    data["klines"] = []
    for i in range(count):
        row = copy.deepcopy(first)
        row[0] = BASE + i * STEP
        row[6] = row[0] + STEP - 1
        data["klines"].append(row)
    data["server_time"]["serverTime"] = BASE + (count + 1) * STEP
    return data


def fixture_file(path, count=2):
    path.write_text(json.dumps(sample(count)))
    return path


def raw(rows):
    return RawResponse("/api/v3/klines", {}, canonical(rows).encode(), "2026-10-24T00:00:00+00:00")
