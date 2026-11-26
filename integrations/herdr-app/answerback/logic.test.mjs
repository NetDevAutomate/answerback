import assert from "node:assert/strict";
import { test } from "node:test";
import {
  STATES,
  badgeText,
  boundText,
  countStates,
  formatAge,
  needsPerson,
  parseStatus,
  parseTasks,
  resolveOptions,
  statusArgs,
  tasksArgs,
} from "./logic.js";

const HOME = "/Users/someone";

test("boundText keeps short plain text unchanged", () => {
  assert.equal(boundText("codex-reviewer"), "codex-reviewer");
});

test("boundText cuts long text to the limit and marks the cut", () => {
  const out = boundText("a".repeat(200), 10);
  assert.equal(out, "aaaaaaaaa…");
  assert.equal(Array.from(out).length, 10);
});

test("boundText never splits a character outside the basic plane", () => {
  const out = boundText("😀".repeat(20), 5);
  assert.equal(out, "😀😀😀😀…");
});

test("boundText turns control characters and line breaks into single spaces", () => {
  assert.equal(boundText("one\ntwo\r\n\tthree\u0000four\u001b[31mred"), "one two three four [31mred");
});

test("boundText removes direction overrides and invisible characters", () => {
  assert.equal(boundText("ab\u202ecd\u2066ef\u200bgh\ufeffij\u2028kl"), "abcdefghij kl");
});

test("boundText gives an empty string for anything that is not a string", () => {
  for (const value of [null, undefined, 7, true, {}, [], () => "x"]) {
    assert.equal(boundText(value), "");
  }
});

test("formatAge uses the largest whole unit", () => {
  assert.equal(formatAge(0), "now");
  assert.equal(formatAge(4.9), "now");
  assert.equal(formatAge(5), "5s");
  assert.equal(formatAge(59.9), "59s");
  assert.equal(formatAge(60), "1m");
  assert.equal(formatAge(3599), "59m");
  assert.equal(formatAge(3600), "1h");
  assert.equal(formatAge(86399), "23h");
  assert.equal(formatAge(86400), "1d");
  assert.equal(formatAge(86400 * 400), "400d");
});

test("formatAge treats a time slightly in the future as now and nonsense as unknown", () => {
  assert.equal(formatAge(-30), "now");
  for (const value of [NaN, Infinity, -Infinity, null, undefined, "12", {}]) {
    assert.equal(formatAge(value), "?");
  }
});

test("countStates reads the four shown states, the permission wait and the rest", () => {
  const counts = countStates({
    pending: 2, submitted: 3, uncertain: 1, completed: 40, blocked: 4,
    queued: 5, running: 6, failed: 7, abandoned: 8, cancelled: 9,
  });
  assert.deepEqual(counts, {
    pending: 2, submitted: 3, uncertain: 1, completed: 40, blocked: 4, other: 35, total: 85,
  });
});

test("countStates counts a missing state as zero", () => {
  assert.deepEqual(countStates({}), {
    pending: 0, submitted: 0, uncertain: 0, completed: 0, blocked: 0, other: 0, total: 0,
  });
});

test("countStates ignores values that are not whole non-negative numbers and unknown states", () => {
  const counts = countStates({
    pending: "3", submitted: -1, uncertain: 1.5, completed: NaN, blocked: 2,
    surprise: 99, __proto__: { pending: 50 },
  });
  assert.deepEqual(counts, {
    pending: 0, submitted: 0, uncertain: 0, completed: 0, blocked: 2, other: 0, total: 2,
  });
});

test("countStates returns null when the CLI gave no usable table", () => {
  for (const value of [null, undefined, "x", 3, [1, 2]]) {
    assert.equal(countStates(value), null);
  }
});

test("needsPerson adds uncertain receipts and receipts waiting for a permission", () => {
  assert.equal(needsPerson(countStates({ uncertain: 2, blocked: 3, pending: 9 })), 5);
  assert.equal(needsPerson(countStates({})), 0);
  assert.equal(needsPerson(null), 0);
});

test("badgeText is null at zero, the number otherwise, capped at 99+", () => {
  assert.equal(badgeText(0), null);
  assert.equal(badgeText(-1), null);
  assert.equal(badgeText(NaN), null);
  assert.equal(badgeText(1), "1");
  assert.equal(badgeText(99), "99");
  assert.equal(badgeText(100), "99+");
});

