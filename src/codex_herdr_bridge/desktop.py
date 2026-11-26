"""Compatibility import for :mod:`answerback_bridge.desktop`."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("answerback_bridge.desktop")
