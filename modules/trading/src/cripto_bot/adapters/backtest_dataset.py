"""Importacao/publicacao de dataset em streaming com limites e proveniencia."""

import gzip
import hashlib
import heapq
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

from cripto_bot.domain.backtest.models import MAX_BYTES, MAX_CANDLES, MAX_LINE, BacktestSettings
from cripto_bot.domain.market import STEP, canonical, integer, parse_candle
from cripto_bot.domain.portfolio.models import PaperError, number, require_object


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path, limit: int = 1024 * 1024) -> dict[str, object]:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise PaperError("MANIFEST_TOO_LARGE")

    def invalid(value: str) -> None:
        raise PaperError("INVALID_JSON")

    return require_object(json.loads(data, parse_constant=invalid))


def contained_file(path: Path, root: Path, parent: Path | None = None) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or (
        parent and not resolved.is_relative_to(parent.resolve())
    ):
        raise PaperError("UNSAFE_DATASET_PATH")
    return resolved


def normalize(value: object) -> dict[str, object]:
    row = require_object(value)
    required = {
        "open_time",
        "close_time",
        "open",
        "high",
        "low",
        "close",
        "base_volume",
        "quote_volume",
        "trades",
        "symbol",
        "interval",
    }
    if (
        set(row) not in (required, required | {"available_at"})
        or row.get("symbol") != "BTCUSDT"
        or row.get("interval") != "15m"
    ):
        raise PaperError("INVALID_HISTORICAL_CANDLE")
    candle = parse_candle(
        [
            row["open_time"],
            row["open"],
            row["high"],
            row["low"],
            row["close"],
            row["base_volume"],
            row["close_time"],
            row["quote_volume"],
            row["trades"],
            "0",
            "0",
            "0",
        ]
    )
    for key in ("open", "high", "low", "close", "base_volume", "quote_volume"):
        number(row[key])
    available = candle.close_time + 1
    if "available_at" in row and (
        type(row["available_at"]) is not int or row["available_at"] != available
    ):
        raise PaperError("INVALID_AVAILABILITY")
    return {**candle.record(), "available_at": available}


def _source(path: Path, root: Path, budget: list[int]) -> Iterator[dict[str, object]]:
    manifest = read_json(path)
    if (
        type(manifest.get("schema")) is not int
        or manifest["schema"] != 1
        or manifest.get("parser") != "spot-market-v1"
        or manifest.get("complete") is not True
        or manifest.get("symbol") != "BTCUSDT"
        or manifest.get("interval") != "15m"
        or manifest.get("source") not in ("fixture", "binance_public")
    ):
        raise PaperError("INCOMPATIBLE_SOURCE_MANIFEST")
    for key in ("start", "end", "count"):
        integer(manifest.get(key))
    if integer(manifest["count"]) > MAX_CANDLES:
        raise PaperError("DATASET_LIMIT")
    names = require_object(manifest["files"])
    if set(names) != {"candles.jsonl.gz", "raw.jsonl.gz", "snapshots.json", "provenance.json"}:
        raise PaperError("INVALID_SOURCE_FILES")
    for name, digest in names.items():
        candidate = contained_file(path.parent / name, root, path.parent)
        if file_hash(candidate) != digest:
            raise PaperError("SOURCE_HASH_MISMATCH")
    count = 0
    previous = -1
    hasher = hashlib.sha256()
    with gzip.open(path.parent / "candles.jsonl.gz", "rb") as stream:
        while True:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                break
            budget[0] += len(line)
            budget[1] += 1
            if len(line) > MAX_LINE or budget[0] > MAX_BYTES or budget[1] > 2 * MAX_CANDLES:
                raise PaperError("DATASET_LIMIT")
            hasher.update(line)
            candle = normalize(json.loads(line))
            opened = integer(candle["open_time"])
            if opened <= previous or not int(str(manifest["start"])) <= opened < int(
                str(manifest["end"])
            ):
                raise PaperError("UNORDERED_SOURCE")
            count += 1
            previous = opened
            yield candle
    if count != manifest.get("count") or hasher.hexdigest() != manifest.get("data_sha256"):
        raise PaperError("SOURCE_CONTENT_MISMATCH")


