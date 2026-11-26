"""Compatibility import for :mod:`answerback_bridge.providers.kiro.workers`."""

import sys
from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:

    def __getattr__(name: str) -> Any: ...
else:
    sys.modules[__name__] = import_module("answerback_bridge.providers.kiro.workers")
