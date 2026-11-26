# Package a provider plugin

A provider distribution depends on the public `answerback` package, registers one zero-argument
factory in the `answerback.providers` entry-point group, owns its transport and policy, and runs
the shipped conformance contract from its own tests. Installed into the same environment as
Answerback, it loads beside the kernel's providers; uninstalled, its surfaces go away and the
kernel does not change.

This page is for someone writing a provider package who has read the
[adapter contract](ADAPTERS.md). The files below satisfy that contract for a neutral
`answerback-example` package, the worked example throughout.

The shortest check that a package composes is its own conformance test, run after an editable
install:

```sh
uv run pytest -q tests/test_provider_conformance.py
```

```text
.....                                                                    [100%]
5 passed in 0.05s
```

The output is from the kernel's own Kiro provider, which runs the same contract
(`tests/test_kiro_provider_conformance.py`). Pytest parametrizes the one test over five cases
(`plugin-shape`, `surfaces`, `config`, `store-hooks`, `receipt-reasons`); five passes mean the
plugin's declarations match the kernel's contract. They do not mean the provider's transport works.

## Package layout

```text
answerback-example/
├── pyproject.toml
├── README.md
├── src/
│   └── answerback_example/
│       ├── __init__.py       # factory and complete ProviderPlugin declaration
│       ├── adapter.py        # ProviderAdapter implementation
│       ├── config.py         # providers.example config block
│       ├── reasons.py        # fixed receipt reasons and writer-source map
│       ├── registrar.py      # optional MCP-client installer via ClientRegistrar
│       ├── cli.py            # optional answerback subcommands
│       └── tools.py          # optional MCP tools
└── tests/
    ├── conftest.py           # package-owned conformance declaration
    └── test_provider_conformance.py
```

Keep provider implementation and policy inside the provider package. Import kernel contracts from
public modules such as `answerback_bridge.plugin`, `answerback_bridge.provider_config` and
`answerback_bridge.adapters.contracts`. Do not import Answerback's repository `tests/` directory;
the conformance API ships in the installed wheel. A provider that has no native MCP client,
reconciliation proof or public diagnostic omits the corresponding registrar, hooks or error module.
Carry an AST guard in your tests that checks nothing in the package reaches a private kernel name
(one that starts with `_`).

## Distribution metadata

```toml
[build-system]
requires = ["hatchling>=1.27,<2"]
build-backend = "hatchling.build"

[project]
name = "answerback-example"
version = "0.1.0"
requires-python = ">=3.11,<3.16"
dependencies = ["answerback>=0.16,<0.17"]

[project.entry-points."answerback.providers"]
example = "answerback_example:example_plugin"

[tool.hatch.build.targets.wheel]
packages = ["src/answerback_example"]
```

The entry-point name, `ProviderDefinition.registration`, `ProviderConfig.provider` and the name
passed to `provider_plugin()` must all be `example`. The plugin API is `PLUGIN_API = 1`
(`answerback_bridge/plugin.py`); the pre-0.16 flat configuration keys stop being read in 0.17, so
pin the upper bound and re-test at the next line.

## Configuration block

Each provider owns a dataclass under `providers.<registration>`:

```python
from dataclasses import dataclass
from typing import ClassVar

from answerback_bridge.provider_config import ProviderConfig


@dataclass
class ExampleConfig(ProviderConfig):
    provider: ClassVar[str] = "example"

    binary: str = "example-agent"
    endpoint: str = "local"

    def validate(self) -> None:
        if not self.binary:
            raise ValueError("example binary must not be empty")
        if self.endpoint not in {"local", "remote"}:
            raise ValueError("example endpoint must be 'local' or 'remote'")
```

Use dataclass defaults for an unconfigured installation. Validate types and bounds in
`validate()`. The kernel serializes this block as nested JSON and preserves an unknown provider
block when its plugin is absent. `binary` is the field `answerback providers register example`
probes with `shutil.which`; a provider with no executable sets
`registration_binary_field=None` on its definition.

## Adapter minimum

`adapter.py` builds on the public `StoredAdapter` defaults. The base supplies `enabled`,
`recovery_states`, `discover`, `resolve_exact`, `previous`, `observe`, `poll_ids` and `close`. A
minimal provider implements the remaining identity and transport methods:

