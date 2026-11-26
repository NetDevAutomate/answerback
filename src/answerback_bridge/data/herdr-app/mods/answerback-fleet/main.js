// Answerback fleet column, version 2 (lane proto-col, fix rounds proto-col-fix to col-fix5, ABPANEL-20261120).
// This lane never installs it. The only `run` calls are pane.focus and terminal.peek, on an address
// that terminals() returns at the moment of the click. col-lead3e (X1): it starts one program, the
// bridge's command with one fixed argument list (releasePlan in fleet.js), only from the click handler
// of `Release`, behind a confirm step (releaseNow). Live rows come
// from terminals(), machines(), agents(), panes() and spaces(); everything else comes from the bridge's
// document, read with readTextFile. The hidden rows are one shared value (shared / setShared).
// All logic is in fleet.js.
// col-lead3f (X2): the release is off unless the layout entry has the prop `clear` set to exactly
// "release" (for example `{"clear": "release"}`); any other value, or none, leaves it off.

import { View, div } from "gpui-kit";
import { Collapsible, Progress, ProgressIndicator, ProgressTrack, h_flex, v_flex } from "gpui-base";
import {
  agents, checking, exec, home, machines, panes, readTextFile, run, setBadge, setShared, shared, spaces, terminals, theme, watchFolder,
} from "herdr";
import {
  HIDDEN_KEY, HIDDEN_ROWS_MAX, LEAD_MARK, MACHINE_LANES_MAX, QUIET_ROWS_MAX, RELEASE_SWITCH, RELEASE_TIMEOUT_MS, commandLine, judgeRelease, needsRowAt, releasePlan, releaseSwitchOn, MODEL_EVERY_MS, NEEDS_OPEN_MAX, PROJECT_LEADS_OPEN, PROJECT_LINES_MAX, RECEIPTS_OPEN_MAX, actionAtClick, batchTouches, boundText, column, displayAddress, documentPath, elementId, folderOf, hideEntry, idMaker,
  hiddenValue, hostStorage, isOld, isRecord, lookup, makeStore, markSeen, needsBase, nextDocument, parseDocument, readHidden,
  readState, reuseModel, sameHidden, toggleProject, useSample,
} from "./fleet.js";
import { FIXTURES } from "./fixtures.js";

/**
 * W1 (col-lead3d): a tooltip promises only what the click does. An opened list shows up to its
 * largest number (the constants of fleet.js), and the muted line under it says how many are left.
 */
const LEFT = "a line below says how many are left";

/** L6f (col-lead3d): the most reads one chain of reads makes (reload). */
const READS_PER_CHAIN = 2;

/** The type scale of the owner's sidebar kit (DESIGN-v2 3). */
const BODY = 13;
const LABEL = 11.5;
const CAPTION = 11;
const BADGE = 10;

/**
 * Every drawn string goes through here: one line, no control or bidirectional characters, bounded (C8).
 * L8 (col-lead3b): and no star-shaped character (each becomes `*`): boundText cleans in fleet.js.
 */
const say = (text, max = 120) => boundText(text, max);
/** The fixed ids of the column; no id built from data can take one (L1c). */
const FIXED_IDS = Object.freeze(["answerback-fleet-root", "needs-head", "needs-more", "needs-hidden", "quiet-line", "machines-head", "projects-head",
  "receipts-head", "receipts-since", "receipts-more"]);
/**
 * L1c (col-lead3c): the ids of the column that is being drawn (idMaker in fleet.js), made anew at the
 * start of every render. A render runs to its end before any other script call, so two copies of
 * the column never share it while it is in use.
 */
let ids = null;
/** Every element id built from data goes through here (C8); unique within one drawn column (L1c). */
const eid = (prefix, key) => (ids === null ? elementId(prefix, key) : ids(prefix, key));

/**
 * G5: what is drawn is decided in fleet.js, which gives tone words. This fixed table maps a tone
 * word to the theme colour of the same name; a word that is not an own key is drawn muted.
 */
const TONES = Object.freeze({ blocked: "blocked", working: "working", muted: "muted", foreground: "foreground", dim: "dim" });
const toneColor = (t, tone) => t[lookup(TONES, tone, "muted")];

/**
 * Clicks that change what the column shows. render() runs after cx.notify() and on every change of
 * Herdr's live data (in/api/AGENTS.md:975-977), so it draws the model on hand while that is recent
 * (reuseModel); a click counted here brings a fresh model with the render that follows it.
 * L6 (col-lead3b): a click is counted before its handler runs. A click on "hide" or "show" builds
 * in its handler, after it was counted, so the render that follows does not build again.
 * So in any stretch of time the builds are at most: one per MODEL_EVERY_MS from renders, one per
 * read of the document that ended, one per tick, one per click, one per release that ended (X1).
 */
let changes = 0;

