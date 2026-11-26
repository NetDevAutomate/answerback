"""Compatibility import for :mod:`answerback_bridge.lifecycle`."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("answerback_bridge.lifecycle")
