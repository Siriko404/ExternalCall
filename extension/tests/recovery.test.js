import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { callCentre } from "../lib/panel.js";


/* Recovery is per call now, not a card of its own: a lost call is a row in the
 * top list whose drawer offers Resume instead of Done. Which matters when
 * several calls are running, because a restart can leave one bound and another
 * not - the old single card treated recovery as a state of the whole panel. */
test("a call the browser lost is marked, and one it still holds is not", () => {
  const { running } = callCentre({
    active: [{ exchange_id: "ex-1" }, { exchange_id: "ex-2" }],
    handoffs: [{ exchangeId: "ex-2", subject: "still bound" }],
  });

  assert.deepEqual(
    running.map((call) => [call.id, call.phase]),
    [["ex-1", "lost"], ["ex-2", "waiting"]],
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

  const lost = panel.slice(panel.indexOf('if (call.phase === "lost")'));
  assert.match(lost.slice(0, lost.indexOf("return card;")),
    /type: "RESUME",\s*exchangeId: call\.id,\s*mode,\s*provider: choice\.agent,/);
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
 * against a blank conversation with no warning.
 *
 * Go now reads a shared toggle, so this matters more than it did. Recovery
 * does not read it: a lost call offers two buttons, one per destination, and
 * pressing one is the answer, given for that call. There is no Resume that
 * falls back on a remembered default.
 */
test("recovery refuses to guess the destination a restart destroyed", async () => {
  const panel = await readFile(new URL("../sidepanel.js", import.meta.url), "utf8");

  const start = panel.indexOf('if (call.phase === "lost")');
  const lost = panel.slice(start, panel.indexOf("return card;", start));
  assert.ok(start > 0 && lost.length > 0, "the lost-call branch is where this test thinks");

  assert.match(lost, /const resume = \(mode\) =>/);
  assert.match(lost, /pill\("Resume in a new tab", \(\) => resume\("new"\)/);
  assert.match(lost, /pill\("Resume in this chat", \(\) => resume\("current"\)/);
  // The shared destination toggle is never consulted for a lost call.
  assert.doesNotMatch(lost, /choice\.destination/);
  assert.equal(panel.match(/type: "RESUME"/g).length, 1, "there is no other way to resume");
});


/* Each call used to carry its own destination picker, because a shared
 * dropdown hidden in a drawer let a second call inherit an answer given about
 * the first. That cost a drawer and two selects per send, and it was replaced
 * on request: the agent and the destination are two toggles that are always on
 * screen above the list, and the whole panel is tinted by the agent.
 *
 * What keeps the old hazard out is when the toggles are read. Go reads them in
 * its click handler, at the moment of the press, and sends both values with
 * the call. Nothing is captured when the list is drawn, so what Go sends is
 * what the toggles showed when it was pressed.
 */
test("Go sends where the toggles point at the moment it is pressed", async () => {
  const panel = await readFile(new URL("../sidepanel.js", import.meta.url), "utf8");

  const start = panel.indexOf('go.addEventListener("click"');
  const press = panel.slice(start, panel.indexOf("showLaunchPrompt(handoff);", start));
  assert.ok(start > 0, "Go has a click handler");
  assert.match(press, /type: "GO",\s*exchangeId: call\.id,\s*mode: choice\.destination,\s*provider: choice\.agent,/);
  assert.equal(panel.match(/type: "GO"/g).length, 1);

  // Both toggles are in the page, not in a drawer, and both are remembered.
  const html = await readFile(new URL("../sidepanel.html", import.meta.url), "utf8");
  assert.match(html, /id="seg-agent" class="seg" role="radiogroup"/);
  assert.match(html, /id="seg-dest" class="seg" role="radiogroup"/);
  assert.match(panel, /chrome\.storage\.local\.set\(\{ agent: value \}\)/);
  assert.match(panel, /chrome\.storage\.local\.set\(\{ goMode: value \}\)/);
});