```python
from answerback_bridge.adapters.base import StoredAdapter
from answerback_bridge.adapters.contracts import (
    Guard,
    Peer,
    PreparedPeer,
    Submission,
    TaskReceipt,
)
from answerback_bridge.plugin import KernelContext


class ExampleAdapter(StoredAdapter):
    name = "example"
    registration = "example"
    aliases = ()

    def __init__(self, ctx: KernelContext) -> None:
        super().__init__(ctx.config, ctx.config_path, ctx.store)

    async def prepare(self, peer: Peer) -> PreparedPeer:
        self.require_enabled()
        # Provider-specific: derive a stable lock key from the discovered identity.
        return PreparedPeer(f"example:{peer.target}", peer)

    async def submit(
        self,
        prepared: PreparedPeer,
        submission: Submission,
        *,
        guard: Guard,
    ) -> TaskReceipt:
        self.require_enabled()
        guard()
        # Provider-specific: call the native transport, persist its receipt, and return it.
        raise NotImplementedError

    def matches(self, peer: Peer, receipt: TaskReceipt) -> bool:
        # Provider-specific: compare the immutable peer and receipt identities.
        return receipt.provider == self.name and receipt.target == f"example:{peer.target}"
```

`controls` (owned execution status, permission decisions, cancellation) and native-evidence
reconciliation are optional capabilities, not part of the minimum. Override any `StoredAdapter`
default whose semantics the provider can implement more precisely, especially discovery and
observation.

## Plugin factory

```python
from answerback_bridge.adapters.catalog import ProviderDefinition
from answerback_bridge.adapters.contracts import ProviderAdapter
from answerback_bridge.plugin import (
    KernelContext,
    PluginOwners,
    ProviderPluginSpec,
    provider_plugin,
)

from .adapter import ExampleAdapter
from .config import ExampleConfig

DEFINITION = ProviderDefinition(
    "example",
    "example",
    recovery_states=("running",),
    routing_fields=("example.binary", "example.endpoint"),
)
CAPABILITIES = frozenset({"discovery", "send"})


def _example(owners: PluginOwners) -> ProviderPluginSpec:
    def build(ctx: KernelContext) -> ProviderAdapter:
        return ExampleAdapter(ctx)

    return provider_plugin(
        "example",
        build,
        ExampleConfig,
        _example,
        definition=DEFINITION,
        capabilities=CAPABILITIES,
    )


def example_plugin() -> ProviderPluginSpec:
    return _example(PluginOwners.detached())
```

Add only the surfaces the provider owns, as keyword arguments of `provider_plugin`:

- `verbs={...}` for RPC handlers.
- `commands=...` for CLI subcommands.
- `tools=(ToolSpec(...), ...)` for MCP tools.
- `sources=(..., ...)` for observer event sources.
- `hooks=...` for receipt reconciliation or project lookup.
- `reasons=(ReasonRule(...), ...)` for provider-written fixed receipt details.
- `status_block=...`, `instructions=...` and `client_registrar=...` where applicable.

