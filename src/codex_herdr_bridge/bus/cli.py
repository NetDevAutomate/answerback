"""Compatibility for the legacy executable bus module."""

import sys
from importlib import import_module

if __name__ == "__main__":
    from answerback_bridge.bus.cli import main

    main()
else:
    sys.modules[__name__] = import_module("answerback_bridge.bus.cli")
