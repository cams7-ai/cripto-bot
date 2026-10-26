"""Fixture fechada, limitada e validada antes de qualquer efeito financeiro."""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cripto_bot.adapters.paper_scenario import QuoteInput, validate_rules
from cripto_bot.domain.market import STEP, integer, parse_candle
from cripto_bot.domain.portfolio.models import PaperError, identifier, instant, number
from cripto_bot.domain.risk.execution import filters, observation


class CandleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    open_time: int
    close_time: int
    available_at: str
    open: str
    high: str
    low: str
    close: str
    base_volume: str
    quote_volume: str
    trades: int


class ExecutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    candle_open: int
    fill_at: str
    observation: QuoteInput


class FixtureInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    schema_version: Literal[1]
    dataset_id: str
    symbol: Literal["BTCUSDT"]
    interval: Literal["15m"]
    as_of: str
    rules: dict[str, object]
    day_starts: list[QuoteInput] = Field(min_length=1, max_length=1000)
    candles: list[CandleInput] = Field(min_length=1, max_length=1000)
    executions: list[ExecutionInput] = Field(max_length=1000)


def load_fixture(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            body = stream.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise ValueError

        def invalid(value: str) -> None:
            raise ValueError

        raw = json.loads(body, parse_constant=invalid)
        if type(raw.get("schema_version")) is not int:
            raise ValueError
        data = FixtureInput.model_validate(raw)
        identifier(data.dataset_id)
        validate_rules(data.rules)
        filters(data.rules)
        cutoff = instant(data.as_of)
        candles = []
        previous = -1
        for c in data.candles:
            parsed = parse_candle(
                [
                    c.open_time,
                    c.open,
                    c.high,
                    c.low,
                    c.close,
                    c.base_volume,
                    c.close_time,
                    c.quote_volume,
                    c.trades,
                    "0",
                    "0",
                    "0",
                ]
            )
            for value in (c.open, c.high, c.low, c.close, c.base_volume, c.quote_volume):
                number(value)
            available = instant(c.available_at)
            if available != parsed.close_time + 1 or available > cutoff:
                raise ValueError
            if previous >= 0 and parsed.open_time != previous + STEP:
                raise ValueError
            previous = parsed.open_time
            candles.append({**parsed.record(), "available_at": available})
        starts = []
        seen_days = set()
        seen_quotes = set()
        for quote in data.day_starts:
            payload = quote.model_dump(exclude_none=True)
            at = instant(quote.observed_at)
            observation(payload, at, 5000)
            if at % 86400000 or at in seen_days:
                raise ValueError
            seen_days.add(at)
            seen_quotes.add(quote.quote_id)
            starts.append({"timestamp": quote.observed_at, "observation": payload})
        needed = {int(str(c["available_at"])) // 86400000 * 86400000 for c in candles}
        needed.add(int(str(candles[0]["open_time"])) // 86400000 * 86400000)
        if seen_days != needed or [instant(str(s["timestamp"])) for s in starts] != sorted(
            seen_days
        ):
            raise ValueError
        executions = {}
        opens = {c.open_time for c in data.candles}
        for execution in data.executions:
            integer(execution.candle_open)
            quote = execution.observation
            payload = quote.model_dump(exclude_none=True)
            at = instant(quote.observed_at)
            # Quotes stale/anteriores a decisao sao recusas de negocio no ciclo,
            # mas a forma, valores e identidade devem ser validos antes de gravar.
            observation(payload, at, 5000)
            if execution.candle_open not in opens or execution.candle_open in executions:
                raise ValueError
            if quote.quote_id in seen_quotes:
                raise ValueError
            seen_quotes.add(quote.quote_id)
            if instant(execution.fill_at) > cutoff:
                raise ValueError
            executions[execution.candle_open] = {
                "observation": payload,
                "fill_at": execution.fill_at,
            }
        return {
            "dataset_id": data.dataset_id,
            "rules": data.rules,
            "candles": candles,
            "day_starts": starts,
            "executions": executions,
            "as_of": cutoff,
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ValidationError):
        raise PaperError("INVALID_STRATEGY_FIXTURE") from None
