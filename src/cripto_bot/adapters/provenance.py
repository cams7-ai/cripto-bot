"""Identity of the code actually imported; never infer it from a sibling checkout."""

import base64
import hashlib
import json
from importlib import import_module, metadata
from pathlib import Path
from urllib.parse import unquote, urlsplit

from cripto_bot.domain.portfolio.models import PaperError


def package_identity(name: str, expected_version: str | None = None) -> dict[str, object]:
    try:
        distribution = metadata.distribution(name.replace("_", "-"))
        if expected_version is not None and distribution.version != expected_version:
            raise PaperError("PACKAGE_VERSION_MISMATCH")
        module = import_module(name)
        if module.__file__ is None:
            raise PaperError("SOURCE_IDENTITY_UNAVAILABLE")
        package = Path(module.__file__).resolve().parent
        sources = sorted(package.rglob("*.py"))
        if not sources or not (package / "__init__.py").is_file():
            raise PaperError("SOURCE_IDENTITY_UNAVAILABLE")
        direct = json.loads(distribution.read_text("direct_url.json") or "{}")
        editable = direct.get("dir_info", {}).get("editable") is True
        content = {
            str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources
        }
        if editable:
            # Only the explicitly installed bot editable is supported for development.
            url = urlsplit(direct["url"])
            if name != "cripto_bot" or url.scheme != "file" or url.netloc:
                raise PaperError("UNSUPPORTED_PACKAGE_INSTALLATION")
            origin = Path(unquote(url.path)).resolve() / "src" / name
            if origin != package:
                raise PaperError("PACKAGE_ORIGIN_MISMATCH")
        else:
            recorded = distribution.files
            if recorded is None:
                raise PaperError("PACKAGE_RECORD_MISSING")
            entries = {
                str(p): p
                for p in recorded
                if str(p).startswith(name + "/")
                and (str(p).endswith(".py") or str(p).endswith("py.typed"))
            }
            typed = package / "py.typed"
            if typed.is_file():
                content["py.typed"] = hashlib.sha256(typed.read_bytes()).hexdigest()
            if set(entries) != {name + "/" + p for p in content}:
                raise PaperError("PACKAGE_RECORD_MISMATCH")
            for relative, digest in content.items():
                entry = entries[name + "/" + relative]
                if Path(str(distribution.locate_file(entry))).resolve() != package / relative:
                    raise PaperError("PACKAGE_ORIGIN_MISMATCH")
                encoded = base64.urlsafe_b64encode(bytes.fromhex(digest)).rstrip(b"=").decode()
                if entry.hash is None or entry.hash.mode != "sha256" or entry.hash.value != encoded:
                    raise PaperError("PACKAGE_RECORD_MISMATCH")
        return {
            "version": distribution.version,
            "installation": "editable" if editable else "wheel",
            "files": content,
        }
    except PaperError:
        raise
    except (OSError, ValueError, KeyError, TypeError, ImportError, metadata.PackageNotFoundError):
        raise PaperError("SOURCE_IDENTITY_UNAVAILABLE") from None


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
        },
    }
