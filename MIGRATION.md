# Migrate to the split repositories (0.16.1)

Until 0.16.0 one combined source tree held the kernel, the internal provider plugin, and the
maintainers' task ledger and evidence. From 0.16.1 they are in separate places. This page says
what moved where, how each composition is installed, and what an installed node does to move.

## The source

Both new repositories were created on 2026-11-26 from the combined tree (`herdr_bridge`) at
commit `1370511d0c9c4f992f79955033eeef6dc9b382bf`, as fresh histories built from
`git archive` of that commit. Neither rewrites the old history.

## What moved where

| What | Where it is now |
| --- | --- |
| The kernel: `src/`, `tests/`, `docs/`, `skills/`, `scripts/`, `tools/`, `assets/`, `integrations/`, CI, `install.sh` | This repository (`answerback`), the public kernel |
| The internal provider plugin (the old `packages/` workspace member), its tests, docs and skill reference | A separate internal repository of provider plugins, one distribution per `packages/<name>/` |
| The task ledger (`status/`), its hand-off notes and evidence, the old gate logs, `STATUS.md` and the old `CHANGELOG.md` | The maintainers' private archive (the combined repository), for now; a later task moves the ledger |

This repository has no `status/` directory. Its [CHANGELOG.md](CHANGELOG.md) starts at 0.16.1.
`uv.lock` was regenerated from the combined tree's lock: every third-party version is the same.

## Install a composition

Both compositions report version `0.16.1`, and the kernel alone owns the `answerback` console
script. Keep the clones side by side under `~/code/personal/tools/`.

The public composition, the kernel alone:

```sh
uv tool install --force --editable ~/code/personal/tools/answerback[bus]
answerback --version          # 0.16.1
answerback providers list     # the kernel's own providers
```

The kernel with a provider plugin from the internal repository (its README has the exact
package path):

```sh
uv tool install --force --editable ~/code/personal/tools/answerback[bus] \
  --with-editable ~/code/personal/tools/<plugin-repository>/packages/<provider-plugin>
answerback --version          # 0.16.1
answerback providers list     # includes the plugin's provider on this composition
```

`answerback providers list` asks the running bridge, so restart it after the install. zsh
reads `[bus]` as a file pattern and stops with `no matches found`; there, quote the extra alone: `~/code/personal/tools/answerback'[bus]'`. Add any other extra you use (`claude`)
inside the same brackets: each `uv tool install` replaces the earlier set.

## Move an installed 0.16.0+internal node

1. Read what the node had: `cat ~/.local/share/uv/tools/answerback/uv-receipt.toml` lists the
   extras and every `--with` package.
2. Clone this repository (and, on a node that had the plugin, the internal repository beside it)
   under `~/code/personal/tools/`.
3. `answerback stop`, then reinstall from the clones with the command above that matches the
   node's composition, with the same extras, then `answerback start` and
   `answerback status --json`: `version` is `0.16.1`.

The configuration loads unchanged. 0.16.1 reads the same `config.json` as 0.16.0, and the flat
pre-0.16 provider keys are still read until 0.17
([the `config.json` layout](docs/ANSWERBACK-UPGRADE.md#the-configjson-layout)). A node that
had the plugin and is reinstalled without it keeps the plugin's settings, and `doctor` shows
that provider as `disabled`
([a provider that is not installed](docs/ANSWERBACK-UPGRADE.md#a-provider-that-is-not-installed)).
From 0.16.2 a still-flat file keeps the plugin's flat keys this way only when its
`provider_selection` names the plugin's provider; otherwise the kernel alone refuses the file
as holding unknown keys. Before removing the plugin, run one of the commands that save the
file ([the `config.json` layout](docs/ANSWERBACK-UPGRADE.md#the-configjson-layout) lists them)
so it is written nested.
Receipts, request identities, service labels and MCP server names are unchanged.
