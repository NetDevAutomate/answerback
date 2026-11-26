// Pure logic for the Answerback panel: no Herdr App imports, so `node --test` can run it.

/** Every receipt state the Answerback bridge reports. */
export const STATES = Object.freeze([
  "pending", "queued", "submitted", "running", "blocked",
  "uncertain", "completed", "failed", "abandoned", "cancelled",
]);

const OTHER_STATES = ["queued", "running", "failed", "abandoned", "cancelled"];
const MAX_OUTPUT = 1_048_576;
const MAX_PATH = 1024;
const DEFAULT_INTERVAL_S = 15;
const MIN_INTERVAL_S = 5;
const MAX_INTERVAL_S = 3600;
const DEFAULT_LIMIT = 8;
const MAX_LIMIT = 30;
const PEER_LENGTH = 40;
const VERSION_LENGTH = 24;

/**
 * @typedef {{ pending: number, submitted: number, uncertain: number, completed: number,
 *   blocked: number, other: number, total: number }} Counts
 * @typedef {{ peer: string, state: string, created: number | null }} Receipt
 * @typedef {{ ok: true, command: string, configArgs: string[], intervalMs: number,
 *   limit: number, badge: boolean }} Options
 * @typedef {{ ok: false, line: string }} BadOptions
 */

/**
 * @param {number} cp
 * @returns {boolean}
 */
function isSpace(cp) {
  return cp <= 0x20 || (cp >= 0x7f && cp <= 0xa0) || cp === 0x1680 || (cp >= 0x2000 && cp <= 0x200a)
    || cp === 0x2028 || cp === 0x2029 || cp === 0x202f || cp === 0x205f || cp === 0x3000;
}

/**
 * @param {number} cp
 * @returns {boolean}
 */
function isInvisible(cp) {
  return cp === 0xad || cp === 0x61c || cp === 0x180e || (cp >= 0x200b && cp <= 0x200f)
    || (cp >= 0x202a && cp <= 0x202e) || (cp >= 0x2060 && cp <= 0x206f) || cp === 0xfeff
    || (cp >= 0xfff9 && cp <= 0xfffb) || (cp >= 0xd800 && cp <= 0xdfff) || (cp >= 0xe0000 && cp <= 0xe0fff);
}

/**
 * Make untrusted text safe to show on one line: drop invisible and direction-changing
 * characters, turn control characters and line breaks into single spaces, cut to `max` characters.
 * @param {unknown} value
 * @param {number} [max]
 * @returns {string}
 */
export function boundText(value, max = 64) {
  if (typeof value !== "string") return "";
  const limit = Number.isInteger(max) && max > 0 ? max : 1;
  /** @type {string[]} */
  const out = [];
  let gap = false;
  for (const ch of value) {
    const cp = ch.codePointAt(0) ?? 0;
    if (isInvisible(cp)) continue;
    if (isSpace(cp)) {
      gap = out.length > 0;
      continue;
    }
    if (gap) out.push(" ");
    gap = false;
    out.push(ch);
    if (out.length > limit) break;
  }
  if (out.length <= limit) return out.join("");
  const kept = out.slice(0, limit - 1);
  if (kept[kept.length - 1] === " ") kept.pop();
  return `${kept.join("")}…`;
}

/**
 * Format an age in seconds as a short word: "now", "42s", "12m", "3h", "5d", or "?" when unknown.
 * @param {unknown} seconds
 * @returns {string}
 */