test("parseStatus reads a running service and its receipt counts", () => {
  const status = parseStatus(JSON.stringify({
    version: "1.2.3", running: true, accepting: true, pid: 4242,
    tasks: { pending: 1, completed: 2, uncertain: 1 },
  }));
  assert.equal(status.ok, true);
  assert.equal(status.service, "running");
  assert.equal(status.version, "1.2.3");
  assert.equal(status.counts.pending, 1);
  assert.equal(status.counts.completed, 2);
  assert.equal(status.counts.uncertain, 1);
});

test("parseStatus names the other service states with fixed words", () => {
  const service = (value) => parseStatus(JSON.stringify(value)).service;
  assert.equal(service({ running: false, accepting: false, state: "stopped" }), "stopped");
  assert.equal(service({ running: true, accepting: false, state: "unresponsive_or_starting" }), "not responding");
  assert.equal(service({ running: true, accepting: false }), "stopping");
  assert.equal(service({}), "stopped");
  assert.equal(service({ running: "yes", accepting: 1 }), "stopped");
});

test("parseStatus has no counts when the service did not report them", () => {
  const status = parseStatus(JSON.stringify({ running: false, accepting: false, state: "stopped" }));
  assert.equal(status.ok, true);
  assert.equal(status.counts, null);
});

test("parseStatus bounds the version text and never copies other fields", () => {
  const status = parseStatus(JSON.stringify({
    running: true, accepting: true, version: "9".repeat(500), socket: "/private/socket", providers: { a: "b" },
  }));
  assert.equal(Array.from(status.version).length, 24);
  assert.deepEqual(Object.keys(status).sort(), ["counts", "ok", "service", "version"]);
});

test("parseStatus refuses text that is not a JSON object", () => {
  for (const text of ["", "not json", "[1,2]", "null", "42", '"text"', '{"running": tru']) {
    assert.deepEqual(parseStatus(text), { ok: false });
  }
  assert.deepEqual(parseStatus(undefined), { ok: false });
});

test("parseStatus refuses output larger than the size limit", () => {
  const huge = JSON.stringify({ running: true, accepting: true, pad: "x".repeat(1_100_000) });
  assert.deepEqual(parseStatus(huge), { ok: false });
});

test("parseTasks keeps only peer, state and creation time", () => {
  const out = parseTasks(JSON.stringify({
    tasks: [{
      id: "t-1", request_key: "rk-123", target: "codex:reviewer", status: "uncertain",
      created: 1700000000.5, updated: 1700000100, detail: "PROMPT TEXT", message: "PROMPT TEXT",
      result: "RESULT TEXT", has_result: true,
    }],
    searched_latest: 100,
  }), 8);
  assert.deepEqual(out, {
    ok: true,
    receipts: [{ peer: "codex:reviewer", state: "uncertain", created: 1700000000.5 }],
  });
  assert.equal(JSON.stringify(out).includes("TEXT"), false);
  assert.equal(JSON.stringify(out).includes("rk-123"), false);
});

test("parseTasks keeps the CLI order and stops at the limit", () => {
  const tasks = Array.from({ length: 12 }, (_, i) => ({ target: `p${i}`, status: "completed", created: 100 - i }));
  const out = parseTasks(JSON.stringify({ tasks }), 3);
  assert.deepEqual(out.receipts.map((r) => r.peer), ["p0", "p1", "p2"]);
});

test("parseTasks replaces an unknown state with a fixed word and bounds the peer", () => {
  const out = parseTasks(JSON.stringify({
    tasks: [
      { target: "x".repeat(300), status: "<script>alert(1)</script>", created: "yesterday" },
      { target: "", status: "pending", created: 5 },
      { target: 12, status: null },
    ],
  }), 8);
  assert.equal(Array.from(out.receipts[0].peer).length, 40);
  assert.equal(out.receipts[0].state, "unknown");
  assert.equal(out.receipts[0].created, null);
  assert.deepEqual(out.receipts[1], { peer: "(no name)", state: "pending", created: 5 });
  assert.deepEqual(out.receipts[2], { peer: "(no name)", state: "unknown", created: null });
});

test("parseTasks skips entries that are not objects", () => {
  const out = parseTasks(JSON.stringify({ tasks: [null, "x", 4, [], { target: "ok", status: "running", created: 1 }] }), 8);
  assert.deepEqual(out.receipts, [{ peer: "ok", state: "running", created: 1 }]);
});

