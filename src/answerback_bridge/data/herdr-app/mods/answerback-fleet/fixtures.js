// Sample data for the fleet column: the 18:05 sample of 2026-11-20, copied from in/data/ and
// in/wave1/data-fleet-contract.json. The panel draws it only when `checking()` is true or the
// block's prop `sample` is true, and then says `sample` in its header and `sample data` in its
// footer. Names, titles and messages here were
// written by programs on five machines: display text only, never a command.
//
// Not in the samples, so set here (each is marked DERIVED or SET below):
// - the fields DESIGN-v2 5.2 adds to the contract: role, reading.pressure, capacity.new_work,
//   capacity.new_suite, and cause on a receipt;
// - `layout: "ready"` for the machines (in/data/hc-machines.json says "connected");
// - `program` built from the `agent` word of in/data/hc-ls.json;
// - the times of worker-d/t1k, from the brief ("waited 6.8 hours when measured again at 18:44");
// - the stored state (what was seen at 14:10), so that the `since` line has numbers;
// - col-fix4: the lane label `ab.lane: "1"` on the unattended lanes (the tools that start lanes
//   put it there, in/docs/FLEET.md "Agents and lanes"), `cwd` on the tabs that are named only by a
//   number, the tab name of such a tab ("1"), and the document fields in/docs/FLEET.md added
//   (writer.mode, agents.lanes, a coordinator with no ceiling). Machine names are worker-a to
//   worker-d; hosts and folders are made up.
// - col-lead: the PROJECTS section. SET: spaces() (ids made up; the remote ones carry the machine
//   prefix, in/api/herdr.d.ts:96), the label `herdr.space` on some shells (with the id local to
//   their machine, as in/data/hc-get-examples.txt:14 shows), `ab.role: "lead"` on three terminals,
//   `ab.project` on two lanes and a `parent` on one. worker-c/t17 sits in a space of worker-c
//   that is also called herdr_bridge (two spaces of one name are one project).

/** Seconds: `written_at` of in/wave1/data-fleet-contract.json, 18:05:00 UTC on 2026-11-20. */
export const WRITTEN_AT = 1795197900;
/** The sample is drawn 40 s after the document was written. */
export const SAMPLE_NOW_MS = (WRITTEN_AT + 40) * 1000;

const SHELLS = ["zsh", "sh"];
/** SET: the label the lane starters put on an unattended lane (in/docs/FLEET.md). */
const LANE = { labels: { "ab.lane": "1" } };
/** SET (col-lead): the label the owner puts on the terminal that leads a project. */
const LEAD = { "ab.role": "lead" };

/** One terminals() row from one line of in/data/hc-ls.json (and hc-get-examples.txt where it has more). */
function row(address, agent, status, name, where, more = {}) {
  return {
    address, state: "live", name, status,
    report: status ? { app: agent, state: status } : null,
    labels: where ? { where } : {},
    title: "", parent: null, created_ms: null, status_ms: null, reported: status !== null,
    program: { key: agent, name: agent, icon: null, tint: false, tile: null, color: null, shell: SHELLS.includes(agent), agent: status !== null },
    ...more,
  };
}