export function formatAge(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "?";
  if (seconds < 5) return "now";
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

/**
 * @param {unknown} value
 * @returns {value is Record<string, unknown>}
 */
function isRecord(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Read the bridge's table of receipt counts by state. Null when there is no table.
 * @param {unknown} table
 * @returns {Counts | null}
 */
export function countStates(table) {
  if (!isRecord(table)) return null;
  /** @type {(state: string) => number} */
  const count = (state) => {
    if (!Object.prototype.hasOwnProperty.call(table, state)) return 0;
    const value = table[state];
    return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : 0;
  };
  const other = OTHER_STATES.reduce((sum, state) => sum + count(state), 0);
  const counts = {
    pending: count("pending"),
    submitted: count("submitted"),
    uncertain: count("uncertain"),
    completed: count("completed"),
    blocked: count("blocked"),
    other,
    total: 0,
  };
  counts.total = counts.pending + counts.submitted + counts.uncertain + counts.completed + counts.blocked + other;
  return counts;
}

/**
 * How many receipts need a person: uncertain ones, and ones waiting for a permission decision.
 * @param {Counts | null} counts
 * @returns {number}
 */
export function needsPerson(counts) {
  return counts ? counts.uncertain + counts.blocked : 0;
}

/**
 * The badge text for a number of receipts: null (no badge) at zero, "99+" above 99.
 * @param {number} count
 * @returns {string | null}
 */
export function badgeText(count) {
  if (typeof count !== "number" || !Number.isFinite(count) || count < 1) return null;
  return count > 99 ? "99+" : String(Math.floor(count));
}

/**
 * @param {unknown} stdout
 * @returns {Record<string, unknown> | null}
 */
function parseObject(stdout) {
  if (typeof stdout !== "string" || stdout.length > MAX_OUTPUT) return null;
  try {
    const value = JSON.parse(stdout);
    return isRecord(value) ? value : null;
  } catch {
    return null;
  }
}

/**
 * Read the output of `answerback status --json`. Only the service state, the version and the
 * receipt counts are kept; the service state is one of four fixed words.
 * @param {unknown} stdout
 * @returns {{ ok: true, service: string, version: string, counts: Counts | null } | { ok: false }}
 */
export function parseStatus(stdout) {
  const value = parseObject(stdout);
  if (!value) return { ok: false };
  let service = "stopped";
  if (value.running === true) {
    if (value.accepting === true) service = "running";
    else if (value.state === "unresponsive_or_starting") service = "not responding";
    else service = "stopping";
  }
  return {
    ok: true,
    service,
    version: boundText(value.version, VERSION_LENGTH),
    counts: countStates(value.tasks),
  };
}

/**
 * @param {unknown} value
 * @returns {number}
 */
function clampLimit(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return DEFAULT_LIMIT;
  return Math.min(Math.max(Math.floor(value), 1), MAX_LIMIT);
}

/**
 * Read the output of `answerback tasks`. Only the peer, the state and the creation time of
 * each receipt are kept; prompt and result text are never read.
 * @param {unknown} stdout
 * @param {number} limit
 * @returns {{ ok: true, receipts: Receipt[] } | { ok: false }}
 */
export function parseTasks(stdout, limit) {
  const value = parseObject(stdout);
  if (!value || !Array.isArray(value.tasks)) return { ok: false };
  const cap = clampLimit(limit);
  /** @type {Receipt[]} */
  const receipts = [];
  for (const item of value.tasks) {
    if (receipts.length >= cap) break;
    if (!isRecord(item)) continue;
    receipts.push({
      peer: boundText(item.target, PEER_LENGTH) || "(no name)",
      state: typeof item.status === "string" && STATES.includes(item.status) ? item.status : "unknown",
      created: typeof item.created === "number" && Number.isFinite(item.created) ? item.created : null,
    });
  }
  return { ok: true, receipts };
}

/**
 * @param {unknown} value
 * @returns {value is string}
 */
function isPlainPath(value) {
  if (typeof value !== "string" || value.length === 0 || value.length > MAX_PATH) return false;
  for (const ch of value) {
    const cp = ch.codePointAt(0) ?? 0;
    if (cp < 0x20 || cp === 0x7f) return false;
  }
  return true;
}

/**
 * @param {unknown} value
 * @param {unknown} home
 * @returns {string | null}
 */
function resolvePath(value, home) {
  if (!isPlainPath(value)) return null;
  if (value.startsWith("/")) return value;
  if (value.startsWith("~/") && isPlainPath(home) && home.startsWith("/")) return `${home}${value.slice(1)}`;
  return null;
}

/**
 * Turn mod props into checked options. A command or config path that is not an absolute path
 * (or `~/…`) is refused with one plain line, and nothing is run.
 * @param {unknown} props
 * @param {unknown} home
 * @returns {Options | BadOptions}
 */
export function resolveOptions(props, home) {
  const given = isRecord(props) ? props : {};
  const command = resolvePath(given.command ?? "~/.local/bin/answerback", home);
  if (command === null) {
    return { ok: false, line: "The command prop must be an absolute path (or start with ~/). Nothing was run." };
  }
  /** @type {string[]} */
  let configArgs = [];
  if (given.config !== undefined && given.config !== null) {
    const config = resolvePath(given.config, home);
    if (config === null) {
      return { ok: false, line: "The config prop must be an absolute path (or start with ~/). Nothing was run." };
    }
    configArgs = ["--config", config];
  }
  const interval = typeof given.interval === "number" && Number.isFinite(given.interval)
    ? Math.min(Math.max(given.interval, MIN_INTERVAL_S), MAX_INTERVAL_S)
    : DEFAULT_INTERVAL_S;
  return {
    ok: true,
    command,
    configArgs,
    intervalMs: Math.round(interval * 1000),
    limit: clampLimit(given.limit),
    badge: given.badge !== false,
  };
}

/**
 * The fixed argument list for the status command.
 * @param {Options} options
 * @returns {string[]}
 */
export function statusArgs(options) {
  return [...options.configArgs, "status", "--json"];
}

/**
 * The fixed argument list for the receipt list command (it prints JSON without a flag).
 * @param {Options} options
 * @returns {string[]}
 */
export function tasksArgs(options) {
  return [...options.configArgs, "tasks", "--limit", String(options.limit)];
}