function list(call) {
  try {
    const v = call();
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

/** The user's home, or "" when the host gives none (X1: then `~/` resolves to nothing). */
function homeDir() {
  try {
    const h = home();
    return typeof h === "string" ? h : "";
  } catch {
    return "";
  }
}

function readShared(key) {
  try {
    return shared(key);
  } catch {
    return null;
  }
}

/**
 * The only two commands this mod runs (C9). The action is decided now, from terminals(), panes()
 * and agents() as they are at the time of the click: an address that has gone since the row was
 * drawn runs nothing.
 */
/** X2 (col-lead3f): one prop of the layout entry, or undefined when it is absent or cannot be read. */
function propOf(props, name) {
  try {
    return isRecord(props) ? props[name] : undefined;
  } catch {
    return undefined;
  }
}

function go(address, sample) {
  const action = actionAtClick(address, { terminals: list(terminals), panes: list(panes), agents: list(agents) }, sample);
  if (action === null) return;
  if (action.command === "pane.focus") run("pane.focus", { pane: action.args.pane });
  else if (action.command === "terminal.peek") run("terminal.peek", { terminal: action.args.terminal });
}

function tipFor(address, action) {
  const shown = displayAddress(address) || "this row";
  if (action === null) return say(`${shown}: not a click target (sample data, or not in the last read)`);
  return say(action.command === "pane.focus" ? `${shown} · click: go to its tab` : `${shown} · click: open a peek`);
}

/** A theme colour at low strength for a chip background; the raised surface when it is not #rrggbb. */
function soft(t, color) {
  return typeof color === "string" && /^#[0-9a-fA-F]{6}$/.test(color) ? `${color}26` : t.raised;
}

function title(t, text) {
  return div().flex_none().text_size(CAPTION).font_semibold().text_color(t.muted).child(say(text).toUpperCase());
}

/** L9 (col-lead3c): the muted line under a bounded list that says how many are not drawn; it cannot be clicked. */
function notDrawn(t, x, indent) {
  return div().pl(indent).text_size(CAPTION).text_color(toneColor(t, x.tone)).child(say(x.text, 32));
}

/** S1 (col-lead3e): the line under a folded section's title: the model's parts, each in its tone (G5). */
function foldedLine(t, line) {
  const parts = Array.isArray(line?.parts) ? line.parts : [];
  return h_flex().pl(19).pr(4).gap(4).flex_wrap()
    .children(parts.map((p) => div().flex_none().text_size(CAPTION).text_color(toneColor(t, p.tone)).child(say(p.text, 48))));
}

/** The machine chip of a row. */
function tag(t, text) {
  return div().flex_none().px(5).h(16).rounded(4).bg(t.raised).flex().items_center()
    .text_size(BADGE).text_color(t.muted).child(say(text, 16));
}

/** A line of text that is a control: "+ 2 more", "▸ 2 hidden until 19:05", the `since` line. */
function line(t, id, text, tip, onClick) {
  return div().id(id).px(4).py(2).rounded(4).text_size(LABEL).text_color(t.muted)
    .cursor_pointer().hover((s) => s.bg(t.hover))
    .on_click((_e, cx) => {
      changes += 1;
      onClick();
      cx.notify();
    })
    .tooltip(say(tip))
    .child(say(text));
}

/**
 * A small bordered control inside a row. Its click does not reach the row. The handler gets the click's
 * context. P2 (col-lead3g): `tone` is a tone word of the model (foreground when none).
 */
function control(t, id, text, tip, onClick, tone = "foreground") {
  return div().id(id).flex_none().px(6).h(18).rounded(4).border(1).border_color(t.border).flex().items_center()
    .text_size(BADGE).text_color(toneColor(t, tone)).cursor_pointer().hover((s) => s.bg(t.raised))
    .on_click((_e, cx) => {
      cx.stop_propagation();
      changes += 1;
      onClick(cx);
      cx.notify();
    })
    .tooltip(say(tip))
    .child(say(text, 24));
}

/**
 * The meter, 40 x 4 px: the signal that decided the state, on the bridge's scale, with a mark at
 * 0.70 (busy) and one at 1.00 (full). Geometry from meter() in fleet.js. An unknown signal draws
 * a neutral meter (C3): `color` is null then.
 */
function meterEl(t, id, r, color) {
  const g = r.meter;
  const fill = r.greyed ? t.dim : color ?? t.muted;
  const mark = (x) => div().absolute().left(x).top(-2).w(1).h(8).bg(t.foreground).opacity(0.55);
  const percent = g.width > 0 ? Math.round((100 * g.fill) / g.width) : 0;
  return Progress.new(id).value(percent).accessibility_label(say(`${r.signal ?? "load"} ${r.value ?? ""}`, 40)).flex_none()
    .child(ProgressTrack.new().relative().w(g.width).h(4).rounded(2).bg(t.border)
      .child(ProgressIndicator.new().absolute().left(0).top(0).h(4).w(g.fill).rounded(2).bg(fill).transition("width", 200))
      .child(mark(g.busyMark))
      .child(mark(g.fullMark)));
}

export default class AnswerbackFleet extends View {
  init(props, cx) {
    /** C1: the fixtures only under a check or with the prop `sample: true`; never because a list is empty. */
    this.sample = useSample(checking(), props);
    /** The one wrapper around localStorage: seen failures, the totals at the last "seen", folds. */
    this.store = makeStore(hostStorage());
    this.state = readState(this.store, this.readClock());
    /** The hidden rows: the shared value, read with shared() (C10). */
    this.hidden = {};
    this.sharedText = null;
    this.hiddenDirty = false;
    /** The sample has its own state, in memory only: clicks on sample rows never reach the store. */
    this.sampleState = JSON.parse(JSON.stringify(FIXTURES.state));
    this.sampleDoc = parseDocument(FIXTURES.documentText);
    this.doc = null;
    this.ui = { moreNeeds: false, moreReceipts: false, hiddenOpen: false, quietOpen: false };
    /** X1 (col-lead3e): the prop `command` of the layout entry, and the home that resolves its `~/`. */
    this.commandProp = isRecord(props) ? props.command : undefined;
    this.homeDir = homeDir();
    /** X2 (col-lead3f): the prop `clear` as it was read; a prop that cannot be read is absent (off). */
    this.clearProp = propOf(props, "clear");
    /** X1: the key of the one open confirm block (at most one in the column), or null. */
    this.confirm = null;
    /**
     * P1: the key whose command was copied; `copied` is drawn until the next build of a tick or a read.
     * P2 (col-lead3g): `{ key, ok }`: ok false is a click whose clipboard failed (`could not copy`, same life).
     */
    this.copied = null;
    /**
     * X1: the releases the column started, by the receipt's full id: { phase: running | released |
     * refused, message }. The view's memory, given to the model with `ui`; forgotten when a document no
     * longer lists the receipt (column() prunes it); never stored.
     */
    this.releases = new Map();
    /** X1: one release at a time in the column: while one runs, a click on any Release runs nothing. */
    this.releasing = false;
    /** Keys of opened machine rows and opened receipt details. */
    this.open = new Set();
    this.openAll = false;
    /** The key of the needs-you row under the pointer: it shows the hide control. */
    this.hot = null;
    this.now = this.sample ? FIXTURES.nowMs : this.readClock();
    this.badgeOn = !(isRecord(props) && props.badge === false);
    this.badgeSet = null;
    this.watching = false;
    this.loading = false;
    /** L6e (col-lead3c): a watch event found the first read of a chain running and asks for one more read after it. */
    this.again = false;
    /**
     * L6f (col-lead3d): a watch event found the second read of a chain running. That chain reads no
     * more; this mark (a flag, not a counter) says that a read is still owed. The tick serves it, or
     * a watch event that comes first and reads; either read clears it.
     */
    this.owed = false;
    /** L6f (col-lead3d): the number of the read that runs in the chain (1 or 2), 0 when none runs. */
    this.chainRead = 0;
    /** L6d (col-lead3c): a deferred badge step that a render started has not run yet. */
    this.stepPending = false;
    this.path = "";
    /** The model the last build made, when it was built, and the click count it was built at. */
    this.cached = null;
    this.cachedAt = 0;
    this.cachedChanges = -1;
    /** L6d (col-lead3c): the number of builds so far. */
    this.builds = 0;

    // The one timer: the ages move, the badge follows, the hidden set is published, and the
    // document is read again when no watch is running or the document on hand is old or absent.
    cx.timer.every(30000, (cx) => this.tick(cx));

    if (checking()) {
      // A check draws the sample once. Every part is opened so that the check walks every builder.
      this.openAll = true;
      this.ui.hiddenOpen = true;
      this.ui.quietOpen = true;
      const rows = this.model().needs.rows;
      if (rows.length > 1) {
        // col-lead3g: the row hidden for the check is the last one that is not an uncertain receipt, so
        // that the check walks the hidden rows and also X3's confirm block (X5: a hidden row has none).
        const away = rows.filter((r) => !(r.source === "receipt" && r.state === "uncertain")).at(-1) ?? rows[rows.length - 1];
        this.sampleState.hidden[away.key] = hideEntry(away, this.now);
        this.hot = rows[0].key;
      }
      return;
    }
    if (this.sample) return;
    this.adoptShared(this.readClock());
    this.path = documentPath(props, this.homeDir);
    this.watch(cx);
  }

  /**
   * X4 (col-lead3g): the one place where the view reads the clock; a test can replace it. The mark of a
   * release judged "released" keeps the time of that judgement from here.
   */
  readClock() {
    return Date.now();
  }

  /** Takes the shared hidden set when another copy or mod changed it since this copy last saw it. */
  adoptShared(nowMs) {
    const value = readShared(HIDDEN_KEY);
    let text = null;
    try {
      text = JSON.stringify(value ?? null);
    } catch {
      text = null;
    }
    if (text === this.sharedText) return;
    this.sharedText = text;
    this.hidden = readHidden(value, nowMs);
  }

  /** Publishes the hidden set with setShared when it changed. Never during render, never under a check or sample. */
  publishHidden() {
    if (checking() || this.sample || !this.hiddenDirty) return;
    const value = hiddenValue(this.hidden);
    this.hiddenDirty = false;
    this.sharedText = JSON.stringify(value);
    setShared(HIDDEN_KEY, value);
  }

  /**
   * X2 (col-lead3f): the switch the model draws with. Under a check it is on whatever the prop says,
   * so that `herdr-app check` walks the builders of X1; nothing can run there (releaseNow and
   * copyCommand return under a check, and the check draws the sample).
   */
  clearSwitch() {
    return checking() ? RELEASE_SWITCH : this.clearProp;
  }

  /** Reads the live lists and builds everything the column draws. */
  model() {
    // L6d (col-lead3c): every build is counted, so that render() knows that it built one.
    this.builds += 1;
    // col-lead3e: the view's memory the model draws from: the check's openAll (S1), the copied key
    // (P1), the open confirm block and the releases (X1), the prop `command` and the home.
    // X3 (col-lead3g): `checking` lets a check walk the confirm block without a click.
    const copied = isRecord(this.copied) ? this.copied : null;
    const ui = { ...this.ui, openAll: this.openAll, copied: copied !== null && copied.ok === true ? copied.key : null,
      copyFailed: copied !== null && copied.ok !== true ? copied.key : null, confirm: this.confirm, releases: this.releases,
      command: this.commandProp, home: this.homeDir, checking: checking(), clear: this.clearSwitch() };
    if (this.sample) {
      this.now = FIXTURES.nowMs;
      const s = column({ live: FIXTURES.live, doc: this.sampleDoc, state: this.sampleState, ui, nowMs: this.now, offsetMin: 0, sample: true });
      this.confirm = s.needs.confirmKey;
      return s;
    }
    this.now = this.readClock();
    this.adoptShared(this.now);
    const live = { terminals: list(terminals), machines: list(machines), agents: list(agents), panes: list(panes), spaces: list(spaces) };
    const m = column({
      live, doc: this.doc, state: { ...this.state, hidden: this.hidden }, ui, nowMs: this.now,
      offsetMin: -new Date().getTimezoneOffset(), sample: false,
    });
    // X1.2: a confirm block whose row is not drawn with it any more is forgotten; X1.6: so is the
    // mark of a release whose receipt no document lists any more.
    this.confirm = m.needs.confirmKey;
    this.releases = m.releases;
    // The pruned set is kept in memory at once, so that an item seen not blocked is released even
    // if it blocks again before the next publish (C4). It is published on the tick or a click.
    if (!sameHidden(m.hidden, this.hidden)) {
      this.hidden = m.hidden;
      this.hiddenDirty = true;
    }
    this.cached = m;
    this.cachedAt = this.now;
    this.cachedChanges = changes;
    return m;
  }

  /**
   * The model a render draws: the one on hand while it is recent and no click changed what is
   * shown, else a fresh one. A render builds only then: a change in the live lists is drawn by the
   * first render after MODEL_EVERY_MS. L6 (col-lead3b): a read of the document that ended, a tick
   * and a click on "hide" or "show" build once each in their handler (refresh), never in a render.
   * So in any stretch of time the builds are at most: one per MODEL_EVERY_MS from renders, one per
   * read of the document that ended, one per tick, one per click, one per release that ended (X1).
   */
  drawn() {
    if (this.sample) return this.model();
    if (reuseModel(this.cached !== null, this.cachedAt, this.readClock(), this.cachedChanges !== changes, MODEL_EVERY_MS)) return this.cached;
    return this.model();
  }

  /**
   * L6 (col-lead3b): the handler of a rare event builds once, keeps that model as the one on hand,
   * sets the badge and publishes the hidden set from it (syncBadge); the caller then notifies, and
   * the render that follows draws this model. Under a check or with sample data it builds nothing:
   * a render draws the sample.
   */
  refresh() {
    if (checking() || this.sample) return;
    this.model();
    this.syncBadge();
  }

  /** Changes one part of the state; only real data is stored. The hidden set is published by refresh(). */
  save(name, value) {
    if (this.sample) this.sampleState[name] = value;
    else if (name === "hidden") {
      this.hidden = value;
      this.hiddenDirty = true;
    } else {
      this.state[name] = value;
      this.store.set(name, value);
    }
  }

  isOpen(key) {
    return this.openAll || this.open.has(key);
  }

  toggle(key) {
    if (this.open.has(key)) this.open.delete(key);
    else this.open.add(key);
    // X1.2: a confirm block closes with its detail.
    if (!this.open.has(key) && this.confirm === key) this.confirm = null;
  }

  /** S1 (col-lead3e): folds or unfolds a section; the fold is the owner's and is stored (save: only real data). */
  fold(name, value) {
    const current = this.sample ? this.sampleState : this.state;
    this.save("folded", { ...(isRecord(current.folded) ? current.folded : {}), [name]: value });
    // X1.2: a confirm block closes when its section is folded.
    if (name === "needs" && value) this.confirm = null;
  }

  /**
   * Reads the document (C6). A read that fails, finds no file, or returns text that is half-written,
   * not JSON, of another schema or too large keeps the last good document and its age.
   * L6e (col-lead3c): false when a read was running, so this call read nothing and the caller's
   * handler must not build; true otherwise.
   * L6f (col-lead3d): a chain of reads has at most READS_PER_CHAIN = 2 reads: the one it started with,
   * and one more when a watch event arrived during the first. Then it ends, and its handler builds
   * once. A watch event during the second read starts no third read; it leaves the mark `owed`.
   */
  async reload() {
    if (this.loading) {
      // L6e (col-lead3c): a read runs. Only the watch gets here (a tick that finds a read running
      // reads nothing), and its handler neither builds nor sets the badge: the running read's handler
      // does. During the first read it asks for exactly one more read (a flag, not a counter);
      // L6f (col-lead3d): during the second it leaves the mark that a read is owed.
      if (this.chainRead < READS_PER_CHAIN) this.again = true;
      else this.owed = true;
      return false;
    }
    if (this.path === "") return true;
    this.loading = true;
    // L6f: this read brings the newest document, so whatever was owed is served by it.
    this.owed = false;
    let text = null;
    for (let n = 1; n <= READS_PER_CHAIN; n += 1) {
      // The read that runs does the second read before its handler builds; a read that fails keeps
      // the last good document. So a chain of reads ends with one build, after at most two reads.
      this.again = false;
      this.chainRead = n;
      try {
        text = await readTextFile(this.path);
      } catch {
        text = null;
      }
      this.doc = nextDocument(this.doc, text).doc;
      if (!this.again) break;
    }
    this.again = false;
    this.chainRead = 0;
    this.loading = false;
    // First run, a damaged base, or the bridge's totals fell below the stored ones: count from
    // now, and never claim an old failure as new.
    if (this.doc !== null && needsBase(this.state.base, this.doc, this.readClock())) {
      const next = markSeen(this.doc, this.state.seen, this.readClock());
      this.state.seen = next.seen;
      this.state.base = next.base;
      this.store.set("seen", next.seen);
      this.store.set("base", next.base);
    }
    return true;
  }

  /** The loop of in/api/AGENTS.md:1128. It ends when the folder cannot be watched or wakes too often. */
  watch(cx) {
    cx.spawn(async (cx) => {
      if (await this.reload()) {
        this.copied = null;
        this.refresh();
        cx.notify();
      }
      const folder = folderOf(this.path);
      let quick = 0;
      this.watching = true;
      try {
        while (quick < 3) {
          const started = this.readClock();
          const paths = await watchFolder(folder);
          quick = this.readClock() - started < 1000 ? quick + 1 : 0;
          if (!batchTouches(paths, this.path)) continue;
          // L6e (col-lead3c): a skipped read builds nothing here; the running read brings the document.
          if (!(await this.reload())) continue;
          // P1: `copied` lasts until the next build that a tick or a read causes.
          this.copied = null;
          this.refresh();
          cx.notify();
        }
      } catch {
        // The folder is absent or cannot be watched: the 30 s tick reads the document instead.
      }
      this.watching = false;
    });
  }

  /**
   * L6 (col-lead3b): the tick builds once. When it reads the document itself (no watch runs, or the
   * document on hand is old or absent), it builds after that read ended, with or without a new
   * document, and not before the read as well.
   * L6f (col-lead3d): a tick that finds the mark `owed` and no read running reads once, also when a
   * watch runs and the document on hand is fresh; that read clears the mark. A tick that finds a
   * read running asks for no read (L6e) and leaves the mark as it is. So between two ticks the column
   * reads at most twice for each watch event that starts a chain, and the newest document is on hand
   * at the latest one tick after the last watch event.
   */
  tick(cx) {
    if (!checking() && !this.sample && this.path !== "" && !this.loading
      && (this.owed || !this.watching || this.doc === null || isOld(this.doc, this.readClock()))) {
      cx.spawn(async (cx) => {
        await this.reload();
        this.copied = null;
        this.refresh();
        cx.notify();
      });
      return;
    }
    this.copied = null;
    this.refresh();
    cx.notify();
  }

  /**
   * The Dock badge shows the count of the first chip (in/api/herdr.d.ts:301-305). It is set only
   * when the text changes, never during render, never under a check and never from sample data.
   * The hidden set is published here too. Both follow the model on hand: refresh() calls this right
   * after it built that model in the handler of a rare event; this never builds one itself.
   * So in any stretch of time the builds are at most: one per MODEL_EVERY_MS from renders, one per
   * read of the document that ended, one per tick, one per click, one per release that ended (X1).
   */
  syncBadge() {
    if (checking() || this.sample) return;
    this.publishHidden();
    const m = this.cached;
    if (m === null || !this.badgeOn) return;
    const want = m.header.badge;
    if (want === this.badgeSet) return;
    this.badgeSet = want;
    setBadge(want);
  }

  setHot(key, hovered, cx) {
    const next = hovered ? key : this.hot === key ? null : this.hot;
    if (next === this.hot) return;
    this.hot = next;
    cx.notify();
  }

  /**
   * L6d (col-lead3c): a model that a render built gets its badge one step after that render. The
   * render never sets the badge or publishes the hidden set; it starts one deferred step, which runs
   * after the render has returned and calls syncBadge(), which takes the model on hand at that moment.
   * At most one such step is pending. Under a check the step is started too (so that `herdr-app
   * check` exercises this call) and does nothing when it runs: syncBadge() returns at once there.
   * The step builds nothing and notifies nothing. setBadge and setShared may make the App render
   * again, but that render reuses the model (it is younger than MODEL_EVERY_MS, and no click was
   * counted), so it starts no step: a chain of render, step, shared-state change and render ends there.
   */
  badgeStep(cx) {
    // The App always hands render a context (in/api/gpui-kit.d.ts:570); a test that calls render()
    // without one gets no step. A refusal of cx.spawn is not caught: `herdr-app check` must see it.
    if (this.stepPending || (this.sample && !checking()) || typeof cx?.spawn !== "function") return;
    this.stepPending = true;
    // The body runs at once up to its first await, so the await comes before syncBadge().
    cx.spawn(async (cx) => {
      try {
        await cx.sleep(0);
      } finally {
        this.stepPending = false;
      }
      this.syncBadge();
    });
  }

  render(cx) {
    const t = theme();
    const builds = this.builds;
    const m = this.drawn();
    if (this.builds !== builds) this.badgeStep(cx);
    const c = { t, m };
    ids = idMaker(FIXED_IDS);
    try {
      return this.draw(c);
    } finally {
      ids = null;
    }
  }

  /** K1 (col-lead3d): draws the whole column; named so that it is not taken for `column` of fleet.js. */
  draw(c) {
    const { t, m } = c;
    return v_flex().id("answerback-fleet-root").size_full().bg(t.surface).p(10).gap(12)
      .text_size(BODY).text_color(t.foreground).overflow_y_scrollbar()
      .child(this.header(c))
      .when(m.needs.total > 0 || m.needs.quietLine !== null || m.needs.cannotShow !== null, (el) => el.child(this.needs(c)))
      .child(this.machines(c))
      // col-lead: PROJECTS uses the space below MACHINES; it is not drawn when there is no project.
      .when(m.projects.rows.length > 0, (el) => el.child(this.projects(c)))
      .when(m.receipts.present, (el) => el.child(this.receipts(c)))
      .child(this.footer(c));
  }

  header(c) {
    const { t, m } = c;
    const h = m.header;
    // G1, G5: the chip's mark, words and tone are the model's.
    const chip = (x) => h_flex().flex_none().gap(4).px(6).h(18).rounded(9).items_center()
      .bg(x.tone === "muted" ? t.raised : soft(t, toneColor(t, x.tone))).text_size(BADGE)
      .child(div().text_color(toneColor(t, x.tone)).child(say(`${x.mark} ${x.text}`, 40)))
      .when(typeof x.suffix === "string", (el) => el.child(div().text_color(t.muted).child(say(x.suffix, 24))));
    return v_flex().gap(4)
      .child(h_flex().gap(6).items_center()
        .child(div().text_size(BODY).font_semibold().child("Answerback"))
        // C1: sample data says so in the header too, not only in the footer.
        .when(h.sample, (el) => el.child(div().flex_none().px(5).h(16).rounded(4).bg(t.raised).flex().items_center()
          .text_size(BADGE).text_color(t.muted).child("sample")))
        .child(div().flex_1())
        .child(chip(h.first)))
      .when(h.second !== null, (el) => el.child(h_flex().child(div().flex_1()).child(chip(h.second))));
  }

  /**
   * S1 (col-lead3e): a section's title line is a control: `▾` open, `▸` folded; a click folds or
   * unfolds it, one change like every click (one build). `right` stands at the right end (MACHINES' advice).
   */
  foldHead(c, id, label, name, folded, right = null) {
    const { t } = c;
    return h_flex().id(id).px(4).py(2).gap(5).rounded(4).items_center()
      .cursor_pointer().hover((s) => s.bg(t.hover))
      .on_click((_e, cx) => {
        this.fold(name, !folded);
        changes += 1;
        cx.notify();
      })
      .tooltip(folded ? "click to unfold" : "click to fold")
      .child(div().w(10).flex_none().text_size(BADGE).text_color(t.dim).child(folded ? "▸" : "▾"))
      .child(title(t, label))
      .when(right !== null, (el) => el.child(div().flex_1()).child(right));
  }

  needs(c) {
    const { t, m } = c;
    const n = m.needs;
    // S1: folded, the title line and one line; no row is built. An item that arrives does not unfold
    // the section: the header chip, the badge and the number of this line show it.
    const folded = n.folded === true;
    return v_flex().gap(2)
      // N1 (col-lead3g): entries that wait and cannot be shown give the section its title line too.
      .when(n.total > 0 || n.cannotShow !== null, (el) => el.child(this.foldHead(c, "needs-head", "Needs you", "needs", folded)))
      .when(folded, (el) => el.child(foldedLine(t, n.foldedLine)))
      .when(!folded, (el) => el
        .children(n.rows.map((r) => this.needRow(c, r, false)))
        .when(n.more > 0, (x) => x.child(line(t, "needs-more", `+ ${n.more} more`, `show up to ${NEEDS_OPEN_MAX} rows; ${LEFT}`, () => {
          this.ui.moreNeeds = true;
        })))
        .when(n.notDrawn !== null, (x) => x.child(notDrawn(t, n.notDrawn, 4)))
        // N1 (col-lead3g): one muted line after the rows for the entries that wait and cannot be shown.
        .when(n.cannotShow !== null, (x) => x.child(div().px(4).text_size(CAPTION).text_color(toneColor(t, n.cannotShow.tone)).child(say(n.cannotShow.text, 120))))
        // W2 (col-lead3e): the tooltips name their largest numbers from the constants.
        .when(n.hiddenLine !== null, (x) => x.child(line(t, "needs-hidden",
          this.ui.hiddenOpen ? n.hiddenLine.replace("▸", "▾") : n.hiddenLine, `hidden rows: click to list up to ${HIDDEN_ROWS_MAX}; ${LEFT}`, () => {
            this.ui.hiddenOpen = !this.ui.hiddenOpen;
          })))
        .when(n.hiddenLine !== null && this.ui.hiddenOpen, (x) => x.children(n.hiddenRows.map((r) => this.needRow(c, r, true))))
        .when(n.hiddenLine !== null && this.ui.hiddenOpen && n.hiddenMore !== null, (x) => x.child(notDrawn(t, n.hiddenMore, 4)))
        .when(n.quietLine !== null, (x) => x.child(line(t, "quiet-line",
          this.ui.quietOpen ? n.quietLine.replace("▸", "▾") : n.quietLine,
          `lanes that never reported a status, so a prompt may wait: click to list up to ${QUIET_ROWS_MAX}; ${LEFT}`, () => {
            this.ui.quietOpen = !this.ui.quietOpen;
          })))
        .when(n.quietLine !== null && this.ui.quietOpen, (x) => x.children(n.quiet.map((q) => this.quietRow(c, q))))
        .when(n.quietLine !== null && this.ui.quietOpen && n.quietMore !== null, (x) => x.child(notDrawn(t, n.quietMore, 4))));
  }

  /**
   * One needs-you row: mark, machine chip, name, age; then what it asks. A hidden row is drawn grey.
   * G1, G5: the mark and its tone come from the model; a stale receipt is `?` and muted.
   * G1 (col-lead3e): a receipt row whose recipient is a safe target goes there on a click, decided at
   * the time of the click (go: a terminal gone since the draw runs nothing and opens nothing); while
   * the pointer is over it, `details` (or `close`) stands before `hide 1 h`. A receipt row with no
   * safe target opens and closes its detail, and draws `▸` or `▾` last on its first line.
   */
  needRow(c, r, hidden) {
    const { t } = c;
    const receipt = r.source === "receipt";
    const target = receipt && r.goThere !== null;
    const open = receipt && this.isOpen(r.key);
    const hot = this.hot === r.key;
    const right = !hot ? div().flex_none().text_size(CAPTION).text_color(t.muted).child(say(r.age, 8))
      : target ? h_flex().flex_none().gap(4)
        .child(control(t, eid("details", r.key), open ? "close" : "details", open ? "close the detail" : "open the detail", () => this.toggle(r.key)))
        .child(this.hideControl(c, r, hidden))
        : this.hideControl(c, r, hidden);
    return v_flex().id(eid(hidden ? "hid" : "need", r.key)).px(4).py(3).gap(1).rounded(4)
      .when(receipt || r.action !== null, (el) => el.cursor_pointer().hover((s) => s.bg(t.hover)))
      .on_hover((hovered, cx) => this.setHot(r.key, hovered, cx))
      .on_click((_e, cx) => {
        if (!receipt) go(r.address, this.sample);
        else if (target) go(r.goThereAddress, this.sample);
        else {
          this.toggle(r.key);
          changes += 1;
          cx.notify();
        }
      })
      .tooltip(!receipt ? tipFor(r.address, r.action) : target ? tipFor(r.goThereAddress, r.goThere) : "a receipt that needs you · click: details")
      .child(h_flex().gap(6).items_center()
        .child(div().w(8).flex_none().font_semibold().text_color(hidden ? t.dim : toneColor(t, r.tone)).child(say(r.mark, 2)))
        .child(tag(t, r.chip))
        .child(div().flex_1().min_w(0).truncate().text_color(hidden ? t.muted : toneColor(t, r.textTone)).child(say(r.name, 28)))
        .child(right)
        .when(receipt && !target, (el) => el.child(div().w(10).flex_none().text_size(BADGE).text_color(t.dim).child(open ? "▾" : "▸"))))
      // G2, X1 (col-lead3e): the second line is the model's (`asksLine`): with ` · open for what you can do`, or `released · …`.
      .child(div().pl(14).text_size(LABEL).text_color(toneColor(t, r.asksTone ?? "muted")).child(say(typeof r.asksLine === "string" ? r.asksLine : r.asks, 80)))
      .when(open, (el) => el.child(this.needDetail(c, r)));
  }

  /** Hide for an hour, or show again. The entry ends after an hour or when the item blocks again. */
  hideControl(c, r, hidden) {
    const { t, m } = c;
    return control(t, eid("hide", r.key), hidden ? "show" : "hide 1 h", hidden ? "show this row again" : "hide this row for an hour", () => {
      const next = { ...m.hidden };
      if (hidden) delete next[r.key];
      else next[r.key] = hideEntry(r, this.now);
      this.save("hidden", next);
      this.hot = null;
      // X1.2: a confirm block closes when its row is hidden.
      if (!hidden && this.confirm === r.key) this.confirm = null;
      // L6 (col-lead3b): counted by control() before this handler; one build, the badge, the publication.
      this.refresh();
    });
  }

  /**
   * A receipt's detail, in place under its row; Go there when the recipient is a live terminal.
   * G2 (col-lead3e): it starts with `first` (the model's whereLine) when the receipt has no safe target.
   */
  detail(c, key, lines, goThere, goThereAddress, first = null) {
    const { t } = c;
    return v_flex().pl(14).pt(2).gap(1).text_size(CAPTION).text_color(t.muted)
      .when(typeof first === "string", (el) => el.child(div().child(say(first, 90))))
      .children(lines.map((x) => div().child(say(x, 90))))
      .when(goThere !== null, (el) => el.child(h_flex().pt(2)
        .child(control(t, eid("go", key), "Go there", "go to the terminal this receipt went to", () => go(goThereAddress, this.sample)))));
  }

  /**
   * The detail of a receipt of NEEDS YOU (col-lead3e): the lines of detail(), then what the model's
   * `act` offers. P1 `copy command`; X1 `Clear…`, which opens the confirm block in its place; the
   * status line (`releasing…`, `not released: …`, why nothing is offered) in its tone.
   */
  needDetail(c, r) {
    const { t } = c;
    const a = isRecord(r.act) ? r.act : { copy: null, clear: null, confirm: null, status: null };
    const controls = a.copy !== null || a.clear !== null || r.goThere !== null;
    // X3 (col-lead3g): only under a check does a detail hold both the controls and the confirm block;
    // then the block stands under them. Outside a check it stands where it stood.
    const below = a.confirm !== null && (a.copy !== null || a.clear !== null);
    const confirmBlock = () => v_flex().pt(2).gap(1)
      .children(a.confirm.lines.map((x, i) => div().text_color(i === 0 ? t.foreground : t.muted).child(say(x, 90))))
      .child(h_flex().pt(2).gap(4)
        .child(control(t, eid("release", r.key), a.confirm.release, "release this receipt in the bridge now", (cx) => this.releaseNow(r, cx)))
        .child(control(t, eid("cancel", r.key), a.confirm.cancel, "keep the receipt as it is", () => {
          this.confirm = null;
        })));
    return v_flex().pl(14).pt(2).gap(1).text_size(CAPTION).text_color(t.muted)
      .when(typeof r.whereLine === "string", (el) => el.child(div().child(say(r.whereLine, 90))))
      .children((r.detail ?? []).map((x) => div().child(say(x, 90))))
      .when(a.status !== null, (el) => el.child(div().text_color(toneColor(t, a.status.tone)).child(say(a.status.text, 120))))
      .when(a.confirm !== null && !below, (el) => el.child(confirmBlock()))
      .when(controls, (el) => el.child(h_flex().pt(2).gap(4)
        .when(a.copy !== null, (x) => x.child(control(t, eid("copy", r.key), a.copy.text,
          r.state === "blocked" ? "put on the clipboard the bridge's command that lists what this worker asks a person to allow"
            : "put on the clipboard the bridge's command that releases this receipt", (cx) => this.copyCommand(r, cx), a.copy.tone ?? "foreground")))
        .when(a.clear !== null, (x) => x.child(control(t, eid("clear", r.key), a.clear.text, "release this receipt in the bridge, after one more step", () => {
          // X1.2: at most one confirm block in the column: opening one closes another.
          // X2 (col-lead3f): no click sets it with the switch off (the control is not drawn then either).
          if (releaseSwitchOn(this.clearSwitch())) this.confirm = r.key;
        })))
        .when(r.goThere !== null, (x) => x.child(control(t, eid("go", r.key), "Go there", "go to the terminal this receipt went to",
          () => go(r.goThereAddress, this.sample))))))
      .when(below, (el) => el.child(confirmBlock()));
  }

  /**
   * P1 (col-lead3e): one line on the clipboard per click, from the id and the state only; with sample data nothing.
   * R2 (col-lead3f): the line is built from the model on hand at the click, looked up by the row's key:
   * nothing when it does not list the receipt any more or its read is not current.
   */
  copyCommand(r, cx) {
    if (this.sample || checking()) return;
    const row = needsRowAt(this.cached, isRecord(r) ? r.key : null);
    if (row === null || row.current !== true) return;
    const text = commandLine(row.id, row.state);
    if (text === null) return;
    // P2 (col-lead3g): the clipboard may fail (no function, or it throws): nothing leaves the handler,
    // and the control reads `could not copy` until the next build of a tick or a read.
    let ok = false;
    try {
      if (isRecord(cx) && typeof cx.write_to_clipboard === "function") {
        cx.write_to_clipboard(text);
        ok = true;
      }
    } catch {
      ok = false;
    }
    this.copied = { key: r.key, ok };
  }

  /**
   * X1 (col-lead3e): the release, the one program this column runs. Only from the click handler of
   * `Release` (never from init, a build, render, the tick, the watch or a deferred step), never under a
   * check, never with sample data, one at a time. The argument list is releasePlan's, exactly; the
   * answer is judged from what the command printed (judgeRelease), never from its exit code. When it
   * settles, the handler builds once and notifies (one build per release that ended).
   * A view closed while a release runs: the program keeps running (in/api/AGENTS.md:996-998: an
   * exec already started keeps running; unfinished tasks are cancelled after 250 ms), so the bridge
   * may release the receipt; this copy draws nothing more and keeps nothing; the next copy sees the
   * receipt leave with the next document.
   */
  releaseNow(r, cx) {
    // X2 (col-lead3f): with the switch off this returns before it does anything.
    if (!releaseSwitchOn(this.clearProp)) return;
    if (checking() || this.sample || this.releasing) return;
    // R2 (col-lead3f): decided at the time of the click, from the model on hand, looked up by the
    // row's key. Not listed any more, in another state than uncertain, released already, or a read
    // that is not current: nothing runs and the confirm block closes.
    const row = needsRowAt(this.cached, isRecord(r) ? r.key : null);
    if (row === null || row.current !== true || row.state !== "uncertain" || row.released === true) {
      this.confirm = null;
      return;
    }
    // X5 (col-lead3g): a hidden row is never released: nothing runs and the confirm block closes.
    if (row.hidden === true) {
      this.confirm = null;
      return;
    }
    const plan = releasePlan(row.id, row.state, this.commandProp, this.homeDir, this.clearProp);
    if (plan === null) return;
    // R1 (col-lead3f): a context with no spawn could not report the answer: nothing is started.
    if (cx === null || cx === undefined || typeof cx.spawn !== "function") {
      this.confirm = null;
      return;
    }
    this.releasing = true;
    this.confirm = null;
    const id = row.id;
    const mark = { phase: "running", message: null };
    this.releases = new Map(this.releases).set(id, mark);
    let pending;
    try {
      // H1 (col-lead3g): what exec returns goes through Promise.resolve: a value that is no promise
      // cannot make this handler throw, also when cx.spawn throws below.
      pending = Promise.resolve(exec(plan.command, plan.args, { timeout: RELEASE_TIMEOUT_MS }));
    } catch (error) {
      pending = Promise.reject(error);
    }
    let dropped = false;
    try {
      cx.spawn(async (cx) => {
        let settled;
        try {
          settled = { status: "fulfilled", value: await pending };
        } catch {
          settled = { status: "rejected" };
        }
        if (dropped) return;
        this.releasing = false;
        const verdict = judgeRelease(settled, id);
        // A receipt no document lists any more was forgotten meanwhile: its answer is dropped.
        if (this.releases.get(id) === mark) {
          // X4 (col-lead3g): a mark `released` keeps the time of this judgement (readClock).
          this.releases.set(id, verdict.released ? { phase: "released", message: null, at: this.readClock() } : { phase: "refused", message: verdict.message });
        }
        this.refresh();
        cx.notify();
      });
    } catch {
      // R1 (col-lead3f): the task that waits for the answer could not be started. `releasing` goes
      // back to false and the mark is removed, so that a later click can run. The program that exec
      // started cannot be taken back: the row then shows `copy command` and `Clear…` again, as before
      // the click, while the bridge may still release the receipt; it leaves with the next reading.
      // A second Release before that runs the command again, and its answer is judged as usual
      // (a refusal reads `not released: …`). INFERRED: no real window was run.
      dropped = true;
      this.releasing = false;
      if (this.releases.get(id) === mark) {
        const next = new Map(this.releases);
        next.delete(id);
        this.releases = next;
      }
      pending.then(undefined, () => {});
    }
  }

  quietRow(c, q) {
    const { t } = c;
    return h_flex().id(eid("quiet", q.address)).gap(6).px(4).h(22).rounded(4).items_center()
      .when(q.action !== null, (el) => el.cursor_pointer().hover((s) => s.bg(t.hover)))
      .on_click(() => go(q.address, this.sample))
      .tooltip(tipFor(q.address, q.action))
      .child(div().w(8).flex_none().text_color(t.dim).child("·"))
      .child(tag(t, q.chip))
      .child(div().flex_1().min_w(0).truncate().text_color(t.muted).child(say(q.name, 28)))
      .child(div().flex_none().text_size(CAPTION).text_color(t.muted).child(say(q.age, 8)));
  }

  machines(c) {
    const { t, m } = c;
    const a = m.machines.advice;
    const strong = a.work.startsWith("New work:");
    // S1 (col-lead3e): the advice stays at the right end of the title line, and the suites line under it.
    const folded = m.machines.folded === true;
    const advice = div().flex_none().text_size(LABEL).text_color(strong ? t.foreground : t.muted)
      .when(strong, (el) => el.font_semibold()).child(say(a.work, 32));
    return v_flex().gap(2)
      .child(this.foldHead(c, "machines-head", "Machines", "machines", folded, advice))
      .when(a.suites !== null, (el) => el.child(div().px(4).text_size(LABEL).text_color(t.muted).text_right().child(say(a.suites, 80))))
      .when(folded, (el) => el.child(foldedLine(t, m.machines.foldedLine)))
      .when(!folded, (el) => el.children(m.machines.rows.map((r) => this.machineRow(c, r)))
        .when(m.machines.more !== null, (x) => x.child(notDrawn(t, m.machines.more, 4))));
  }

  /** One machine: state, name, the signal that decided, its meter and value, agents; then why; then what runs. */
  machineRow(c, r) {
    const { t } = c;
    // L1c (col-lead3c): this computer is `machine`, another machine `machine:` and its whole name, so
    // that a machine named `self` (the row key of this computer) never shares this computer's state.
    const key = r.name === null ? "machine" : `machine:${r.name}`;
    const open = this.isOpen(key);
    // Red only for the coordinator full on memory; busy and full use `working`; the rest is grey.
    const color = r.tone === "blocked" ? t.blocked : r.tone === "working" ? t.working : null;
    const loud = r.tone === "blocked" || r.tone === "working";
    const head = v_flex().id(eid("machine", r.key)).px(4).py(3).gap(1).rounded(4)
      .cursor_pointer().hover((s) => s.bg(t.hover))
      .on_click((_e, cx) => {
        this.toggle(key);
        changes += 1;
        cx.notify();
      })
      .tooltip(say(open ? `${r.full} · click to close` : `${r.full} · click for every signal and up to ${MACHINE_LANES_MAX} lanes; ${LEFT}`))
      .child(h_flex().gap(5).items_center()
        .child(div().w(10).flex_none().text_size(BADGE).text_color(color ?? t.dim).child(say(r.mark, 2)))
        .child(div().flex_none().text_size(LABEL).text_color(color ?? t.muted).child(say(r.word, 8)))
        .child(div().flex_1().min_w(0).truncate().font_semibold().child(say(r.label, 16)))
        .when(r.signal !== null, (el) => el.child(div().flex_none().text_size(CAPTION).text_color(t.muted).child(say(r.signal, 10))))
        .when(r.meter !== null, (el) => el.child(meterEl(t, eid("meter", r.key), r, r.signalKnown ? color : null)))
        .when(r.value !== null, (el) => el.child(div().flex_none().text_size(CAPTION)
          .text_color(r.greyed ? t.dim : t.foreground).child(say(r.value, 8))))
        .when(r.agents !== null, (el) => el.child(div().flex_none().text_size(CAPTION)
          .text_color(r.greyed && !r.coordinator ? t.dim : t.muted).child(say(r.agents, 16)))))
      .when(r.reason !== null, (el) => el.child(div().pl(15).text_size(LABEL).text_color(loud ? t.foreground : t.muted).child(say(r.reason, 120))))
      .when(r.work !== null, (el) => el.child(div().pl(15).text_size(LABEL).text_color(t.muted).child(say(r.work, 120))));
    return Collapsible.new().open(open).child(head).content(this.machineDetail(c, r));
  }

  /** The opened row: every signal with its number, the reading's age, every lane there, the phase 3 button. */
  machineDetail(c, r) {
    const { t } = c;
    const d = r.detail;
    const lane = (l) => v_flex().id(eid("lane", l.address)).px(4).py(2).rounded(4)
      .when(l.action !== null, (el) => el.cursor_pointer().hover((s) => s.bg(t.hover)))
      .on_click(() => go(l.address, this.sample))
      .tooltip(tipFor(l.address, l.action))
      .child(h_flex().gap(5).items_center()
        .child(div().w(10).flex_none().text_size(BADGE).text_color(l.blocked ? t.blocked : t.dim).child(say(l.mark, 2)))
        .child(div().flex_1().min_w(0).truncate().text_size(LABEL).child(say(l.name, 26)))
        .child(div().flex_none().text_size(CAPTION).text_color(t.muted).child(say([l.status, l.time].filter(Boolean).join(" · "), 24))))
      .when(l.by !== null, (el) => el.child(div().pl(15).text_size(BADGE).text_color(t.muted).child(say(l.by, 40))));
    return v_flex().pl(15).pb(4).gap(1)
      .children(d.signals.map((s) => h_flex().gap(6).items_start().text_size(CAPTION)
        .child(div().w(40).flex_none().text_color(t.muted).child(say(s.name, 8)))
        .child(div().flex_1().min_w(0).text_color(r.greyed ? t.dim : t.foreground).child(say(s.text, 120)))))
      .child(div().text_size(CAPTION).text_color(t.muted).child(say(d.age, 32)))
      .children(d.lanes.map(lane))
      // L9 (col-lead3c): at most MACHINE_LANES_MAX lanes; then one muted line, no click.
      .when(d.lanesMore !== null, (el) => el.child(notDrawn(t, d.lanesMore, 4)))
      // F6: plain shells with no status fold into one last line.
      .when(d.shells !== null, (el) => el.child(div().px(4).text_size(CAPTION).text_color(t.muted).child(say(`· ${d.shells}`, 24))))
      .when(d.lanes.length === 0 && d.shells === null, (el) => el.child(div().text_size(CAPTION).text_color(t.muted).child("no lane on this machine")))
      // Phase 3: drawn, never active. It has no click handler; the bridge's capacity command does not exist yet.
      .when(d.start !== null, (el) => el.child(h_flex().pt(3)
        .child(div().id(eid("start", r.key)).flex_none().px(8).h(20).rounded(4).border(1).border_color(t.border)
          .flex().items_center().text_size(CAPTION).text_color(t.muted).opacity(0.6)
          .disabled(true).tooltip("needs the capacity command").child(say(d.start, 56)))));
  }

  /** col-lead: who leads each project. What is drawn, and its tone words, are the model's (projects() in fleet.js). */
  projects(c) {
    const { t, m } = c;
    const p = m.projects;
    // S1 (col-lead3e): folded, the title line and the count of projects; no block is built.
    const folded = p.folded === true;
    return v_flex().gap(2)
      .child(this.foldHead(c, "projects-head", "Projects", "projectsSection", folded))
      .when(folded, (el) => el.child(foldedLine(t, p.foldedLine)))
      .when(!folded, (el) => el.children(p.rows.map((r) => this.projectBlock(c, r)))
        // K4 (col-lead2): the blocks beyond the first twelve, as one muted line with no click.
        .when(p.more !== null, (x) => x.child(div().px(4).py(2).text_size(CAPTION).text_color(toneColor(t, p.more.tone)).child(say(p.more.text, 32))))
        .when(p.none !== null, (x) => x.child(this.projectBlock(c, p.none))));
  }

  /** One project: its title line (a click opens or folds it), its lead lines always, then, opened, every other line. */
  projectBlock(c, r) {
    const { t } = c;
    const open = this.openAll || r.open === true;
    const head = h_flex().id(eid("project", r.key)).px(4).py(2).gap(5).rounded(4).items_center()
      .cursor_pointer().hover((s) => s.bg(t.hover))
      .on_click((_e, cx) => {
        const current = this.sample ? this.sampleState : this.state;
        this.save("folded", toggleProject(current.folded, r.key));
        changes += 1;
        cx.notify();
      })
      .tooltip(open ? "click to fold" : `click: up to ${PROJECT_LEADS_OPEN} leads and ${PROJECT_LINES_MAX} other agents and lanes of this project; ${LEFT}`)
      .child(div().w(10).flex_none().text_size(BADGE).text_color(t.dim).child(open ? "▾" : "▸"))
      .child(div().flex_1().min_w(0).truncate().font_semibold().text_color(toneColor(t, r.tone)).child(say(r.name, 28)))
      .when(typeof r.count === "string", (el) => el.child(div().flex_none().text_size(CAPTION).text_color(t.muted).child(say(r.count, 24))));
    return v_flex().gap(1)
      .child(head)
      // L3 (col-lead3): folded, at most three lead lines; opened, at most twelve. Then one muted line, no click.
      .children((open ? r.leads : r.foldedLeads).map((l) => this.projectLine(c, l, "plead")))
      .when((open ? r.leadsMore : r.foldedLeadsMore) !== null, (el) => el.child(div().pl(19).text_size(CAPTION)
        .text_color(toneColor(t, (open ? r.leadsMore : r.foldedLeadsMore).tone)).child(say((open ? r.leadsMore : r.foldedLeadsMore).text, 32))))
      .when(r.leadNote !== null, (el) => el.child(div().pl(19).text_size(CAPTION).text_color(toneColor(t, r.leadNote.tone)).child(say(r.leadNote.text, 24))))
      .when(r.noLead !== null, (el) => el.child(div().id(eid("nolead", r.key)).pl(19).py(2).text_size(LABEL)
        .text_color(toneColor(t, r.noLead.tone)).tooltip(say(r.noLead.tip)).child(say(r.noLead.text, 24))))
      .when(open, (el) => el.children(r.lines.map((l) => this.projectLine(c, l, "pline"))))
      // K4: the opened lines beyond the first twelve, as one muted line with no click.
      .when(open && r.linesMore !== null, (el) => el.child(div().pl(19).text_size(CAPTION).text_color(toneColor(t, r.linesMore.tone)).child(say(r.linesMore.text, 24))))
      .when(open && r.shells !== null, (el) => el.child(div().pl(19).text_size(CAPTION).text_color(t.muted).child(say(`· ${r.shells}`, 24))));
  }

  /**
   * One terminal of a project: mark, name, machine chip, status and time. A click goes to it (pane.focus or terminal.peek).
   * L8 (col-lead3b): a lead's mark is the column's own (LEAD_MARK) and does not pass `say`, which turns
   * every star-shaped character of a foreign text into `*`; so it is the only star in the column.
   */
  projectLine(c, l, prefix) {
    const { t } = c;
    return h_flex().id(eid(prefix, l.address)).gap(5).px(4).h(22).rounded(4).items_center()
      .when(l.action !== null, (el) => el.cursor_pointer().hover((s) => s.bg(t.hover)))
      .on_click(() => go(l.address, this.sample))
      .tooltip(tipFor(l.address, l.action))
      .child(div().w(10).flex_none().text_size(BADGE).text_color(toneColor(t, l.markTone)).child(l.lead === true ? LEAD_MARK : say(l.mark, 2)))
      .child(div().flex_1().min_w(0).truncate().text_size(LABEL).child(say(l.name, 26)))
      .child(tag(t, l.chip))
      .child(div().flex_none().text_size(CAPTION).text_color(toneColor(t, l.statusTone)).child(say([l.status, l.time].filter(Boolean).join(" · "), 24)));
  }

  receipts(c) {
    const { t, m } = c;
    const rc = m.receipts;
    const open = !rc.folded;
    // G5: the parts and their tone words are the model's (receiptsHeader). They wrap under the
    // title: "as of 5 min · 2 in progress · oldest 8 h · 3 new failures" is wider than the column.
    const parts = Array.isArray(rc.header.parts) ? rc.header.parts : [];
    const head = v_flex().id("receipts-head").px(4).py(2).gap(1).rounded(4)
      .cursor_pointer().hover((s) => s.bg(t.hover))
      .on_click((_e, cx) => {
        this.save("folded", { ...(this.sample ? this.sampleState : this.state).folded, receipts: open });
        changes += 1;
        cx.notify();
      })
      .tooltip(open ? "click to fold" : "click to unfold")
      .child(h_flex().gap(5).items_center()
        .child(div().w(10).flex_none().text_size(BADGE).text_color(t.dim).child(open ? "▾" : "▸"))
        .child(title(t, "Receipts")))
      .when(parts.length > 0, (el) => el.child(h_flex().pl(15).gap(4).flex_wrap()
        .children(parts.map((p, i) => div().flex_none().text_size(CAPTION)
        // F16: a stale read is never drawn as current, so nothing in its header is red.
        .text_color(toneColor(t, p.tone)).child(say(i === 0 ? p.text : `· ${p.text}`, 32))))));
    // S1 (col-lead3e): folded, no row is built (the header keeps its parts, as before).
    const body = !open ? v_flex() : v_flex().gap(2)
      .when(rc.since !== null, (el) => el.child(line(t, "receipts-since", rc.since?.text ?? "",
        "click: seen. The count starts again and listed failures turn grey", () => this.seen())))
      .children(rc.rows.map((r) => this.receiptRow(c, r)))
      .when(rc.older > 0, (el) => el.child(line(t, "receipts-more", `+ ${rc.older} older`, `show up to ${RECEIPTS_OPEN_MAX} listed receipts; ${LEFT}`, () => {
        this.ui.moreReceipts = true;
      })))
      .when(rc.notDrawn !== null, (el) => el.child(notDrawn(t, rc.notDrawn, 4)));
    return Collapsible.new().open(open).child(head).content(body);
  }

  /** "Seen": the totals now become the base of the `since` line; every listed failure turns grey. */
  seen() {
    const doc = this.sample ? this.sampleDoc : this.doc;
    const current = this.sample ? this.sampleState : this.state;
    const next = markSeen(doc, current.seen, this.now);
    this.save("seen", next.seen);
    this.save("base", next.base);
  }

  /** One receipt: mark, label, state, age; under a failure the verdict, which names the recipient. */
  receiptRow(c, r) {
    const { t } = c;
    const open = this.isOpen(r.key);
    const bright = r.tone === "blocked";
    return v_flex().id(eid("receipt", r.key)).px(4).py(3).gap(1).rounded(4)
      .cursor_pointer().hover((s) => s.bg(t.hover))
      .on_click((_e, cx) => {
        this.toggle(r.key);
        changes += 1;
        cx.notify();
      })
      .tooltip(open ? "click to close" : "click: details")
      .child(h_flex().gap(5).items_center()
        .child(div().w(10).flex_none().text_size(BADGE).text_color(bright ? t.blocked : t.dim).child(say(r.mark, 2)))
        .child(div().flex_1().min_w(0).truncate().text_color(r.tone === "muted" ? t.muted : t.foreground).child(say(r.label, 28)))
        .child(div().flex_none().text_size(CAPTION).text_color(bright ? t.blocked : t.muted).child(say(r.stateText, 20)))
        .child(div().flex_none().text_size(CAPTION).text_color(t.muted).child(say(r.age, 8))))
      .when(r.verdict !== null, (el) => el.child(div().pl(15).text_size(LABEL)
        .text_color(r.tone === "blocked" ? t.foreground : t.muted).child(say(r.verdict ?? "", 80))))
      .when(open, (el) => el.child(this.detail(c, r.key, r.detail, r.goThere, r.goThereAddress, r.whereLine ?? null)));
  }

  footer(c) {
    const { t, m } = c;
    const f = m.footer;
    const color = f.tone === "blocked" ? t.blocked : f.tone === "foreground" ? t.foreground : t.muted;
    return h_flex().px(4).pt(6).gap(6).items_center().border_t(1).border_color(t.border)
      .child(div().flex_1().min_w(0).truncate().text_size(CAPTION).text_color(color).child(say(f.text, 60)))
      .when(f.sample, (el) => el.child(div().flex_none().px(5).h(16).rounded(4).bg(t.raised).flex().items_center()
        .text_size(BADGE).text_color(t.muted).child("sample data")));
  }
}