// in/data/hc-ls.json: all 38 live terminals.
const terminals = [
  row("t53", "claude-code", "working", null, "anycustomer_poc / 1 · Please can you use"),
  row("t59", "kiro-cli", "done", null, "herdr_bridge / 1 · chat -a --v3 --agent=default --model=claude-fa"),
  row("t6n", "claude-code", "working", null, "herdr_bridge / 3 · readme-wow › please can you review the project"),
  row("t6q", "claude-code", "working", null, "anycustomer_poc / 3 · Analytics_AB › please can you use to firstly e"),
  row("t7j", "zsh", null, null, "anycustomer_poc / 4 · Analytics_AB", { labels: { where: "anycustomer_poc / 4 · Analytics_AB", "herdr.space": "sp-any" } }),
  row("t7k", "zsh", null, null, "anycustomer_poc / 5 · anycustomer_poc", { labels: { where: "anycustomer_poc / 5 · anycustomer_poc", "herdr.space": "sp-any" } }),
  row("t7n", "zsh", null, null, "harness_collab / 1 · please can you review this repo's code and doc"),
  row("t83", "zsh", null, null, "anycustomer_poc / 3 · anycustomer_poc", { labels: { where: "anycustomer_poc / 3 · anycustomer_poc", "herdr.space": "sp-any" } }),
  row("t85", "zsh", null, null, "herdr_bridge / 1 · herdr_bridge", { labels: { where: "herdr_bridge / 1 · herdr_bridge", "herdr.space": "sp-hb" } }),
  row("t86", "claude-code", "done", null, "herdr_bridge / 2 · Restarted after another machine lockup/freeze"),
  row("t89", "zsh", null, null, "herdr_bridge / 2 · herdr_bridge", { labels: { where: "herdr_bridge / 2 · herdr_bridge", "herdr.space": "sp-hb" } }),
  row("t8b", "claude-code", "working", null, "herdr_bridge / 6 · please can you review the docs?"),
  row("t8c", "claude-code", "working", null, "herdr_bridge / 4 · please can you use with lan nodes, including K", {
    // in/data/hc-get-examples.txt, `hc get t8c --json`
    title: "Claude Code", created_ms: 1795196905468, status_ms: 1795197134689,
    report: { app: "claude-code", msg: "Capturing live data samples and documents into the study pack", state: "working" },
  }),
  row("t8d", "claude-code", "working", null, "herdr_bridge / 5 · please delegate using to lan nodes investigati"),
  // SET (col-lead): in a space of worker-c called herdr_bridge, and marked as the project's lead.
  row("worker-c/t17", "kiro-cli", "done", null, "herdr_bridge / 1", {
    cwd: "/home/owner/code/herdr_bridge", labels: { where: "herdr_bridge / 1", "herdr.space": "sp-c1", ...LEAD },
  }),
  row("worker-c/t21", "claude-code", "blocked", "trust-setup-claude-worker-c", null, {
    // in/data/hc-get-examples.txt, `hc get worker-c/t21 --json`
    created_ms: 1795175521680, status_ms: 1795175523032,
    report: { app: "claude-code", kind: "permission", msg: "trust this folder", state: "blocked" },
    // SET (col-lead): the lane names its project with the label ab.project.
    labels: { "ab.project": "herdr_bridge" },
  }),
  row("worker-c/t22", "zsh", null, null, "home / 2"),
  row("worker-a/t3f", "codex", "idle", "design-impl-codex-261120-mb47", null),
  // SET (col-lead): started by t8c, so it takes its project from its parent.
  row("worker-a/t48", "sh", null, "ak-t56-m3", null, { ...LANE, parent: "t8c" }),
  row("worker-a/t49", "sh", null, "ak-t53-fix3", null, LANE),
  row("worker-a/t4b", "sh", null, "ak-p6-xcodex2", null, LANE),
  row("worker-d/t7", "kiro-cli", "done", null, "~ / 1", { cwd: "/home/owner", labels: { where: "~ / 1", ...LEAD } }),
  row("worker-d/t8", "kiro-cli", "done", null, "~ / 2", { cwd: "/home/owner" }),
  row("worker-d/t9", "claude-code", "done", null, "~ / 4", {
    // in/data/hc-get-examples.txt, `hc get worker-d/t9 --json`
    title: "Claude Code", created_ms: 1795016604922, status_ms: 1795094299587, cwd: "/home/owner",
    // SET (col-lead): a second lead in the same project; the column does not choose.
    labels: { where: "~ / 4", ...LEAD },
  }),
  // SET (col-lead): Herdr's space label holds the id local to worker-d (no machine prefix).
  row("worker-d/t10", "zsh", null, null, "~ / 3", { labels: { where: "~ / 3", "herdr.space": "sp-d1" } }),
  row("worker-d/t11", "zsh", null, null, "~ / 5"),
  // Blocked in in/data/hc-ls.json; no `hc get` sample, so its report has no kind and no message.
  // DERIVED times: the brief says it had waited 6.8 hours at 18:44 (1795200240 - 24480 s).
  row("worker-d/t1k", "claude-code", "blocked", "trust-setup-claude-worker-d", null, {
    created_ms: 1795175760000, status_ms: 1795175760000,
  }),
  row("worker-d/t1w", "herdr-nvim", null, null, "user / 3"),
  row("worker-d/t1x", "herdr-nvim", null, "herdr-nvim-watch", null),
  row("worker-d/t26", "sh", null, "acw-w4-eod", null, LANE),
  row("worker-d/t27", "sh", null, "acw-w4-surge", null, LANE),
  row("worker-d/t28", "sh", null, "ak-fab-f2", null, LANE),
  row("worker-b/t15", "zsh", null, null, "tools / 1"),
  row("worker-b/t1f", "harness_launch.sh", null, "trust-setup-claude-worker-b", null, {
    // in/data/hc-get-examples.txt, `hc get worker-b/t1f --json`: reported false, no status.
    // SET: the lane label, as the lane starters put it (in/docs/FLEET.md).
    created_ms: 1795175521774, reported: false, labels: { "ab.lane": "1" },
  }),
  // SET (col-lead): a lane outside every tab that names its project with the label ab.project.
  row("worker-b/t29", "sh", null, "acw-w4-track", null, { labels: { "ab.lane": "1", "ab.project": "herdr_bridge" } }),
  row("worker-b/t2b", "sh", null, "acw-w4-prodctl", null, LANE),
  row("worker-b/t2d", "sh", null, "ak-p6-linux2", null, LANE),
  row("worker-b/t2f", "sh", null, "fb-ans-a", null, LANE),
];