`PluginOwners.detached()` lets startup inspect declarations without a live service. A package
provider that needs service-owned objects keeps an owner factory (`_example` above) so the kernel
can rebind the plugin during composition. Depend only on this public owner API: the kernel still
constructs its built-ins' service objects itself, and the change that removes that
([deferred to 0.17](ADAPTERS.md#deferred-to-017)) does not alter the package contract.

## Optional client registrar

A provider that installs Answerback into its native MCP client implements the public
`ClientRegistrar` protocol and passes that object as `client_registrar`. The registrar owns
destination-path calculation, preparation, lock selection, validation, apply and rollback; the
kernel owns the multi-client transaction. Use the public helpers in
`answerback_bridge.installer_kit` rather than importing private registration functions. A provider
whose client already uses one of the kernel's supported registration formats needs no registrar.

At minimum, pin the registrar's nonempty `name` and every method of `ClientRegistrar` in the
conformance shape check. Add provider tests for native path rules, migration and rollback; the
shared conformance suite cannot know those semantics.

## Composition and the absent-package case

The kernel never imports a provider distribution. It discovers only installed
`answerback.providers` entry points, passes the public `KernelContext` to the adapter and rebinds
public `PluginOwners` where needed. Uninstalling a provider removes its live surfaces without
changing the kernel; a saved config block for the absent provider is kept, reported as `disabled`
with a warning, and written back unchanged.

Answerback's own gate runs in [two compositions](TESTING-HARNESSES.md#the-two-compositions): with
no plugin package installed, and with the internal provider package installed. Apply the same rule
in your own CI: the kernel suite with your package absent, and your package's suite with it
installed.

## Opt in to conformance

The `answerback` distribution registers `answerback.testing.conformance` in the `pytest11`
entry-point group. Pytest loads the fixture wherever Answerback and pytest are installed, but it
runs no conformance check by itself. A provider opts in by declaring its package contract in
`tests/conftest.py` and passing that fixture to one small test.

```python
# tests/conftest.py
from pathlib import Path

import answerback_example
import pytest
from answerback.testing.conformance import ProviderConformance
from answerback_example import example_plugin


@pytest.fixture
def answerback_provider_contract() -> ProviderConformance:
    return ProviderConformance(
        factory=example_plugin,
        source_root=Path(answerback_example.__file__).resolve().parent,
        config={"binary": "example-test", "endpoint": "remote"},
        rpc=frozenset(),
        cli=frozenset(),
        mcp=frozenset(),
        events=frozenset(),
        store_hooks=None,
    )
```

```python
# tests/test_provider_conformance.py
from answerback.testing.conformance import ProviderConformance, ProviderConformanceRunner


def test_example_provider_conformance(
    answerback_provider_conformance: ProviderConformanceRunner,
    answerback_provider_contract: ProviderConformance,
) -> None:
    answerback_provider_conformance(answerback_provider_contract)
```

Declare every surface exactly, including empty surfaces. If the provider returns store hooks, pass
`StoreHooksConformance` with provider-owned examples:

```python
from answerback.testing.conformance import StoreHookReconcileRefusal, StoreHooksConformance

store_hooks = StoreHooksConformance(
    project_cases=(({"cwd": "/tmp/example"}, "/tmp/example"), ({}, None)),
    reconcilable_cases=(
        ({"status": "uncertain", "metadata": {"kind": "example"}}, True),
        ({"status": "failed", "metadata": {"kind": "example"}}, False),
    ),
    reconcile_refusals=(
        StoreHookReconcileRefusal(
            task={"status": "failed", "metadata": {"kind": "example"}},
            match="requires an uncertain example receipt",
        ),
    ),
)
```

The shared contract checks the plugin API version, registration, definition fields, capabilities,
registrar exports and config-schema shape; the exact RPC, CLI, MCP and event-source declarations;
the declared store-hook project and reconciliation behaviour; nested config load/save/load round
trips; the preservation, disabled status and warning of an unknown provider block; and receipt
reason source paths and fixed-fragment counts, which catch writer/table drift.

`ProviderConformance.source_root` is the base for every path in each `ReasonRule.sources` mapping;
a package whose reason paths are package-relative passes its package directory. A provider with no
reason rules still passes the same contract. A provider with no settings declares
`config_schema = None` and passes `config=None`; the other checks still run.

For a non-pytest harness, call the public function directly:

```python
from answerback.testing.conformance import check_provider_conformance

check_provider_conformance(declaration, temporary_directory)
```

Run the contract test with the same lint, format and type checks the kernel uses
(`uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check src`), then add
protocol-specific tests for discovery, submission, observation, cancellation, recovery and any
native process or network boundary.

## What it cannot do yet

- The conformance contract checks plugin composition only. It does not prove the provider's
  external transport works, and it is not R1 support: that needs a live run through the real
  delivery path.
- A third-party plugin cannot receive a service-owned object from the kernel; `PluginOwners`
  hard-codes one field per built-in ([deferred to 0.17](ADAPTERS.md#deferred-to-017)).
- The flat configuration keys of 0.15 are read until 0.17 and then stop; a template package pins
  `answerback<0.17` for that reason.

## See also

- [ADAPTERS.md](ADAPTERS.md): the contract each file above satisfies.
- [TESTING-HARNESSES.md](TESTING-HARNESSES.md#the-two-compositions): how the kernel's gate runs with
  and without a plugin package.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md#provider-and-module-plugins): what an operator sees when
  the loader skips a plugin.
