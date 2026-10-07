import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { archiveVerdict } from "../lib/panel.js";

const read = (name) => readFile(new URL(`../${name}`, import.meta.url), "utf8");


/* The panel reports on the pipe, not on the answer. Delivery is the half this
 * end of the exchange can prove: every promised file arrived, and each matched
 * the hash declared for it. What the responder said about its own work, and
 * whether the declared hashes were usable, are in the validation report on
 * disk — the operator reads them there.
 *
 * A version of this panel showed all three side by side, and a version demoted
 * a COMPLETE delivery to amber when the responder called its own work PARTIAL.
 * Both are the same mistake in opposite directions: the panel grading an answer
 * it cannot see, and an intact delivery worth reading looking like a failure.
 */
test("the panel shows delivery state and never work or hashes", async () => {
  const panel = await read("sidepanel.js");
  const lib = await read("lib/panel.js");

  for (const source of [panel, lib]) {
    assert.doesNotMatch(source, /resultFacts/);
    assert.doesNotMatch(source, /manifest_verified/);
    assert.doesNotMatch(source, /response_status/);
  }
});


test("an archive row carries the delivery verdict, and only that", () => {
  assert.deepEqual(archiveVerdict({ state: "COMPLETE" }), { label: "COMPLETE", tone: "ok" });
  assert.deepEqual(archiveVerdict({ state: "INCOMPLETE" }), { label: "INCOMPLETE", tone: "bad" });
  assert.deepEqual(archiveVerdict({ state: "STOPPED" }), { label: "STOPPED", tone: "" });
  assert.deepEqual(archiveVerdict({}), { label: "—", tone: "" });

  // An intact delivery of self-declared partial work reads as delivered, which
  // is what it is. The gaps are in the response, and that is where they belong.
  assert.deepEqual(
    archiveVerdict({ state: "COMPLETE", responseStatus: "PARTIAL" }),
    { label: "COMPLETE", tone: "ok" },
  );
});


/* Recall used to mean opening the exchange folder by hand. Every row is a
 * button now, and what it opens is metadata: state, report, defects, paths. */
