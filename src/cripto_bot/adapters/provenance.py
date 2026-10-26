"""Consumer-owned composition and Git provenance; data library does not import the bot."""

import hashlib
import subprocess
from importlib import import_module as import_module
from importlib import metadata as metadata
from pathlib import Path

from cripto_core.errors import PaperError
from cripto_core.serialization import canonical, digest
from cripto_market_data.adapters.inspection import inspect_package


def package_identity(name: str, expected_version: str | None = None) -> dict[str, object]:
    if name == "cripto_analytics":
        from cripto_bot.adapters.analytics_dependency import require_analytics

        require_analytics()
    return inspect_package(
        name, expected_version, importer=import_module, editable_names=frozenset({"cripto_bot"})
    )


def runtime_identity(root: Path) -> dict[str, object]:
    try:
        controls = {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("pyproject.toml", "uv.lock", ".python-version")
        }
    except OSError:
        raise PaperError("SOURCE_IDENTITY_UNAVAILABLE") from None
    return {
        "schema": "runtime-composition-v1",
        "controls": controls,
        "packages": {
            "cripto_bot": package_identity("cripto_bot"),
            "cripto_core": package_identity("cripto_core", "0.1.0"),
            "cripto_market_data": package_identity("cripto_market_data", "0.1.0"),
            "cripto_analytics": package_identity("cripto_analytics", "0.1.0"),
        },
    }


def code_revision(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
            timeout=2,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
            timeout=2,
        )
        return result.stdout.strip() + ("+dirty" if status.stdout else "")
    except (OSError, subprocess.SubprocessError):
        return "unversioned"


def source_fingerprint(root: Path) -> str:
    return digest(canonical(runtime_identity(root)).encode())
