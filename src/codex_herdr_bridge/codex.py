"""Compatibility import for :mod:`answerback_bridge.codex`."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("answerback_bridge.codex")