def prepare(config: BacktestSettings) -> dict[str, object]:
    target = config.dataset_dir
    contained_file(target, config.root)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".dataset-pending-", dir=target.parent))
    try:
        manifests = [read_json(p) for p in config.source_manifests]
        sources = {str(m.get("source")) for m in manifests}
        if len(sources) != 1 or not sources <= {"fixture", "binance_public"}:
            raise PaperError("MIXED_OR_INVALID_ORIGIN")
        slow = config.module.slow_window if config.module else 50
        lower = min(config.start - slow * STEP, config.start // 86400000 * 86400000 - STEP)
        budget = [0, 0]
        count = 0
        duplicates = 0
        previous = None
        hasher = hashlib.sha256()
        with (temp / "candles.jsonl").open("wb") as output:
            merged = heapq.merge(
                *(_source(p, config.root, budget) for p in config.source_manifests),
                key=lambda c: int(str(c["open_time"])),
            )
            for candle in merged:
                opened = int(str(candle["open_time"]))
                if previous is not None and opened == int(str(previous["open_time"])):
                    if candle != previous:
                        raise PaperError("CONFLICTING_DUPLICATE")
                    duplicates += 1
                    continue
                if lower <= opened < config.end:
                    expected = lower + count * STEP
                    if opened != expected:
                        raise PaperError("DATASET_GAP")
                    body = (canonical(candle) + "\n").encode()
                    output.write(body)
                    hasher.update(body)
                    count += 1
                    if count > MAX_CANDLES:
                        raise PaperError("DATASET_LIMIT")
                previous = candle
            output.flush()
            os.fsync(output.fileno())
        if count != (config.end - lower) // STEP:
            raise PaperError("INCOMPLETE_DATASET")
        manifest: dict[str, object] = {
            "schema": 1,
            "dataset_id": "BTCUSDT-15m-historical-v1",
            "source": next(iter(sources)),
            "symbol": "BTCUSDT",
            "interval": "15m",
            "start": lower,
            "end": config.end,
            "score_start": config.start,
            "score_end": config.end,
            "warmup": slow,
            "count": count,
            "score_count": (config.end - config.start) // STEP,
            "data_sha256": hasher.hexdigest(),
            "duplicates": duplicates,
            "gaps": 0,
            "availability": "bar_close_plus_1ms",
            "files": {"candles.jsonl": hasher.hexdigest()},
            "sources": [
                {
                    "path": str(p),
                    "sha256": file_hash(p),
                    "source": m["source"],
                    "dataset": m.get("dataset"),
                    "exported_at": m.get("exported_at"),
                }
                for p, m in zip(config.source_manifests, manifests, strict=True)
            ],
            "filters": "synthetic proxy declared in frozen protocol; not historical exchangeInfo",
            "terms": (
                "Public data origin recorded; no claim of redistribution rights; s"
                "ynthetic fixtures marked"
            ),
        }
        (temp / "manifest.json").write_text(canonical(manifest) + "\n")
        if target.exists():
            if (
                read_json(target / "manifest.json") != manifest
                or file_hash(target / "candles.jsonl") != hasher.hexdigest()
            ):
                raise PaperError("DATASET_CONTEXT_CONFLICT")
        else:
            temp.rename(target)
        return manifest
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def validate(config: BacktestSettings) -> dict[str, object]:
    manifest = read_json(contained_file(config.dataset_dir / "manifest.json", config.root))
    if (
        manifest.get("schema") != 1
        or manifest.get("score_start") != config.start
        or manifest.get("score_end") != config.end
        or manifest.get("source") not in ("fixture", "binance_public")
    ):
        raise PaperError("DATASET_CONTEXT_CONFLICT")
    for key in (
        "schema",
        "start",
        "end",
        "count",
        "score_start",
        "score_end",
        "score_count",
        "warmup",
        "duplicates",
        "gaps",
    ):
        integer(manifest.get(key))
    if (
        manifest.get("files") != {"candles.jsonl": manifest.get("data_sha256")}
        or manifest.get("availability") != "bar_close_plus_1ms"
    ):
        raise PaperError("DATASET_CONTEXT_CONFLICT")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or len(sources) != len(config.source_manifests):
        raise PaperError("SOURCE_CONTEXT_CONFLICT")
    for path_source, raw_source in zip(config.source_manifests, sources, strict=True):
        source = require_object(raw_source)
        if source.get("path") != str(path_source) or source.get("sha256") != file_hash(path_source):
            raise PaperError("SOURCE_CONTEXT_CONFLICT")
        original = read_json(path_source)
        for name, digest in require_object(original["files"]).items():
            if name not in (
                "candles.jsonl.gz",
                "raw.jsonl.gz",
                "snapshots.json",
                "provenance.json",
            ):
                raise PaperError("INVALID_SOURCE_FILES")
            if (
                file_hash(
                    contained_file(path_source.parent / name, config.root, path_source.parent)
                )
                != digest
            ):
                raise PaperError("SOURCE_HASH_MISMATCH")
    path = contained_file(config.dataset_dir / "candles.jsonl", config.root, config.dataset_dir)
    if file_hash(path) != manifest.get("data_sha256"):
        raise PaperError("DATASET_HASH_MISMATCH")
    count = 0
    total = 0
    first = int(str(manifest["start"]))
    with path.open("rb") as stream:
        while line := stream.readline(MAX_LINE + 1):
            total += len(line)
            if len(line) > MAX_LINE or total > MAX_BYTES or count >= MAX_CANDLES:
                raise PaperError("DATASET_LIMIT")
            candle = normalize(json.loads(line))
            if int(str(candle["open_time"])) != first + count * STEP:
                raise PaperError("DATASET_GAP")
            count += 1
    if count != manifest.get("count") or first + count * STEP != config.end:
        raise PaperError("INCOMPLETE_DATASET")
    return manifest


def records(config: BacktestSettings) -> Iterator[dict[str, object]]:
    with (config.dataset_dir / "candles.jsonl").open("rb") as stream:
        while line := stream.readline(MAX_LINE + 1):
            if len(line) > MAX_LINE:
                raise PaperError("DATASET_LIMIT")
            yield normalize(json.loads(line))
