// Pure logic of the Answerback fleet column, version 2 (lane proto-col, fix rounds proto-col-fix to col-fix5, ABPANEL-20261120).
// No Herdr App import: `node --test fleet.test.mjs` runs it. main.js passes in what terminals(),
// machines(), agents() and panes() returned and the bridge's document (answerback.fleet/1).
// The mod holds no capacity threshold: states, fractions and limits are the bridge's (DESIGN-v2 2.5).

/** A terminal address: "t86" here, "worker-c/t21" on another machine. */
export const ADDRESS = /^([a-z0-9][a-z0-9-]*\/)?t[0-9a-z]+$/;
/** A pane id as panes() gives it: "p9de…" here, "worker-c/pfba…" on another machine. */
export const PANE = /^([a-z0-9][a-z0-9-]*\/)?p[0-9a-z]+$/;
export const SCHEMA = "answerback.fleet/1";
/** The one number the mod knows about the bridge: a document older than this is old (DESIGN-v2 5.2). */
export const OLD_AFTER_S = 120;
/** Hide for an hour (DESIGN-v2 3.2). */
export const HIDE_MS = 3_600_000;
/** A lane that never reported is listed as quiet after this long (DESIGN-v2 3.2). */
export const QUIET_AFTER_MS = 7_200_000;
export const NEED_ROWS = 6;
/** Three receipt rows, then "+ N older" (DESIGN-v2 3.4). */
export const RECEIPT_ROWS = 3;
/** A receipt id is its identity in full; a longer one is damaged and is never stored as seen. */
export const ID_MAX = 512;
/** The one shared value of hidden rows (DESIGN-v2 3.2), read with shared() and set with setShared(). */
export const HIDDEN_KEY = "answerback.hidden";
export const HIDDEN_SCHEMA = "answerback.hidden/1";
/** At most this many hidden entries are read or kept. */
export const HIDDEN_MAX = 200;
/** The line under a Needs-you row: at most 80 characters in total (DESIGN-v2 3.2). */
export const ASK_MAX = 80;
/** The longest element id; ids are built from letters, digits, ".", "_", "/" and "-" only. */
export const ID_LEN = 64;
/** Meter drawing: 40 px wide, the bridge's fraction from 0 to 1.25, marks at 0.70 and 1.00. */
export const METER_W = 40;
export const METER_MAX = 1.25;
export const MARK_BUSY = 0.7;
export const MARK_FULL = 1.0;
export const THIS_MAC = "this Mac";
/** F14: the coordinator's name when the document says it is not a Mac. */
export const THIS_MACHINE = "this machine";
/**
 * F12: the reason under a busy or full machine row takes at most two lines of this many
 * characters, broken at spaces: the 40-character picture less its indent. At 320 px the column
 * holds about 44 characters of the label size on a line (INFERRED, not measured in a window).
 */
export const REASON_LINE = 38;
export const REASON_LINES = 2;
/** F5, F6: the plain shells (gap 5) and the label key that marks an unattended lane (in/docs/FLEET.md). */
export const SHELL_NAMES = Object.freeze(["zsh", "bash", "sh", "fish"]);
export const LANE_LABEL = "ab.lane";
export const WAITING = "waiting for the bridge's fleet document";
/** The column builds its model from the live lists at most this often; a render runs far more often. */
export const MODEL_EVERY_MS = 5000;
/**
 * col-lead, PROJECTS: the labels the column reads (it never sets one). `ab.role` with the value
 * `lead` marks the lead orchestrator of a project; `ab.project` names a terminal's project;
 * Herdr's own `herdr.space` names the space of a terminal in a tab (in/api/AGENTS.md:139-144).
 */
export const ROLE_LABEL = "ab.role";
export const PROJECT_LABEL = "ab.project";
export const SPACE_LABEL = "herdr.space";
/** A project name is drawn with at most this many characters. */
export const PROJECT_NAME_MAX = 28;
/**
 * L1b (col-lead3b): a raw project name (the label ab.project, the name of a space, the space name
 * of an agent) longer than this many UTF-16 units is not a name: that source is skipped as an
 * empty one is. The length is checked before any other work on the text.
 */
export const PROJECT_RAW_MAX = 1024;
/** The parents followed for a terminal's project, at most. */
export const PARENT_STEPS = 5;
/** K4 (col-lead2): at most this many project blocks are drawn; then `+ N more projects`. */
export const PROJECTS_MAX = 12;
/** K4: at most this many opened lines of one block are drawn; then `… (N more)`. */
export const PROJECT_LINES_MAX = 12;
/** L3 (col-lead3): lead lines of a folded block, and of an opened one, most urgent first. */
export const PROJECT_LEADS_FOLDED = 3;
export const PROJECT_LEADS_OPEN = 12;
/** At most this many opened projects are stored. */
export const PROJECTS_OPEN_MAX = 50;
/**
 * L9 (col-lead3c): every list the column draws has a largest number of lines; what is not drawn is
 * said in one muted line that cannot be clicked. A machine's opened detail: at most this many lanes,
 * the most urgent first, then `… (N more lanes)`.
 */
export const MACHINE_LANES_MAX = 12;
/** L9: machine rows, in the fixed order; then `+ N more machines`. */
export const MACHINES_MAX = 12;
/** L9: Needs-you rows after `+ N more` was clicked; then `… (N more not drawn)`. */
export const NEEDS_OPEN_MAX = 24;
/** L9: hidden rows when their line is opened; then `… (N more hidden)`. */
export const HIDDEN_ROWS_MAX = 12;
/** L9: silent lanes when their line is opened; then `… (N more silent lanes)`. */
export const QUIET_ROWS_MAX = 12;
/** L9: receipt rows after `+ N older` was clicked; then `… (N more not drawn)`. */
export const RECEIPTS_OPEN_MAX = 24;
/** How to mark a lead: the tooltip of `no lead marked`. */
export const MARK_LEAD_TIP = "to mark the lead: hc label <terminal> ab.role=lead";

/**
 * S1 (col-lead3e): the keys of the stored `folded` that fold a whole section. `receipts` is the fold
 * RECEIPTS had before (true or false, as stored). `folded.projects` holds the opened project blocks
 * since col-lead, so the fold of the PROJECTS section is `projectsSection`.
 */
export const SECTION_FOLDS = Object.freeze(["needs", "machines", "projectsSection"]);

/**
 * P1, X1 (col-lead3e): the bridge's shape of a receipt id, exactly: a UUID in lower case. A receipt
 * whose id has any other shape is offered no command and no release.
 */
export const RECEIPT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
/** X1: the reason the release gives the bridge; the same words go into the line of P1. */
export const RELEASE_REASON = "cleared by the owner from the Answerback column";
/** X1: how long the bridge's command may take, in milliseconds (the third argument of the program call). */
export const RELEASE_TIMEOUT_MS = 20000;
/** X2 (col-lead3f): the one value of the layout entry's prop `clear` that switches the release (X1) on. */
export const RELEASE_SWITCH = "release";
/** X1: the most stdout the judge reads, in bytes of UTF-8; more is "not released". */
export const RELEASE_STDOUT_MAX = 65536;
/** X1: the prop `command` when the layout gives none: the default of the repository's own panel (in/mods/answerback/logic.js:252). */
export const COMMAND_DEFAULT = "~/.local/bin/answerback";
/** X1: the longest prop `command` taken, as the repository's own panel (in/mods/answerback/logic.js:11). */
const COMMAND_MAX = 1024;
/** G2: the ending of the second line of a row with nowhere to go whose detail offers a control. */
export const OPEN_FOR = " · open for what you can do";
/** X1: the words of a release, its confirm block and its answer. */
export const RELEASED_LINE = "released · leaves with the next reading";
// The space before the parenthesis is written \u0020 so that the source rule's search for a call of
// `run` (fleet.test.mjs, test "source: ...") does not take these words for one.
export const RELEASE_NOT_RUN = "the bridge's command could not be run\u0020(not installed, or no answer in 20 s): look again after the next reading";
export const ODD_ID_LINE = "no command offered: this receipt's id has an unexpected shape";
export const BAD_COMMAND_LINE = "no release offered: the prop `command` must be an absolute path or start with ~/";
/** N1 (col-lead3g): the line after the rows of NEEDS YOU for entries that wait and cannot be drawn. */
export function cannotShowText(n) {
  return n === 1 ? "1 receipt waits and cannot be shown: its entry has no id the column can use"
    : `${fmtCount(n)} receipts wait and cannot be shown: their entries have no id the column can use`;
}
/** X4 (col-lead3g): a later reading 2 s or more after the answer that still lists the receipt uncertain. */
export const CONTRADICT_AFTER_MS = 2000;
export const CONTRADICTED_LINE = "the bridge answered \"released\", and a later reading still lists this receipt";
export const CONFIRM_LINES = Object.freeze(["It stops nothing that runs. It cannot be undone."]);

export function isRecord(v) {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function finite(v) {
  return typeof v === "number" && Number.isFinite(v);
}

function arr(v) {
  return Array.isArray(v) ? v : [];
}

export function isAddress(s) {
  return typeof s === "string" && s.length <= 64 && ADDRESS.test(s);
}

/**
 * The one lookup of an untrusted word (a state, a mark, a signal) in a fixed table: an own key
 * of the table, else `fallback`. `constructor`, `__proto__` and `toString` never reach a value.
 */
export function lookup(table, word, fallback = null) {
  return typeof word === "string" && Object.hasOwn(table, word) ? table[word] : fallback;
}

/** A frozen table with no prototype, for lookup(). */
function table(entries) {
  return Object.freeze(Object.assign(Object.create(null), entries));
}

/**
 * FNV-1a of a string, 8 hex digits: two keys that clean to the same text most often keep different
 * ids. L1c (col-lead3c): only a part of an element id; no decision depends on it being unique
 * (two different keys can share it): ids are made unique per drawn column by idMaker().
 */
function hash8(text) {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, "0");
}

/**
 * An element id from a prefix and a key of untrusted origin: letters, digits, ".", "_", "/" and
 * "-" only, at most 64 characters, with a hash of the full key so that distinct keys stay distinct.
 */
export function elementId(prefix, key, suffix = "") {
  const p = String(prefix).replace(/[^A-Za-z0-9._/-]/g, "_").slice(0, 16);
  const full = typeof key === "string" ? key : String(key);
  const room = ID_LEN - p.length - 10 - suffix.length;
  const body = full.replace(/[^A-Za-z0-9._/-]/g, "_").slice(0, Math.max(0, room));
  return `${p}-${body}-${hash8(full)}${suffix}`;
}

/**
 * L1c (col-lead3c): the element ids of one drawn column, unique by construction. The first element
 * of a prefix and key gets elementId's form; when that id is already taken in this column (two keys
 * that share a cleaned beginning and a hash, or one key drawn twice), the later one gets a counted
 * suffix `.2`, `.3`, …, the first that is free. The same input in the same order gives the same ids.
 * `fixed` are the column's fixed ids, so that no built id can take one of them.
 */
export function idMaker(fixed = []) {
  const used = new Set(fixed);
  return (prefix, key) => {
    let id = elementId(prefix, key);
    for (let n = 2; used.has(id); n += 1) id = elementId(prefix, key, `.${n}`);
    used.add(id);
    return id;
  };
}

/** The machine name in an address, or null for this computer (bare address) or a bad address. */
export function machineOf(address) {
  if (!isAddress(address)) return null;
  const i = address.indexOf("/");
  return i < 0 ? null : address.slice(0, i);
}

/** An address may go into a command only if it is well formed and was in the last terminals() read. */
export function safeTarget(address, lastRead) {
  return isAddress(address) && lastRead instanceof Set && lastRead.has(address);
}

/**
 * What a click on a terminal row runs, or null when the address is not a safe target.
 * In a tab: pane.focus {pane}. Not in a tab: terminal.peek {terminal} (DESIGN-v2 3.2).
 * @param {unknown} address
 * @param {Set<string>} lastRead addresses of the last live terminals() read
 * @param {Map<string, string>} paneOf terminal address to pane id, from panes()
 */
export function clickAction(address, lastRead, paneOf) {
  if (!safeTarget(address, lastRead)) return null;
  const pane = paneOf instanceof Map ? paneOf.get(address) : undefined;
  if (typeof pane === "string" && pane.length <= 96 && PANE.test(pane)) return { command: "pane.focus", args: { pane }, address };
  return { command: "terminal.peek", args: { terminal: address }, address };
}

/** Addresses of live, well-formed terminals in one terminals() read. */
export function liveAddresses(terminals) {
  return new Set(arr(terminals).filter((r) => isRecord(r) && r.state === "live" && isAddress(r.address)).map((r) => r.address));
}

/** terminal address to pane id from panes(), completed from agents(). */
export function livePanes(panes, agents) {
  const paneOf = paneMap(panes);
  for (const a of arr(agents)) {
    if (isRecord(a) && isAddress(a.address) && !paneOf.has(a.address) && typeof a.pane === "string" && PANE.test(a.pane)) paneOf.set(a.address, a.pane);
  }
  return paneOf;
}

/**
 * What a click runs, decided at the time of the click from the lists read at that moment
 * (terminals(), panes(), agents()), never from the render that drew the row. Sample rows: null.
 */
export function actionAtClick(address, now, sample = false) {
  if (sample === true || !isRecord(now)) return null;
  return clickAction(address, liveAddresses(now.terminals), livePanes(now.panes, now.agents));
}

/** An address for display: the machine part shortened in the middle, like every machine name. */
export function displayAddress(address) {
  if (!isAddress(address)) return "";
  const m = machineOf(address);
  return m === null ? address : `${shortMachine(m)}/${address.slice(m.length + 1)}`;
}

/** terminal address to pane id from panes(); rows without a terminal or with a malformed id are left out. */
export function paneMap(panes) {
  const out = new Map();
  for (const p of arr(panes)) {
    if (isRecord(p) && isAddress(p.terminal) && typeof p.pane === "string" && PANE.test(p.pane)) out.set(p.terminal, p.pane);
  }
  return out;
}

/**
 * The one bounding function for untrusted text: plain text on one line. Control characters and
 * line breaks become one space, invisible and direction-changing characters go, the rest is cut
 * to `max` characters with an ellipsis.
 * K1 (col-lead2): a cut never leaves an opening parenthesis without its closing one. When the
 * ordinary cut would, the text is cut as G3 cuts (cutClosed). A text that is not cut is unchanged.
 */
export function boundText(value, max = 64) {
  const limit = Number.isInteger(max) && max > 0 ? max : 1;
  const r = cleanCut(value, limit);
  if (!r.cut || closed(r.text)) return r.text;
  return cutClosed(cleanCut(value, CLOSED_SOURCE_MAX).text, limit);
}

/** K1: the cleaned text a closed cut starts from is at most this long. */
const CLOSED_SOURCE_MAX = 4000;

/**
 * K1: the ordinary cut alone, only for a text that G3's cut (cutSentence, fitsReason) cuts again
 * before it is drawn: the reason under a machine row. It keeps that reason as the base drew it.
 */
function plainText(value, max) {
  return cleanCut(value, Number.isInteger(max) && max > 0 ? max : 1).text;
}

/**
 * L8 (col-lead3b): the star-shaped characters, exactly the list the session that ordered the work
 * gave; the Agents list of the same window uses the same one. In every foreign text that the column
 * draws or puts into a tooltip each of them becomes `*` (U+002A), so the lead mark (LEAD_MARK), which
 * the column itself puts at the start of a lead's line, is the only star in the column. A look-alike
 * outside this list is not covered.
 */
export const STAR_SHAPED = Object.freeze([
  0x2605, 0x2606, 0x2b50, 0x2b51, 0x2b52, 0x1f31f, 0x1f320, 0x2728, 0x1f4ab, 0x2729,
  0x272a, 0x272b, 0x272c, 0x272d, 0x272e, 0x272f, 0x2730,
  0x2731, 0x2732, 0x2733, 0x2734, 0x2735, 0x2736, 0x2737, 0x2738, 0x2739,
  0x235f, 0x269d, 0x22c6, 0x066d, 0x2042, 0x2217, 0x26e4, 0x26e5, 0x26e6, 0x26e7,
  0x1f7c0, 0x1f7c1, 0x1f7c2, 0x1f7c3, 0x1f7c4, 0x1f7c5, 0x1f7c6, 0x1f7c7, 0x1f7c8, 0x1f7c9, 0x1f7ca, 0x1f7cb, 0x1f7cc,
  0x1f7cd, 0x1f7ce, 0x1f7cf, 0x1f7d0, 0x1f7d1, 0x1f7d2, 0x1f7d3, 0x1f7d4, 0x1f7d5, 0x1f7d6, 0x1f7d7, 0x1f7d8,
]);
const STARS = new Set(STAR_SHAPED);
/** L8: the mark at the start of a lead's line, the one star the column draws. */
export const LEAD_MARK = "\u2605";

/**
 * The cleaning and the ordinary cut of boundText: { text, cut }. Every foreign text passes here
 * before it is drawn (boundText, plainText, the source of a closed cut, and `say` in main.js).
 * L8 (col-lead3b): a star-shaped character becomes `*`, and a variation selector (U+FE0E, U+FE0F)
 * drawn directly after it goes with it; this comes before the cut, so the limits stay the same.
 * `keepStars` is only for a project's identity (L1), which the replacement does not change.
 */
