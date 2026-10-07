import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { callCentre } from "../lib/panel.js";


test("an empty active list is reported as absence, not as a running call", async () => {
  const worker = await readFile(new URL("../service_worker.js", import.meta.url), "utf8");

  // [] is truthy in JavaScript, so any panel testing `if (state.active)` would
  // treat "no calls running" as "a call is running".
  assert.match(worker, /active\.length === 0 \? null : active/);
});


/* The companion reports absence as null and presence as an array, so every
 * consumer has to count rather than test. callCentre is the one place that
 * normalises it now, which is why this is a behaviour test and not a grep. */
test("the panel counts the active list, and never tests it for truthiness", async () => {
  const panel = await readFile(new URL("../sidepanel.js", import.meta.url), "utf8");
  assert.doesNotMatch(panel, /if \(state\.active\)/);

  assert.deepEqual(callCentre({ active: null }).running, []);
  assert.deepEqual(callCentre({}), { ready: [], running: [], past: [] });
  assert.equal(callCentre({ active: [{ exchange_id: "ex-1" }] }).running.length, 1);
});


/* A call appears once. `recent` is every exchange the companion knows about,
 * prepared and running ones included, so History is what is left after the two
 * lists above it have taken what they need. Otherwise a running call sits in
 * the panel twice: once as work, once as history. */
test("a call is in exactly one of the three lists", () => {
  const { ready, running, past } = callCentre({
    active: [{ exchange_id: "ex-run" }],
    ready: [{ exchange_id: "ex-ready", subject: "ready one" }],
    recent: [
      { exchange_id: "ex-run", subject: "running one", state: "ACTIVE" },
      { exchange_id: "ex-ready", subject: "ready one", state: "PREPARED" },
      { exchange_id: "ex-old", subject: "finished one", state: "COMPLETE" },
    ],
  });

  assert.deepEqual(ready.map((call) => call.id), ["ex-ready"]);
  assert.deepEqual(running.map((call) => call.id), ["ex-run"]);
  assert.deepEqual(past.map((row) => row.id), ["ex-old"]);
});


/* The order the operator asked for, and the order the work happens in: what
 * is ready to send, then what is out, then what is over. It used to be running
 * first; that was changed on request, so the page is pinned and not only the
 * function. */
test("the panel is Ready, then Running, then History", async () => {
  const html = await readFile(new URL("../sidepanel.html", import.meta.url), "utf8");

  const ready = html.indexOf('id="ready"');
  const running = html.indexOf('id="running"');
  const past = html.indexOf('id="past"');
  assert.ok(ready > 0 && ready < running && running < past);
  // History is folded until it is opened, and a folded list is out of reach of
  // the keyboard as well as the eye.
  assert.match(html, /id="history-toggle"[^>]*aria-expanded="false"/);
  assert.match(html, /id="history-wrap" class="collapse" inert/);
});


/* The project is what tells two calls with similar titles apart, so every list
 * carries it. A call prepared before jobs had a project simply has none. */
test("every list carries the project, and a call without one still renders", () => {
  const { ready, running, past } = callCentre({
    ready: [
      { exchange_id: "ex-a", subject: "Audit", project: "dalio" },
      { exchange_id: "ex-b", subject: "Old audit" },
    ],
    active: [{ exchange_id: "ex-c" }],
    progress: [{ exchange_id: "ex-c", subject: "Lap review", project: "arracing" }],
    handoffs: [{ exchangeId: "ex-c", status: "ATTACHED" }],
    recent: [{ exchange_id: "ex-d", subject: "Survey", project: "creditml", state: "COMPLETE" }],
  });

  assert.deepEqual(ready.map((call) => call.project), ["dalio", null]);
  assert.deepEqual(running.map((call) => [call.subject, call.project]), [["Lap review", "arracing"]]);
  assert.deepEqual(past.map((row) => row.project), ["creditml"]);
});
