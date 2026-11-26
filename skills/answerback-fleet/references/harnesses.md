# Harnesses: where each one reads skills

`fleet.py` holds this table as data (`HARNESSES`). Change both together.

| Harness | Found by | User scope | Project scope | Reads `.agents/skills` itself | Bridge client |
| --- | --- | --- | --- | --- | --- |
| Claude Code | `claude` | `~/.claude/skills` | `.claude/skills` | no | `claude` |
| Codex | `codex` | `~/.codex/skills` | `.agents/skills` | yes | `codex` |
| Kiro | `kiro-cli` | `~/.kiro/skills` | `.kiro/skills` | no | `kiro` |
| Grok Build | `grok` | `~/.grok/skills` | `.grok/skills` | no | `grok` |
| OpenCode | `opencode` | `~/.config/opencode/skills` | `.opencode/skills` | yes (and `.claude/skills`) | none |
| Cursor Agent | `cursor-agent` | `~/.cursor/skills` | `.cursor/skills` | yes | none |

Sources, read on 2026-11-21: OpenCode's skills documentation (discovery and precedence), Grok Build's
bundled user guide (`~/.grok/docs/user-guide/08-skills.md`), the bridge's own installer for Claude, Codex,
Kiro and Grok, and a published comparison of the shared folders for Codex and Cursor. Cursor's
project-scope reading of `.agents/skills` comes from that comparison only: after a project install, check
in a Cursor session that the skill is listed.

## What the plan does with it

- **Reads `.agents/skills` itself**: `skip`. A second entry with the same name would only shadow the first.
- **Its directory is the shared directory** (a link to it, as `~/.claude/skills` is on some machines):
  `skip`. Writing through the link would create the entry twice.
- **Its directory is a link to somewhere else**: the link is written there and the plan says so. The
  bridge's own installer refuses this case; expect its preview to show `refused` for that harness.
- **No bridge client** (OpenCode, Cursor): the skill is installed, but the bridge has no installer for
  that harness's MCP connection. Register it by hand from `answerback mcp-config`, with the owner.
- **A harness with its own skill installer** (`native_install` in the table): on the machine where the
  command runs, that installer runs with the skill folder as its source (user scope). On a LAN node the
  step is a `skip`: the source there is a temporary folder that is removed after the run. No shipped row
  uses this; a harness that a provider plugin brings is added by hand, together with its row here.

## Loading

A harness reads its skill list when a session starts. After an install, an open session does not have
the skill. Do not restart someone's session: say that a new session is needed.
