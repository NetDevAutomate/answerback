"""Compatibility import for :mod:`answerback_bridge.runtime`."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("answerback_bridge.runtime")
