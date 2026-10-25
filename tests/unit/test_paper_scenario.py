import json

import pytest
from cripto_bot.adapters.paper_scenario import load_scenario
from cripto_bot.domain.portfolio.models import PaperError

from tests.paper_factories import scenario


@pytest.mark.parametrize(
    "kind",
    [
        "naive",
        "order",
        "float",
        "unknown",
        "extra",
        "duplicate",
        "timestamp",
        "large",
        "malformed",
        "flag",
        "day",
        "schema_bool",
    ],
)
def test_reject_invalid_scenarios_before_wallet(tmp_path, kind):
    source = scenario()
    if kind == "naive":
        source["events"][1]["timestamp"] = "2026-10-01T00:15:01"
    elif kind == "order":
        source["events"] = list(reversed(source["events"]))
    elif kind == "float":
        source["events"][1]["quantity"] = 1.0
    elif kind == "unknown":
        source["events"][1]["side"] = "SYNTHETIC_SECRET"
    elif kind == "extra":
        source["events"][1]["api_key"] = "SYNTHETIC_SECRET"
    elif kind == "duplicate":
        source["events"][1]["event_id"] = source["events"][0]["event_id"]
    elif kind == "timestamp":
        source["events"][0]["observation"]["observed_at"] = "bad"
    elif kind == "flag":
        source["rules"]["filters"][3]["applyToMarket"] = "true"
    elif kind == "day":
        source["events"].append(source["events"][0])
    elif kind == "schema_bool":
        source["schema_version"] = True
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(source))
    if kind == "large":
        path.write_bytes(b"x" * (1024 * 1024 + 1))
    elif kind == "malformed":
        path.write_text("NaN")
    with pytest.raises(PaperError) as caught:
        load_scenario(path)
    assert "SYNTHETIC_SECRET" not in str(caught.value)
    assert not (tmp_path / "paper.sqlite3").exists()


def test_valid_scenario(tmp_path):
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario()))
    assert len(load_scenario(path)["events"]) == 3


@pytest.mark.parametrize(
    "kind",
    [
        "rules_keys",
        "filters_type",
        "filter_name",
        "filter_fields",
        "empty",
        "too_many",
        "quote_collision",
        "quote_mixed",
        "bid_ask_mixed",
        "missing_action",
        "mark_order_fields",
        "intraday_start",
        "submillisecond",
    ],
)
def test_remaining_structural_contracts(tmp_path, kind):
    source = scenario()
    if kind == "rules_keys":
        source["rules"]["extra"] = True
    elif kind == "filters_type":
        source["rules"]["filters"] = {}
    elif kind == "filter_name":
        source["rules"]["filters"][0]["filterType"] = 1
    elif kind == "filter_fields":
        source["rules"]["filters"][0]["extra"] = "ignored"
    elif kind == "empty":
        source["events"] = []
    elif kind == "too_many":
        source["events"] *= 334
    elif kind == "quote_collision":
        source["events"][2]["observation"]["quote_id"] = source["events"][1]["observation"][
            "quote_id"
        ]
    elif kind == "quote_mixed":
        source["events"][1]["observation"]["bid"] = "100"
    elif kind == "bid_ask_mixed":
        source["events"][1]["observation"].update(
            kind="BID_ASK", bid="100", ask="101", bid_qty="1", ask_qty="1"
        )
    elif kind == "missing_action":
        del source["events"][1]["action_id"]
    elif kind == "mark_order_fields":
        source["events"][0]["quantity"] = "1"
    elif kind == "intraday_start":
        source["events"][0]["timestamp"] = "2026-10-01T00:01:00Z"
    else:
        source["events"][1]["timestamp"] = "2026-10-01T00:15:01.000001Z"
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(source))
    with pytest.raises(PaperError):
        load_scenario(path)
