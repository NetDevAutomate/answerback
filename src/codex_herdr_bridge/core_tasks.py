"""Compatibility import for :mod:`answerback_bridge.providers.herdr.core_tasks`."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("answerback_bridge.providers.herdr.core_tasks")
