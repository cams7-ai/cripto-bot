"""Explicit synthetic clocks/observations; never a prospective observation certificate."""

import copy
import json
import shutil

from cripto_core.serialization import fingerprint

from cripto_bot.adapters.config import load_config
from cripto_bot.adapters.continuous_market import Frame
from cripto_bot.domain.continuous.engine import candles
from cripto_bot.domain.market import STEP, iso
from tests.factories import BASE, ROOT, sample


class Clock:
    def __init__(self, at=BASE - 1000):
        self.at = at
        self.elapsed = 0.0

    def now(self):
        return self.at

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.at += int(seconds * 1000)
        self.elapsed += seconds

    def advance(self, ms):
        self.at += ms
        self.elapsed += ms / 1000


def rules():
    return copy.deepcopy(
        json.loads((ROOT / "config/fixtures/paper-roundtrip.json").read_text())["rules"]
    )


def rows(close=BASE, count=51, values=None):
    original = sample(1)["klines"][0]
    result = []
    for index in range(count):
        row = copy.deepcopy(original)
        row[0] = close - (count - index) * STEP
        row[6] = row[0] + STEP - 1
        value = str(values[index] if values else 100)
        row[1:5] = [value] * 4
        row[5] = "100"
        row[7] = "10000"
        row[8] = 1
        result.append(row)
    return result


def quote(at=BASE - 1000, price="100", qty="10", key="quote"):
    return {
        "kind": "BID_ASK",
        "symbol": "BTCUSDT",
        "quote_id": key,
        "observed_at": iso(at),
        "bid": price,
        "ask": price,
        "bid_qty": qty,
        "ask_qty": qty,
        "market_timestamp": None,
    }


def normalized(raw):
    return candles(
        [
            {
                "open_time": r[0],
                "open": r[1],
                "high": r[2],
                "low": r[3],
                "close": r[4],
                "base_volume": r[5],
                "close_time": r[6],
                "quote_volume": r[7],
                "trades": r[8],
                "symbol": "BTCUSDT",
                "interval": "15m",
            }
            for r in raw
        ]
    )


def fixture(path, at=BASE - 1000):
    body = {
        "schema": 1,
        "exchange_info": {"symbols": [rules()]},
        "frames": [
            {
                "at": at,
                "server_time": at,
                "klines": rows((at // STEP) * STEP, 51),
                "book_ticker": {
                    "symbol": "BTCUSDT",
                    "bidPrice": "100",
                    "askPrice": "100",
                    "bidQty": "10",
                    "askQty": "10",
                },
            }
        ],
    }
    path.write_text(json.dumps(body))
    return path


def config(path, *, enabled=True, source="fixture", notifications=False):
    for name in ("pyproject.toml", "uv.lock", ".python-version"):
        shutil.copyfile(ROOT / name, path / name)
    fixture_file = fixture(path / "capture.json")
    table = {
        "mode": "PAPER",
        "continuous": {
            "enabled": enabled,
            "pilot_id": "drill",
            "wallet_id": "drill",
            "data_dir": str(path / "wallet"),
            "market_data_dir": str(path / "market"),
            "notification_data_dir": str(path / "delivery"),
            "source": source,
            "slippage_bps": "0",
        },
        "modules": {
            "trend_btc": {
                "module_id": "trend_sma",
                "enabled": enabled,
                "environments": ["local"],
                "modes": ["PAPER"],
                "evaluation_types": ["continuous"],
                "symbols": ["BTCUSDT"],
                "capital_weight": "0.10",
                "execution": "paper_bounded",
                "on_disable": "hold_and_monitor",
            }
        },
    }
    if source == "fixture":
        table["continuous"]["fixture_file"] = str(fixture_file)
    if notifications:
        table["continuous"]["notifications"] = {
            "enabled": True,
            "mail_sender_url": "http://127.0.0.1:8002",
            "mail_to": "test@example.invalid",
        }

    # Keep the TOML writer local and tiny; no new runtime dependency.
    def scalar(v):
        return json.dumps(v)

    lines = ['mode="PAPER"', "[continuous]"]
    for k, v in table["continuous"].items():
        if k != "notifications":
            lines.append(k + "=" + scalar(v))
    if notifications:
        lines.append("[continuous.notifications]")
        lines.extend(k + "=" + scalar(v) for k, v in table["continuous"]["notifications"].items())
    lines.append("[modules.trend_btc]")
    lines.extend(k + "=" + scalar(v) for k, v in table["modules"]["trend_btc"].items())
    p = path / "continuous.toml"
    p.write_text("\n".join(lines) + "\n")
    settings = load_config(p, {}, root=path)
    return settings, p


def context(parameters=None):
    params = {
        "initial_usdt": "10000.00",
        "fee_bps": "10",
        "slippage_bps": "0",
        "spread_bps": "0",
        "max_quote_age_ms": 5000,
        "max_entry_fraction": "0.10",
        "max_exposure_fraction": "0.10",
        "max_daily_loss_fraction": "0.01",
        "max_drawdown_fraction": "0.05",
    }
    if parameters:
        params.update(parameters)
    return {
        "pilot_id": "drill",
        "owner": "trend_btc",
        "source": "fixture",
        "parameters": params,
        "configuration": {"module": {"capital_weight": "0.10"}},
        "notification_config_sha256": fingerprint({"enabled": False}),
    }


def arm_payload(at=BASE - 1000, t0=BASE):
    return {
        "at": at,
        "t0": t0,
        "rules": rules(),
        "quote": quote(at, key="preflight"),
        "candles": normalized(rows(BASE - STEP, 50)),
        "metadata_at": at,
    }


class Feed:
    def __init__(self, clock, *, fail=False):
        self.clock = clock
        self.fail = fail
        self.closed = False

    def sample(self, close=None, count=50, *, quote_only=False):
        from cripto_core.errors import PaperError

        if self.fail:
            raise PaperError("TEST_DATA_FAILURE")
        now = self.clock.now()
        closed = close if close is not None else now // STEP * STEP
        return Frame(
            now,
            now,
            rules(),
            quote(now, key=f"quote-{now}"),
            [] if quote_only else normalized(rows(closed, count)),
            now,
            "synthetic",
        )

    def close(self):
        self.closed = True