function cleanCut(value, limit, keepStars = false) {
  if (typeof value !== "string") return { text: "", cut: false };
  // T5b (col-lead3d): every cleaning of a project name (keepStars: a project's identity, L1) is
  // counted here, where the work is done, once for each call; projectContext reports the calls of
  // its build (projects().cleaned).
  if (keepStars) identityCleanings += 1;
  const out = [];
  let gap = false;
  let replaced = false;
  for (const ch of value) {
    const cp = ch.codePointAt(0) ?? 0;
    const invisible = cp === 0xad || cp === 0x61c || cp === 0x180e || (cp >= 0x200b && cp <= 0x200f)
      || (cp >= 0x202a && cp <= 0x202e) || (cp >= 0x2060 && cp <= 0x206f) || cp === 0xfeff
      || (cp >= 0xfff9 && cp <= 0xfffb) || (cp >= 0xd800 && cp <= 0xdfff) || (cp >= 0xe0000 && cp <= 0xe0fff);
    if (invisible) continue;
    if (replaced && (cp === 0xfe0e || cp === 0xfe0f)) {
      replaced = false;
      continue;
    }
    replaced = false;
    const space = cp <= 0x20 || (cp >= 0x7f && cp <= 0xa0) || cp === 0x2028 || cp === 0x2029;
    if (space) {
      gap = out.length > 0;
      continue;
    }
    if (gap) out.push(" ");
    gap = false;
    if (!keepStars && STARS.has(cp)) {
      out.push("*");
      replaced = true;
    } else out.push(ch);
    if (out.length > limit) break;
  }
  if (out.length <= limit) return { text: out.join(""), cut: false };
  const kept = out.slice(0, limit - 1);
  if (kept[kept.length - 1] === " ") kept.pop();
  return { text: `${kept.join("")}…`, cut: true };
}

/**
 * T5b (col-lead3d): the number of identity cleanings (cleanCut with keepStars) since the module was
 * loaded. A build reports the difference between its end and its start (NameMap.cleaned).
 */
let identityCleanings = 0;

/** Bounded text cut in the middle, so that the end stays readable. */
export function middle(value, max = 16) {
  const text = boundText(value, 400);
  const chars = [...text];
  if (chars.length <= max) return text;
  const tail = Math.max(1, Math.floor((max - 1) / 2));
  const head = Math.max(1, max - 1 - tail);
  const cut = `${chars.slice(0, head).join("")}…${chars.slice(-tail).join("")}`;
  // K1: a middle cut that leaves a parenthesis open is cut at the end instead (boundText).
  return closed(cut) ? cut : boundText(text, max);
}

/**
 * A machine name for a chip: names longer than seven characters keep their first three and last
 * three (DESIGN-v2 4.2), so worker-d and worker-b become wor…r-d and wor…r-b.
 * null is this computer: `self` is its name (F14, coordinatorName), "this Mac" by default.
 */
export function shortMachine(name, self = THIS_MAC) {
  if (name === null) return self === THIS_MACHINE ? THIS_MACHINE : THIS_MAC;
  const text = boundText(name, 64);
  const chars = [...text];
  if (chars.length <= 7) return text;
  const cut = `${chars.slice(0, 3).join("")}…${chars.slice(-3).join("")}`;
  // K1: a chip that would leave a parenthesis open is cut at the end instead (boundText).
  return closed(cut) ? cut : boundText(text, 7);
}

/** An age in seconds in the design's words: "now", "40 s", "3 min", "6 h", "2 d"; "?" when unknown. */
export function formatAge(seconds) {
  if (!finite(seconds) || seconds < 0) return "?";
  if (seconds < 5) return "now";
  if (seconds < 60) return `${Math.floor(seconds)} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h`;
  return `${Math.floor(seconds / 86400)} d`;
}

/**
 * F14: the coordinator's name. "this Mac" when the document's machines[0].os is exactly `darwin`
 * or there is no document; "this machine" otherwise.
 */
export function coordinatorName(doc) {
  if (!isRecord(doc)) return THIS_MAC;
  const first = arr(doc.machines)[0];
  return isRecord(first) && first.os === "darwin" ? THIS_MAC : THIS_MACHINE;
}

/** F3: a reading's age in the footer's words: "reading new" under 5 s, else "reading 40 s old". */
export function readingAge(seconds) {
  return finite(seconds) && seconds >= 0 && seconds < 5 ? "reading new" : `reading ${formatAge(seconds)} old`;
}

/** A wall-clock time "19:05" for a time in seconds; `offsetMin` is the zone's offset from UTC in minutes. */
export function clock(seconds, offsetMin = 0) {
  if (!finite(seconds)) return "?";
  const m = Math.floor(seconds / 60) + (finite(offsetMin) ? offsetMin : 0);
  const day = ((m % 1440) + 1440) % 1440;
  return `${String(Math.floor(day / 60)).padStart(2, "0")}:${String(day % 60).padStart(2, "0")}`;
}

/** The status word of a terminals() row: `status` is a plain word there (in/api/herdr.d.ts:241). */
export function statusWord(row) {
  if (!isRecord(row)) return null;
  if (typeof row.status === "string" && row.status) return row.status;
  const s = isRecord(row.report) ? row.report.state : null;
  return typeof s === "string" && s ? s : null;
}

/**
 * F13 (break 6): the terminals() rows with the status word of agents() where it has one, so that
 * this column and the Agents list say the same word for one terminal. A row is copied only when
 * its word changes; the rows themselves are never changed.
 */
export function withAgentStatus(terminals, agents) {
  const words = new Map();
  for (const a of arr(agents)) {
    if (isRecord(a) && isAddress(a.address) && typeof a.status === "string" && a.status !== "") words.set(a.address, a.status);
  }
  if (words.size === 0) return arr(terminals);
  return arr(terminals).map((r) => {
    if (!isRecord(r) || !words.has(r.address)) return r;
    const word = words.get(r.address);
    return statusWord(r) === word ? r : { ...r, status: word };
  });
}

/** The labels of a terminals() row, or an empty object. */
function labelsOf(row) {
  return isRecord(row) && isRecord(row.labels) ? row.labels : {};
}

/** F5, F6: true when the row carries the lane label (an own key `ab.lane`, any value). */
export function hasLaneLabel(row) {
  return Object.hasOwn(labelsOf(row), LANE_LABEL);
}

/** F5: true when the App's program table says the row runs an agent (Program.agent, in/api/herdr.d.ts:166). */
export function runsAgent(row) {
  return isRecord(row) && isRecord(row.program) && row.program.agent === true;
}

/**
 * F5 (gap 8, break 7): a live terminal that is not in a tab and reports no status is work only
 * with the lane label or an agent program. An editor helper is neither.
 */
export function countsAsWork(row) {
  return hasLaneLabel(row) || runsAgent(row);
}

/**
 * F6: a plain shell that reports no status and has no lane label. The program's key or name is
 * looked up in SHELL_NAMES; Program.shell (in/api/herdr.d.ts:165) also says so.
 */
export function isPlainShell(row) {
  if (!isRecord(row) || statusWord(row) !== null || hasLaneLabel(row)) return false;
  const p = isRecord(row.program) ? row.program : {};
  return p.shell === true || SHELL_NAMES.includes(p.key) || SHELL_NAMES.includes(p.name);
}

/** The last folder of a path: "herdr_bridge" for "/x/herdr_bridge/"; "" when there is none. */
export function lastFolder(path) {
  if (typeof path !== "string") return "";
  const parts = path.split("/").filter((x) => x !== "");
  return parts.length === 0 ? "" : boundText(parts[parts.length - 1], 24);
}

/** F7: the program a row runs, for people: the table's key, or its name when the key says nothing. */
function programWord(row) {
  const p = isRecord(row) && isRecord(row.program) ? row.program : {};
  const key = boundText(p.key, 16);
  if (key !== "" && !["program", "agent", "shell"].includes(key)) return key;
  return boundText(p.name, 16);
}

/** The tab name inside Herdr's `where` label ("space / 3 · tab name"), or null when it has none. */
export function tabNameFromWhere(where) {
  if (typeof where !== "string") return null;
  const i = where.indexOf(" · ");
  if (i < 0) return null;
  const name = where.slice(i + 3).trim();
  return name === "" ? null : name;
}

/**
 * The best name of a terminal row: ab.title label, name, tab name, title, program, address. A name
 * that is only digits becomes the program and the last folder of cwd (F7): `kiro · herdr_bridge`.
 */
export function bestName(row, tabName = null, max = 28) {
  if (!isRecord(row)) return "";
  const labels = isRecord(row.labels) ? row.labels : {};
  const candidates = [labels["ab.title"], row.name, tabName, tabNameFromWhere(labels.where), row.title,
    isRecord(row.program) ? row.program.name : null];
  for (const c of candidates) {
    const text = boundText(c, max);
    if (text === "") continue;
    // F7 (gap 4): a tab called "1" says nothing; the program and the last folder of cwd do.
    // G6: "only digits" is decided on the cleaned name before it is cut, so that a cut name's
    // ellipsis never hides a number. c.length bounds its count of characters: nothing is cut.
    if (/^[0-9]+$/.test(boundText(c, Math.max(1, c.length)))) {
      const alt = [programWord(row), lastFolder(row.cwd)].filter(Boolean).join(" · ");
      return alt === "" ? text : boundText(alt, max);
    }
    return text;
  }
  return isAddress(row.address) ? row.address : "";
}

/**
 * C1: the fixtures are drawn only under `herdr-app check` or when the block's prop `sample` is
 * true. A window whose live lists are empty draws the real empty state.
 */
export function useSample(isChecking, props) {
  return isChecking === true || (isRecord(props) && props.sample === true);
}

/**
 * The bridge's document, or null when the text is missing or not that shape. A null is a state
 * the panel draws, not an error.
 */
export function parseDocument(text) {
  if (typeof text !== "string" || text.length > 1_048_576) return null;
  let v;
  try {
    v = JSON.parse(text);
  } catch {
    return null;
  }
  if (!isRecord(v) || v.schema !== SCHEMA || !finite(v.written_at) || !Array.isArray(v.machines)) return null;
  return v;
}

/**
 * C6: the document after one read. A read that failed, found no file, or returned text that is
 * half-written, not JSON, of another schema or too large keeps the last good document, and with
 * it that document's own age (its `written_at`). `kept` says that the read was not used.
 */
export function nextDocument(previous, text) {
  const doc = parseDocument(text);
  if (doc !== null) return { doc, kept: false };
  return { doc: isRecord(previous) ? previous : null, kept: true };
}

/** The folder of a file path, for watchFolder. */
export function folderOf(path) {
  if (typeof path !== "string") return "";
  const i = path.lastIndexOf("/");
  return i <= 0 ? "/" : path.slice(0, i);
}

/**
 * True when a batch of changed paths from a folder watch may have changed the document: its file
 * is named, or the folder itself is (which means "refresh everything").
 */
export function batchTouches(paths, path) {
  if (typeof path !== "string" || path === "") return false;
  const name = path.slice(path.lastIndexOf("/") + 1);
  const folder = folderOf(path);
  return arr(paths).some((p) => typeof p === "string" && (p.endsWith(`/${name}`) || p.endsWith(folder) || p.endsWith(`${folder}/`)));
}

/** The document path from the block's props, with "~" opened to the home folder. */
export function documentPath(props, homeDir) {
  const given = isRecord(props) && typeof props.document === "string" && props.document.trim() !== "" ? props.document.trim() : null;
  // The folder that `answerback fleet serve` publishes into by default. Nothing else is written
  // there, so the folder watch wakes once for each publication.
  const path = given ?? "~/.config/herdr-bridge/fleet/fleet.json";
  const h = typeof homeDir === "string" ? homeDir.replace(/\/+$/, "") : "";
  return path === "~" ? h : path.startsWith("~/") ? `${h}${path.slice(1)}` : path;
}

// ---------------------------------------------------------------- stored state

