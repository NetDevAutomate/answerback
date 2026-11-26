"""Compatibility import for :mod:`answerback_bridge.adapters`."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("answerback_bridge.adapters")
