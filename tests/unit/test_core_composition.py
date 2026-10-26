"""Regression tests for the installed composition and compatibility exports."""

import base64
import hashlib
import importlib
import json
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

import pytest
from cripto_core.contracts import Plan as CorePlan
from cripto_core.errors import PaperError as CoreError
from cripto_core.strategy.sma import Snapshot as CoreSnapshot
from cripto_core.strategy.sma import TrendSMA as CoreSMA

from cripto_bot.adapters import provenance
from cripto_bot.adapters.storage.market_export import source_fingerprint
from cripto_bot.domain.portfolio.models import PaperError, Plan
from cripto_bot.domain.strategy.sma import Snapshot, TrendSMA


def test_compatibility_exports_preserve_type_identity():
    assert Plan is CorePlan and PaperError is CoreError
    assert Snapshot is CoreSnapshot and TrendSMA is CoreSMA
    from cripto_core.execution import make_plan as core_make_plan

    from cripto_bot.domain.risk.execution import make_plan

    assert make_plan is core_make_plan


@pytest.fixture
def installed_core(tmp_path, monkeypatch):
    package = tmp_path / "cripto_core"
    package.mkdir()
    source = package / "__init__.py"
    source.write_text('"""Installed fixture."""\n')
    typed = package / "py.typed"
    typed.touch()
    info = tmp_path / "cripto_core-0.1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.3\nName: cripto-core\nVersion: 0.1.0\n")

    def seal():
        rows = []
        for file in [source, typed]:
            encoded = base64.urlsafe_b64encode(hashlib.sha256(file.read_bytes()).digest())
            rows.append(f"cripto_core/{file.name},sha256={encoded.rstrip(b'=').decode()},")
        (info / "RECORD").write_text("\n".join(rows) + "\n")

    seal()
    original_dist = metadata.distribution
    original_import = importlib.import_module
    monkeypatch.setattr(
        provenance.metadata,
        "distribution",
        lambda name: (
            metadata.PathDistribution(info) if name == "cripto-core" else original_dist(name)
        ),
    )
    monkeypatch.setattr(
        provenance,
        "import_module",
        lambda name: (
            SimpleNamespace(__file__=str(source))
            if name == "cripto_core"
            else original_import(name)
        ),
    )
    return source, info, seal


def test_installed_record_detects_missing_extra_or_tampered_source(installed_core):
    source, info, seal = installed_core
    assert provenance.package_identity("cripto_core", "0.1.0")["installation"] == "wheel"
    source.write_text("# changed code\n")
    with pytest.raises(PaperError, match="PACKAGE_RECORD_MISMATCH"):
        provenance.package_identity("cripto_core", "0.1.0")
    seal()
    extra = source.parent / "unrecorded.py"
    extra.write_text("# not part of the installed wheel\n")
    with pytest.raises(PaperError, match="PACKAGE_RECORD_MISMATCH"):
        provenance.package_identity("cripto_core", "0.1.0")
    extra.unlink()
    (info / "RECORD").unlink()
    with pytest.raises(PaperError, match="PACKAGE_RECORD_MISSING"):
        provenance.package_identity("cripto_core", "0.1.0")


def test_version_and_editable_core_rejected(installed_core):
    source, info, seal = installed_core
    with pytest.raises(PaperError, match="PACKAGE_VERSION_MISMATCH"):
        provenance.package_identity("cripto_core", "9.9.9")
    (info / "direct_url.json").write_text(
        json.dumps(
            {
                "url": source.parent.parent.as_uri(),
                "dir_info": {"editable": True},
            }
        )
    )
    with pytest.raises(PaperError, match="UNSUPPORTED_PACKAGE_INSTALLATION"):
        provenance.package_identity("cripto_core", "0.1.0")


def test_only_core_change_changes_fingerprint_and_blocks_resume(installed_core, tmp_path):
    from cripto_bot.adapters.backtest_dataset import prepare
    from cripto_bot.adapters.config import load_config
    from cripto_bot.adapters.storage.backtest_sqlite import BacktestStore
    from cripto_bot.application.backtest_run import protocol, verify_protocol

    source, info, seal = installed_core
    root = Path(__file__).resolve().parents[2]
    import shutil

    for name in ("pyproject.toml", "uv.lock", ".python-version"):
        shutil.copyfile(root / name, tmp_path / name)
    shutil.copytree(
        root / "config/fixtures/backtest-sma", tmp_path / "config/fixtures/backtest-sma"
    )
    config_file = tmp_path / "config/backtest.toml"
    template = (root / "config/backtest.sma.fixture.toml").read_text()
    template = template.replace(
        "config/fixtures/backtest-sma/manifest.json",
        str(tmp_path / "config/fixtures/backtest-sma/manifest.json"),
    )
    # Resolve relative data paths within this isolated application root.
    config_file.write_text(template)
    from unittest.mock import patch

    with patch("pathlib.Path.cwd", return_value=tmp_path):
        config = load_config(config_file, {}, root=tmp_path).backtest
    assert config is not None
    prepare(config)
    before = source_fingerprint(tmp_path)
    with BacktestStore(config.data_dir, create=True) as store:
        store.freeze(protocol(config))
        source.write_text("# new core build, unchanged bot/config/dataset\n")
        seal()
        assert source_fingerprint(tmp_path) != before
        with pytest.raises(PaperError, match="FROZEN_PROTOCOL_CHANGED"):
            verify_protocol(store, config)


def test_source_identity_requires_controls(tmp_path):
    with pytest.raises(PaperError, match="SOURCE_IDENTITY_UNAVAILABLE"):
        provenance.runtime_identity(tmp_path)