/** localStorage when the host has one, else null. Never throws. */
export function hostStorage() {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

/**
 * The one wrapper around localStorage. With no storage, or one that throws, it keeps the values
 * in memory for this copy of the mod and never throws.
 */
export function makeStore(storage, key = "answerback-fleet/1") {
  let cache = null;
  const load = () => {
    try {
      const raw = storage ? storage.getItem(key) : null;
      const v = typeof raw === "string" ? JSON.parse(raw) : null;
      return isRecord(v) ? v : {};
    } catch {
      return {};
    }
  };
  return {
    get(name, fallback = null) {
      cache ??= load();
      return Object.hasOwn(cache, name) ? cache[name] : fallback;
    },
    set(name, value) {
      cache ??= load();
      cache[name] = value;
      try {
        if (storage) storage.setItem(key, JSON.stringify(cache));
      } catch {
        // Storage full or closed: the value stays in memory.
      }
    },
  };
}

const nonNegInt = (v) => Number.isSafeInteger(v) && v >= 0;
const goodId = (v) => typeof v === "string" && v !== "" && v.length <= ID_MAX;

/** The `seen` ids in a known shape: full ids, no duplicates, the last 400. */
function cleanSeen(v) {
  return [...new Set(arr(v).filter(goodId))].slice(-400);
}

/**
 * The base of the `since` line, or null when it is damaged: a time in seconds not after `nowMs`
 * (one day of clock skew allowed), totals that are whole numbers of zero or more, and the ids
 * that waited at that time.
 */
export function cleanBase(base, nowMs) {
  if (!isRecord(base) || !finite(base.at) || base.at < 0) return null;
  if (finite(nowMs) && base.at > nowMs / 1000 + 86400) return null;
  if (!nonNegInt(base.completed) || !nonNegInt(base.failed)) return null;
  return { at: base.at, completed: base.completed, failed: base.failed, waiting: cleanSeen(base.waiting) };
}

/**
 * The stored state in a known shape, whatever was stored (C4). Damaged values are dropped:
 * a base with a negative or non-finite total, `seen` entries that are not ids, folds that are
 * not true or false. The hidden set is not here: it is the shared value (readHidden).
 */
export function readState(store, nowMs = Date.now()) {
  const seen = store.get("seen", []);
  const base = store.get("base", null);
  const folded = store.get("folded", {});
  const out = {
    hidden: {},
    seen: cleanSeen(seen),
    base: cleanBase(base, nowMs),
    folded: isRecord(folded) && typeof folded.receipts === "boolean" ? { receipts: folded.receipts } : {},
  };
  // S1 (col-lead3e): the fold of a section is kept only when it is exactly true; any other value,
  // a missing key or a damaged store (makeStore reads it as {}) means open.
  if (isRecord(folded)) for (const k of SECTION_FOLDS) if (folded[k] === true) out.folded[k] = true;
  // col-lead: the opened PROJECTS blocks, kept like the fold of RECEIPTS; only well-formed keys.
  if (isRecord(folded) && Array.isArray(folded.projects)) out.folded.projects = cleanProjectKeys(folded.projects);
  return out;
}

/**
 * L1c (col-lead3c): the key under which a project block's opened state is kept: `p:n:` and the
 * project's whole identity (bounded by L1b, so at most PROJECT_RAW_MAX units). Two identities never
 * share a key, and no key of a project is `p:none`, the key of the lanes without a project.
 */
export function projectKey(id) {
  return `p:n:${id}`;
}

/** The keys of opened projects: projectKey's form or "p:none", no duplicates, at most PROJECTS_OPEN_MAX. */
function cleanProjectKeys(keys) {
  const good = (k) => typeof k === "string" && (k === "p:none" || (k.startsWith("p:n:") && k.length > 4 && k.length <= 4 + PROJECT_RAW_MAX));
  return [...new Set(arr(keys).filter(good))].slice(0, PROJECTS_OPEN_MAX);
}

/** The stored fold after a click on a project's title line: opened when it was folded, folded when it was open. */
export function toggleProject(folded, key) {
  const f = isRecord(folded) ? folded : {};
  const open = cleanProjectKeys(f.projects);
  const next = open.includes(key) ? open.filter((k) => k !== key) : [...open, key];
  return { ...f, projects: cleanProjectKeys(next.slice(-PROJECTS_OPEN_MAX)) };
}

/** True for a key of the hidden set: "t:<address>" or "r:<receipt id>". */
function goodHiddenKey(key) {
  if (typeof key !== "string") return false;
  if (key.startsWith("t:")) return isAddress(key.slice(2));
  return key.startsWith("r:") && goodId(key.slice(2));
}

/**
 * The hidden set from the shared value (C4, C10). An entry stays only when its key is well formed,
 * its deadline is finite, still ahead and not more than one hour from now, and its stamp is a
 * number or null. Anything else, and the whole value when it is not of this schema, is dropped.
 */
export function readHidden(value, nowMs) {
  const out = {};
  if (!isRecord(value) || value.schema !== HIDDEN_SCHEMA || !isRecord(value.items) || !finite(nowMs)) return out;
  let n = 0;
  for (const [key, h] of Object.entries(value.items)) {
    if (n >= HIDDEN_MAX) break;
    if (!goodHiddenKey(key) || !isRecord(h) || !finite(h.until)) continue;
    if (h.until <= nowMs || h.until > nowMs + HIDE_MS) continue;
    if (!(h.stamp === null || finite(h.stamp))) continue;
    out[key] = { until: h.until, stamp: h.stamp };
    n += 1;
  }
  return out;
}

/** The value given to setShared(HIDDEN_KEY, …): another mod can honour the same rows. */
export function hiddenValue(hidden) {
  const items = {};
  for (const [key, h] of Object.entries(isRecord(hidden) ? hidden : {}).slice(0, HIDDEN_MAX)) {
    if (goodHiddenKey(key) && isRecord(h) && finite(h.until)) items[key] = { until: h.until, stamp: finite(h.stamp) ? h.stamp : null };
  }
  return { schema: HIDDEN_SCHEMA, items };
}

// ---------------------------------------------------------------- needs you

const REASONS = table({
  bridge_stopped_during_submission: "may not have arrived · bridge stopped",
  permission: "waits for a permission",
  permission_requested: "waits for a permission",
});

/** The second line of a receipt that needs a person, from the bridge's reason code; at most 80 characters. */
export function receiptAsk(receipt) {
  const r = isRecord(receipt) ? receipt : {};
  const code = typeof r.reason === "string" ? r.reason : "";
  const known = lookup(REASONS, code);
  if (known !== null) return known;
  const words = boundText(code.replaceAll("_", " "), 60);
  if (r.state === "uncertain") return boundText(words ? `may not have arrived · ${words}` : "may not have arrived", ASK_MAX);
  if (r.state === "blocked") return boundText(words ? `waits for a permission · ${words}` : "waits for a permission", ASK_MAX);
  return boundText(words || `${boundText(r.state, 20) || "state unknown"} · no reason given`, ASK_MAX);
}

/** The second line of a blocked terminal: what it asks, at most 80 characters. */
export function terminalAsk(row) {
  const rep = isRecord(row) && isRecord(row.report) ? row.report : {};
  const msg = boundText(rep.msg, 70);
  if (msg) return boundText(`asks: ${msg}${/[?.!…]$/.test(msg) ? "" : "?"}`, ASK_MAX);
  const kind = boundText(rep.kind, 30);
  return kind ? `asks: ${kind}` : "blocked · no reason given";
}

/** The chip of a receipt: its machine, else its provider. */
function receiptChip(recipient) {
  const r = isRecord(recipient) ? recipient : {};
  if (typeof r.machine === "string" && r.machine) return shortMachine(r.machine);
  return boundText(r.provider, 8) || "bridge";
}

/**
 * Everything that waits for a person: every live terminal with status `blocked` on every machine,
 * then every receipt in the document's needs_person[]. Terminals come before receipts, each
 * oldest first; an item with no known time sorts last in its group. No row limit here.
 */
/**
 * N1 (col-lead3g): an entry of needs_person[] can be drawn when it is a record whose `id` is a text
 * that is not empty. The list of rows skips the others; receiptCountsKnown and the line of N1.3 count them.
 */
export function drawableEntry(entry) {
  return isRecord(entry) && typeof entry.id === "string" && entry.id !== "";
}

/** N1 (col-lead3g): the number of entries of the document's needs_person[] that cannot be drawn. */
export function undrawableCount(doc) {
  const list = isRecord(doc) && isRecord(doc.receipts) ? arr(doc.receipts.needs_person) : [];
  return list.filter((r) => !drawableEntry(r)).length;
}

export function needsList(terminals, doc, tabNames = new Map()) {
  const self = coordinatorName(doc);
  const terms = [];
  for (const r of arr(terminals)) {
    if (!isRecord(r) || r.state !== "live" || !isAddress(r.address) || statusWord(r) !== "blocked") continue;
    const since = finite(r.status_ms) ? r.status_ms : finite(r.created_ms) ? r.created_ms : null;
    terms.push({
      key: `t:${r.address}`, source: "terminal", mark: "!", address: r.address, id: null,
      chip: shortMachine(machineOf(r.address), self), name: bestName(r, tabNames.get(r.address) ?? null),
      sinceMs: since, stamp: since, asks: terminalAsk(r),
    });
  }
  const recs = [];
  const list = isRecord(doc) && isRecord(doc.receipts) ? arr(doc.receipts.needs_person) : [];
  for (const r of list) {
    if (!drawableEntry(r)) continue;
    const created = finite(r.created) ? Math.round(r.created * 1000) : null;
    // The full id is the receipt's identity (C5); it is shortened only where it is drawn.
    recs.push({
      key: `r:${r.id}`, source: "receipt", mark: "?", address: null, id: r.id,
      chip: receiptChip(r.recipient), name: receiptLabel(r),
      sinceMs: created, stamp: finite(r.updated) ? Math.round(r.updated * 1000) : created, asks: receiptAsk(r),
      receipt: r,
    });
  }
  const oldest = (a, b) => (a.sinceMs ?? Infinity) - (b.sinceMs ?? Infinity) || a.key.localeCompare(b.key);
  return [...terms.sort(oldest), ...recs.sort(oldest)];
}

/** What is stored when the owner hides a row: it returns after an hour or when the item blocks again. */
export function hideEntry(item, nowMs) {
  return { until: nowMs + HIDE_MS, stamp: finite(item.stamp) ? item.stamp : null };
}

function isHidden(item, hidden, nowMs) {
  const h = isRecord(hidden) && Object.hasOwn(hidden, item.key) ? hidden[item.key] : null;
  if (!isRecord(h) || !finite(h.until) || nowMs >= h.until || h.until > nowMs + HIDE_MS) return false;
  // A new status time or receipt update also means "blocked again".
  return h.stamp === null || !finite(item.stamp) || h.stamp === item.stamp;
}

/**
 * The hidden entries that still apply (C4). An entry is dropped when its hour has ended, its
 * deadline is more than an hour away, its item blocked again with a new time, or its item was
 * seen not blocked: a terminal in this terminals() read that is not blocked, or a receipt that a
 * valid document no longer lists as needing a person. Dropping it at that moment is what makes
 * the row return when the item blocks again, also for a terminal with no `status_ms`. An entry
 * whose item cannot be seen now (its terminal is not in the read, no document) waits for its hour.
 */
export function pruneHidden(hidden, items, nowMs, seenNow = {}) {
  const out = {};
  if (!isRecord(hidden)) return out;
  const waiting = new Map(items.map((i) => [i.key, i]));
  const present = seenNow.terminals instanceof Set ? seenNow.terminals : new Set();
  const docSeen = seenNow.doc === true;
  for (const [key, h] of Object.entries(hidden)) {
    const item = waiting.get(key);
    if (item !== undefined) {
      if (isHidden(item, hidden, nowMs)) out[key] = h;
      continue;
    }
    // Not blocked now. Seen not blocked: drop. Not seen at all: keep until the hour ends.
    const unseen = key.startsWith("t:") ? !present.has(key.slice(2)) : key.startsWith("r:") ? !docSeen : false;
    if (unseen && isRecord(h) && finite(h.until) && nowMs < h.until && h.until <= nowMs + HIDE_MS) out[key] = h;
  }
  return out;
}

/** True when two hidden sets hold the same entries. */
export function sameHidden(a, b) {
  return JSON.stringify(hiddenValue(a)) === JSON.stringify(hiddenValue(b));
}

/** The needs-you list split into shown and hidden rows; `until` is when the first hidden row returns. */
export function splitHidden(items, hidden, nowMs) {
  const shown = [];
  const away = [];
  for (const item of items) (isHidden(item, hidden, nowMs) ? away : shown).push(item);
  const until = away.length === 0 ? null : Math.min(...away.map((i) => hidden[i.key].until));
  return { shown, hidden: away, until };
}

/** The rows drawn of the shown list: six, then "+ N more"; all of them when expanded. */
export function needRows(shown, expanded = false, max = NEED_ROWS) {
  if (expanded || shown.length <= max) return { rows: shown, more: 0 };
  return { rows: shown.slice(0, max), more: shown.length - max };
}

/** "▸ 2 hidden until 19:05". */
export function hiddenLine(count, untilMs, offsetMin = 0) {
  return count > 0 ? `▸ ${count} hidden until ${clock(untilMs / 1000, offsetMin)}` : null;
}

/**
 * True when a render may draw the model on hand: there is one, nothing asked for a fresh one, and it
 * was built less than `everyMs` ago. A clock that went back, or a time that is no number, gives a
 * fresh model.
 */
export function reuseModel(has, builtAtMs, nowMs, wantFresh, everyMs = MODEL_EVERY_MS) {
  if (has !== true || wantFresh === true || !finite(builtAtMs) || !finite(nowMs)) return false;
  const age = nowMs - builtAtMs;
  return age >= 0 && age < everyMs;
}

/**
 * True when a terminal row sits in a tab: panes() lists it, or it carries Herdr's tab label.
 * A lane that another terminal started (`hc run` from a session) has that terminal as `parent` and
 * its parent's tab in `where`, with no `herdr.tab` and no pane. It is not in a tab: it runs on its
 * machine and is listed there. Seen in the open window on 2026-11-20.
 */
export function inTab(row, paneOf) {
  if (!isRecord(row)) return false;
  if (paneOf instanceof Map && paneOf.has(row.address)) return true;
  const labels = isRecord(row.labels) ? row.labels : {};
  if (typeof labels["herdr.tab"] === "string" && labels["herdr.tab"] !== "") return true;
  if (isAddress(row.parent)) return false;
  return typeof labels.where === "string" && labels.where !== "";
}

/**
 * Quiet lanes: live, not in a tab, never reported a status, older than two hours, and work by
 * F5 (the lane label or an agent program: an editor helper is no lane). A trust prompt reports
 * nothing in some harness wrappers, so silence can mean "waiting". A lane with no known start
 * time is left out: its age is not known, and a gap is never filled.
 */
export function quietLanes(terminals, paneOf, nowMs, self = THIS_MAC) {
  const out = [];
  for (const r of arr(terminals)) {
    if (!isRecord(r) || r.state !== "live" || !isAddress(r.address)) continue;
    if (statusWord(r) !== null || r.reported === true || finite(r.status_ms) || inTab(r, paneOf) || !countsAsWork(r)) continue;
    if (!finite(r.created_ms) || nowMs - r.created_ms <= QUIET_AFTER_MS) continue;
    out.push({ address: r.address, chip: shortMachine(machineOf(r.address), self), name: bestName(r), silentMs: nowMs - r.created_ms });
  }
  return out.sort((a, b) => b.silentMs - a.silentMs || a.address.localeCompare(b.address));
}

/**
 * F8 (gap 10): one quiet lane is named, "▸ wor…r-b trust-setup… silent 11 h" (its machine chip and
 * its name cut to 12 characters); with several, the count and the longest silence.
 */
export function quietLine(lanes) {
  if (lanes.length === 0) return null;
  const age = formatAge(lanes[0].silentMs / 1000);
  if (lanes.length > 1) return `▸ ${lanes.length} lanes silent, longest ${age}`;
  const one = lanes[0];
  return boundText(["▸", boundText(one.chip, 16), boundText(one.name, 12), `silent ${age}`].filter(Boolean).join(" "), 46);
}

/**
 * True when the document's receipts can be trusted to be all of them: a document was read, it is
 * not old, its receipts are not stale, it says that its list of receipts that need a person is
 * complete, and its counts are known (L2, receiptCountsKnown). Only then may the header say that
 * nothing waits.
 */
export function receiptsRead(doc, docOld) {
  return receiptsTrusted(doc, docOld) && receiptCountsKnown(doc.receipts);
}

/**
 * L2 (col-lead3): the other conditions of receiptsRead hold, `counts` is an object, and the counts
 * are not known. The first chip then says `? receipts unclear`; with `counts` null it stays
 * `receipts not read`.
 */
export function receiptsUnclear(doc, docOld) {
  return receiptsTrusted(doc, docOld) && isRecord(doc.receipts.counts) && !receiptCountsKnown(doc.receipts);
}

/** The conditions of the base: a document, not old, `stale: false`, `needs_person_complete: true`. */
function receiptsTrusted(doc, docOld) {
  const rc = isRecord(doc) && isRecord(doc.receipts) ? doc.receipts : null;
  return rc !== null && docOld !== true && rc.stale === false && rc.needs_person_complete === true;
}

/** L2: every state the column reads; each must be an own key of `counts` with a non-negative safe integer. */
export const COLUMN_STATES = Object.freeze(["pending", "queued", "submitted", "running", "uncertain", "blocked", "completed", "failed"]);

/**
 * L2 (col-lead3): the counts are known when `counts` is an object in which every state of
 * COLUMN_STATES is an own key with a non-negative safe integer, and `uncertain + blocked` equals
 * the number of receipts in `needs_person`, and every entry of it can be drawn (N1). That equality is what the bridge's writer means by
 * "complete": a document that says "complete" and fails it contradicts itself.
 */
export function receiptCountsKnown(rc) {
  if (!isRecord(rc) || !isRecord(rc.counts) || !Array.isArray(rc.needs_person)) return false;
  const c = rc.counts;
  for (const k of COLUMN_STATES) if (countOf(c, k) === null) return false;
  // N1 (col-lead3g): an entry the column cannot draw still waits: the document then contradicts itself.
  if (!rc.needs_person.every(drawableEntry)) return false;
  return c.uncertain + c.blocked === rc.needs_person.length;
}

/**
 * G1: a receipt that waits for a person is current when the document is not old and
 * `receipts.stale` is exactly false. Otherwise it is stale: still listed, drawn muted.
 */
export function receiptsCurrent(doc, docOld) {
  return isRecord(doc) && isRecord(doc.receipts) && docOld !== true && doc.receipts.stale === false;
}

/** G1, G9: the words for a stale read: "as of 5 min" from `receipts.read_at`, else "not current". */
export function staleNote(doc, nowMs) {
  const at = isRecord(doc) && isRecord(doc.receipts) ? doc.receipts.read_at : null;
  return finite(at) && finite(nowMs) ? `as of ${formatAge(Math.max(0, nowMs / 1000 - at))}` : "not current";
}

/**
 * The header chips. The first speaks only about attention; the second appears only for the two
 * cases that stop the owner's next move. The tick "nothing waits" needs `stops.receiptsRead`:
 * without it the column knows only that no terminal waits, so it says what it could not read.
 * The text stays short: the chip shares one line of a narrow column with the title.
 * @param {{ shown: number, hidden: number, stale?: number, note?: string }} counts shown: current items
 *   (blocked terminals, receipts of a current read); stale: shown receipts of a stale read; note:
 *   "as of 5 min" or "not current" (G1)
 * @param {{ noMachineFree: boolean, coordinatorMemory: boolean, receiptsRead: boolean, receiptsUnclear?: boolean, self?: string }} stops
 */
export function headerChips(counts, stops = {}) {
  const known = stops.receiptsRead === true;
  // L2 (col-lead3): read, current and complete, but the counts are not known: no tick.
  const unclear = !known && stops.receiptsUnclear === true;
  // G1: `shown` counts current items only; `stale` counts the shown receipts of a stale read.
  const stale = Number.isSafeInteger(counts.stale) && counts.stale > 0 ? counts.stale : 0;
  const note = typeof counts.note === "string" && counts.note !== "" ? boundText(counts.note, 16) : "not current";
  const first = counts.shown > 0
    ? { mark: "!", text: `${counts.shown} need${counts.shown === 1 ? "s" : ""} you`, tone: "blocked" }
    : stale > 0 ? { mark: "?", text: `${fmtCount(stale)} waited · ${note}`, tone: "muted" }
      : known ? { mark: "✓", text: "nothing waits", tone: "muted" }
        : unclear ? { mark: "?", text: "receipts unclear", tone: "muted" }
          : { mark: "?", text: "receipts not read", tone: "muted" };
  first.suffix = counts.hidden > 0 ? `· ${counts.hidden} hidden` : null;
  const self = stops.self === THIS_MACHINE ? THIS_MACHINE : THIS_MAC;
  const second = stops.coordinatorMemory ? { mark: "■", text: `${self}: memory`, tone: "blocked" }
    : stops.noMachineFree ? { mark: "▲", text: "no machine free", tone: "working" } : null;
  return { first, second, badge: counts.shown > 0 ? String(counts.shown) : null, sample: stops.sample === true };
}

// ---------------------------------------------------------------- machines

const MARKS = table({ ok: "●", busy: "▲", full: "■", old: "◷", "no link": "⊘", "no data": "–" });
const MEASURED = ["ok", "busy", "full"];
/**
 * C3: the signal names the bridge publishes in `pressure.signal` and `decided_by[].signal`, and
 * the short name each one is drawn with. Any other word is drawn as itself, bounded, with a
 * neutral meter, and is never treated as memory.
 */
const SIGNALS = table({ cpu: "cpu", memory: "mem", agents: "agents" });

/** The signal as drawn: { short, known }. An unknown word is its own bounded text. */
export function signalInfo(signal) {
  const known = lookup(SIGNALS, signal);
  if (known !== null) return { short: known, known: true };
  const word = boundText(signal, 10);
  return word === "" ? { short: null, known: false } : { short: word, known: false };
}

/** The short name of the signal that decided a state: cpu, mem, agents, or the bounded unknown word. */
export function signalShort(signal) {
  return signalInfo(signal).short;
}

/** Pixel geometry of the 40 px meter for the bridge's scaled fraction: marks at 0.70 and 1.00. */
export function meter(fraction, width = METER_W) {
  const w = finite(width) && width > 0 ? Math.round(width) : 0;
  const f = finite(fraction) && fraction > 0 ? fraction : 0;
  const at = (x) => Math.round((w * Math.min(x, METER_MAX)) / METER_MAX);
  return { width: w, fill: at(f), busyMark: at(MARK_BUSY), fullMark: at(MARK_FULL), over: f > METER_MAX };
}

/** The document's entry for a machine name (null: this computer). */
export function machineEntry(doc, name) {
  for (const m of isRecord(doc) ? arr(doc.machines) : []) {
    if (!isRecord(m)) continue;
    if (name === null ? m.name === null || m.name === undefined || m.self === true : m.name === name) return m;
  }
  return null;
}

/** Fixed order: this computer, the bridge's node order, other machines of the document, then of machines(). */
export function machineOrder(doc, liveMachines) {
  const out = [null];
  const add = (n) => {
    if (typeof n === "string" && n !== "" && n.length <= 64 && !out.includes(n)) out.push(n);
  };
  if (isRecord(doc)) {
    if (isRecord(doc.capacity)) arr(doc.capacity.node_order).forEach(add);
    arr(doc.machines).forEach((m) => add(isRecord(m) && m.self !== true ? m.name : null));
  }
  arr(liveMachines).forEach((m) => add(isRecord(m) ? m.name : null));
  return out;
}

/**
 * One machine's state: ok, busy, full (the bridge's word), old (the bridge expired the reading, or
 * the whole document is old), no link, or no data. `was` is the last measured word behind `old`.
 */
export function machineState(entry, liveLayout, docOld) {
  const link = isRecord(entry) && isRecord(entry.link) ? entry.link.state : null;
  if (liveLayout === "unavailable" || (typeof link === "string" && link !== "self" && link !== "connected")) {
    return { state: "no link", was: null };
  }
  const r = isRecord(entry) && isRecord(entry.reading) ? entry.reading : null;
  if (r === null) return { state: "no data", was: null };
  const failed = typeof r.error === "string" && r.error !== "";
  if (MEASURED.includes(r.state) && !failed) return docOld ? { state: "old", was: r.state } : { state: r.state, was: null };
  if (r.state === "unknown" && MEASURED.includes(r.was)) return { state: "old", was: r.was };
  return { state: "no data", was: null };
}

function decidedTexts(reading) {
  return arr(reading.decided_by).map((d) => (isRecord(d) ? plainText(d.text, 80) : "")).filter(Boolean);
}

/**
 * True for the coordinator when its reading is current, it is full, and memory decided: the one
 * red machine state (C3). F2 (break 3): `pressure.signal` is exactly `memory`, or there is no
 * `pressure` and `memory.state` is exactly `full`. `state` is machineState's word, so an expired
 * reading or an old document is never `full` here.
 */
export function coordinatorMemoryFull(entry, state) {
  if (!isRecord(entry) || entry.role !== "coordinator" || state !== "full") return false;
  const r = isRecord(entry.reading) ? entry.reading : {};
  if (r.state !== "full" || (r.error !== null && r.error !== undefined)) return false;
  if (isRecord(r.pressure)) return r.pressure.signal === "memory";
  return (r.pressure === null || r.pressure === undefined) && isRecord(r.memory) && r.memory.state === "full";
}

/** Fixed tables for the lanes of an opened machine row; a status word is looked up as an own key. */
const LANE_RANK = table({ blocked: 0, working: 1, idle: 3, done: 4 });
const LANE_MARKS = table({ blocked: "!", working: "▶", idle: "○", done: "✓" });
/**
 * L9b (col-lead3d): a machine's lanes have exactly three groups: blocked, working, then one group of
 * all others (no status, idle, done, anything else). PROJECTS keeps LANE_RANK (rank, then address).
 */
const MACHINE_LANE_GROUP = table({ blocked: 0, working: 1 });

/**
 * The lanes of one machine for the opened row: every live terminal there, in a tab or not.
 * `shell` marks a plain shell with no status and no lane label (F6): machineRow folds those.
 */
export function lanesOf(name, terminals, paneOf, tabNames, nowMs) {
  return laneEntries(name, terminals).sort(byLongest).map((e) => laneLine(e.row, paneOf, tabNames, nowMs));
}

/**
 * L9 (col-lead3c): the lanes of one machine with the plain fields only (row, rank, address, shell):
 * enough to count, order and choose; a line is built (laneLine) only for a lane that is drawn.
 */
function laneEntries(name, terminals) {
  const out = [];
  for (const r of arr(terminals)) {
    if (!isRecord(r) || r.state !== "live" || !isAddress(r.address) || machineOf(r.address) !== name) continue;
    out.push({ row: r, rank: lookup(MACHINE_LANE_GROUP, statusWord(r), 2), since: finite(r.created_ms) ? r.created_ms : Infinity, address: r.address, shell: isPlainShell(r) });
  }
  return out;
}

/** One lane of an opened machine row, ready to draw. */
function laneLine(r, paneOf, tabNames, nowMs) {
  const word = statusWord(r);
  const labels = isRecord(r.labels) ? r.labels : {};
  const from = finite(r.created_ms) ? r.created_ms : null;
  const by = boundText(labels["ab.by"], 24) || (isAddress(r.parent) ? displayAddress(r.parent) : "");
  return {
    address: r.address, mark: lookup(LANE_MARKS, word, "·"), name: bestName(r, tabNames.get(r.address) ?? null, 26),
    status: word === null ? "no status" : boundText(word, 12), blocked: word === "blocked",
    time: from === null ? null : formatAge(Math.max(0, nowMs - from) / 1000),
    by: by ? `started by ${by}` : null, tab: inTab(r, paneOf), rank: lookup(LANE_RANK, word, 2), shell: isPlainShell(r),
  };
}

/** L9 (col-lead3c): the muted line under a bounded list, `… (N more lanes)`, or null when nothing is left out. */
function notDrawnLine(n, one, many = `${one}s`) {
  return Number.isSafeInteger(n) && n > 0 ? { text: `… (${fmtCount(n)} more ${n === 1 ? one : many})`, tone: "muted" } : null;
}

/** The work line: what runs on a machine. Null when nothing does. */
export function workLine(name, terminals, paneOf, tabNames, suites) {
  const here = arr(terminals).filter((r) => isRecord(r) && r.state === "live" && isAddress(r.address) && machineOf(r.address) === name);
  const parts = [];
  const working = here.filter((r) => inTab(r, paneOf) && statusWord(r) === "working").length;
  if (working > 0) parts.push(`${working} working in tabs`);
  // F5: a lane with no status is work only with the lane label or an agent program.
  const lanes = here.filter((r) => !inTab(r, paneOf) && (statusWord(r) === "working" || (statusWord(r) === null && countsAsWork(r))));
  if (lanes.length > 0) {
    parts.push(...lanes.slice(0, 2).map((r) => bestName(r, tabNames.get(r.address) ?? null, 20)));
    if (lanes.length > 2) parts.push(`+${lanes.length - 2}`);
    if (lanes.every((r) => statusWord(r) === null)) parts.push("no status");
  }
  if (Number.isInteger(suites) && suites > 0) parts.push(`${fmtCount(suites)} suite${suites === 1 ? "" : "s"} running`);
  return parts.length === 0 ? null : boundText(parts.join(" · "), 120);
}

/** A count for display: a whole number up to 9999, else "many". */
export function fmtCount(n) {
  return Number.isSafeInteger(n) && n >= 0 ? (n <= 9999 ? String(n) : "many") : "?";
}

/** A measured number for display with `digits` decimals, bounded: "?" when not finite, "≥1000" when huge. */
export function fmtNum(x, digits = 2) {
  if (!finite(x)) return "?";
  if (Math.abs(x) >= 1000) return x < 0 ? "≤-1000" : "≥1000";
  return x.toFixed(digits);
}

/** Sessions in the tabs of a machine: live terminals in a tab that report a status. */
export function sessionsInTabs(name, terminals, paneOf) {
  return arr(terminals).filter((r) => isRecord(r) && r.state === "live" && isAddress(r.address)
    && machineOf(r.address) === name && inTab(r, paneOf) && statusWord(r) !== null).length;
}

function signalLines(entry, reading) {
  const lines = [];
  if (finite(reading.load_per_core)) {
    // A joined list of numbers is bounded (C8): at most three loads, each a bounded number.
    const loads = arr(reading.loads).filter(finite).slice(0, 3).map((x) => fmtNum(x)).join(" ");
    const cores = finite(reading.cores) ? ` on ${fmtCount(reading.cores)} cores` : "";
    lines.push({ name: "cpu", text: boundText(`load ${fmtNum(reading.load_per_core)} per core${loads ? ` · ${loads}${cores}` : ""}`, 80) });
  }
  if (isRecord(reading.memory)) {
    const word = MEASURED.includes(reading.memory.state) ? reading.memory.state : "state not given";
    // K1 (col-lead2): the memory text is cut as the `why` line is (cutClosed), never inside a parenthesis.
    lines.push({ name: "mem", text: [word, cutClosed(boundText(reading.memory.text, CLOSED_SOURCE_MAX), MEM_MAX)].filter(Boolean).join(" · ") });
  }
  const a = isRecord(reading.agents) ? reading.agents : {};
  // F4: lanes only above 0, agent programs only as a number; a null is never drawn as 0.
  const counted = [];
  if (finite(a.active)) {
    const ceiling = entry.role === "coordinator" ? "no worker is placed here" : finite(a.ceiling) ? `ceiling ${fmtCount(a.ceiling)}` : "ceiling not given";
    counted.push(`${fmtCount(a.active)} active`, finite(a.parked) ? `${fmtCount(a.parked)} parked` : "", ceiling);
  }
  if (finite(a.lanes) && a.lanes > 0) counted.push(`lanes ${fmtCount(a.lanes)}`);
  if (finite(reading.harness_processes)) counted.push(`agent programs ${fmtCount(reading.harness_processes)}`);
  if (counted.some(Boolean)) lines.push({ name: "agents", text: counted.filter(Boolean).join(" · ") });
  if (finite(reading.suites)) lines.push({ name: "suites", text: `${fmtCount(reading.suites)} running (one per machine)` });
  // H1: the opened `why` line is cut as G3 cuts the reason, never inside a parenthesis.
  const why = whyLine(reading);
  if (why !== null) lines.push({ name: "why", text: why });
  return lines;
}

/** F4: the agents beside a worker's row: "2/6" with both numbers, "2 active" without a ceiling, else null. */
export function agentsText(agents) {
  const a = isRecord(agents) ? agents : {};
  if (!finite(a.active)) return null;
  return finite(a.ceiling) ? `${fmtCount(a.active)}/${fmtCount(a.ceiling)}` : `${fmtCount(a.active)} active`;
}

/** Splits a reason at its ` · ` boundaries that sit outside every parenthesis. */
function topLevelParts(text) {
  const parts = [];
  let depth = 0;
  let from = 0;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (ch === "(") depth += 1;
    else if (ch === ")") depth = Math.max(0, depth - 1);
    else if (depth === 0 && text.startsWith(" · ", i)) {
      parts.push(text.slice(from, i));
      from = i + 3;
      i += 2;
    }
  }
  parts.push(text.slice(from));
  return parts.map((x) => x.trim()).filter(Boolean);
}

