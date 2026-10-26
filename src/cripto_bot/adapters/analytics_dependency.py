"""Load report capabilities only at the command boundary that requires them."""

from importlib import import_module
from types import ModuleType

from cripto_core.errors import PaperError


def load(name: str) -> ModuleType:
    try:
        return import_module(name)
    except ModuleNotFoundError as error:
        if error.name and (
            error.name == "duckdb" or error.name.split(".")[0] == "cripto_analytics"
        ):
            raise PaperError("ANALYTICS_DEPENDENCY_UNAVAILABLE") from None
        raise


def require_analytics(*, engine: bool = False) -> None:
    load("cripto_analytics")
    if engine:
        load("duckdb")
