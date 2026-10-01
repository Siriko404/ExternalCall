import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { callCentre } from "../lib/panel.js";


/* Recovery is per call now, not a card of its own: a lost call is a row in the
 * top list whose drawer offers Resume instead of Done. Which matters when
 * several calls are running, because a restart can leave one bound and another
 * not - the old single card treated recovery as a state of the whole panel. */
test("a call the browser lost is marked, and one it still holds is not", () => {
  const { now } = callCentre({
    active: [{ exchange_id: "ex-1" }, { exchange_id: "ex-2" }],
    handoffs: [{ exchangeId: "ex-2", subject: "still bound" }],
  });

  assert.deepEqual(
    now.map((call) => [call.id, call.needsResume]),
    [["ex-1", true], ["ex-2", false]],
  );
});


test("side panel exposes an explicit user-controlled Resume action", async () => {
  const panel = await readFile(new URL("../sidepanel.js", import.meta.url), "utf8");
  const worker = await readFile(new URL("../service_worker.js", import.meta.url), "utf8");

  assert.match(panel, /type: "RESUME"/);
  assert.match(panel, /exchangeId: call\.id/);
  assert.match(worker, /case "RESUME"/);
  assert.match(worker, /nativeCommand\("call\.resume"/);
});


/* One zip goes up with the prompt inside it, so the message ChatGPT receives
 * says nothing on its own and a bare archive gets a model asking what to do.
 * The companion writes the line; it is typed once the attachment lands.
 *
 * The ordering is the part that breaks silently, in both directions. Go must
 * NOT type the line: text sitting in the composer before the archive is
 * attached can be sent ahead of it. And completeAttachment must not type
 * until its own debugger session is detached — insertPromptIntoComposer
 * attaches its own session, and a second attach on an armed tab throws and
 * strips the file-chooser interception off a tab waiting for it.
 */
test("Go holds the launch line back; the attachment triggers the typing", async () => {
  const worker = await readFile(new URL("../service_worker.js", import.meta.url), "utf8");

  const goBody = worker.slice(
    worker.indexOf("async function beginGo"),
    worker.indexOf("async function typeLaunchPrompt"),
  );
  assert.ok(
    goBody.indexOf("typeLaunchPrompt") === -1,
    "beginGo must not type the launch line",
  );
  assert.match(goBody, /pendingLaunchPrompt/, "beginGo holds the line on the handoff");
  assert.match(goBody, /armTab\(/, "beginGo arms the tab");

  const attachBody = worker.slice(
    worker.indexOf("async function completeAttachment"),
    worker.indexOf("async function findFileInputNode"),
  );
  const detached = attachBody.indexOf("safeDetach(source.tabId)");
  const typed = attachBody.indexOf("typeLaunchPrompt");
  assert.ok(detached > 0, "completeAttachment detaches after setting files");
  assert.ok(typed > detached, "the launch line is typed only after the detach");

  assert.match(worker, /result\.launch_prompt/);
  // Failing to type it hands the operator the text instead of losing the call.
  assert.match(worker, /inserted: false/);
});


/* A fresh conversation needs telling what the attachment is. A thread the
 * operator is already working in does not: it has the context that makes the
 * archive make sense, and typing into it would put words in a conversation the
 * operator is conducting. */
test("the launch line is typed only into a fresh conversation", async () => {
  const worker = await readFile(new URL("../service_worker.js", import.meta.url), "utf8");

  const body = worker.slice(
    worker.indexOf("async function typeLaunchPrompt"),
    worker.indexOf("async function resumeCall"),
  );
  assert.match(body, /if \(mode === "current"\) \{\s*\n\s*return \{ inserted: null/);
  assert.ok(
    body.indexOf('mode === "current"') < body.indexOf("insertPromptIntoComposer"),
    "the current-conversation case returns before anything is typed",
  );
});


test("a launch line that could not be typed is offered to copy; one never attempted is not", async () => {
  const panel = await readFile(new URL("../sidepanel.js", import.meta.url), "utf8");

  assert.match(panel, /function showLaunchPrompt\(handoff\)/);
  // Strictly false. `null` is "never attempted" and must not surface a card.
  assert.match(panel, /handoff\.launchInserted !== false/);
  assert.match(panel, /showPromptToCopy\(handoff\.launchPrompt\)/);
  assert.match(panel, /showPromptToCopy\(handoff\?\.repairPrompt\)/);
});


/* Resume is the path a conductor call takes after a browser restart, and a
 * restart is exactly when the handoff naming its tab is gone. Resolving the
 * destination the same way Go does is what keeps the thread; creating a tab
 * outright would lose it with no error and no warning.
 */
test("resume sends the destination, and resolves it the way Go does", async () => {
  const panel = await readFile(new URL("../sidepanel.js", import.meta.url), "utf8");
  const worker = await readFile(new URL("../service_worker.js", import.meta.url), "utf8");

  assert.match(panel, /mode: destinationFor\(call\.id\)/);
  assert.match(panel, /provider: providerFor\(call\.id\)/);
  assert.match(worker, /case "RESUME":\s*\n\s*return resumeCall\(message\.exchangeId, message\.mode, message\.provider\);/);
  assert.match(worker, /async function resumeCall\(exchangeId, mode, provider\) \{\s*\n\s*const agent = normalizeProvider\(provider\);/);
  assert.doesNotMatch(
    worker.slice(worker.indexOf("async function resumeCall")),
    /chrome\.tabs\.create/,
  );
});


/* What a browser restart destroys is the binding to a conversation, and that
 * cannot be guessed back. An early panel resumed with whatever the shared Go
 * dropdown happened to say, which is how a conductor call was once re-armed
 * against a blank conversation with no warning. Recovery asks, and Resume stays
 * disabled until the operator answers.
 */
test("recovery refuses to guess the destination a restart destroyed", async () => {
  const panel = await readFile(new URL("../sidepanel.js", import.meta.url), "utf8");

  // Disabled until this call — not the panel, this call — has been answered
  // for. destinationFor falls back to a remembered default, so `has` is the
  // question being asked here and not `get`.
  assert.match(panel, /resume\.disabled = !destinations\.has\(call\.id\)/);
  assert.ok(
    panel.indexOf("if (call.needsResume)") < panel.indexOf("resume.disabled"),
    "the resume path is the one that holds its button",
  );
});


/* The single shared dropdown was the real hazard in the old panel: two calls
 * running at once read the same value, so the second inherited an answer given
 * about the first. A destination belongs to the call it will be used for. */
test("a destination is remembered per call, never once for the panel", async () => {
  const panel = await readFile(new URL("../sidepanel.js", import.meta.url), "utf8");

  assert.match(panel, /const destinations = new Map\(\)/);
  assert.match(panel, /destinations\.set\(exchangeId, picker\.value\)/);
  assert.match(panel, /function destinationFor\(exchangeId\)/);
  // Every send resolves the destination through the call it belongs to.
  assert.doesNotMatch(panel, /mode: goMode\.value/);
  assert.doesNotMatch(panel, /mode: recoveryMode\.value/);
});