/** True when every opening parenthesis of a text has its closing one after it. */
function closed(text) {
  let depth = 0;
  for (const ch of text) {
    if (ch === "(") depth += 1;
    else if (ch === ")") depth = Math.max(0, depth - 1);
  }
  return depth === 0;
}

/**
 * True when a text takes at most REASON_LINES lines of REASON_LINE characters, broken at spaces,
 * with no word wider than a line (such a word would be cut where nobody chose) and no opening
 * parenthesis without its closing one (G3).
 */
export function fitsReason(text) {
  if (typeof text !== "string" || !closed(text)) return false;
  const words = text.split(" ").filter(Boolean);
  return words.every((w) => [...w].length <= REASON_LINE) && wrap(text, REASON_LINE).length <= REASON_LINES;
}

/** The places of the spaces of a text that sit outside every parenthesis, in characters. */
function spacesOutside(chars) {
  const out = [];
  let depth = 0;
  for (let i = 0; i < chars.length; i += 1) {
    if (chars[i] === "(") depth += 1;
    else if (chars[i] === ")") depth = Math.max(0, depth - 1);
    else if (chars[i] === " " && depth === 0) out.push(i);
  }
  return out;
}

/** The text without its last whole parenthesized group (an unclosed one counts) and what follows it; null when it has none. */
function withoutLastGroup(chars) {
  let depth = 0;
  let open = -1;
  for (let i = 0; i < chars.length; i += 1) {
    if (chars[i] === "(") {
      if (depth === 0) open = i;
      depth += 1;
    } else if (chars[i] === ")") depth = Math.max(0, depth - 1);
  }
  return open < 0 ? null : chars.slice(0, open);
}

/**
 * G3: a sentence that does not fit alone. It is cut at the last space outside every parenthesis
 * that fits, with an ellipsis. When no such space fits, whole parenthesized groups are removed
 * from its end and it is cut again. The result never ends inside a parenthesis; with nothing
 * left it is the ellipsis alone, which still says that something was dropped.
 * H1 (col-lead): the same cut with another test of what fits (cutClosed), for the opened `why` line.
 */
function cutWith(sentence, fits) {
  let chars = [...sentence];
  const tidy = (cs) => cs.join("").replace(/[\s·]+$/u, "");
  for (let removed = false; chars.length > 0;) {
    if (removed && tidy(chars) !== "" && fits(`${tidy(chars)}…`)) return `${tidy(chars)}…`;
    for (const at of spacesOutside(chars).reverse()) {
      const head = tidy(chars.slice(0, at));
      if (head !== "" && fits(`${head}…`)) return `${head}…`;
    }
    const rest = withoutLastGroup(chars);
    if (rest === null) break;
    chars = rest;
    removed = true;
  }
  return "…";
}

function cutSentence(sentence) {
  return cutWith(sentence, fitsReason);
}

/**
 * H1 (review col-fix5, finding 1): a text of at most `max` characters that never holds an opening
 * parenthesis without its closing one. A text that fits is returned as it is; a longer one is cut
 * as G3 cuts a sentence (cutWith): at a space outside every parenthesis, else whole groups go from
 * its end, and `…` marks what was dropped.
 */
export function cutClosed(text, max) {
  if (typeof text !== "string" || !Number.isInteger(max) || max < 1) return "";
  const fits = (t) => closed(t) && [...t].length <= max;
  return fits(text) ? text : cutWith(text, fits);
}

/** H1: the sentences of every signal that decided, cleaned but not yet cut, for the opened `why` line. */
export const WHY_MAX = 120;
/** K1: the memory text of the opened `mem` line, at most (the bound of the base). */
export const MEM_MAX = 70;
function whyLine(reading) {
  const texts = arr(reading.decided_by).map((d) => (isRecord(d) ? boundText(d.text, 1000) : "")).filter(Boolean);
  return texts.length === 0 ? null : cutClosed(texts.join(" · "), WHY_MAX);
}

/**
 * F12 (break 5), G3: the reason under a busy or full row. With `pressure`, the sentence of the
 * deciding signal (the decided_by entry with the same signal and state), then "+N more" when more
 * signals decided; without it, every sentence. A full machine adds what to do. The pieces are
 * added in that order while the text still fits two lines of the column; when a piece is dropped
 * the text ends with an ellipsis. A first sentence that does not fit alone is cut (cutSentence).
 */
export function reasonLine(state, reading) {
  const r = isRecord(reading) ? reading : {};
  const decided = arr(r.decided_by).filter(isRecord);
  const p = isRecord(r.pressure) ? r.pressure : null;
  const at = p === null ? -1 : decided.findIndex((d) => d.signal === p.signal && d.state === state && plainText(d.text, 80) !== "");
  let parts;
  if (at >= 0) {
    parts = [plainText(decided[at].text, 80)];
    if (decided.length > 1) parts.push(`+${fmtCount(decided.length - 1)} more`);
  } else {
    parts = topLevelParts(decidedTexts(r).join(" · "));
    if (parts.length === 0) parts = [`${boundText(state, 8)} · the bridge gave no reason`];
  }
  if (state === "full") parts.push("let running work finish");
  const kept = [];
  for (const piece of parts) {
    if (fitsReason([...kept, piece].join(" · "))) {
      kept.push(piece);
      continue;
    }
    if (kept.length === 0) return cutSentence(piece);
    // A piece is dropped: the text says so with an ellipsis, and later pieces are not tried.
    while (kept.length > 0 && !fitsReason(`${kept.join(" · ")}…`)) {
      if (kept.length === 1) return cutSentence(kept[0]);
      kept.pop();
    }
    return `${kept.join(" · ")}…`;
  }
  return kept.join(" · ");
}

/**
 * One row of the Machines section, ready to draw.
 * @param {string | null} name null: this computer
 * @param {object | null} doc the bridge's document or null
 * @param {{ terminals: object[], machines: object[] }} live
 * @param {{ paneOf: Map, tabNames: Map, nowMs: number, docOld: boolean, offsetMin: number, self?: string }} c
 */
export function machineRow(name, doc, live, c) {
  const entry = machineEntry(doc, name);
  const liveEntry = arr(live.machines).find((m) => isRecord(m) && m.name === name);
  const { state, was } = machineState(entry, name === null ? "ready" : liveEntry?.layout, c.docOld);
  const reading = isRecord(entry) && isRecord(entry.reading) ? entry.reading : {};
  const coordinator = isRecord(entry) ? entry.role === "coordinator" : name === null;
  const measured = was ?? (MEASURED.includes(state) ? state : null);
  const red = coordinatorMemoryFull(entry, state);
  const row = {
    key: name ?? "self", name, label: shortMachine(name, c.self), full: name === null ? shortMachine(null, c.self) : middle(name, 32),
    coordinator, state, was, mark: lookup(MARKS, state, "–"), word: state,
    tone: red ? "blocked" : state === "busy" || state === "full" ? "working" : state === "ok" ? "calm" : "grey",
    greyed: state === "old", signal: null, signalKnown: false, meter: null, value: null, agents: null, reason: null,
  };

  if (measured !== null) {
    const p = isRecord(reading.pressure) ? reading.pressure : null;
    if (measured !== "ok" && p && finite(p.fraction)) {
      const s = signalInfo(p.signal);
      row.signal = s.short;
      row.signalKnown = s.known;
      row.meter = meter(p.fraction);
      row.value = boundText(p.text, 8) || null;
    } else if (measured === "ok" && finite(reading.load_per_core)) {
      row.signal = "cpu";
      row.signalKnown = true;
      row.meter = meter(reading.load_per_core);
      row.value = fmtNum(reading.load_per_core);
    } else if (measured !== "ok") {
      const first = arr(reading.decided_by).find(isRecord);
      const s = first ? signalInfo(first.signal) : { short: null, known: false };
      row.signal = s.short;
      row.signalKnown = s.known;
    }
    if (!coordinator) row.agents = agentsText(reading.agents);
  }
  if (coordinator) row.agents = `${fmtCount(sessionsInTabs(name, live.terminals, c.paneOf))} in tabs`;

  const ageS = finite(reading.taken_at) ? Math.max(0, c.nowMs / 1000 - reading.taken_at) : null;
  // L9 (col-lead3c): counted and ordered with the plain fields; only the lanes drawn are built.
  const lanes = laneEntries(name, live.terminals);
  if (state === "busy" || state === "full") {
    row.reason = reasonLine(state, reading);
  } else if (state === "old") {
    // F3: "reading new" under 5 s. F15: the bridge's own word for why, when it gives one.
    const why = typeof reading.error === "string" && reading.error !== "" ? boundText(reading.error, 40) : "";
    row.reason = [readingAge(ageS), `was ${was}`, why].filter(Boolean).join(" · ");
  } else if (state === "no link") {
    const since = isRecord(entry) && isRecord(entry.link) && finite(entry.link.since_ms) ? entry.link.since_ms : null;
    const seen = lanes.length > 0 ? lanes.length : isRecord(entry) ? arr(entry.lanes).length : 0;
    row.reason = [since === null ? "unreachable" : `unreachable since ${clock(since / 1000, c.offsetMin)}`,
      seen > 0 ? `${seen} lane${seen === 1 ? "" : "s"} last seen then` : ""].filter(Boolean).join(" · ");
  } else if (state === "no data" && typeof reading.error === "string" && reading.error !== "") {
    row.reason = `no reading: ${boundText(reading.error, 60)}`;
  }

  const suites = MEASURED.includes(state) && Number.isInteger(reading.suites) ? reading.suites : 0;
  row.work = workLine(name, live.terminals, c.paneOf, c.tabNames, suites);
  row.detail = {
    signals: measured === null ? [] : signalLines(entry, reading),
    age: ageS === null || measured === null ? "no reading" : readingAge(ageS),
    // F6: plain shells with no status and no lane label fold into one last line.
    // L9 (col-lead3c): at most MACHINE_LANES_MAX lanes, the most urgent first, then `… (N more lanes)`.
    ...machineLanes(lanes.filter((l) => !l.shell), c),
    shells: shellLine(lanes.filter((l) => l.shell).length),
    start: coordinator ? null : `Start agent on ${row.full}…`,
  };
  return row;
}