test("parseTasks refuses output without a receipt list", () => {
  for (const text of ["", "nope", "[]", "{}", '{"tasks": "many"}', '{"tasks": {"0": {}}}']) {
    assert.deepEqual(parseTasks(text, 8), { ok: false });
  }
});

test("every state the bridge can report is known", () => {
  assert.deepEqual([...STATES].sort(), [
    "abandoned", "blocked", "cancelled", "completed", "failed",
    "pending", "queued", "running", "submitted", "uncertain",
  ]);
});

test("resolveOptions uses safe defaults when no props are given", () => {
  for (const props of [undefined, null, {}]) {
    assert.deepEqual(resolveOptions(props, HOME), {
      ok: true,
      command: "/Users/someone/.local/bin/answerback",
      configArgs: [],
      intervalMs: 15000,
      limit: 8,
      badge: true,
    });
  }
});

test("resolveOptions accepts absolute paths and paths under the home folder", () => {
  const options = resolveOptions({ command: "/opt/tools/answerback", config: "~/cfg/bridge.json" }, HOME);
  assert.equal(options.ok, true);
  assert.equal(options.command, "/opt/tools/answerback");
  assert.deepEqual(options.configArgs, ["--config", "/Users/someone/cfg/bridge.json"]);
  assert.equal(resolveOptions({ command: "~/bin/answerback" }, HOME).command, "/Users/someone/bin/answerback");
});

test("resolveOptions refuses a command that is not an absolute path", () => {
  for (const command of ["answerback", "./answerback", "bin/answerback", "", "   ", "-rf", 12, {}, ["/bin/sh"], "~other/bin/x", "/bin/a\nb", "/bin/a\u0000b", "/" + "a".repeat(2000)]) {
    const options = resolveOptions({ command }, HOME);
    assert.equal(options.ok, false, `command ${JSON.stringify(command)} must be refused`);
    assert.match(options.line, /command/);
  }
});

test("resolveOptions refuses a config that is not an absolute path", () => {
  for (const config of ["config.json", "--version", "", 5, "/tmp/a\tb"]) {
    const options = resolveOptions({ config }, HOME);
    assert.equal(options.ok, false, `config ${JSON.stringify(config)} must be refused`);
    assert.match(options.line, /config/);
  }
});

test("resolveOptions keeps the interval between 5 seconds and one hour", () => {
  const ms = (interval) => resolveOptions({ interval }, HOME).intervalMs;
  assert.equal(ms(30), 30000);
  assert.equal(ms(5), 5000);
  assert.equal(ms(1), 5000);
  assert.equal(ms(0), 5000);
  assert.equal(ms(-10), 5000);
  assert.equal(ms(0.001), 5000);
  assert.equal(ms(7.5), 7500);
  assert.equal(ms(999999), 3600000);
  for (const value of ["30", NaN, Infinity, null, {}, true]) assert.equal(ms(value), 15000);
});

test("resolveOptions keeps the list length between 1 and 30 whole receipts", () => {
  const limit = (value) => resolveOptions({ limit: value }, HOME).limit;
  assert.equal(limit(12), 12);
  assert.equal(limit(0), 1);
  assert.equal(limit(-4), 1);
  assert.equal(limit(500), 30);
  assert.equal(limit(4.9), 4);
  for (const value of ["12", NaN, Infinity, null, {}]) assert.equal(limit(value), 8);
});

test("resolveOptions turns the badge off only for an explicit false", () => {
  assert.equal(resolveOptions({ badge: false }, HOME).badge, false);
  for (const value of [true, 0, "", null, undefined, "false"]) {
    assert.equal(resolveOptions({ badge: value }, HOME).badge, true);
  }
});

test("statusArgs and tasksArgs are fixed argument lists", () => {
  const plain = resolveOptions({}, HOME);
  assert.deepEqual(statusArgs(plain), ["status", "--json"]);
  assert.deepEqual(tasksArgs(plain), ["tasks", "--limit", "8"]);
  const custom = resolveOptions({ config: "/etc/awb/config.json", limit: 3 }, HOME);
  assert.deepEqual(statusArgs(custom), ["--config", "/etc/awb/config.json", "status", "--json"]);
  assert.deepEqual(tasksArgs(custom), ["--config", "/etc/awb/config.json", "tasks", "--limit", "3"]);
});