/**
 * SET (col-lead): spaces() in the shape of SpaceInfo (in/api/herdr.d.ts:123-145). The ids are made
 * up; a remote id has its machine's prefix (in/api/herdr.d.ts:96), as in/data/herdr-app-ls.json shows.
 */
const space = (id, index, name, machine) => ({ id, index, name, named: true, folder: "", machine, branch: null, active: index === 1, status: null, tabs: [] });
const spaces = [
  space("sp-any", 1, "anycustomer_poc", null),
  space("sp-hb", 2, "herdr_bridge", null),
  space("worker-c/sp-c1", 3, "herdr_bridge", "worker-c"),
  space("worker-d/sp-d1", 4, "~", "worker-d"),
];

// in/data/hc-machines.json, in its order. SET: layout "ready" (the sample says "connected").
const machines = [
  { name: "worker-c", layout: "ready" },
  { name: "worker-a", layout: "ready" },
  { name: "worker-d", layout: "ready" },
  { name: "worker-b", layout: "ready" },
];

// in/data/herdr-app-ls.json: terminal and pane of the 22 terminals that sit in a tab.
const PANES = [
  ["t53", "pb9de9214a13a2b7afdbb2c14f903563a"], ["t6q", "pd7f0bbd7d0820d8793dd4969b176f7bf"],
  ["t83", "p2b03bc9df6a7f360e091f0882144da52"], ["t7j", "p3e346859d4e7211f6606bfb1a2303803"],
  ["t7k", "pc459349a64fa615f474f06206dfa12b5"], ["t59", "pf8bc4e2290e929bfb701390d5a1e45a2"],
  ["t86", "p3fd1e3fa29ad294cadd1022314376fe8"], ["t6n", "pd3cbc06187eccff5dc5932e062c67d45"],
  ["t8c", "p00be35d0b8b8c076d3afb3a4450d649e"], ["t8d", "p7b9fd0a61cf0882007b999cb0e31f646"],
  ["t8b", "pde83850bb1d753bddda8785ee05a88ea"], ["t7n", "p9e0cab61e8f00d6eab6efa8be05e425d"],
  ["t85", "p408f779893695392876c919f506f3ec5"], ["t89", "p5bcb080210ef9f4c8e78e4d6dd61ebef"],
  ["worker-c/t17", "worker-c/pfbacc76e343e58a9204b9e3b3908781c"], ["worker-c/t22", "worker-c/pe26f4db6e4d919d8254506dade4f1d5f"],
  ["worker-d/t7", "worker-d/p124b25e087bb13475548dac2f2875aec"], ["worker-d/t8", "worker-d/p465933088229daad78dee5795700e320"],
  ["worker-d/t10", "worker-d/p0d3b552f54a3a57b6359a105e4d29b55"], ["worker-d/t9", "worker-d/pa841663c7ab857e0119945cb868f8bce"],
  ["worker-d/t11", "worker-d/p658588a3cd775908ac75b4d604f49934"], ["worker-b/t15", "worker-b/pdae88e74f6299a6d7f6804844b260279"],
];
const byAddress = new Map(terminals.map((t) => [t.address, t]));
// A tab with no name of its own is called by its number (in/data/herdr-app-ls.json: "1", "2").
const tabName = (t) => {
  const where = t.labels.where ?? "";
  const i = where.indexOf(" · ");
  if (i >= 0) return where.slice(i + 3);
  const j = where.lastIndexOf(" / ");
  return j < 0 ? where : where.slice(j + 3);
};
// SET: tab ids are not in the sample; the pane id stands in.
const panes = PANES.map(([terminal, pane]) => {
  const t = byAddress.get(terminal);
  return { pane, terminal, tab: `tab-${pane.slice(-6)}`, title: t.title, cwd: null, branch: null, status: t.status, app: t.report?.app ?? null, program: t.program, focused: false, zoomed: false, movable: true };
});
const agents = panes.filter((p) => p.status !== null).map((p, i) => {
  const t = byAddress.get(p.terminal);
  const space = (t.labels.where ?? "").split(" / ")[0];
  return { address: p.terminal, pane: p.pane, space, space_name: space, tab: p.tab, tab_index: i, tab_name: tabName(t), status: p.status, app: p.app, program: p.program, title: p.title, seq: i, pinned: false, focused: false };
});