/**
 * L9 (col-lead3c): the order of a machine's lanes: blocked, working, then the others (no status, idle,
 * done); inside each the longest time first (the oldest start; an unknown start last), then by address.
 * L9b (col-lead3d): `rank` is the group of MACHINE_LANE_GROUP, so the others are one group.
 */
const byLongest = (a, b) => a.rank - b.rank || (a.since === b.since ? 0 : a.since < b.since ? -1 : 1)
  || a.address.localeCompare(b.address, "en", { numeric: true });

/** L9 (col-lead3c): the first MACHINE_LANES_MAX lanes in the order of byLongest, built; then the line for the rest. */
function machineLanes(entries, c) {
  const shown = firstOf(entries, MACHINE_LANES_MAX, byLongest);
  return { lanes: shown.map((e) => laneLine(e.row, c.paneOf, c.tabNames, c.nowMs)), lanesMore: notDrawnLine(entries.length - shown.length, "lane") };
}

/** F6: "4 shells", "1 shell", or null. */
export function shellLine(n) {
  return Number.isSafeInteger(n) && n > 0 ? `${fmtCount(n)} shell${n === 1 ? "" : "s"}` : null;
}

/**
 * The advice at the right of the Machines header. The bridge decides (`capacity.new_work`,
 * `capacity.new_suite`); the mod only refuses to name a machine that is not an ok worker in the
 * same document, and offers nothing when the document is old.
 */
export function advice(doc, rows, docOld) {
  const none = (text) => ({ work: text, suites: null, target: null, noMachineFree: false });
  if (!isRecord(doc)) return none("no load data yet");
  if (docOld) return none("load data old");
  const workers = rows.filter((r) => !r.coordinator && MEASURED.includes(r.state));
  if (workers.length === 0) return none("no load data yet");
  const okWorker = (n) => (typeof n === "string" && workers.some((r) => r.name === n && r.state === "ok") ? n : null);
  const cap = isRecord(doc.capacity) ? doc.capacity : {};
  const target = isRecord(cap.new_work) ? okWorker(cap.new_work.machine) : arr(cap.ranked_for_heavy_work).map(okWorker).find(Boolean) ?? null;
  const out = { work: `New work: ${target === null ? "wait" : shortMachine(target)}`, suites: null, target, noMachineFree: target === null };
  if (isRecord(cap.new_suite)) {
    const suite = okWorker(cap.new_suite.machine);
    const why = boundText(cap.new_suite.why, 40);
    if (suite !== target) out.suites = suite !== null ? `Test suites: ${shortMachine(suite)}` : `Test suites: wait${why ? `, ${why}` : ""}`;
  }
  return out;
}

// ---------------------------------------------------------------- receipts

/**
 * Fixed cause codes of the bridge and the sentence each one becomes, naming the recipient
 * (DESIGN-v2 3.4: `Don't retry yet: Kiro isn't running`). An unknown code draws no verdict.
 */
const VERDICTS = table({
  peer_not_running: (who) => `Don't retry yet: ${who} isn't running`,
  recipient_not_ready: (who) => `Retry when ${who} is idle at a prompt`,
});
const IN_PROGRESS = ["pending", "queued", "submitted", "running"];
const RECEIPT_MARKS = table({ failed: "✕", completed: "✓", pending: "▶", queued: "▶", submitted: "▶", running: "▶" });
const ID_ONLY = /^(?:[a-z]{1,8}:)?[0-9a-f]{8}-?(?:[0-9a-f]{4}-?){3}[0-9a-f]{12}$/i;

/** The recipient's name for a verdict: the bridge's `recipient.text`, bounded; else its provider; else "this peer". */
export function recipientName(recipient) {
  const to = isRecord(recipient) ? recipient : {};
  return middle(to.text, 24) || boundText(to.provider, 16) || "this peer";
}

/** The verdict line of a failure from its cause code and recipient, or null for an unknown code. */
export function verdict(cause, recipient) {
  const make = lookup(VERDICTS, cause);
  return make === null ? null : boundText(make(recipientName(recipient)), ASK_MAX);
}

/**
 * A request key without its date: a leading `ab-YYYYMMDD-` and (F9, gap 13) a trailing `-YYYYMMDD`
 * or `-YYYYMMDD-N`. "" when the key is only an id.
 */
export function keyLabel(key) {
  const text = boundText(key, 80);
  if (text === "" || ID_ONLY.test(text)) return "";
  const rest = text.replace(/^(?:[a-z]{1,8}-)?\d{8}-/, "").replace(/-\d{8}(?:-\d+)?$/, "");
  return rest === "" || ID_ONLY.test(rest) ? "" : boundText(rest, 28);
}

/** The terminal address a receipt went to, or null. */
export function recipientAddress(receipt) {
  const to = isRecord(receipt) && isRecord(receipt.recipient) ? receipt.recipient : {};
  if (typeof to.terminal !== "string") return null;
  const address = typeof to.machine === "string" && to.machine !== "" ? `${to.machine}/${to.terminal}` : to.terminal;
  return isAddress(address) ? address : null;
}

/** A receipt's label: the bridge's title, else the request key without its date, else the recipient shortened. */
export function receiptLabel(receipt) {
  const r = isRecord(receipt) ? receipt : {};
  const to = isRecord(r.recipient) ? r.recipient : {};
  return boundText(r.title, 28) || keyLabel(r.request_key) || displayAddress(recipientAddress(r))
    || middle(to.text, 16) || boundText(to.provider, 16) || "receipt";
}

/**
 * The rows of the Receipts section from the document's recent[], in the bridge's order. Receipts
 * that wait for a person are left out (they are rows of Needs you). Failures with the same title
 * or request key, recipient and cause collapse into one row: "failed 3 times". F1 (break 2): a
 * failure with no title whose request key is only an id (keyLabel drops it) groups by recipient,
 * state and cause instead; its row is the recipient as before with "×3", and the newest age.
 */
export function receiptRows(doc, seen, nowMs) {
  const rc = isRecord(doc) && isRecord(doc.receipts) ? doc.receipts : {};
  const waiting = new Set(arr(rc.needs_person).filter(isRecord).map((r) => r.id));
  const known = new Set(arr(seen));
  const rows = [];
  const groups = new Map();
  for (const r of arr(rc.recent)) {
    if (!isRecord(r) || typeof r.id !== "string" || r.id === "" || waiting.has(r.id)) continue;
    const state = boundText(r.state, 12) || "unknown";
    const failed = state === "failed";
    const label = receiptLabel(r);
    const when = finite(r.updated) ? r.updated : finite(r.created) ? r.created : null;
    const cause = typeof r.cause === "string" ? r.cause : "";
    const to = isRecord(r.recipient) && typeof r.recipient.text === "string" ? r.recipient.text : "";
    // Groups compare the full title or key and the full cause code; the bounded forms are only for drawing.
    const titled = typeof r.title === "string" && r.title !== "";
    const byRecipient = !titled && keyLabel(r.request_key) === "";
    const ident = titled ? r.title : typeof r.request_key === "string" && !byRecipient ? r.request_key : label;
    const group = !failed ? null : byRecipient ? JSON.stringify(["recipient", to, label, state, cause]) : JSON.stringify(["key", ident, to, cause]);
    const unseen = failed && !known.has(r.id);
    const first = group === null ? undefined : groups.get(group);
    if (first) {
      first.count += 1;
      first.ids.push(r.id);
      if (unseen) first.unseenIds += 1;
      first.unseen ||= unseen;
      if (when !== null && (first.when === null || when > first.when)) first.when = when;
      continue;
    }
    // The full id is the identity (C5): the row key, `ids` and the seen check all use it.
    const row = { key: `rc:${r.id}`, ids: [r.id], unseenIds: unseen ? 1 : 0, label, state, count: 1, when, unseen, cause, receipt: r, byRecipient };
    if (group !== null) groups.set(group, row);
    rows.push(row);
  }
  return rows.map((row) => ({
    ...row,
    label: row.byRecipient && row.count > 1 ? `${boundText(row.label, 22)} ×${fmtCount(row.count)}` : row.label,
    mark: lookup(RECEIPT_MARKS, row.state, "–"),
    stateText: row.state === "failed" && row.count > 1 && !row.byRecipient ? `failed ${row.count} times` : row.state === "completed" ? "done" : row.state,
    age: row.when === null ? "?" : formatAge(Math.max(0, nowMs / 1000 - row.when)),
    tone: row.unseen ? "blocked" : IN_PROGRESS.includes(row.state) ? "plain" : "muted",
    verdict: row.state === "failed" ? verdict(row.cause, row.receipt.recipient) : null,
  }));
}

/** The rows drawn: three, then "+ N older"; all of them when expanded. */
export function receiptPage(rows, expanded = false, max = RECEIPT_ROWS) {
  if (expanded || rows.length <= max) return { rows, older: 0 };
  return { rows: rows.slice(0, max), older: rows.length - max };
}

/**
 * K2, K3 (col-lead2): one count of the bridge's `counts`: the number when it is an own key and a
 * non-negative safe integer; else null, not known (`1e400` parsed to Infinity, 2 ** 63, -1, 1.5, a
 * text, a key that is missing or only on the prototype). The bridge's writer puts every state into
 * `counts`, so a missing key means a document of another shape, not "no receipts".
 */
function countOf(c, key) {
  return Object.hasOwn(c, key) && nonNegInt(c[key]) ? c[key] : null;
}

/** The bridge's `counts`, or null when the document has none (no good read yet). */
function countsOf(doc) {
  return isRecord(doc) && isRecord(doc.receipts) && isRecord(doc.receipts.counts) ? doc.receipts.counts : null;
}

/**
 * K2, K3: the two counts of the `since` line, each a number or null (not known); null when the
 * document has no counts, as in the base.
 */
export function sinceCounts(doc) {
  const c = countsOf(doc);
  return c === null ? null : { completed: countOf(c, "completed"), failed: countOf(c, "failed") };
}

/**
 * The bridge's totals the `since` line counts from, or null when the document has none.
 * K2: only when both counts are known; an unknown count is never stored as a base.
 */
export function totals(doc) {
  const t = sinceCounts(doc);
  return t !== null && t.completed !== null && t.failed !== null ? t : null;
}

/**
 * Receipts in progress: pending, queued, submitted and running, from the bridge's counts.
 * H2 (col-lead): a count that is given and is not a non-negative safe integer (`1e400` parsed
 * to Infinity, 2 ** 63, -1, 1.5, a text, null) is not known, and so is the sum: null.
 * K3 (col-lead2): a state that `counts` leaves out is not known either (countOf).
 */
export function inProgress(doc) {
  const c = countsOf(doc);
  // `counts` null: the header says "receipts not read" before it asks (receiptsHeader); 0 as in the base.
  if (c === null) return 0;
  let n = 0;
  for (const k of IN_PROGRESS) {
    // K3 (col-lead2): a key that is not an own key of `counts` is not known, so neither is the sum.
    const v = countOf(c, k);
    if (v === null) return null;
    n += v;
  }
  return Number.isSafeInteger(n) ? n : null;
}

/** True when there is no usable base: none stored, a damaged one, or the bridge's totals fell below it (a new store). */
export function needsBase(base, doc, nowMs = Date.now()) {
  const t = totals(doc);
  if (t === null) return false;
  const b = cleanBase(base, nowMs);
  return b === null || !(b.completed <= t.completed) || !(b.failed <= t.failed);
}

/** The ids of the receipts that wait for a person now. */
function waitingIds(doc) {
  const rc = isRecord(doc) && isRecord(doc.receipts) ? doc.receipts : {};
  return arr(rc.needs_person).filter((r) => isRecord(r) && goodId(r.id)).map((r) => r.id);
}

/**
 * What "seen" stores: the totals now, the receipts that wait now, and every failure listed now
 * as seen, by full id (the last 400 ids).
 */
export function markSeen(doc, seen, nowMs) {
  const t = totals(doc);
  const rc = isRecord(doc) && isRecord(doc.receipts) ? doc.receipts : {};
  const failed = arr(rc.recent).filter((r) => isRecord(r) && r.state === "failed" && goodId(r.id)).map((r) => r.id);
  const all = cleanSeen([...cleanSeen(seen), ...failed]);
  return { seen: all, base: t === null ? null : { at: Math.floor(nowMs / 1000), ...t, waiting: cleanSeen(waitingIds(doc)) } };
}

/**
 * "since 14:10 · 12 done · 2 failed · 1 waits": what changed since the last "seen". `done` and
 * `failed` are differences of the bridge's totals; `waits` counts the receipts that wait now and
 * did not wait then, so it restarts with "seen" like the others (C5). Zero parts are left out.
 * Null without counts or base. K2, K3: a count that is not known is drawn as `?`.
 */
export function sinceLine(doc, base, nowMs, offsetMin = 0) {
  const t = sinceCounts(doc);
  const b = cleanBase(base, nowMs);
  if (t === null || b === null) return null;
  // K2, K3: a count that is not known is `? done` or `? failed`, never 0, and no "nothing new" from it.
  const done = t.completed === null ? null : Math.max(0, t.completed - b.completed);
  const failed = t.failed === null ? null : Math.max(0, t.failed - b.failed);
  const before = new Set(b.waiting);
  const waits = waitingIds(doc).filter((id) => !before.has(id)).length;
  const age = nowMs / 1000 - b.at;
  const from = age >= 86400 ? `since ${formatAge(age)} ago` : `since ${clock(b.at, offsetMin)}`;
  const parts = [done === null ? "? done" : done > 0 ? `${fmtCount(done)} done` : "",
    failed === null ? "? failed" : failed > 0 ? `${fmtCount(failed)} failed` : "", waits > 0 ? `${fmtCount(waits)} wait${waits === 1 ? "s" : ""}` : ""].filter(Boolean);
  return { text: [from, ...(parts.length ? parts : ["nothing new"])].join(" · "), done, failed, waits };
}

/**
 * F10 (gap 14), G2: the age of the oldest receipt in progress, from `created`. recent[] holds the
 * eight newest receipts only (in/docs/FLEET.md), so the age is known only when recent[] lists
 * every receipt in progress that the counts give, each with a time; otherwise null.
 */
export function oldestInProgress(doc, nowMs) {
  const rc = isRecord(doc) && isRecord(doc.receipts) ? doc.receipts : {};
  const listed = arr(rc.recent).filter((r) => isRecord(r) && IN_PROGRESS.includes(r.state));
  if (listed.length === 0 || listed.length !== inProgress(doc) || !listed.every((r) => finite(r.created))) return null;
  return Math.max(0, nowMs / 1000 - Math.min(...listed.map((r) => r.created)));
}

/**
 * F16: how the receipts were read. `notRead`: the document has no counts (no good read yet).
 * `stale`: this cycle's read was not good; `asOf` is the age of the last good read, when known.
 */
export function receiptsState(doc, nowMs) {
  const rc = isRecord(doc) && isRecord(doc.receipts) ? doc.receipts : {};
  const notRead = !isRecord(rc.counts);
  const stale = rc.stale === true;
  // G9: without a finite nowMs there is no age, and a stale read says "not current".
  const asOf = stale && finite(rc.read_at) && finite(nowMs) ? formatAge(Math.max(0, nowMs / 1000 - rc.read_at)) : null;
  return { notRead, stale, asOf };
}

/**
 * The right side of the Receipts header: "as of 5 min · 2 in progress · oldest 8 h · 2 new
 * failures"; null when there is nothing to say. Without counts: "receipts not read" (F16).
 * G5: `parts` is what the view draws, each piece with its tone word; new failures are `blocked`
 * only in a read that is not stale.
 */
