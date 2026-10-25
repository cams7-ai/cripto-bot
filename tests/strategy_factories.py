import copy
import json
from pathlib import Path

from cripto_bot.adapters.config import load_config
from cripto_bot.domain.market import STEP, iso
from cripto_bot.entrypoints.strategy import execute_strategy

from tests.factories import ROOT


def source(closes=(100, 100, 110, 100), prices=(110, 100)):
    original = json.loads((ROOT / "config/fixtures/strategy-sma-crossings.json").read_text())
    first = original["candles"][0]["open_time"]
    template = original["candles"][0]
    candles = []
    prior = closes[0]
    for index, close in enumerate(closes):
        opened = first + index * STEP
        candles.append(
            {
                **template,
                "open_time": opened,
                "close_time": opened + STEP - 1,
                "available_at": iso(opened + STEP),
                "open": str(prior),
                "high": str(max(prior, close)),
                "low": str(min(prior, close)),
                "close": str(close),
            }
        )
        prior = close
    original["candles"] = candles
    original["executions"] = []
    for index, price in zip((2, 3), prices, strict=True):
        if index >= len(candles):
            continue
        opened = candles[index]["open_time"]
        original["executions"].append(
            {
                "candle_open": opened,
                "fill_at": iso(opened + STEP + 1001),
                "observation": {
                    "quote_id": f"quote-{index}",
                    "kind": "REFERENCE",
                    "symbol": "BTCUSDT",
                    "observed_at": iso(opened + STEP + 1000),
                    "price": str(price),
                },
            }
        )
    return original


def write_case(path, *, enabled=True, compact=True, data=None):
    data = (
        source()
        if data is None and compact
        else (
            json.loads((ROOT / "config/fixtures/strategy-sma-crossings.json").read_text())
            if data is None
            else data
        )
    )
    sample = path / "fixture.json"
    sample.write_text(json.dumps(data))
    text = (ROOT / "config/strategy.sma.example.toml").read_text()
    text = text.replace("data/sma-demo", str(path / "wallet"))
    text = text.replace("config/fixtures/strategy-sma-crossings.json", str(sample))
    if enabled:
        text = text.replace("enabled = false", "enabled = true")
    if compact:
        text = text.replace("fast_window = 20", "fast_window = 1").replace(
            "slow_window = 50", "slow_window = 2"
        )
    config = path / "strategy.toml"
    config.write_text(text)
    return config


def run_case(config, command="strategy-run", run_id="test"):
    return execute_strategy(load_config(Path(config), {}), command, run_id)


def truncated(data, count):
    result = copy.deepcopy(data)
    result["candles"] = result["candles"][:count]
    opens = {c["open_time"] for c in result["candles"]}
    result["executions"] = [e for e in result["executions"] if e["candle_open"] in opens]
    return result