/** One machine of the document, in the shape of in/wave1/data-fleet-contract.json plus DESIGN-v2 5.2. */
function machine(name, role, host, os, state, loads, cores, memory, largest, counts, suites, processes, extra = {}) {
  return {
    name, self: name === null, role, host, os,
    link: { state: name === null ? "self" : "connected", since_ms: null, why: null, ...(name === null ? {} : { hc_version: "0.1.21" }) },
    reading: {
      taken_at: WRITTEN_AT, error: null, state, decided_by: [], pressure: null,
      load_per_core: Math.round((Math.max(loads[0], loads[1]) / cores) * 100) / 100, loads, cores,
      memory, largest_process: largest, agents: counts, suites, processes: processes[0], harness_processes: processes[1],
      ...extra,
    },
  };
}

// in/wave1/data-fleet-contract.json `machines[]` (values as in in/data/capacity-table.txt).
const documentMachines = [
  machine(null, "coordinator", null, "darwin", "full", [16.98, 18.56, 17.26], 16,
    { state: "ok", worst: "compressor", text: "pressure 1, compressor 19%, largest process 8%" },
    { name: "an endpoint security agent", share_pct: 8 }, { active: 6, parked: 2, ceiling: null, lanes: 0 }, 0, [1612, 18], {
      // The coordinator's word comes from load and memory only (DESIGN-v2 3.3): the contract's
      // second entry ("6 active, ceiling 0") is gone. SET: pressure and the wording of the text.
      decided_by: [{ signal: "cpu", state: "full", text: "load 1.16 per core (full at 1.00)" }],
      pressure: { signal: "cpu", fraction: 1.16, text: "1.16" },
    }),
  machine("worker-a", "worker", "host-a", "darwin", "ok", [2.75, 2.72, 2.74], 16,
    { state: "ok", worst: "largest_process", text: "pressure 1, compressor 0%, largest process 7%" },
    { name: "python", share_pct: 7 }, { active: 3, parked: 1, ceiling: 10, lanes: 3 }, 1, [1084, 4]),
  machine("worker-c", "worker", "host-c", "darwin", "busy", [2.8, 2.8, 2.6], 14,
    { state: "busy", worst: "compressor", text: "pressure 1, compressor 32%, largest process 13%" },
    { name: "python", share_pct: 13 }, { active: 0, parked: 2, ceiling: 5, lanes: 0 }, 0, [876, 5], {
      // SET: fraction 0.84 = 0.70 + 0.30 x (32 - 25) / (40 - 25), the bridge's scale for a
      // compressor between its busy and full limits (in/docs/CAPACITY-T96.md:23-30). The signal
      // name is `memory`, one of the three the bridge publishes (cpu, memory, agents: DESIGN-v2 3.3).
      decided_by: [{ signal: "memory", state: "busy", text: "memory tight: 32% compressed (limit 25%)" }],
      pressure: { signal: "memory", fraction: 0.84, text: "32%" },
    }),
  machine("worker-d", "worker", "worker-d", "linux", "ok", [5.4, 2.9, 2.3], 16,
    { state: "ok", worst: "available", text: "available 64% of 61G, largest process 5%" },
    { name: "frigate.embeddi", share_pct: 5 }, { active: 3, parked: 4, ceiling: 6, lanes: 3 }, 1, [766, 9]),
  machine("worker-b", "worker", "worker-b", "linux", "ok", [1.64, 0.92, 0.62], 16,
    { state: "ok", worst: "available", text: "available 78% of 61G, largest process 4%" },
    { name: "python", share_pct: 4 }, { active: 5, parked: 0, ceiling: 6, lanes: 5 }, 1, [658, 5]),
];

