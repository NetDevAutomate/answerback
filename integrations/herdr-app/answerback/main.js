// Answerback panel for the Herdr App: a read-only view of the Answerback bridge on this machine.
// Written by the Answerback project against the Herdr App mod interface (herdr.d.ts, gpui-kit).

import { View, div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { checking, exec, home, setBadge, theme } from "herdr";
import { readBridge } from "./bridge.js";
import { badgeText, formatAge, resolveOptions } from "./logic.js";

/**
 * @param {Record<string, string>} t
 * @param {string | null} word a service or receipt state from the fixed vocabulary
 * @returns {string}
 */
function stateColor(t, word) {
  if (word === "running" || word === "completed") return t.done;
  if (word === "uncertain" || word === "blocked" || word === "failed" || word === "not responding") return t.blocked;
  if (word === "pending" || word === "queued" || word === "submitted" || word === "stopping") return t.working;
  return t.dim;
}

export default class AnswerbackPanel extends View {
  /**
   * @param {unknown} props
   * @param {any} cx
   */
  init(props, cx) {
    this.options = resolveOptions(props, home());
    /** @type {import("./bridge.js").Snapshot | null} */
    this.snapshot = null;
    this.busy = false;
    /** @type {string | null} the text this copy last put on the Dock icon */
    this.badge = null;
    this.checking = checking();
    if (!this.options.ok || this.checking) return;
    cx.timer.every(this.options.intervalMs, (cx) => this.refresh(cx));
    this.refresh(cx);
  }

  /** @param {any} cx */
  refresh(cx) {
    if (this.busy) return;
    this.busy = true;
    cx.spawn(async (cx) => {
      try {
        this.snapshot = await readBridge(exec, this.options);
      } finally {
        this.busy = false;
      }
      this.updateBadge();
      cx.notify();
    });
  }

  updateBadge() {
    if (!this.options.badge || !this.snapshot) return;
    const text = badgeText(this.snapshot.needs);
    if (text === this.badge) return;
    this.badge = text;
    setBadge(text);
  }

  async close() {
    if (this.badge !== null) setBadge(null);
  }

  /**
   * @param {Record<string, string>} t
   * @param {string} text
   */
  line(t, text) {
    return div().text_color(t.muted).child(text);
  }

  /**
   * @param {Record<string, string>} t
   * @param {string} label
   * @param {number} count
   * @param {string} color
   */
  countRow(t, label, count, color) {
    return h_flex().justify_between()
      .child(div().text_color(t.muted).child(label))
      .child(div().text_color(count > 0 ? color : t.dim).child(String(count)));
  }

  /** @param {Record<string, string>} t */
  body(t) {
    if (!this.options.ok) return [this.line(t, this.options.line)];
    if (this.checking) return [this.line(t, "The Answerback CLI is not run during a check.")];
    const s = this.snapshot;
    if (!s) return [this.line(t, "Reading the bridge status…")];
    if (s.line !== null) return [this.line(t, s.line)];

    const rows = [
      h_flex().justify_between()
        .child(div().text_color(t.muted).child("Service"))
        .child(h_flex().gap(6)
          .when(s.version !== "", (el) => el.child(div().text_color(t.dim).child(s.version)))
          .child(div().text_color(stateColor(t, s.service)).child(s.service ?? "unknown"))),
    ];
    const c = s.counts;
    if (c) {
      rows.push(
        this.countRow(t, "Pending", c.pending, t.working),
        this.countRow(t, "Submitted", c.submitted, t.working),
        this.countRow(t, "Uncertain", c.uncertain, t.blocked),
        this.countRow(t, "Waiting for permission", c.blocked, t.blocked),
        this.countRow(t, "Completed", c.completed, t.done),
      );
      if (c.other > 0) rows.push(this.countRow(t, "Other", c.other, t.foreground));
      if (s.needs > 0) {
        rows.push(div().text_color(t.blocked).font_semibold()
          .child(s.needs === 1 ? "1 receipt needs a person" : `${s.needs} receipts need a person`));
      }
    } else {
      rows.push(this.line(t, "Receipt counts are not available while the service is not running."));
    }
    if (s.service !== "running") return rows;

    rows.push(div().text_color(t.muted).font_semibold().child("Newest receipts"));
    if (s.listLine !== null) rows.push(this.line(t, s.listLine));
    else if (s.receipts.length === 0) rows.push(this.line(t, "No receipts yet."));
    for (const r of s.receipts) {
      const age = r.created === null ? "?" : formatAge(s.at / 1000 - r.created);
      rows.push(h_flex().gap(8)
        .child(div().flex_1().min_w_0().truncate().child(r.peer))
        .child(div().flex_none().text_color(stateColor(t, r.state)).child(r.state))
        .child(div().flex_none().text_color(t.dim).child(age)));
    }
    return rows;
  }

  render() {
    const t = theme();
    return v_flex().id("answerback-root").size_full().bg(t.surface).p(10).gap(6)
      .text_size(13).text_color(t.foreground).overflow_y_scrollbar()
      .child(h_flex().justify_between()
        .child(div().font_semibold().child("Answerback"))
        .child(div().text_color(t.dim).child("read-only")))
      .children(this.body(t));
  }
}
