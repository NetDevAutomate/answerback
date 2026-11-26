"""Bus dependencies are optional; bus paths import them on first use."""

from typing import TYPE_CHECKING

from ..process import BridgeError

if TYPE_CHECKING:
    from .broker import Broker
    from .jetstream import JetStreamBus

BUS_IMPORT_ROOTS = frozenset({"nats", "psutil"})

MISSING_BUS_EXTRA = (
    "The agent bus needs the optional extra: install answerback[bus] "
    "(uv tool install 'answerback[bus]' or uv sync --extra bus)"
)


def missing_bus_extra(error: ImportError) -> BridgeError:
    """Explain absent bus packages; re-raise broken or unrelated imports."""
    if not isinstance(error, ModuleNotFoundError) or error.name not in BUS_IMPORT_ROOTS:
        raise error
    return BridgeError(MISSING_BUS_EXTRA)


def broker_class() -> type["Broker"]:
    try:
        from .broker import Broker
    except ImportError as error:
        raise missing_bus_extra(error) from error
    return Broker


def jetstream_bus_class() -> type["JetStreamBus"]:
    try:
        from .jetstream import JetStreamBus
    except ImportError as error:
        raise missing_bus_extra(error) from error
    return JetStreamBus
