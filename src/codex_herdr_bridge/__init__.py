"""Import compatibility for the canonical :mod:`answerback_bridge` package.

Redirect nested imports before Python searches the canonical package's path under
the legacy name. Otherwise it would execute implementation files twice, creating
different exception classes and independent module state.
"""

import sys
from collections.abc import Sequence
from importlib import import_module
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from importlib.util import find_spec, spec_from_loader
from types import CodeType, ModuleType

_LEGACY = "codex_herdr_bridge"
_CANONICAL = "answerback_bridge"
_MOVED_MODULES = {
    ".core": ".providers.herdr.core",
    ".core_events": ".providers.herdr.core_events",
    ".core_rpc": ".providers.herdr.core_rpc",
    ".core_tasks": ".providers.herdr.core_tasks",
    ".herdr": ".providers.herdr.herdr",
    ".herdr_app": ".providers.herdr.herdr_app",
    ".adapters.core": ".providers.herdr.adapters.core",
    ".adapters.herdr": ".providers.herdr.adapters.herdr",
}


class _AliasLoader(Loader):
    def __init__(self, canonical: ModuleType) -> None:
        self.canonical = canonical
        self.canonical_spec = canonical.__spec__

    def create_module(self, spec: ModuleSpec) -> ModuleType:
        return self.canonical

    def exec_module(self, module: ModuleType) -> None:
        # Import machinery assigns the alias spec even to an existing module.
        # Keep canonical resource lookup, introspection and reload semantics.
        module.__spec__ = self.canonical_spec

    def get_code(self, fullname: str) -> CodeType:
        # runpy asks for code instead of exec_module for `python -m`. Its wrapper
        # calls the already imported CLI; implementation state stays canonical.
        source = (
            f"from {self.canonical.__name__} import main\nmain()\n"
            if fullname == f"{_LEGACY}.bus.cli"
            else f"import {self.canonical.__name__}\n"
        )
        return compile(
            source,
            f"<compatibility entry point {fullname}>",
            "exec",
        )


class _LegacyImports(MetaPathFinder):
    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if not fullname.startswith(f"{_LEGACY}.") or fullname == f"{_LEGACY}.__main__":
            return None
        suffix = fullname[len(_LEGACY) :]
        canonical_name = _CANONICAL + _MOVED_MODULES.get(suffix, suffix)
        if find_spec(canonical_name) is None:
            return None
        canonical = import_module(canonical_name)
        return spec_from_loader(
            fullname, _AliasLoader(canonical), is_package=hasattr(canonical, "__path__")
        )


sys.meta_path.insert(0, _LegacyImports())
sys.modules[__name__] = import_module(_CANONICAL)