/** One receipt of the document. `cause` and `request_key` are the fields DESIGN-v2 5.2 adds. */
function receipt(id, state, provider, text, terminal, title, created, updated, more = {}) {
  return { id, state, recipient: { provider, text, machine: null, terminal }, title, requested_by: null, reason: null, cause: null, created, updated, ...more };
}

const KIRO_CLIENT = "kiro:ui-0e15d650864d44ca7426e547007d0aee";
// SET: cause peer_not_running, because the kiro client has no live instance (in/data/answerback-status.json).
// SET (DESIGN-v2 3.4, B1): the bridge titles a desktop thread `Kiro task` and names its recipient
// `Kiro`; the raw client id is kept as the request key, which the label never shows (keyLabel).
const kiroFailed = { cause: "peer_not_running" };
const kiroReceipt = (id, created, updated) => receipt(id, "failed", "kiro", "Kiro", null, "Kiro task", created, updated, { ...kiroFailed, request_key: KIRO_CLIENT });

// in/wave1/data-fleet-contract.json `receipts` (rows as in in/data/answerback-tasks.json).
const receipts = {
  source: "status+task_list",
  read_at: WRITTEN_AT,
  stale: false,
  error: null,
  needs_person_complete: true,
  needs_person_omitted: 0,
  counts: { pending: 0, queued: 0, submitted: 1, running: 0, blocked: 0, uncertain: 1, completed: 90, failed: 6, abandoned: 19, cancelled: 5, total: 122 },
  needs_person: [
    receipt("050ecd8d-7632-4b5b-9e07-46aff406d44a", "uncertain", "kiro", "kiro:herdr-bridge:/Users/owner/code/personal/tools/herdr_bridge", null,
      "harness-pref-kiro-1", 1795177254.140129, 1795189219.8014379, { reason: "bridge_stopped_during_submission", request_key: "ab-20261120-harness-pref-kiro-1" }), // gitleaks:allow (a request key of the sample, not a credential)
  ],
  recent: [
    kiroReceipt("4e8cd0dd-907f-4146-a91c-3c4deecda622", 1795193757.7189388, 1795193758.276504),
    kiroReceipt("3960df95-6246-42de-b698-b335a78ec661", 1795193534.198965, 1795193534.698406),
    kiroReceipt("8102552a-3f67-494a-a41a-3a9f7860bdb4", 1795193384.472335, 1795193387.137793),
    receipt("2e3e1839-8c12-4717-b455-a3b6f246548d", "submitted", "crew", "crew:http://localhost:5476:chat-129-1794703521", null,
      "venv-per-worktree-1", 1795188131.4108968, 1795188132.2682528, { request_key: "ab-20261120-venv-per-worktree-1" }),
    receipt("79eeea81-a726-4bef-9cb8-c5743d6e2c43", "completed", "hc", "hc:default:t84", "t84", "T-51 review", 1795184360.830115, 1795185251.498494),
    receipt("2efa2de2-45aa-447e-90ed-5a21e8ba3148", "completed", "hc", "hc:default:t82", "t82", "T-56 xrev1b", 1795179273.279806, 1795179789.5070562),
    // SET: cause recipient_not_ready, after the task's detail in in/data/answerback-tasks.json
    // ("Core terminal needs a recognized foreground command, known directory, and reported idle").
    receipt("20c01de5-7e89-44fc-9055-7911a51830c4", "failed", "hc", "hc:default:t82", "t82", "T-56 xrev1", 1795179233.614719, 1795179233.685293, { cause: "recipient_not_ready" }),
    receipt("01f1dc00-062e-4664-9158-dca7791c8d38", "completed", "hc", "hc:default:t81", "t81", "DOCS readme-wow xrev1", 1795178777.7913191, 1795179721.672352),
  ],
};