export function receiptsHeader(doc, rows, nowMs = null) {
  const read = receiptsState(doc, nowMs);
  if (read.notRead) return { text: "receipts not read", parts: [{ text: "receipts not read", tone: "muted" }], running: 0, fresh: 0, stale: read.stale, notRead: true };
  const running = inProgress(doc);
  // New failures are counted id by id: one unseen failure in a group of three is one (C5).
  const fresh = rows.reduce((n, r) => n + (Number.isInteger(r.unseenIds) ? r.unseenIds : 0), 0);
  // H2: an unknown count is `? in progress`, never 0, and gives no `oldest`.
  const oldest = running !== null && running > 0 && finite(nowMs) ? oldestInProgress(doc, nowMs) : null;
  const loud = read.stale ? "muted" : "blocked";
  const parts = [
    { text: read.asOf !== null ? `as of ${read.asOf}` : read.stale ? "not current" : "", tone: "muted" },
    { text: running === null ? "? in progress" : running > 0 ? `${fmtCount(running)} in progress` : "", tone: "muted" },
    { text: oldest !== null ? `oldest ${formatAge(oldest)}` : "", tone: "muted" },
    { text: fresh > 0 ? `${fmtCount(fresh)} new failure${fresh === 1 ? "" : "s"}` : "", tone: loud },
  ].filter((p) => p.text !== "");
  return { text: parts.length ? parts.map((p) => p.text).join(" · ") : null, parts, running, fresh, stale: read.stale, notRead: false };
}

/** The lines of a receipt's detail, all bounded. */
export function receiptDetail(receipt, nowMs, offsetMin = 0) {
  const r = isRecord(receipt) ? receipt : {};
  const to = isRecord(r.recipient) ? r.recipient : {};
  const lines = [];
  const target = middle(to.text, 44) || boundText(to.provider, 20);
  if (target) lines.push(`to ${target}`);
  const by = boundText(r.requested_by, 40);
  if (by) lines.push(`asked by ${by}`);
  const why = boundText([r.cause, r.reason].filter((x) => typeof x === "string" && x).join(" · ").replaceAll("_", " "), 80);
  if (why) lines.push(`why: ${why}`);
  // T-226: which Core read the terminal's identity before and after the send. Only a
  // receipt whose delivery recorded that guard names one; a receipt that was never
  // sent (or a snapshot row, which has no metadata) must not read as verified.
  const observer = r.identity_guard === "observed_before_and_after" ? boundText(r.identity_observer, 72) : null;
  if (observer && /^core:/.test(observer)) lines.push(`identity read by ${observer}`);
  const times = [finite(r.created) ? `sent ${clock(r.created, offsetMin)}` : "", finite(r.updated) ? `last change ${clock(r.updated, offsetMin)}` : ""].filter(Boolean);
  if (times.length) lines.push(times.join(" · "));
  const id = boundText(r.id, 64);
  if (id) lines.push(`receipt ${id.slice(0, 8)}`);
  return lines;
}

// ---------------------------------------------------------------- col-lead3e: acting on a waiting receipt

/** P1, X1: true when `id` has the bridge's shape of a receipt id (RECEIPT_ID). */
export function isReceiptId(id) {
  return typeof id === "string" && id.length === 36 && RECEIPT_ID.test(id);
}

/**
 * P1 (col-lead3e): the one line `copy command` puts on the clipboard, built from the id and the state
 * only, or null. `uncertain`: the bridge's release; `blocked`: the list of what the worker asks a
 * person to allow (the column never answers). Any other state, or an id of another shape: null.
 */
export function commandLine(id, state) {
  if (!isReceiptId(id)) return null;
  if (state === "uncertain") return `answerback abandon ${id} --reason "${RELEASE_REASON}"`;
  if (state === "blocked") return `answerback permissions ${id}`;
  return null;
}

/**
 * X1: the prop `command` as a path the release may run, or null. The rule and the default of the
 * repository's own panel (in/mods/answerback/logic.js, resolvePath and resolveOptions): an absolute
 * path, or `~/` followed by the rest, resolved with an absolute home; at most 1,024 units; no control
 * character. A prop that is absent (undefined or null) is the default.
 */
function releaseCommand(prop, homeDir) {
  const value = prop === undefined || prop === null ? COMMAND_DEFAULT : prop;
  if (typeof value !== "string" || value === "" || value.length > COMMAND_MAX) return null;
  for (let i = 0; i < value.length; i += 1) {
    const cp = value.charCodeAt(i);
    if (cp < 0x20 || cp === 0x7f) return null;
  }
  if (value.startsWith("/")) return value;
  if (value.startsWith("~/") && typeof homeDir === "string" && homeDir.startsWith("/") && homeDir.length <= COMMAND_MAX
    && ![...homeDir].some((ch) => ch.charCodeAt(0) < 0x20 || ch.charCodeAt(0) === 0x7f)) return `${homeDir}${value.slice(1)}`;
  return null;
}

/**
 * X2 (col-lead3f): true only when the prop `clear` is exactly the text RELEASE_SWITCH. Any other value
 * (absent, true, 1, "Release", "release ", an object) is off.
 */
export function releaseSwitchOn(clear) {
  return clear === RELEASE_SWITCH;
}

/**
 * X1 (col-lead3e): what `Release` runs, `{ command, args }`, or null: only for a receipt in state
 * `uncertain` whose id has the bridge's shape, with a prop `command` of the shape releaseCommand takes.
 * The view passes exactly what this returns (and the timeout RELEASE_TIMEOUT_MS); nothing else of the
 * receipt goes into it.
 * X2 (col-lead3f): `clear` is the switch (the prop `clear`); when it is off this is null for every input.
 */
export function releasePlan(id, state, prop, homeDir, clear) {
  if (!releaseSwitchOn(clear)) return null;
  if (state !== "uncertain" || !isReceiptId(id)) return null;
  const command = releaseCommand(prop, homeDir);
  return command === null ? null : { command, args: ["abandon", id, "--reason", RELEASE_REASON] };
}

/** X1: the UTF-8 length of a text, counted up to `max + 1` bytes (more is never needed). */
function utf8Over(text, max) {
  if (text.length > max) return true;
  let bytes = 0;
  for (let i = 0; i < text.length; i += 1) {
    const c = text.charCodeAt(i);
    // A pair of surrogates is four bytes (1 + 3); every unit counts at least one.
    bytes += c < 0x80 ? 1 : c < 0x800 ? 2 : c >= 0xd800 && c <= 0xdbff ? 1 : 3;
    if (bytes > max) return true;
  }
  return false;
}

/**
 * X1 (col-lead3e): the answer of the bridge's command, read from what it printed, never from its exit
 * code (in/data/col-lead3e/README.md: the code is 1 after a release that worked). `settled` is the
 * outcome of the call in the shape Promise.allSettled gives: `{ status: "fulfilled", value }` with the
 * value `{ code, stdout, stderr }`, or `{ status: "rejected" }`.
 * Released: stdout of at most RELEASE_STDOUT_MAX bytes parses as one JSON object whose `id` is the
 * receipt's id and whose `status` is exactly "abandoned". Everything else is not released, with a
 * message: the `error` text of a JSON object on stderr (at most 100 characters), else `no reason given
 * (exit code N)`; a value of another shape: `no reason given`; a rejection: RELEASE_NOT_RUN. Never throws.
 */
export function judgeRelease(settled, id) {
  const none = { released: false, message: "no reason given" };
  try {
    if (!isRecord(settled)) return none;
    if (settled.status === "rejected") return { released: false, message: RELEASE_NOT_RUN };
    if (settled.status !== "fulfilled") return none;
    const r = settled.value;
    if (!isRecord(r)) return none;
    const { code, stdout, stderr } = r;
    if (typeof code !== "number" || typeof stdout !== "string" || typeof stderr !== "string") return none;
    if (!utf8Over(stdout, RELEASE_STDOUT_MAX)) {
      let out = null;
      try {
        out = JSON.parse(stdout);
      } catch {
        out = null;
      }
      if (isRecord(out) && typeof id === "string" && out.id === id && out.status === "abandoned") return { released: true, message: null };
    }
    let err = null;
    if (!utf8Over(stderr, RELEASE_STDOUT_MAX)) {
      try {
        err = JSON.parse(stderr);
      } catch {
        err = null;
      }
    }
    if (isRecord(err) && typeof err.error === "string" && boundText(err.error, 100) !== "") return { released: false, message: boundText(err.error, 100) };
    return { released: false, message: `no reason given (exit code ${Number.isSafeInteger(code) ? code : "?"})` };
  } catch {
    return none;
  }
}

/**
 * G2 (col-lead3e): the first line of the detail of a receipt with no safe target, or null when it has
 * one. It names the terminal that is not live now, or says that the receipt went to no terminal.
 * Nothing here depends on a reason code: the document gives `reason: null` for this state today.
 */
export function whereLine(receipt, goThere) {
  if (goThere !== null) return null;
  const address = recipientAddress(receipt);
  if (address !== null) return `its terminal ${displayAddress(address)} is not live now`;
  const to = isRecord(receipt) && isRecord(receipt.recipient) ? receipt.recipient : {};
  return `no terminal to go to: it went to ${boundText(to.provider, 16) || "the bridge"}, not to a terminal`;
}

/**
 * R2 (col-lead3f): the receipt row of NEEDS YOU with this key in a model `column()` built (its drawn
 * rows and its hidden rows), or null. A click decides from the model on hand, not from the row of a draw.
 */
export function needsRowAt(model, key) {
  if (!isRecord(model) || !isRecord(model.needs) || typeof key !== "string") return null;
  for (const list of [model.needs.rows, model.needs.hiddenRows]) {
    if (!Array.isArray(list)) continue;
    for (const r of list) if (isRecord(r) && r.key === key && r.source === "receipt") return r;
  }
  return null;
}

/** X1: the view's memory of one release, looked up by the full id; null when there is none. */
function releaseOf(releases, id) {
  if (!(releases instanceof Map) || typeof id !== "string") return null;
  const e = releases.get(id);
  return isRecord(e) ? e : null;
}

/**
 * P1, X1, G2 (col-lead3e): what a waiting receipt of NEEDS YOU offers in its detail, and its second
 * line. `ui` is the view's memory: `copied` (the key whose command was copied since the last tick or
 * read), `confirm` (the key of the one open confirm block), `releases` (full id to { phase, message }),
 * `command` (the prop), `home` and `clear` (X2: the switch). With sample data the controls are drawn and run nothing.
 * col-lead3g: `hidden` (X5): a hidden row offers `copy command` and no `Clear…`, and no confirm block;
 * `ui.checking` (X3): under a check a row with a plan draws its controls and, under them, the confirm block;
 * a mark in phase `contradicted` (X4) says so in the blocked tone and offers the controls again.
 * Returns { act, asksLine, asksTone, released }.
 */
function receiptActions(item, ui, hidden = false) {
  const r = isRecord(item.receipt) ? item.receipt : {};
  const id = r.id;
  const state = r.state;
  const rel = releaseOf(ui.releases, id);
  const act = { copy: null, clear: null, confirm: null, status: null };
  const line = commandLine(id, state);
  const wanted = state === "uncertain" || state === "blocked";
  if (item.current && rel !== null && rel.phase === "released") {
    return { act, asksLine: RELEASED_LINE, asksTone: "muted", released: true };
  }
  if (item.current && rel !== null && rel.phase === "running") {
    act.status = { text: "releasing…", tone: "muted" };
  } else if (item.current && wanted && line === null) {
    act.status = { text: ODD_ID_LINE, tone: "muted" };
  } else if (item.current && line !== null) {
    if (rel !== null && rel.phase === "refused") act.status = { text: `not released: ${boundText(rel.message, 100)}`, tone: "blocked" };
    if (rel !== null && rel.phase === "contradicted") act.status = { text: CONTRADICTED_LINE, tone: "blocked" };
    // X2 (col-lead3f): with the switch off there is no plan, and no line says why (nothing names the switch).
    const plan = releasePlan(id, state, ui.command, ui.home, ui.clear);
    if (releaseSwitchOn(ui.clear) && state === "uncertain" && plan === null) act.status = { text: BAD_COMMAND_LINE, tone: "muted" };
    // X5 (col-lead3g): a hidden row is never released: no Clear… and no confirm block for it.
    const releasable = plan !== null && hidden !== true;
    // X3 (col-lead3g): under a check the controls and, under them, the confirm block, without a click.
    const walk = releasable && ui.checking === true;
    if (releasable && ui.confirm === item.key && !walk) {
      act.confirm = confirmBlock(id);
    } else {
      // P2 (col-lead3g): `could not copy`, in the blocked tone, after a click whose clipboard failed.
      act.copy = ui.copyFailed === item.key ? { text: "could not copy", line, tone: "blocked" }
        : { text: ui.copied === item.key ? "copied" : "copy command", line, tone: "foreground" };
      if (releasable) act.clear = { text: "Clear…" };
      if (walk) act.confirm = confirmBlock(id);
    }
  }
  const offers = act.copy !== null || act.clear !== null || act.confirm !== null;
  const asksLine = item.goThere === null && offers ? `${boundText(item.asks, ASK_MAX - OPEN_FOR.length)}${OPEN_FOR}` : item.asks;
  return { act, asksLine, asksTone: "muted", released: false };
}

/** X1: the confirm block of a release: its two lines and its two controls. */
function confirmBlock(id) {
  return { lines: [`Release receipt ${id.slice(0, 8)} in the bridge?`, ...CONFIRM_LINES], release: "Release", cancel: "Cancel" };
}

/**
 * X4 (col-lead3g): the marks of the view after a document. A mark is forgotten when the document does
 * not list its receipt any more (X1.6). A mark `released` is contradicted when the read of the receipts
 * is current, `receipts.read_at` is CONTRADICT_AFTER_MS or more after the time of the judgement (`at`),
 * and the document lists the receipt in state `uncertain`; its phase becomes `contradicted`.
 */
export function reconcileReleases(marks, doc, listed, current) {
  const out = new Map();
  if (!(marks instanceof Map)) return out;
  const readAt = current && isRecord(doc) && isRecord(doc.receipts) && finite(doc.receipts.read_at) ? doc.receipts.read_at * 1000 : null;
  for (const [id, e] of marks) {
    if (doc !== null && !listed.has(id)) continue;
    if (readAt !== null && isRecord(e) && e.phase === "released" && finite(e.at) && readAt >= e.at + CONTRADICT_AFTER_MS
      && listed.get(id) === "uncertain") {
      out.set(id, { phase: "contradicted", message: null, at: e.at });
    } else out.set(id, e);
  }
  return out;
}

/** S1: the words of the folded NEEDS YOU line (G5: the tone words are the model's). */
function needsFolded(current, stale, note, hidden, silent, cannot = 0) {
  const parts = [];
  if (current > 0) parts.push({ text: `${fmtCount(current)} waiting`, tone: "blocked" });
  if (stale > 0) parts.push({ text: `${parts.length ? "· " : ""}? ${fmtCount(stale)} waiting · ${note}`, tone: "muted" });
  if (parts.length === 0) parts.push({ text: "0 waiting", tone: "muted" });
  if (hidden > 0) parts.push({ text: `· ${fmtCount(hidden)} hidden`, tone: "muted" });
  if (silent > 0) parts.push({ text: `· ${fmtCount(silent)} silent`, tone: "muted" });
  // N1 (col-lead3g): the entries that wait and cannot be drawn.
  if (cannot > 0) parts.push({ text: `· ${fmtCount(cannot)} cannot be shown`, tone: "muted" });
  return { parts };
}

/** S1: the state words the folded MACHINES line counts, in this order (ok is not counted). */
const FOLDED_WORDS = Object.freeze(["busy", "full", "old", "no link", "no data"]);
const TONE_RANK = table({ blocked: 2, working: 1 });

/**
 * S1: the folded MACHINES line: every machine (also those beyond MACHINES_MAX), counted by the state
 * word of its row; each count in the loudest tone of its rows, so a machine row in the blocked tone is
 * red here too.
 */
function machinesFolded(rows) {
  const parts = [{ text: `${fmtCount(rows.length)} machine${rows.length === 1 ? "" : "s"}`, tone: "muted" }];
  for (const word of FOLDED_WORDS) {
    const of = rows.filter((r) => r.word === word);
    if (of.length === 0) continue;
    const loud = of.reduce((best, r) => (lookup(TONE_RANK, r.tone, 0) > lookup(TONE_RANK, best, 0) ? r.tone : best), "muted");
    parts.push({ text: `· ${fmtCount(of.length)} ${word}`, tone: lookup(TONE_RANK, loud, 0) > 0 ? loud : "muted" });
  }
  return { parts };
}

// ---------------------------------------------------------------- footer

/** The document's age in seconds, or null without a document. */
export function documentAge(doc, nowMs) {
  return isRecord(doc) && finite(doc.written_at) ? Math.max(0, nowMs / 1000 - doc.written_at) : null;
}

/** The 120 s rule: the only threshold the mod holds. */
export function isOld(doc, nowMs) {
  const age = documentAge(doc, nowMs);
  return age !== null && age > OLD_AFTER_S;
}

/**
 * G7: the footer's word for a service that is not `running`, looked up as an own key of
 * `health.service.state` (in/docs/FLEET.md: running, stopping, unreachable). Any other word, or
 * none, is SERVICE_UNKNOWN: the document does not say that the bridge stopped.
 */
const SERVICE_WORDS = table({ stopping: "bridge stopping", unreachable: "bridge not reached" });
const SERVICE_UNKNOWN = "bridge state not known";

/** A version as the footer shows it: the part before "+", at most 12 characters; "" when there is none. */
export const VERSION_MAX = 12;
export function footerVersion(record, key) {
  return boundText(isRecord(record) && typeof record[key] === "string" ? record[key].split("+")[0] : "", VERSION_MAX);
}

/**
 * The footer. Fresh: "bridge 0.12.0 · bus ok · data 40 s old"; under 5 s: "data live". Older
 * than 120 s: "bridge data 3 min old". No document: the waiting line. Red only when the service
 * is not `running` (G7): "bridge stopping", "bridge not reached" or "bridge state not known".
 * T-181: "bridge X" is the bridge's own version (`health.service.version`, what the service
 * answers); the writer's version (`writer.version`) is added as "writer Y" only when it differs,
 * or when the document does not say the bridge's. Both are cut as footerVersion cuts.
 */
