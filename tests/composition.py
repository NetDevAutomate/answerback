"""Which providers this test run composes: the kernel's own and any installed plugin.

The kernel suite runs in two compositions (docs/TESTING-HARNESSES.md): PUBLIC, the default
`uv sync` of this repository, composes the kernel's providers only; INTERNAL adds exactly one
provider plugin, installed editable beside the kernel from the plugin's own repository.
Snapshot literals that differ between the two are pinned side by side and
selected here (`pick`), and what the plugin adds to a kernel pin (names, snapshot fragments,
fixture programs) is read from the plugin's own `tests/fixtures/kernel_composition.py`
(`contributed`), so the kernel suite names no plugin.
"""

import importlib.util
import sys
from importlib import import_module, metadata
from pathlib import Path
from types import ModuleType
from typing import TypeVar

from answerback_bridge.adapters.builtins import builtin_plugins
from answerback_bridge.plugin import PROVIDER_GROUP

KERNEL_PROVIDERS = (
    "hc",
    "herdr",
    "codex",
    "kiro",
    "crew",
    "rex",
    "codex_cli",
    "claude_code",
    "grok",
)
INSTALLED_PROVIDERS = tuple(plugin.definition.registration for plugin in builtin_plugins())
PLUGIN_PROVIDERS = tuple(name for name in INSTALLED_PROVIDERS if name not in KERNEL_PROVIDERS)
# The internal composition is the public one plus exactly one installed plugin.
PUBLIC = PLUGIN_PROVIDERS == ()
INTERNAL = len(PLUGIN_PROVIDERS) == 1
assert PUBLIC or INTERNAL, f"unexpected composition: {PLUGIN_PROVIDERS}"
_T = TypeVar("_T")


def pick(public: _T, internal: _T) -> _T:
    """The literal of this composition."""
    return internal if INTERNAL else public


def plugin_fixture_root() -> Path:
    """The installed plugin's own `tests/fixtures/` directory (editable install).

    Found through the plugin's entry point: its module's package root is the package
    directory of the plugin's package (`<package>/src/<module>/__init__.py`).
    """
    assert INTERNAL, "the public composition installs no plugin"
    (name,) = PLUGIN_PROVIDERS
    (entry,) = [e for e in metadata.entry_points(group=PROVIDER_GROUP) if e.name == name]
    module = import_module(entry.module.partition(".")[0])
    assert module.__file__ is not None
    return Path(module.__file__).resolve().parents[2] / "tests" / "fixtures"


def plugin_fixture(name: str) -> Path:
    """A file under the installed plugin's own `tests/fixtures/`."""
    return plugin_fixture_root() / name


def _contributions() -> ModuleType | None:
    """The plugin's `kernel_composition.py`: what it adds to each kernel pin."""
    if PUBLIC:
        return None
    path = plugin_fixture("kernel_composition.py")
    module_name = "answerback_plugin_kernel_composition"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    assert tuple(module.PROVIDERS) == PLUGIN_PROVIDERS, (module.PROVIDERS, PLUGIN_PROVIDERS)
    return module


CONTRIBUTIONS = _contributions()


def contributed(name: str, public: _T) -> _T:
    """What the installed plugin adds to the kernel pin `name`; `public` when none is."""
    if CONTRIBUTIONS is None:
        return public
    return getattr(CONTRIBUTIONS, name)
