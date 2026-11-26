import assert from "node:assert/strict";
import { test } from "node:test";
import { readBridge } from "./bridge.js";
import { resolveOptions } from "./logic.js";

const HOME = "/Users/someone";
const OPTIONS = resolveOptions({}, HOME);
const NOW = () => 1_700_000_500_000;

const RUNNING = JSON.stringify({
  version: "1.4.0", running: true, accepting: true,
  tasks: { pending: 1, submitted: 2, uncertain: 1, completed: 7, blocked: 2 },
});
const TASKS = JSON.stringify({
  tasks: [
    { id: "a", target: "codex:reviewer", status: "uncertain", created: 1_700_000_440, detail: "PROMPT TEXT" },
    { id: "b", target: "claude:writer", status: "completed", created: 1_700_000_000, detail: "RESULT TEXT" },
  ],
});

/** A stand-in for the App's exec that records every call and answers by subcommand. */
function fakeExec(answers) {
  const calls = [];
  const exec = async (command, args, options) => {
    calls.push({ command, args, options });
    const name = args.includes("status") ? "status" : "tasks";
    const answer = answers[name];
    if (answer instanceof Error) throw answer;
    return answer;
  };
  return { exec, calls };
}

test("a healthy bridge gives the service state, the counts and the newest receipts", async () => {
  const { exec, calls } = fakeExec({
    status: { code: 0, stdout: RUNNING, stderr: "" },
    tasks: { code: 0, stdout: TASKS, stderr: "" },
  });
  const snapshot = await readBridge(exec, OPTIONS, NOW);
  assert.deepEqual(snapshot, {
    at: 1_700_000_500_000,
    line: null,
    service: "running",
    version: "1.4.0",
    counts: { pending: 1, submitted: 2, uncertain: 1, completed: 7, blocked: 2, other: 0, total: 13 },
    needs: 3,
    receipts: [
      { peer: "codex:reviewer", state: "uncertain", created: 1_700_000_440 },
      { peer: "claude:writer", state: "completed", created: 1_700_000_000 },
    ],
    listLine: null,
  });
  assert.equal(JSON.stringify(snapshot).includes("TEXT"), false);
  assert.equal(calls.length, 2);
});

test("the CLI is run directly with fixed argument lists and a timeout, never through a shell", async () => {
  const { exec, calls } = fakeExec({
    status: { code: 0, stdout: RUNNING, stderr: "" },
    tasks: { code: 0, stdout: TASKS, stderr: "" },
  });
  const options = resolveOptions({ command: "/opt/awb/answerback", config: "/etc/awb/config.json", limit: 5 }, HOME);
  await readBridge(exec, options, NOW);
  assert.deepEqual(calls, [
    { command: "/opt/awb/answerback", args: ["--config", "/etc/awb/config.json", "status", "--json"], options: { timeout: 10000 } },
    { command: "/opt/awb/answerback", args: ["--config", "/etc/awb/config.json", "tasks", "--limit", "5"], options: { timeout: 10000 } },
  ]);
});

test("text from a receipt never reaches a later command", async () => {
  const hostile = JSON.stringify({
    tasks: [{ target: "; rm -rf ~ #", status: "$(reboot)", created: 1, detail: "--config /tmp/evil" }],
  });
  const { exec, calls } = fakeExec({
    status: { code: 0, stdout: RUNNING, stderr: "" },
    tasks: { code: 0, stdout: hostile, stderr: "" },
  });
  await readBridge(exec, OPTIONS, NOW);
  await readBridge(exec, OPTIONS, NOW);
  for (const call of calls) {
    assert.equal(call.command, "/Users/someone/.local/bin/answerback");
    assert.ok(
      JSON.stringify(call.args) === '["status","--json"]' || JSON.stringify(call.args) === '["tasks","--limit","8"]',
      `unexpected arguments ${JSON.stringify(call.args)}`,
    );
  }
});