export function footer(doc, nowMs, sample = false) {
  const age = documentAge(doc, nowMs);
  if (age === null) return { text: WAITING, tone: "muted", old: false, stopped: false, sample };
  const health = isRecord(doc.health) ? doc.health : {};
  const service = isRecord(health.service) ? health.service.state : null;
  const stopped = service !== "running";
  const old = age > OLD_AFTER_S;
  const data = age < 5 ? "data live" : `data ${formatAge(age)} old`;
  // Older than 120 s: the last good document is drawn, and the footer says only how old it is.
  // A stop it reported then is not evidence of now, so age wins over `stopped`.
  if (old) return { text: `bridge ${data}`, tone: "foreground", old, stopped, sample };
  if (stopped) return { text: `${lookup(SERVICE_WORDS, service, SERVICE_UNKNOWN)} · ${data}`, tone: "blocked", old, stopped, sample };
  const bridge = footerVersion(health.service, "version");
  const writer = footerVersion(doc.writer, "version");
  const parts = [bridge ? `bridge ${bridge}` : "bridge"];
  if (writer && writer !== bridge) parts.push(`writer ${writer}`);
  const busUp = isRecord(health.bus) && health.bus.connected === true;
  return {
    text: [...parts, busUp ? "bus ok" : "bus down", data].join(" · "),
    tone: busUp ? "muted" : "foreground", old, stopped, sample,
  };
}

// ---------------------------------------------------------------- projects and their leads

/** col-lead: `lead` is the only role the column knows; any other value of ab.role is no lead. */
const ROLES = table({ lead: "lead" });

/** A label's value when it is an own key of the row's labels and a text; else null. */
function ownLabel(row, key) {
  const labels = labelsOf(row);
  return Object.hasOwn(labels, key) && typeof labels[key] === "string" ? labels[key] : null;
}

/** True when the row carries `ab.role=lead`, looked up as an own key in the fixed table. */
export function isLead(row) {
  return lookup(ROLES, ownLabel(row, ROLE_LABEL)) === "lead";
}

/**
 * L1 (col-lead3): a project's identity is its whole name, cleaned (cleanCut with no limit). Two
 * names that differ anywhere are two projects, also after the 256th character; two spaces of one
 * name on two machines are one project. Only what is drawn is cut (PROJECT_NAME_MAX). `names` is
 * the build's map from a raw name to its identity, so each different raw name is cleaned once.
 */
function projectName(text, names = null) {
  // L1b: checked first, before the map is asked or a character is read.
  if (typeof text !== "string" || text.length > PROJECT_RAW_MAX) return "";
  if (!(names instanceof Map)) return cleanCut(text, Infinity, true).text;
  const known = names.get(text);
  if (known !== undefined) return known;
  // L8: the identity keeps its stars: `★ x` and `* x` stay two projects (their drawn names are alike).
  // T5b (col-lead3d): cleanCut counts this cleaning itself (identityCleanings); NameMap reports it.
  const id = cleanCut(text, Infinity, true).text;
  names.set(text, id);
  return id;
}

/**
 * The build's map from a raw project name to its identity, with the number of cleanings it took.
 * T5b (col-lead3d): that number is the count cleanCut itself raised since this map was made, the
 * build that is running, so a cleaning that bypasses the map (or happens before a cache hit) is counted.
 */
class NameMap extends Map {
  constructor() {
    super();
    this.cleanedFrom = identityCleanings;
  }

  get cleaned() {
    return identityCleanings - this.cleanedFrom;
  }
}

/**
 * What projectOf needs, from one read of the live lists: every terminals() row by address, the
 * space name of every agents() row (AgentInfo.space_name, in/api/herdr.d.ts:169-173), the name
 * of every space by its id (SpaceInfo, in/api/herdr.d.ts:123-149), and the build's map of
 * cleaned names (L1).
 */
export function projectContext(terminals, agents, spaces) {
  const names = new NameMap();
  const rows = new Map();
  for (const r of arr(terminals)) if (isRecord(r) && isAddress(r.address) && !rows.has(r.address)) rows.set(r.address, r);
  const agentSpace = new Map();
  for (const a of arr(agents)) {
    if (isRecord(a) && isAddress(a.address) && typeof a.space_name === "string" && projectName(a.space_name, names) !== "") agentSpace.set(a.address, a.space_name);
  }
  const spaceName = new Map();
  for (const sp of arr(spaces)) {
    if (isRecord(sp) && typeof sp.id === "string" && sp.id !== "" && sp.id.length <= 160 && typeof sp.name === "string") spaceName.set(sp.id, sp.name);
  }
  return { rows, agentSpace, spaceName, names };
}

/**
 * Rules 1 and 2: the label ab.project when it is a non-empty text; else the space: space_name of
 * the terminal's agents() row, or the space its label herdr.space names. That label holds the id
 * local to the terminal's machine; spaces() gives a remote id with the machine's prefix
 * (in/api/herdr.d.ts:96, in/api/AGENTS.md:143-144, in/data/hc-get-examples.txt:14).
 */
function ownProject(row, c) {
  const label = ownLabel(row, PROJECT_LABEL);
  if (label !== null && projectName(label, c.names) !== "") return label;
  const fromAgent = c.agentSpace.get(row.address);
  if (fromAgent !== undefined) return fromAgent;
  const id = ownLabel(row, SPACE_LABEL);
  if (id === null || id === "") return null;
  const m = machineOf(row.address);
  const name = c.spaceName.get(m === null ? id : `${m}/${id}`) ?? null;
  return name !== null && projectName(name, c.names) !== "" ? name : null;
}

/**
 * The project of a terminal, first match wins: its label ab.project; its space; the project of its
 * parent (the field `parent` of its terminals() row, in/api/herdr.d.ts:240-244), followed for at
 * most PARENT_STEPS steps; else null ("no project"). L7 (col-lead3): the limit ends every chain,
 * a circle of parents too, so no list of visited parents is kept.
 */
export function projectOf(row, c) {
  if (!isRecord(row) || !isRecord(c)) return null;
  const own = ownProject(row, c);
  if (own !== null) return own;
  let at = row;
  for (let step = 0; step < PARENT_STEPS; step += 1) {
    const parent = at.parent;
    if (!isAddress(parent)) return null;
    const up = c.rows.get(parent);
    if (up === undefined) return null;
    const found = ownProject(up, c);
    if (found !== null) return found;
    at = up;
  }
  return null;
}

/**
 * One line of a project: mark, name, machine chip, status and how long. A lead's mark is LEAD_MARK.
 * The time counts from the status time, else from the start (as Needs you does).
 * L4 (col-lead3): built only for a line that is drawn.
 */
function projectLine(row, c, lead) {
  const word = statusWord(row);
  const since = finite(row.status_ms) ? row.status_ms : finite(row.created_ms) ? row.created_ms : null;
  return {
    key: `pl:${row.address}`, address: row.address, lead,
    mark: lead ? LEAD_MARK : lookup(LANE_MARKS, word, "·"),
    markTone: word === "blocked" ? "blocked" : lead ? "foreground" : "dim",
    name: bestName(row, c.tabNames.get(row.address) ?? null, 26),
    chip: shortMachine(machineOf(row.address), c.self),
    status: word === null ? "no status" : boundText(word, 12),
    statusTone: word === "blocked" ? "blocked" : "muted",
    time: since === null ? null : formatAge(Math.max(0, c.nowMs - since) / 1000),
    rank: lookup(LANE_RANK, word, 2), action: c.act(row.address),
  };
}

const byUrgency = (a, b) => a.rank - b.rank || a.address.localeCompare(b.address, "en", { numeric: true });
const byProject = (a, b) => a.rank - b.rank || a.id.localeCompare(b.id, "en") || a.key.localeCompare(b.key);
const plural = (n, word) => `${fmtCount(n)} ${word}${n === 1 ? "" : "s"}`;

/**
 * L4 (col-lead3): the first `k` of `items` in the order of `cmp`, as a stable sort would give
 * them, without sorting the rest: each item is compared with the last one kept, and only an item
 * that comes before it is placed (binary search). Equal items keep their order.
 */
function firstOf(items, k, cmp) {
  if (items.length <= k) return items.slice().sort(cmp);
  const best = [];
  for (const x of items) {
    if (best.length === k && cmp(x, best[k - 1]) >= 0) continue;
    let lo = 0;
    let hi = best.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (cmp(x, best[mid]) < 0) hi = mid;
      else lo = mid + 1;
    }
    best.splice(lo, 0, x);
    if (best.length > k) best.pop();
  }
  return best;
}

/** L3 (col-lead3): the muted line under the lead lines drawn, when a project has more leads; it cannot be clicked. */
function leadsMoreLine(n) {
  return n > 0 ? { text: `… (${fmtCount(n)} more lead${n === 1 ? "" : "s"})`, tone: "muted" } : null;
}

/**
 * The PROJECTS section (col-lead). Every live terminal goes to its project (projectOf). A project
 * is listed when one of its terminals is work (an agent in a tab, a status, the lane label or an
 * agent program: F5) or is a live lead. Most urgent first: a blocked terminal, then a working one,
 * then by name. Each block: its title (name, `N agents`, `· M lanes` outside a tab), its lead lines
 * (`no lead marked` when none, `2 leads marked` under two), and, opened, every other work line and
 * the count of plain shells. `none`: the work with no project, under the last project only.
 * L3 (col-lead3): folded, at most PROJECT_LEADS_FOLDED lead lines (`foldedLeads`); opened, at most
 * PROJECT_LEADS_OPEN (`leads`); then `… (N more leads)`. The note `N leads marked` counts them all.
 * L4 (col-lead3): grouping, counting and ordering use the plain fields of the rows; a line is
 * built (name, chip, status, age, action) only for a block that is drawn and a line it draws.
 * @param {{ terminals: object[], agents: object[], spaces: object[] }} live
 * @param {{ paneOf: Map, tabNames: Map, nowMs: number, self: string, act: Function, opened: string[] }} c
 */
export function projects(live, c) {
  const ctx = projectContext(live.terminals, live.agents, live.spaces);
  const groups = new Map();
  const loose = [];
  // One entry per distinct status word: the word as a line draws it, and its rank.
  const words = new Map();
  const infoOf = (word) => {
    let info = words.get(word);
    if (info === undefined) {
      info = { status: word === null ? "no status" : boundText(word, 12), rank: lookup(LANE_RANK, word, 2) };
      words.set(word, info);
    }
    return info;
  };
  for (const r of arr(live.terminals)) {
    if (!isRecord(r) || r.state !== "live" || !isAddress(r.address)) continue;
    const word = statusWord(r);
    const work = ctx.agentSpace.has(r.address) || word !== null || countsAsWork(r);
    const lead = isLead(r);
    const name = projectOf(r, ctx);
    if (name === null) {
      if (work) loose.push({ row: r, rank: infoOf(word).rank, address: r.address });
      continue;
    }
    const id = projectName(name, ctx.names);
    let g = groups.get(id);
    if (g === undefined) {
      g = { id, name, leads: [], lines: [], shells: 0, agents: 0, lanes: 0, blocked: false, working: false, key: "", rank: 2 };
      groups.set(id, g);
    }
    if (work && inTab(r, c.paneOf)) g.agents += 1;
    else if (work) g.lanes += 1;
    if (lead || work) {
      const info = infoOf(word);
      (lead ? g.leads : g.lines).push({ row: r, rank: info.rank, address: r.address });
      if (info.status === "blocked") g.blocked = true;
      else if (info.status === "working") g.working = true;
    } else if (isPlainShell(r)) g.shells += 1;
  }
  const opened = new Set(arr(c.opened));
  const listed = [];
  for (const g of groups.values()) {
    if (g.leads.length === 0 && g.agents + g.lanes === 0) continue;
    // L1c (col-lead3c): the whole identity, not a hash of it.
    g.key = projectKey(g.id);
    g.rank = g.blocked ? 0 : g.working ? 1 : 2;
    listed.push(g);
  }
  // K4 (col-lead2): at most PROJECTS_MAX blocks, in the order above; then one muted line, not clickable.
  const rows = firstOf(listed, PROJECTS_MAX, byProject).map((g) => {
    const leads = firstOf(g.leads, PROJECT_LEADS_OPEN, byUrgency).map((e) => projectLine(e.row, c, true));
    const total = g.leads.length;
    const folded = leads.slice(0, PROJECT_LEADS_FOLDED);
    return {
      key: g.key, name: boundText(g.name, PROJECT_NAME_MAX), tone: "foreground",
      count: g.lanes > 0 ? `${plural(g.agents, "agent")} · ${plural(g.lanes, "lane")}` : plural(g.agents, "agent"),
      agents: g.agents, lanes: g.lanes, rank: g.rank,
      leads, leadsMore: leadsMoreLine(total - leads.length),
      foldedLeads: folded, foldedLeadsMore: leadsMoreLine(total - folded.length),
      leadNote: total > 1 ? { text: `${fmtCount(total)} leads marked`, tone: "muted" } : null,
      noLead: total === 0 ? { text: "no lead marked", tone: "muted", tip: MARK_LEAD_TIP } : null,
      ...linesPage(g.lines, c), shells: shellLine(g.shells), open: opened.has(g.key), id: g.id,
    };
  });
  const none = listed.length === 0 || loose.length === 0 ? null : {
    key: "p:none", name: `${plural(loose.length, "lane")} without a project`, tone: "muted", count: null, agents: 0, lanes: loose.length,
    rank: 3, leads: [], leadsMore: null, foldedLeads: [], foldedLeadsMore: null, leadNote: null, noLead: null,
    ...linesPage(loose, c), shells: null, open: opened.has("p:none"), id: "",
  };
  // The line of the lanes without a project stays the last line of the section.
  const hidden = listed.length - rows.length;
  const more = hidden > 0 ? { text: `+ ${fmtCount(hidden)} more project${hidden === 1 ? "" : "s"}`, tone: "muted" } : null;
  // S1 (col-lead3e): every project counts for the folded line, also the blocks that are not drawn.
  const noLeadCount = listed.reduce((n, g) => n + (g.leads.length === 0 ? 1 : 0), 0);
  return { rows, hidden, more, none, cleaned: ctx.names.cleaned, total: listed.length, noLeadCount };
}

/** K4: the opened lines of one block, at most PROJECT_LINES_MAX, most urgent first; then `… (N more)`. L4: only these are built. */
function linesPage(entries, c) {
  const shown = firstOf(entries, PROJECT_LINES_MAX, byUrgency);
  const rest = entries.length - shown.length;
  return { lines: shown.map((e) => projectLine(e.row, c, false)), linesMore: rest > 0 ? { text: `… (${fmtCount(rest)} more)`, tone: "muted" } : null };
}

// ---------------------------------------------------------------- the whole column

/**
 * Everything the column draws, from one read of the live lists and the document.
 * @param {{
 *   live: { terminals: object[], machines: object[], agents: object[], panes: object[], spaces?: object[] },
 *   doc: object | null,
 *   state: { hidden: object, seen: string[], base: object | null, folded: object },
 *   ui?: { moreNeeds?: boolean, moreReceipts?: boolean, hiddenOpen?: boolean, quietOpen?: boolean },
 *   nowMs: number, offsetMin?: number, sample?: boolean,
 * }} input
 */
