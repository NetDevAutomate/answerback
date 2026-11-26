// The status line (footer) of the Answerback fleet column, as T-181 changed it. Run with
// `node --test tests/herdr-app/footer.test.mjs` (tests/test_herdr_app_mod.py runs it under pytest
// when node is on PATH). fleet.js and fixtures.js import nothing from the Herdr App.
import assert from "node:assert/strict";
import { test } from "node:test";

import { FIXTURES } from "../../src/answerback_bridge/data/herdr-app/mods/answerback-fleet/fixtures.js";
import {
  OLD_AFTER_S, VERSION_MAX, WAITING, footer, footerVersion,
} from "../../src/answerback_bridge/data/herdr-app/mods/answerback-fleet/fleet.js";

/** A fresh document, 40 s old at `now`, with the given bridge and writer versions. Names are invented. */
function document(bridge, writer, more = {}) {
  const service = { state: "running", accepting: true, started: 1_700_000_000 };
  if (bridge !== undefined) service.version = bridge;
  return {
    schema: "answerback.fleet/1",
    written_at: 1_700_000_400,
    writer: writer === undefined ? { pid: 1 } : { version: writer, pid: 1 },
    health: { service, bus: { connected: true }, ...more },
  };
}
const NOW = (1_700_000_400 + 40) * 1000;

test("one version when the writer is the installed bridge", () => {
  const f = footer(document("0.14.0+internal", "0.14.0+internal"), NOW);
  assert.equal(f.text, "bridge 0.14.0 · bus ok · data 40 s old");
  assert.equal(f.tone, "muted");
});

test("the bridge's version is named as bridge, the writer's only when it differs", () => {
  const f = footer(document("0.14.0+internal", "0.13.0"), NOW);
  assert.equal(f.text, "bridge 0.14.0 · writer 0.13.0 · bus ok · data 40 s old");
});

test("a document without the bridge's version still names the writer's", () => {
  assert.equal(footer(document(undefined, "0.13.0"), NOW).text, "bridge · writer 0.13.0 · bus ok · data 40 s old");
  assert.equal(footer(document(undefined, undefined), NOW).text, "bridge · bus ok · data 40 s old");
});

test("a reading under 5 s old reads as live, not new", () => {
  const f = footer(document("0.14.0", "0.14.0"), 1_700_000_400 * 1000 + 4_999);
  assert.equal(f.text, "bridge 0.14.0 · bus ok · data live");
  assert.ok(!f.text.includes("data new"));
});

test("a bus that is down keeps its word and its tone", () => {
  const f = footer(document("0.14.0", "0.14.0", { bus: { connected: false } }), NOW);
  assert.equal(f.text, "bridge 0.14.0 · bus down · data 40 s old");
  assert.equal(f.tone, "foreground");
});

test("versions are cut to 12 characters, the last an ellipsis, and lose their build part", () => {
  assert.equal(VERSION_MAX, 12);
  assert.equal(footerVersion({ version: "0.14.0+internal" }, "version"), "0.14.0");
  assert.equal(footerVersion({ version: "1234567890123456+local" }, "version"), "12345678901…");
  assert.equal(footerVersion({ version: 14 }, "version"), "");
  assert.equal(footerVersion(null, "version"), "");
  assert.equal(footerVersion({}, "version"), "");
  const f = footer(document("1234567890123456", "abcdefghijklmnop"), NOW);
  assert.equal(f.text, "bridge 12345678901… · writer abcdefghijk… · bus ok · data 40 s old");
});

test("an old document, a stopped service and no document read as before", () => {
  const doc = document("0.14.0", "0.13.0");
  assert.equal(footer(doc, (1_700_000_400 + OLD_AFTER_S + 1) * 1000).text, "bridge data 2 min old");
  const stopped = document("0.14.0", "0.13.0");
  stopped.health.service.state = "stopping";
  assert.equal(footer(stopped, NOW).text, "bridge stopping · data 40 s old");
  assert.equal(footer(stopped, NOW).tone, "blocked");
  assert.equal(footer(null, NOW).text, WAITING);
});

test("the sample document names one version and keeps its footer within the drawn bound", () => {
  const f = footer(JSON.parse(FIXTURES.documentText), FIXTURES.nowMs, true);
  assert.equal(f.text, "bridge 0.12.0 · bus ok · data 40 s old");
  assert.equal(f.sample, true);
  assert.ok(f.text.length <= 60);
});