const document = {
  schema: "answerback.fleet/1",
  written_at: WRITTEN_AT,
  writer: { version: "0.12.0+internal", instance_id: "07bca4ea-2094-4154-8e4a-ad45021d279b", pid: 4242, mode: "normal", interval_s: 15 },
  // in/data/answerback-status.json
  health: {
    // SET (T-181): `version`, the bridge's own, as the service answers it (docs/FLEET-SNAPSHOT.md);
    // the same as the writer's here, so the sample footer names one version.
    service: { state: "running", accepting: true, started: 1795189219.7738109, version: "0.12.0+internal" },
    bus: { connected: true, last_error: null, rejected_messages: 0, outbox_errors: 0, actors_enabled: false },
    clients: [
      { client: "claude", available: true, live_instances: 9, last_heartbeat_age_s: 2.152 },
      { client: "codex", available: true, live_instances: 1, last_heartbeat_age_s: 4.956 },
      { client: "kiro-crew", available: true, live_instances: 2, last_heartbeat_age_s: 8.525 },
      { client: "kiro", available: false, live_instances: 0, last_heartbeat_age_s: null },
      { client: "grok", available: false, live_instances: 0, last_heartbeat_age_s: null },
    ],
    heartbeat_interval_s: 10,
  },
  capacity: {
    taken_at: WRITTEN_AT, max_age_s: 60,
    ranked_for_heavy_work: ["worker-a", "worker-b", "worker-d"],
    node_order: ["worker-a", "worker-c", "worker-b", "worker-d"],
    // SET (DESIGN-v2 5.2): the first ok worker of the ranking; every ok worker already runs a suite.
    new_work: { machine: "worker-a", why: "first ok worker of the ranking" },
    new_suite: { machine: null, why: "every ok machine runs one" },
  },
  machines: documentMachines,
  receipts,
  errors: [],
};

/**
 * SET: the stored state of the sample. The owner clicked "seen" at 14:10 (1795183800), when the
 * totals were 78 completed and 3 failed and the failure of 12:13 was listed. No sample holds
 * totals for an earlier time; these numbers exist so that the `since` line has something to count.
 */
const state = {
  hidden: {},
  seen: ["20c01de5-7e89-44fc-9055-7911a51830c4"],
  base: { at: 1795183800, completed: 78, failed: 3 },
  folded: {},
};

/**
 * F15, SET: the same document as the writer publishes it in mode `unmeasured` (in/docs/FLEET.md
 * "Modes and intervals"): the coordinator's probe timed out and no reading of it is current. Its
 * last good reading is 90 s old, so it is `unknown` with `was: "full"`, the error word says why,
 * and `capacity.paused` is null. The workers are measured as before.
 */
function unmeasured() {
  const d = JSON.parse(JSON.stringify(document));
  d.writer.mode = "unmeasured";
  d.writer.interval_s = 45;
  d.capacity.paused = null;
  Object.assign(d.machines[0].reading, {
    taken_at: WRITTEN_AT - 90, error: "probe timed out", state: "unknown", was: "full", decided_by: [], pressure: null,
  });
  d.errors = [{ at: WRITTEN_AT, where: "probe", text: "the coordinator: probe timed out" }];
  return d;
}

export const FIXTURES = Object.freeze({
  nowMs: SAMPLE_NOW_MS,
  /** What terminals(), machines(), agents(), panes() and spaces() would return. */
  live: { terminals, machines, agents, panes, spaces },
  /** The text of the bridge's document, as readTextFile would return it. */
  documentText: JSON.stringify(document),
  /** F15: the document of a writer in mode `unmeasured`. */
  unmeasuredText: JSON.stringify(unmeasured()),
  state,
});