test("past calls are inspectable from the panel", async () => {
  const html = await read("sidepanel.html");
  const panel = await read("sidepanel.js");
  const worker = await read("service_worker.js");

  assert.match(html, /id="past"/);
  // A button, not a list item, so it is reachable by keyboard.
  assert.match(panel, /h\("button", \{\s*class: "past-row",\s*type: "button",/);
  assert.match(panel, /type: "INSPECT", exchangeId/);
  assert.match(worker, /case "INSPECT":/);
  assert.match(worker, /nativeCommand\("call\.inspect"/);
});


/* The panel points at responses; it never renders them. Returned content stays
 * inert on disk, which is where "never execute what a call returns" can hold.
 */
test("recall shows metadata and paths, never response content", async () => {
  const panel = await read("sidepanel.js");

  assert.match(panel, /inspect\.paths\?\.main_response/);
  // Nothing reads a file body into the DOM.
  assert.doesNotMatch(panel, /inspect\.content/);
  assert.doesNotMatch(panel, /innerHTML/);
});


/* Every incomplete call sent the operator to a terminal to ask why. The same
 * diagnosis a correction round would send is on screen before they decide
 * whether to open one. */
test("defects are shown before a correction round is offered", async () => {
  const panel = await read("sidepanel.js");

  assert.match(panel, /inspect\.defects/);
  assert.ok(
    panel.indexOf("defectList(inspect.defects")
      < panel.indexOf('pill("Open correction round"'),
    "the diagnosis is built before the button that acts on it",
  );
  // The companion caps what it sends to keep inside the 1 MB frame; a capped
  // list that says it is complete is worse than no list.
  assert.match(panel, /inspect\.defects_omitted/);
});


test("a correction round is visible as a round number", async () => {
  const panel = await read("sidepanel.js");

  assert.match(panel, /inspect\.repair_round > 0/);
  assert.match(panel, /\["rounds", String\(inspect\.repair_round\)\]/);
});


/* A resend and the call it came from are two rows with the same subject,
 * separated by a timestamp. The link is the only thing that says which. */
test("a resend says what it is a copy of", async () => {
  const panel = await read("sidepanel.js");

  assert.match(panel, /item\.clonedFrom/);
  assert.match(panel, /"resend of"/);
});


/* An empty list says that it is empty. A heading with nothing under it reads
 * as a panel that failed to load, and the absence is itself information. */
test("an empty list says so", async () => {
  const html = await read("sidepanel.html");
  const panel = await read("sidepanel.js");

  for (const list of ["ready", "running", "past"]) {
    assert.match(html, new RegExp(`id="${list}-empty" class="empty" hidden`));
    assert.match(panel, new RegExp(`el\\("${list}-empty"\\)\\.hidden = (calls|rows)\\.length > 0`));
  }
});


/* A filing failure recorded durably by the companion is attributed to the call
 * that expected the filename, so it survives the browser restart that erases
 * session storage — which is exactly when the operator needs it. */
test("a durable filing failure is shown on the call it belongs to", async () => {
  const panel = await read("sidepanel.js");
  const worker = await read("service_worker.js");

  assert.match(panel, /call\.record\?\.download_failures/);
  assert.match(worker, /nativeCommand\("download\.failure\.record"/);
  // Session storage is written first: if the native channel is the broken
  // piece, the immediate record is all there is.
  const body = worker.slice(worker.indexOf("async function recordDownloadFailure"));
  assert.ok(
    body.indexOf("lastDownloadFailure") < body.indexOf("download.failure.record"),
    "the session record is written before the durable one",
  );
});


/* Cloning prepares; it never sends. The finished call keeps its response,
 * because that response is the only copy of work the model already did. */
test("preparing a copy does not send it, and does not touch the original", async () => {
  const panel = await read("sidepanel.js");
  const worker = await read("service_worker.js");

  assert.match(panel, /type: "CLONE", exchangeId: item\.id/);
  assert.match(worker, /nativeCommand\("call\.clone"/);
  // The clone lands in the top list as a prepared call, and Go is a separate
  // press. Nothing chains one into the other.
  const body = panel.slice(panel.indexOf('pill("Prepare a copy"'));
  assert.doesNotMatch(body.slice(0, 400), /type: "GO"/);
});


/* The history drawer is filled by a function that awaits the companion first.
 * It is `async`, so what it returns is a Promise, and appending a Promise
 * writes "[object Promise]" into the drawer - which an earlier panel did.
 *
 * So it appends into the drawer itself, and its result is discarded where it
 * is called, visibly, with `void`. Nothing may hand that result to the page.
 */
test("an async drawer builder cannot leak a Promise into the page", async () => {
  const panel = await read("sidepanel.js");

  assert.match(panel, /async function fillDetail\(detail, item\)/);
  assert.match(panel, /^\s*void fillDetail\(detail, item\);$/m);
  assert.equal(panel.match(/fillDetail\(/g).length, 2, "defined once, called once");
  assert.doesNotMatch(panel, /append\([^)]*fillDetail\(/);
  assert.doesNotMatch(panel, /replaceChildren\([^)]*fillDetail\(/);
});


/* The boundary, restated as a test because a redesign is exactly when it would
 * be eroded by accident.
 *
 * One line of it moved, on request. Done was a button the operator pressed,
 * "never a timer"; a call now finishes itself when its archive lands. What did
 * not move is everything a person still decides: what is attached, what is
 * sent, and what is downloaded. No software here presses a key, clicks a
 * control on the site, or starts a download.
 */
test("no panel code sends, attaches or downloads on its own", async () => {
  const panel = await read("sidepanel.js");
  const worker = await read("service_worker.js");

  for (const source of [panel, worker]) {
    assert.doesNotMatch(source, /Input\.dispatchKeyEvent/);
    assert.doesNotMatch(source, /\.click\(\)/);
    assert.doesNotMatch(source, /chrome\.downloads\.download/);
  }
});


/* Finishing is the companion's decision, taken from the archive. The panel has
 * no Done to press and no way to ask for one; it can only ask the companion to
 * look in the folder, and the companion finishes nothing that has not landed. */
test("the panel cannot finish a call; it can only ask the companion to look", async () => {
  const panel = await read("sidepanel.js");
  const worker = await read("service_worker.js");

  assert.doesNotMatch(panel, /"DONE"/);
  assert.match(panel, /type: "COLLECT"/);
  assert.match(worker, /case "COLLECT":\s*\n(?:\s*\/\/[^\n]*\n)*\s*return collectLanded\(\);/);
  assert.match(worker, /nativeCommand\("calls\.collect"\)/);
});
