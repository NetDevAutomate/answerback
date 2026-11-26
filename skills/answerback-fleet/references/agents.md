# A dedicated agent per harness: findings

Researched on 2026-11-21 and reviewed by a five-model council twice. Records: `docs/council/` in the
`answerback_skill` repository, where this skill was first written. This skill installs no agent. Do not create one on your own initiative; the owner decides.

## Short answer

| Question | Answer |
| --- | --- |
| A dedicated answerback agent (subagent or persona) installed in each harness? | No. |
| That agent as each harness's default? | No. All five reviewers rejected it. |
| Anything worth building? | One thing: a narrow **worker** agent for sessions the bridge itself starts. It needs a bridge change first. |

## Why not a general agent

- **A skill already does the job.** It costs one description line until it is loaded. An agent holding the
  same text adds a second copy to keep in step, in five formats.
- **It grants nothing.** A child agent cannot widen what the human authorized. It is either as limited as
  its parent or pre-approved for more, and the second is a permissions decision, not a convenience.
- **Receipts must outlive it.** Task and command ids have to stay with the session that collects the
  result. A subagent returns one final message; ids kept only in its context are lost.
- **Large reads are better filtered than delegated.** One inbox read here returned 135,656 characters.
  A subagent keeps that out of the parent's context, but so does a filter on the command line, without a
  second model reading it all.

## Why not a default agent

| Harness | What "default agent" is | What it would change in every session |
| --- | --- | --- |
| Claude Code | `--agent`, or an `agent` key in settings | the main thread's prompt, tool list and model |
| Kiro | `kiro-cli agent set-default` (`chat.defaultAgent`) | prompt, tools, MCP servers, and one pre-approved tool list for every chat |
| Grok Build | `[agent] name` in config, or `GROK_AGENT` | the selected agent definition and its permissions |
| OpenCode | the primary agent in config | per-agent permissions |
| Codex | none; custom agents are subagents only | not possible |
| Cursor Agent | none | not possible |

None is set on the owner's machines today. A default agent takes the one slot per harness, puts
coordination text into sessions that only write code, and cannot be uniform across six harnesses. Its one
real advantage, that the model need not decide to load the skill, matters for a worker that must close its
receipt and not for an ordinary session.

On Kiro there is a trap: the bridge starts its Kiro workers without naming an agent, so a Kiro default
agent would also become every bridge-started worker's agent.

## What is worth building: a worker agent

For sessions the bridge starts as managed workers only. Ordinary sessions are untouched.

- **Purpose**: the worker always reports through the callback, and does not stop for an approval on the
  steps every assignment needs.
- **Blocked on**: the bridge passes no agent when it starts a worker. Until it can, a worker agent has no
  effect. That change belongs to the bridge's repo and its owner.
- **Harnesses**: Claude Code, Kiro, Grok Build; a named profile for Codex. Not OpenCode or Cursor: the
  bridge starts no workers there.
- **May do without asking** (a proposal; the owner writes the list): the completion callback, reading the
  assigned workspace, edits and agreed checks inside the task's tree.
- **Never pre-approved**: credentials or secrets, pushes or history rewrites, writes outside the task's
  tree, installs, network beyond the bridge, changes to harness settings, approval policy, default agents or
  the bridge.
- **Test first**: that the callback itself runs without an approval prompt in that harness.
- **Measure**: uncertain receipts and approval stalls per assignment, before and after. On 2026-11-21 the
  bus held 4 uncertain commands beside 14 bound and 2 cancelled.

## If a pointer is wanted

Each harness has an always-loaded instruction file (`CLAUDE.md`, `AGENTS.md`, rules). Two lines there do
what a default agent would, at almost no cost: load the usage skill before work crosses a session, agent or
machine; report an assigned task through its callback. Those files are the owner's: propose the lines, do
not write them.
