# Older Herdr terminal UI

This path applies to the `herdr` executable, TUI sessions and `w…:p…` pane IDs.
It does not describe Herdr App / `hc` core terminal addresses. Configure this
optional provider with `configure --runtime tui --session NAME` using an explicit
bridge config directory. `configure --session NAME` retains the same legacy TUI
meaning. The regular App/core setup uses `configure --runtime hc` and needs no TUI.

For direct in-pane control, verify `HERDR_ENV=1`, then read the installed standard
Herdr skill (`herdr --skill`) and relevant CLI help. Its context-sensitive
operations are intended for a Herdr-managed pane. From Codex Desktop, use a
verified explicitly configured bridge route for TUI agents; do not guess the
human's focused pane. User-approved external tooling must also select an explicit
session/target instead of borrowing focus.

Use returned IDs/live names for the selected session and machine. Keep the same
selector for every discovery and mutation. Human `herdr` / `herdr --session NAME`
startup creates or attaches a TUI; inside Herdr App it can nest the TUI. Only use
that startup path when the user specifically wants the older runtime.

Within a verified TUI context, the typical sequence is:

```sh
uv run --no-project herdr agent list
uv run --no-project herdr agent get <observed-agent-name>
uv run --no-project herdr agent prompt <observed-agent-name> "<scoped assignment>" --wait --timeout 45000
uv run --no-project herdr agent read <observed-agent-name> --source recent-unwrapped --lines 100
```

TUI prompt timeout units are **milliseconds**; `hc wait --timeout` uses seconds.
A settled agent state is not a correlated final answer. Inspect returned output
and task evidence; for bridge work, follow the exact generated completion receipt.

If creating an authorized helper, the standard skill uses an available shell pane
created with the current project path and `--no-focus`; `agent start` does not
create that pane. Read the returned ID rather than predicting it. Apply the
requested agent's normal approvals. Leave blocked dialogs to the user's explicit
decision. Clean up only task-owned resources and preserve the selected TUI server.

For remote TUI work, `--machine <observed-label-or-id>` consistently scopes its
API commands. This is a different namespace from `hc`'s `machine/t7` addresses.

Sources: supplied standard `herdr/SKILL.md` and `agent-guide.md`, installed
`herdr --help`, https://herdr.dev/tui/docs/.