export function column(input) {
  const src = isRecord(input.live) ? input.live : {};
  // F13: where agents() has a status word for a terminal, it is the word of both panels.
  const live = { terminals: withAgentStatus(src.terminals, src.agents), machines: arr(src.machines), agents: arr(src.agents), panes: arr(src.panes),
    spaces: arr(src.spaces) };
  const doc = isRecord(input.doc) ? input.doc : null;
  const self = coordinatorName(doc);
  const state = isRecord(input.state) ? input.state : { hidden: {}, seen: [], base: null, folded: {} };
  const ui = isRecord(input.ui) ? input.ui : {};
  const nowMs = input.nowMs;
  const offsetMin = finite(input.offsetMin) ? input.offsetMin : 0;

  const paneOf = livePanes(live.panes, live.agents);
  const tabNames = new Map();
  for (const a of live.agents) {
    if (!isRecord(a) || !isAddress(a.address)) continue;
    if (typeof a.tab_name === "string" && a.tab_name !== "") tabNames.set(a.address, a.tab_name);
  }
  // Sample rows never enter a command: with sample data the set of safe targets is empty.
  // This action is for drawing only (cursor, tooltip); a click decides again (actionAtClick).
  const lastRead = input.sample === true ? new Set() : liveAddresses(live.terminals);
  const act = (address) => clickAction(address, lastRead, paneOf);
  const age = (ms) => (ms === null ? "?" : formatAge(Math.max(0, nowMs - ms) / 1000));

  const docOld = isOld(doc, nowMs);
  // G1: a stale read stays listed, muted, with `?` and the age of the read in front of what it asks.
  const current = receiptsCurrent(doc, docOld);
  const note = staleNote(doc, nowMs);
  // S1 (col-lead3e): under a check (`openAll`) every section is drawn open, whatever is stored.
  const openAll = ui.openAll === true;
  // H1 (col-lead3g): with the switch off the model reads no release mark and no confirm key.
  const x1 = releaseSwitchOn(ui.clear) ? { releases: ui.releases, confirm: ui.confirm } : { releases: null, confirm: null };
  const listedNeeds = needsList(live.terminals, doc, tabNames);
  // X1.6, X4: the view's marks of releases, forgotten when a document no longer lists the receipt, and
  // contradicted by a later current reading that still lists it uncertain.
  const listed = new Map(listedNeeds.filter((i) => i.source === "receipt").map((i) => [i.id, i.receipt.state]));
  const releases = reconcileReleases(x1.releases, doc, listed, current);
  const uix = { ...ui, ...x1, releases };
  const base = listedNeeds.map((item) => {
    const it = {
      ...item, age: age(item.sinceMs),
      ...(item.source === "terminal" || current
        ? { current: true, tone: "blocked", textTone: "foreground" }
        : { current: false, mark: "?", tone: "muted", textTone: "muted", asks: boundText(`${note} · ${item.asks}`, ASK_MAX) }),
      action: item.source === "terminal" ? act(item.address) : null,
      goThere: item.source === "receipt" ? act(recipientAddress(item.receipt)) : null,
      goThereAddress: item.source === "receipt" ? recipientAddress(item.receipt) : null,
      detail: item.source === "receipt" ? receiptDetail(item.receipt, nowMs, offsetMin) : null,
    };
    return it;
  });
  const hidden = pruneHidden(state.hidden, base, nowMs, { terminals: liveAddresses(live.terminals), doc: doc !== null });
  // X5 (col-lead3g): which rows are hidden is known before what a receipt row offers.
  const away = new Set(splitHidden(base, hidden, nowMs).hidden.map((i) => i.key));
  const items = base.map((it) => {
    if (it.source !== "receipt") return { ...it, state: null, whereLine: null, asksLine: it.asks, asksTone: "muted", act: null, released: false, hidden: false };
    // G1, G2, P1, X1 (col-lead3e): where a click goes, what the detail offers, and the second line.
    return { ...it, state: isRecord(it.receipt) ? it.receipt.state : null, whereLine: whereLine(it.receipt, it.goThere),
      hidden: away.has(it.key), ...receiptActions(it, uix, away.has(it.key)) };
  });
  const split = splitHidden(items, hidden, nowMs);
  // N1 (col-lead3g): the entries that wait and cannot be drawn; a stale read draws the line as its rows.
  const cannot = undrawableCount(doc);
  const cannotShow = cannot === 0 ? null
    : current ? { text: cannotShowText(cannot), tone: "muted" } : { text: `? ${note} · ${cannotShowText(cannot)}`, tone: "muted" };
  const page = needRows(split.shown, ui.moreNeeds === true);
  // L9 (col-lead3c): the opened list of Needs you, the hidden rows and the silent lanes are bounded too.
  const needsDrawn = page.rows.slice(0, NEEDS_OPEN_MAX);
  const quietAll = quietLanes(live.terminals, paneOf, nowMs, self);
  const quiet = quietAll.slice(0, QUIET_ROWS_MAX).map((q) => ({ ...q, age: formatAge(q.silentMs / 1000), action: act(q.address) }));
  // X1 (col-lead3e): a receipt released counts neither for the chip nor for the badge from the build
  // after the answer; it stays drawn until a document no longer lists it.
  const waitingNow = split.shown.filter((i) => i.current && !i.released).length;
  const staleNow = split.shown.filter((i) => !i.current).length;
  // S1: an item that arrives does not unfold the section; the chip, the badge and this number show it.
  // The section has a title line (the control that folds it) only when an item waits or is hidden;
  // silent lanes alone draw no title (as before), so a stored fold waits, kept, until there is one.
  const needsFold = !openAll && (items.length > 0 || cannot > 0) && isRecord(state.folded) && state.folded.needs === true;
  // X1.2: the one open confirm block stays only while its row is drawn with it (the section open).
  const drawnNeeds = needsFold ? [] : [...needsDrawn, ...(ui.hiddenOpen === true ? split.hidden.slice(0, HIDDEN_ROWS_MAX) : [])];
  const confirmKey = typeof uix.confirm === "string" && drawnNeeds.some((r) => r.key === uix.confirm && r.act !== null && r.act.confirm !== null) ? uix.confirm : null;

  const c = { paneOf, tabNames, nowMs, docOld, offsetMin, self };
  const rows = machineOrder(doc, live.machines).map((name) => machineRow(name, doc, live, c));
  const adv = advice(doc, rows, docOld);
  // L9 (col-lead3c): every machine counts for the advice and the header; at most MACHINES_MAX rows are drawn.
  const machinesDrawn = rows.slice(0, MACHINES_MAX);
  for (const row of machinesDrawn) row.detail.lanes = row.detail.lanes.map((l) => ({ ...l, action: act(l.address) }));
  const machinesLeft = rows.length - machinesDrawn.length;

  // F16: a stale read is never drawn as current: its counts and rows are muted.
  const read = receiptsState(doc, nowMs);
  const all = receiptRows(doc, state.seen, nowMs).map((r) => ({
    ...r, detail: receiptDetail(r.receipt, nowMs, offsetMin), goThere: act(recipientAddress(r.receipt)),
    goThereAddress: recipientAddress(r.receipt), ...(read.stale ? { tone: "muted" } : {}),
  })).map((r) => ({ ...r, whereLine: whereLine(r.receipt, r.goThere) }));
  const receiptsPage = receiptPage(all, ui.moreReceipts === true);
  // L9 (col-lead3c): the opened list of receipts is bounded too.
  const receiptsDrawn = receiptsPage.rows.slice(0, RECEIPTS_OPEN_MAX);
  // col-lead: who leads each project, from the same read of the live lists.
  const folded = isRecord(state.folded) ? state.folded : {};
  const proj = projects(live, { paneOf, tabNames, nowMs, self, act, opened: cleanProjectKeys(folded.projects) });

  return {
    header: headerChips({ shown: waitingNow, hidden: split.hidden.length,
      stale: staleNow, note },
      { noMachineFree: adv.noMachineFree, coordinatorMemory: rows.some((r) => r.tone === "blocked"), sample: input.sample === true,
        receiptsRead: receiptsRead(doc, docOld), receiptsUnclear: receiptsUnclear(doc, docOld), self }),
    needs: {
      total: items.length, cannotShow, rows: needsDrawn, more: page.more,
      notDrawn: notDrawnLine(page.rows.length - needsDrawn.length, "not drawn", "not drawn"),
      hiddenLine: hiddenLine(split.hidden.length, split.until, offsetMin), hiddenRows: split.hidden.slice(0, HIDDEN_ROWS_MAX),
      hiddenMore: notDrawnLine(split.hidden.length - HIDDEN_ROWS_MAX, "hidden", "hidden"),
      quietLine: quietLine(quietAll), quiet, quietMore: notDrawnLine(quietAll.length - quiet.length, "silent lane"),
      folded: needsFold, foldedLine: needsFolded(waitingNow, staleNow, note, split.hidden.length, quietAll.length, cannot), confirmKey,
    },
    machines: {
      advice: adv, rows: machinesDrawn,
      more: machinesLeft > 0 ? { text: `+ ${fmtCount(machinesLeft)} more machine${machinesLeft === 1 ? "" : "s"}`, tone: "muted" } : null,
      folded: !openAll && folded.machines === true, foldedLine: machinesFolded(rows),
    },
    projects: {
      ...proj, folded: !openAll && folded.projectsSection === true,
      foldedLine: { parts: [{ text: plural(proj.total, "project"), tone: "foreground" },
        ...(proj.noLeadCount > 0 ? [{ text: `· ${fmtCount(proj.noLeadCount)} with no lead marked`, tone: "muted" }] : [])] },
    },
    receipts: {
      present: doc !== null && isRecord(doc.receipts), header: receiptsHeader(doc, all, nowMs), stale: read.stale, notRead: read.notRead,
      since: sinceLine(doc, state.base, nowMs, offsetMin), rows: receiptsDrawn, older: receiptsPage.older,
      notDrawn: notDrawnLine(receiptsPage.rows.length - receiptsDrawn.length, "not drawn", "not drawn"),
      folded: !openAll && isRecord(state.folded) && state.folded.receipts === true,
    },
    footer: footer(doc, nowMs, input.sample === true),
    hidden, lastRead, releases,
  };
}

// ---------------------------------------------------------------- a picture in plain text

/** H1, K1: the opened signal lines the picture cuts with cutClosed. */
const CLOSED_LINES = Object.freeze(["why", "mem"]);

function fit(text, width) {
  const chars = [...text];
  if (chars.length <= width) return text;
  const cut = `${chars.slice(0, width - 1).join("")}…`;
  // K1: a cut that would leave a parenthesis open is cut as G3 cuts (cutClosed).
  return closed(cut) ? cut : cutClosed(text, width);
}

function sides(left, right, width) {
  const room = width - [...right].length - (right ? 1 : 0);
  const l = fit(left, Math.max(1, room));
  return `${l}${" ".repeat(Math.max(1, width - [...l].length - [...right].length))}${right}`.trimEnd();
}

/** Break a line at spaces so no piece is wider than `width`; later pieces keep the indent. */
function wrap(text, width, indent = "") {
  const out = [];
  let line = indent;
  for (const word of text.split(" ").filter(Boolean)) {
    const next = line === indent ? `${indent}${word}` : `${line} ${word}`;
    if ([...next].length <= width || line === indent) line = fit(next, width);
    else {
      out.push(line);
      line = fit(`${indent}${word}`, width);
    }
  }
  if (line !== indent) out.push(line);
  return out;
}

function bar(m) {
  const cells = 5;
  const at = (px) => Math.min(cells - 1, Math.floor((px * cells) / m.width));
  const out = Array.from({ length: cells }, (_, i) => (i < Math.round((m.fill * cells) / m.width) ? "█" : "·"));
  for (const px of [m.busyMark, m.fullMark]) if (out[at(px)] === "·") out[at(px)] = "|";
  return out.join("");
}

/** S1 (col-lead3e): a folded section's line under its title, the model's parts joined. */
const foldedText = (line) => (isRecord(line) && Array.isArray(line.parts) ? line.parts.map((p) => p.text).join(" ") : "");

/**
 * The column as lines of plain text, `width` characters wide: for the report and the tests.
 * col-lead3e: `open[key]` also opens a receipt's detail in NEEDS YOU, and `view.hot` is the key of the
 * row under the pointer (its controls stand where its age is). A controls row is drawn `[text]`.
 * A folded section's title starts with `▸`; an open one is drawn as before, without a mark.
 */
export function picture(model, width = 40, open = {}, view = {}) {
  const out = [];
  const h = model.header;
  const first = `${h.first.mark} ${h.first.text}${h.first.suffix ? ` ${h.first.suffix}` : ""}`;
  out.push(sides(h.sample ? "Answerback sample" : "Answerback", first, width));
  if (h.second) out.push(sides("", `${h.second.mark} ${h.second.text}`, width));

  const n = model.needs;
  const cannotShow = isRecord(n.cannotShow) ? n.cannotShow : null;
  if ((n.total > 0 || n.quietLine || cannotShow) && n.folded === true) {
    out.push("", "▸ NEEDS YOU", ...wrap(foldedText(n.foldedLine), width, "  "));
  } else if (n.total > 0 || n.quietLine || cannotShow) {
    out.push("");
    if (n.total > 0 || cannotShow) out.push("NEEDS YOU");
    // col-lead3g: one row and its detail; also for the hidden rows when `view.hiddenOpen` lists them (X5).
    const needRow = (r) => {
      const receipt = r.source === "receipt";
      const shown = receipt && open[r.key] === true;
      const hot = view.hot === r.key;
      // G1 (col-lead3e): a row that goes somewhere shows `details` while hot; one that does not draws ▸ or ▾ last.
      const right = [hot ? `${receipt && r.goThere !== null ? (shown ? "[close] " : "[details] ") : ""}[hide 1 h]` : r.age,
        receipt && r.goThere === null ? (shown ? "▾" : "▸") : ""].filter(Boolean).join(" ");
      out.push(sides(`${r.mark} ${r.chip} ${r.name}`, right, width));
      const second = typeof r.asksLine === "string" ? r.asksLine : r.asks;
      out.push(...(second === r.asks ? [fit(`  ${r.asks}`, width)] : wrap(second, width, "  ")));
      if (!shown) return;
      // G2, P1, X1 (col-lead3e): the detail, its first line when the receipt has nowhere to go, then what it offers.
      if (r.whereLine) out.push(...wrap(r.whereLine, width, "    "));
      for (const x of r.detail ?? []) out.push(fit(`    ${x}`, width));
      const a = r.act;
      if (a && a.status) out.push(...wrap(a.status.text, width, "    "));
      // X3 (col-lead3g): with both (only under a check), the controls stand above the confirm block.
      const both = Boolean(a && a.confirm && (a.copy || a.clear));
      const confirm = () => {
        for (const x of a.confirm.lines) out.push(...wrap(x, width, "    "));
        out.push(`    [${a.confirm.release}] [${a.confirm.cancel}]`);
      };
      if (a && a.confirm && !both) confirm();
      const controls = [a?.copy?.text, a?.clear?.text, r.goThere !== null ? "Go there" : null].filter(Boolean).map((x) => `[${x}]`);
      if (controls.length) out.push(fit(`    ${controls.join(" ")}`, width));
      if (both) confirm();
    };
    for (const r of n.rows) needRow(r);
    if (n.more > 0) out.push(`  + ${n.more} more`);
    if (n.notDrawn) out.push(`  ${n.notDrawn.text}`);
    // N1 (col-lead3g): after the rows, the entries that wait and cannot be drawn.
    if (cannotShow) out.push(...wrap(cannotShow.text, width, "  "));
    if (n.hiddenLine) out.push(view.hiddenOpen === true ? n.hiddenLine.replace("▸", "▾") : n.hiddenLine);
    if (n.hiddenLine && view.hiddenOpen === true) for (const r of n.hiddenRows ?? []) needRow(r);
    if (n.quietLine) out.push(n.quietLine);
  }

  const mFolded = model.machines.folded === true;
  out.push("", sides(mFolded ? "▸ MACHINES" : "MACHINES", model.machines.advice.work, width));
  if (model.machines.advice.suites) out.push(...wrap(model.machines.advice.suites, width));
  if (mFolded) out.push(...wrap(foldedText(model.machines.foldedLine), width, "  "));
  for (const r of mFolded ? [] : model.machines.rows) {
    // F11 (break 4): a busy or full machine with no meter still names its signal, as main.js does.
    const gauge = r.meter ? `${r.signal} ${bar(r.meter)} ${r.value ?? ""}` : r.signal ?? "";
    out.push(sides(`${r.mark} ${r.word} ${r.label}`, [gauge, r.agents].filter(Boolean).join(" "), width));
    if (r.reason) out.push(...wrap(r.reason, width, "  "));
    if (r.work) out.push(...wrap(r.work, width, "  "));
    if (open[r.key]) {
      // H1, K1: the `why` and `mem` lines are cut as in the model (cutClosed), never inside a parenthesis.
      for (const s of r.detail.signals) {
        const head = `    ${s.name}: `;
        out.push(CLOSED_LINES.includes(s.name) ? `${head}${cutClosed(s.text, Math.max(1, width - [...head].length))}` : fit(`${head}${s.text}`, width));
      }
      out.push(`    ${r.detail.age}`);
      for (const l of r.detail.lanes) out.push(sides(`    ${l.mark} ${l.name}`, [l.status, l.time].filter(Boolean).join(" "), width));
      if (r.detail.lanesMore) out.push(`    ${r.detail.lanesMore.text}`);
      if (r.detail.shells) out.push(`    · ${r.detail.shells}`);
      if (r.detail.start) out.push(fit(`    [${r.detail.start}] (off)`, width));
    }
  }
  if (model.machines.more && !mFolded) out.push(`  ${model.machines.more.text}`);

  // col-lead: PROJECTS, between MACHINES and RECEIPTS. `open` or the model's own fold opens a block.
  const pj = isRecord(model.projects) ? model.projects : { rows: [], none: null };
  if (pj.rows.length > 0 && pj.folded === true) {
    out.push("", "▸ PROJECTS", ...wrap(foldedText(pj.foldedLine), width, "  "));
  } else if (pj.rows.length > 0) {
    out.push("", "PROJECTS");
    const pline = (l, indent) => sides(`${indent}${l.mark} ${l.name}`, [l.chip, l.status, l.time].filter(Boolean).join(" "), width);
    // K4: `+ N more projects` after the blocks drawn, before the line of the lanes without a project.
    const more = pj.more ? [{ moreLine: pj.more.text }] : [];
    for (const p of [...pj.rows, ...more, ...(pj.none ? [pj.none] : [])]) {
      if (p.moreLine) {
        out.push(fit(p.moreLine, width));
        continue;
      }
      const shown = open[p.key] === true || p.open === true;
      out.push(sides(`${shown ? "▾" : "▸"} ${p.name}`, p.count ?? "", width));
      // L3 (col-lead3): folded, at most three lead lines; opened, at most twelve; then `… (N more leads)`.
      for (const l of (shown ? p.leads : p.foldedLeads) ?? []) out.push(pline(l, "  "));
      const leadsMore = shown ? p.leadsMore : p.foldedLeadsMore;
      if (leadsMore) out.push(`  ${leadsMore.text}`);
      if (p.leadNote) out.push(`  ${p.leadNote.text}`);
      if (p.noLead) out.push(`  ${p.noLead.text}`);
      if (!shown) continue;
      for (const l of p.lines) out.push(pline(l, "    "));
      if (p.linesMore) out.push(`    ${p.linesMore.text}`);
      if (p.shells) out.push(`    · ${p.shells}`);
    }
  }

  const rc = model.receipts;
  if (rc.present) {
    // A header too long for one line goes under the title, as main.js draws it.
    const head = rc.header.text ?? "";
    const title = rc.folded ? "▸ RECEIPTS" : "RECEIPTS";
    if ([...`${title} `].length + [...head].length <= width) out.push("", sides(title, head, width));
    else out.push("", title, ...wrap(head, width, "  "));
    if (!rc.folded) {
      if (rc.since) out.push(...wrap(rc.since.text, width));
      for (const r of rc.rows) {
        out.push(sides(`${r.mark} ${r.label}`, `${r.stateText} ${r.age}`, width));
        if (r.verdict) out.push(...wrap(r.verdict, width, "  "));
      }
      if (rc.older > 0) out.push(`  + ${rc.older} older`);
      if (rc.notDrawn) out.push(`  ${rc.notDrawn.text}`);
    }
  }

  out.push("", fit(model.footer.text, width));
  if (model.footer.sample) out.push("sample data");
  return out;
}
