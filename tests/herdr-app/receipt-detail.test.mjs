// The detail lines of one receipt on the fleet page (T-226 review F2). Run with
// `node --test tests/herdr-app/receipt-detail.test.mjs` (tests/test_herdr_app.py runs it under
// pytest when node is on PATH). Machine names are invented.
import assert from "node:assert/strict";
import { test } from "node:test";

import { receiptDetail } from "../../src/answerback_bridge/data/herdr-app/mods/answerback-fleet/fleet.js";

const TO = { provider: "hc", machine: "nuc01", terminal: "t15", text: "nuc01/t15" };

function lines(more) {
  return receiptDetail({ id: "0123456789abcdef", recipient: TO, ...more }, 0);
}

test("a verified send names the core that read the identity", () => {
  const got = lines({ state: "submitted", identity_guard: "observed_before_and_after", identity_observer: "core:nuc01" });
  assert.ok(got.includes("identity read by core:nuc01"));
});

test("a receipt that was never sent does not say an identity was read", () => {
  const got = lines({ state: "failed", reason: "delivery_not_attempted", identity_guard: null, identity_observer: "core:nuc01" });
  assert.ok(got.includes("why: delivery not attempted"));
  assert.ok(!got.some((line) => line.startsWith("identity read by")));
});

test("an observer without the guard, or not a core, prints nothing", () => {
  for (const more of [
    { identity_observer: "core:nuc01" },
    { identity_guard: "observed", identity_observer: "core:nuc01" },
    { identity_guard: "observed_before_and_after", identity_observer: "nuc01" },
  ]) {
    assert.ok(!lines(more).some((line) => line.startsWith("identity read by")), JSON.stringify(more));
  }
});
