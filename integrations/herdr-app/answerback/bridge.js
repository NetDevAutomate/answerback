// Reads the Answerback bridge through its CLI. No Herdr App imports: `exec` is passed in.

import { boundText, needsPerson, parseStatus, parseTasks, statusArgs, tasksArgs } from "./logic.js";

const TIMEOUT_MS = 10000;

/**
 * @typedef {import("./logic.js").Options} Options
 * @typedef {import("./logic.js").Counts} Counts
 * @typedef {import("./logic.js").Receipt} Receipt
 * @typedef {(command: string, args: string[], options: { timeout: number }) =>
 *   Promise<{ code: number, stdout: string, stderr: string }>} Exec
 * @typedef {{ at: number, line: string | null, service: string | null, version: string,
 *   counts: Counts | null, needs: number, receipts: Receipt[], listLine: string | null }} Snapshot
 */

/**
 * Run the CLI once with an argument list (no shell). Never throws.
 * @param {Exec} exec
 * @param {string} command
 * @param {string[]} args
 * @returns {Promise<{ kind: "ok", stdout: string } | { kind: "failed", code: number } | { kind: "missing" | "odd" }>}
 */
async function run(exec, command, args) {
  let result;
  try {
    result = await exec(command, args, { timeout: TIMEOUT_MS });
  } catch {
    return { kind: "missing" };
  }
  if (typeof result !== "object" || result === null
    || !Number.isInteger(result.code) || typeof result.stdout !== "string") {
    return { kind: "odd" };
  }
  return result.code === 0 ? { kind: "ok", stdout: result.stdout } : { kind: "failed", code: result.code };
}

/**
 * Read the bridge once: the service state and receipt counts, then the newest receipts when the
 * service is running. Never throws; a problem becomes one plain line in `line` or `listLine`.
 * @param {Exec} exec
 * @param {Options} options
 * @param {() => number} [now]
 * @returns {Promise<Snapshot>}
 */
export async function readBridge(exec, options, now = Date.now) {
  /** @type {Snapshot} */
  const snapshot = {
    at: now(), line: null, service: null, version: "", counts: null, needs: 0, receipts: [], listLine: null,
  };
  const status = await run(exec, options.command, statusArgs(options));
  if (status.kind === "missing") {
    snapshot.line = `Cannot run the Answerback CLI at ${boundText(options.command, 80)} (not installed, or it timed out).`;
    return snapshot;
  }
  if (status.kind === "failed") {
    snapshot.line = `The Answerback CLI failed (exit code ${status.code}).`;
    return snapshot;
  }
  const parsed = status.kind === "ok" ? parseStatus(status.stdout) : { ok: false };
  if (!parsed.ok) {
    snapshot.line = "The Answerback CLI printed something that is not valid JSON.";
    return snapshot;
  }
  snapshot.service = parsed.service;
  snapshot.version = parsed.version;
  snapshot.counts = parsed.counts;
  snapshot.needs = needsPerson(parsed.counts);
  if (parsed.service !== "running") return snapshot;

  const list = await run(exec, options.command, tasksArgs(options));
  const tasks = list.kind === "ok" ? parseTasks(list.stdout, options.limit) : { ok: false };
  if (tasks.ok) snapshot.receipts = tasks.receipts;
  else snapshot.listLine = "The receipt list is not available right now.";
  return snapshot;
}
