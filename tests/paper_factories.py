import copy
import json

from cripto_bot.domain.models import PaperSettings
from cripto_bot.domain.portfolio.engine import transition
from cripto_bot.domain.portfolio.models import MODEL, Wallet, fingerprint
from tests.factories import ROOT


def scenario():
    return json.loads((ROOT / "config/fixtures/paper-roundtrip.json").read_text())


def settings(path):
    return PaperSettings(root=ROOT, data_dir=path / "data", scenario_file=path / "scenario.json")


def context(path):
    source = scenario()
    config = settings(path)
    return {
        "model": MODEL,
        "parameters": config.parameters(),
        "rules": source["rules"],
        "scenario_id": source["scenario_id"],
        "scenario_sha256": fingerprint(source),
        "start_time": source["events"][0]["timestamp"],
    }


def initialized(path):
    ctx = context(path)
    wallet, _ = transition(
        Wallet(), "GENESIS", {"timestamp": ctx["start_time"]}, ctx["parameters"], ctx["rules"]
    )
    wallet, _ = transition(
        wallet, "DAY_START", scenario()["events"][0], ctx["parameters"], ctx["rules"]
    )
    return ctx, wallet


def order(index=1, **changes):
    event = copy.deepcopy(scenario()["events"][index])
    event.update(changes)
    event["action_key"] = "paper-demo:roundtrip-v1:" + event["action_id"]
    return event


def write_case(path, source=None, **overrides):
    source = scenario() if source is None else source
    sample = path / "scenario.json"
    sample.write_text(json.dumps(source))
    options = {
        "wallet_id": "paper-demo",
        "data_dir": str(path / "data"),
        "scenario_file": str(sample),
        **overrides,
    }
    config = path / "paper.toml"
    config.write_text(
        'mode="PAPER"\n[paper]\n'
        + "\n".join(f"{key}={json.dumps(value)}" for key, value in options.items())
    )
    return config
