"""Installed analytics identity, early dependency failures and lazy report composition."""

import base64
import hashlib
import importlib
import json
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

import pytest
from cripto_core.errors import PaperError

from cripto_bot.adapters import provenance
from cripto_bot.adapters.backtest_dataset import prepare
from cripto_bot.adapters.storage.backtest_sqlite import BacktestStore
from cripto_bot.application.backtest_run import protocol, verify_protocol
from tests.unit.test_backtest import config as config


@pytest.fixture
def installed_analytics(tmp_path, monkeypatch):
    package = tmp_path / "cripto_analytics"
    package.mkdir()
    source = package / "__init__.py"
    source.write_text("# original analytics fixture\n")
    info = tmp_path / "cripto_analytics-0.1.1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.3\nName: cripto-analytics\nVersion: 0.1.1\n"
    )

    def seal():
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(source.read_bytes()).digest())
            .rstrip(b"=")
            .decode()
        )
        (info / "RECORD").write_text(f"cripto_analytics/__init__.py,sha256={digest},\n")

    seal()
    dist = metadata.distribution
    loader = importlib.import_module
    monkeypatch.setattr(
        provenance.metadata,
        "distribution",
        lambda n: metadata.PathDistribution(info) if n == "cripto-analytics" else dist(n),
    )
    monkeypatch.setattr(
        provenance,
        "import_module",
        lambda n: SimpleNamespace(__file__=str(source)) if n == "cripto_analytics" else loader(n),
    )
    return source, info, seal


def test_analytics_only_change_blocks_frozen_resume(config, installed_analytics):
    source, info, seal = installed_analytics
    prepare(config)
    before = provenance.source_fingerprint(config.root)
    with BacktestStore(config.data_dir, create=True) as store:
        store.freeze(protocol(config))
        source.write_text("# new analytics package; unchanged bot/core/config/data\n")
        seal()
        assert provenance.source_fingerprint(config.root) != before
        with pytest.raises(PaperError, match="FROZEN_PROTOCOL_CHANGED"):
            verify_protocol(store, config)


def test_analytics_record_version_and_editable_rejected(installed_analytics):
    source, info, seal = installed_analytics
    with pytest.raises(PaperError, match="PACKAGE_VERSION_MISMATCH"):
        provenance.package_identity("cripto_analytics", "9.0.0")
    source.write_text("# modified without updating RECORD\n")
    with pytest.raises(PaperError, match="PACKAGE_RECORD_MISMATCH"):
        provenance.package_identity("cripto_analytics", "0.1.1")
    seal()
    (info / "direct_url.json").write_text(
        json.dumps({"url": source.parent.parent.as_uri(), "dir_info": {"editable": True}})
    )
    with pytest.raises(PaperError, match="UNSUPPORTED_PACKAGE_INSTALLATION"):
        provenance.package_identity("cripto_analytics", "0.1.1")


def test_cli_defaults_do_not_load_duckdb():
    code = "import sys,io;from cripto_bot.entrypoints.cli import main;out=io.StringIO();err=io.StringIO();assert main(['check-config'],environ={},stdout=out,stderr=err)==0;assert main(['run-once'],environ={},stdout=out,stderr=err)==0;assert 'duckdb' not in sys.modules"  # noqa: E501
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(__file__).resolve().parents[1] / "offline_runner.py"),
            "--code",
            code,
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("missing", ["cripto_analytics", "duckdb"])
def test_required_dependency_missing_fails_before_store(config, monkeypatch, missing):
    from cripto_bot.adapters import analytics_dependency
    from cripto_bot.domain.models import Settings
    from cripto_bot.entrypoints.backtest import execute_backtest

    loader = analytics_dependency.import_module

    def load(name):
        if name == missing:
            raise ModuleNotFoundError(name=missing)
        return loader(name)

    monkeypatch.setattr(analytics_dependency, "import_module", load)
    code, result = execute_backtest(Settings(backtest=config), "backtest-run", "missing-dependency")
    assert code == 3 and result["error_code"] == "ANALYTICS_DEPENDENCY_UNAVAILABLE"
    assert result["simulated_fills"] == 0 and not config.data_dir.exists()