test("a missing CLI gives one plain line and no receipts", async () => {
  const { exec, calls } = fakeExec({ status: new Error("No such file or directory (os error 2)") });
  const snapshot = await readBridge(exec, OPTIONS, NOW);
  assert.equal(snapshot.line, "Cannot run the Answerback CLI at /Users/someone/.local/bin/answerback (not installed, or it timed out).");
  assert.equal(snapshot.service, null);
  assert.equal(snapshot.counts, null);
  assert.equal(snapshot.needs, 0);
  assert.deepEqual(snapshot.receipts, []);
  assert.equal(calls.length, 1);
});

test("a failing CLI gives one plain line with the exit code only", async () => {
  const { exec } = fakeExec({ status: { code: 1, stdout: "", stderr: '{"error": "PRIVATE DETAIL"}' } });
  const snapshot = await readBridge(exec, OPTIONS, NOW);
  assert.equal(snapshot.line, "The Answerback CLI failed (exit code 1).");
  assert.equal(JSON.stringify(snapshot).includes("PRIVATE"), false);
});

test("invalid JSON from the CLI gives one plain line", async () => {
  const { exec } = fakeExec({ status: { code: 0, stdout: "Traceback (most recent call last):", stderr: "" } });
  const snapshot = await readBridge(exec, OPTIONS, NOW);
  assert.equal(snapshot.line, "The Answerback CLI printed something that is not valid JSON.");
  assert.equal(snapshot.service, null);
});

test("a result that is not an exec result is reported, not thrown", async () => {
  for (const answer of [undefined, null, "text", { code: "0", stdout: RUNNING }, { code: 0, stdout: 12 }]) {
    const { exec } = fakeExec({ status: answer });
    const snapshot = await readBridge(exec, OPTIONS, NOW);
    assert.equal(typeof snapshot.line, "string");
    assert.equal(snapshot.service, null);
  }
});

test("a stopped service is shown as stopped and the receipt list is not asked for", async () => {
  const { exec, calls } = fakeExec({
    status: { code: 0, stdout: JSON.stringify({ running: false, accepting: false, state: "stopped" }), stderr: "" },
  });
  const snapshot = await readBridge(exec, OPTIONS, NOW);
  assert.equal(snapshot.line, null);
  assert.equal(snapshot.service, "stopped");
  assert.equal(snapshot.counts, null);
  assert.equal(snapshot.needs, 0);
  assert.deepEqual(snapshot.receipts, []);
  assert.equal(snapshot.listLine, null);
  assert.equal(calls.length, 1);
});

test("a failing receipt list keeps the counts and adds one plain line", async () => {
  for (const tasks of [
    new Error("timed out"),
    { code: 1, stdout: "", stderr: '{"error": "x"}' },
    { code: 0, stdout: "{}", stderr: "" },
  ]) {
    const { exec } = fakeExec({ status: { code: 0, stdout: RUNNING, stderr: "" }, tasks });
    const snapshot = await readBridge(exec, OPTIONS, NOW);
    assert.equal(snapshot.line, null);
    assert.equal(snapshot.service, "running");
    assert.equal(snapshot.needs, 3);
    assert.deepEqual(snapshot.receipts, []);
    assert.equal(snapshot.listLine, "The receipt list is not available right now.");
  }
});

test("an exec that throws synchronously is reported, not thrown", async () => {
  const exec = () => { throw new TypeError("exec is broken"); };
  const snapshot = await readBridge(exec, OPTIONS, NOW);
  assert.match(snapshot.line, /^Cannot run the Answerback CLI/);
});

test("a very long command path is shortened in the message", async () => {
  const options = resolveOptions({ command: "/" + "d".repeat(900) }, HOME);
  const { exec } = fakeExec({ status: new Error("missing") });
  const snapshot = await readBridge(exec, options, NOW);
  assert.ok(Array.from(snapshot.line).length < 200);
});
