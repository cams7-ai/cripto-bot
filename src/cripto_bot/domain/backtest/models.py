"""Protocolo e configuracao historicos, sem I/O."""

from dataclasses import asdict, dataclass
from pathlib import Path

from cripto_bot.domain.market import canonical, digest, sequence
from cripto_bot.domain.models import PaperSettings
from cripto_bot.domain.modules.models import ModuleConfig
from cripto_bot.domain.portfolio.models import PaperError, number, require_object, text

MODEL = "paper-backtest-v1"
VARIANTS = ("base", "double_costs", "delay_1candle", "double_costs_delay")
BENCHMARKS = ("cash", "buy_hold_100pct", "buy_hold_10pct")
PROFILES = ("sma", *BENCHMARKS)
SEGMENTS = ("development", "validation", "test")
MAX_CANDLES = 200_000
MAX_BYTES = 256 * 1024 * 1024
MAX_LINE = 16 * 1024
RULES: dict[str, object] = {
    "rule_version": "historical-proxy-v1",
    "symbol": "BTCUSDT",
    "status": "TRADING",
    "isSpotTradingAllowed": True,
    "baseAsset": "BTC",
    "quoteAsset": "USDT",
    "filters": [
        {
            "filterType": "PRICE_FILTER",
            "minPrice": "0.01",
            "maxPrice": "1000000",
            "tickSize": "0.01",
        },
        {"filterType": "LOT_SIZE", "minQty": "0.000001", "maxQty": "1000", "stepSize": "0.000001"},
        {
            "filterType": "MARKET_LOT_SIZE",
            "minQty": "0.000001",
            "maxQty": "1000",
            "stepSize": "0.000001",
        },
        {"filterType": "MIN_NOTIONAL", "minNotional": "10", "applyToMarket": True},
    ],
}


@dataclass(frozen=True)
class BacktestSettings:
    paper: PaperSettings
    experiment_id: str
    source_manifests: tuple[Path, ...]
    dataset_dir: Path
    start: int
    end: int
    modules: tuple[ModuleConfig, ...]
    symbol_limits: tuple[tuple[str, str], ...]

    @property
    def root(self) -> Path:
        return self.paper.root

    @property
    def data_dir(self) -> Path:
        return self.paper.data_dir

    @property
    def module(self) -> ModuleConfig | None:
        return next((m for m in self.modules if m.active_for("backtest")), None)

    def snapshot(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "mode": "PAPER",
            "environment": "local",
            "evaluation_type": "backtest",
            "parameters": self.paper.parameters(),
            "modules": [asdict(m) for m in self.modules],
            "symbol_limits": dict(self.symbol_limits),
            "start": self.start,
            "end": self.end,
            "variants": list(VARIANTS),
            "benchmarks": list(BENCHMARKS),
            "split_percentages": [60, 20, 20],
            "terminal_policy": "mark_to_market",
            "policy": "exclusive_owner",
            "observation_delay_ms": 1,
            "fill_delay_ms": 2,
            "metrics_schema": 1,
            "availability": "bar_close_plus_1ms",
            "rules": RULES,
            "filter_provenance": "synthetic proxy; not historical exchangeInfo",
        }


def split(start: int, end: int, slow: int) -> dict[str, list[int]]:
    count = (end - start) // 900000
    dev = count * 60 // 100
    val = count * 20 // 100
    counts = (dev, val, count - dev - val)
    if min(counts) < slow + 2:
        raise PaperError("INSUFFICIENT_SEGMENT_DATA")
    result = {}
    cursor = start
    for name, n in zip(SEGMENTS, counts, strict=True):
        result[name] = [cursor, cursor + n * 900000]
        cursor += n * 900000
    return result


def run_key(protocol_hash: str, segment: str, variant: str, profile: str) -> str:
    return fingerprint([protocol_hash, segment, variant, profile])


def fingerprint(value: object) -> str:
    """Hash de estruturas historicas ja normalizadas, sem serializacao financeira dupla."""
    return digest(canonical(value).encode())


def build_context(
    sealed: dict[str, object], segment: str, variant: str, profile: str
) -> dict[str, object]:
    if segment not in SEGMENTS or variant not in VARIANTS or profile not in PROFILES:
        raise PaperError("INVALID_CURVE_SCOPE")
    configuration = require_object(sealed["configuration"])
    modules = [require_object(m) for m in sequence(configuration["modules"])]
    active = [
        m
        for m in modules
        if m["enabled"] is True
        and "backtest" in sequence(m["evaluation_types"])
        and "local" in sequence(m["environments"])
        and "PAPER" in sequence(m["modes"])
        and "BTCUSDT" in sequence(m["symbols"])
    ]
    if len(active) != 1:
        raise PaperError("INVALID_CURVE_MODULE")
    module = active[0]
    bounds = sequence(require_object(sealed["segments"])[segment])
    params = require_object(configuration["parameters"]).copy()
    limits = list(require_object(configuration["symbol_limits"]).items())
    for pair in sequence(module["limits"]):
        values = sequence(pair)
        limits.append((str(values[0]), values[1]))
    for name, value in limits:
        params[name] = text(min(number(params[name]), number(value)))
    weight = module["capital_weight"]
    if profile != "sma":
        weight = "1" if profile == "buy_hold_100pct" else "0.10"
        params["max_entry_fraction"] = weight
        params["max_exposure_fraction"] = weight
    double = variant in ("double_costs", "double_costs_delay")
    params.update(
        fee_bps="20" if double else "10",
        spread_bps="20" if double else "10",
        slippage_bps="10" if double else "5",
    )
    key = run_key(str(sealed["protocol_hash"]), segment, variant, profile)
    params["wallet_id"] = key
    return {
        "run_key": key,
        "model": MODEL,
        "protocol_hash": sealed["protocol_hash"],
        "dataset_id": require_object(sealed["dataset"])["dataset_id"],
        "dataset_sha256": require_object(sealed["dataset"])["data_sha256"],
        "config_hash": fingerprint(configuration),
        "module": module,
        "parameters": params,
        "rules": configuration["rules"],
        "start": bounds[0],
        "end": bounds[1],
        "segment": segment,
        "variant": variant,
        "profile": profile,
        "capital_weight": weight,
        "delay_candles": int(variant in ("delay_1candle", "double_costs_delay")),
    }
