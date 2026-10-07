import assert from "node:assert/strict";
import test from "node:test";

/* The service worker, run for real against a stand-in browser.
 *
 * Done used to be a press, and the tests could pin that by reading the source
 * for a button. A call now finishes itself, so what has to be pinned is
 * behaviour: which native commands are sent, in what order, for which
 * exchange, and - more than anything - which are not sent. Everything below
 * drives the worker's own listeners and reads what it asked the companion.
 */

function memoryStorage() {
  const data = new Map();
  return {
    data,
    async get(keys) {
      if (keys === null || keys === undefined) return Object.fromEntries(data);
      const wanted = Array.isArray(keys) ? keys : [keys];
      return Object.fromEntries(wanted.filter((key) => data.has(key)).map((key) => [key, data.get(key)]));
    },
    async set(values) {
      for (const [key, value] of Object.entries(values)) data.set(key, value);
    },
    async remove(keys) {
      for (const key of Array.isArray(keys) ? keys : [keys]) data.delete(key);
    },
  };
}

const session = memoryStorage();
const local = memoryStorage();
const listeners = {};
const hook = (name) => ({ addListener: (fn) => { listeners[name] = fn; } });
const browser = { downloads: [], alarms: new Map(), native: [], answers: {}, detached: [] };

globalThis.chrome = {
  storage: { session, local },
  runtime: {
    onInstalled: hook("installed"),
    onStartup: hook("startup"),
    onMessage: hook("message"),
    async sendNativeMessage(_host, message) {
      browser.native.push({ command: message.command, payload: message.payload });
      const answer = browser.answers[message.command];
      const value = typeof answer === "function" ? answer(message.payload) : answer;
      if (value instanceof Error) return { ok: false, error: { message: value.message } };
      return { ok: true, result: value ?? null };
    },
    async sendMessage() {},
  },
  debugger: {
    onEvent: hook("debugger"),
    async attach() {},
    async detach(target) { browser.detached.push(target.tabId); },
    async sendCommand() { return {}; },
  },
  downloads: {
    onChanged: hook("downloads"),
    async search(query) {
      return browser.downloads.filter((item) => query.id === undefined || item.id === query.id);
    },
  },
  alarms: {
    onAlarm: hook("alarm"),
    async create(name, info) { browser.alarms.set(name, info); },
    async clear(name) { return browser.alarms.delete(name); },
    async get(name) { return browser.alarms.get(name); },
  },
  sidePanel: { setPanelBehavior() {} },
  tabs: {
    async query() { return []; },
    async create() { return { id: 31 }; },
    async get() { return {}; },
  },
};

await import("../service_worker.js");

const ARCHIVE = "/home/u/Downloads/fixture_ec_aaaaaaaaaaaa_outputs.zip";
const STARTED = "2026-10-07T10:00:00.000Z";

function reset() {
  session.data.clear();
  local.data.clear();
  browser.downloads = [];
  browser.alarms.clear();
  browser.native = [];
  browser.answers = {};
  browser.detached = [];
}

/* A call the browser is bound to, as Go leaves it once the file is attached. */
function bind(exchangeId, extra = {}) {
  session.data.set(`externalcall:handoff:${exchangeId}`, {
    exchangeId,
    tabId: 3,
    provider: "claude",
    armed: false,
    monitoring: true,
    monitoringStartedAt: STARTED,
    downloadBaseline: [],
    observedDownloadIds: [],
    generation: `generation-${exchangeId}`,
    ...extra,
  });
}

function saved(id, filename) {
  browser.downloads.push({ id, filename, state: "complete", startTime: "2026-10-07T10:05:00.000Z" });
}

/* The listeners start work and return; let every queued step run. */
async function quiet() {
  for (let turn = 0; turn < 20; turn += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

async function downloadCompletes(id) {
  listeners.downloads({ id, state: { current: "complete" } });
  await quiet();
}

const ask = (message) => new Promise((resolve) => listeners.message(message, {}, resolve));
const commands = () => browser.native.map((entry) => entry.command);
const finishedMarks = () => Object.fromEntries(
  [...local.data].filter(([key]) => key.startsWith("externalcall:finished:"))
    .map(([key, value]) => [key.slice("externalcall:finished:".length), value.status]),
);


test("a filed archive finishes its call with nobody pressing Done", async () => {
  reset();
  bind("ex-1");
  saved(9, ARCHIVE);
  browser.answers["download.completed"] = { status: "MOVED", exchange_id: "ex-1", stored_name: "fixture_ec_aaaaaaaaaaaa_outputs.zip" };
  browser.answers["call.done"] = { exchange_id: "ex-1", status: "COMPLETE" };

  await downloadCompletes(9);

  // Filed first, finished second, and the finish names the exchange.
  assert.deepEqual(commands(), ["download.completed", "call.done"]);
  assert.deepEqual(browser.native[1].payload, { exchange_id: "ex-1" });
  assert.deepEqual(finishedMarks(), { "ex-1": "COMPLETE" });
  // The call is over, so it is no longer bound; the tab is kept for a correction.
  assert.equal(session.data.has("externalcall:handoff:ex-1"), false);
  assert.deepEqual(session.data.get("binding:ex-1"), { tabId: 3, exchangeId: "ex-1", provider: "claude" });
});


test("an archive that came down faulty still finishes its call, as incomplete", async () => {
  reset();
  bind("ex-1");
  saved(9, ARCHIVE);
  browser.answers["download.completed"] = {
    status: "INVALID", exchange_id: "ex-1", stored_name: "fixture_ec_aaaaaaaaaaaa_outputs.zip", error: "not a zip",
  };
  browser.answers["call.done"] = { exchange_id: "ex-1", status: "INCOMPLETE" };

  await downloadCompletes(9);

  assert.deepEqual(commands(), ["download.completed", "call.done"]);
  assert.deepEqual(finishedMarks(), { "ex-1": "INCOMPLETE" });
});


/* The one thing an automatic Done must never do: close a call whose answer has
 * not arrived. Each of these is a download the companion did not file. */
test("a download the companion did not file finishes nothing", async () => {
  for (const answer of [
    { status: "IGNORED", reason: "no active call expects holiday.zip" },
    { status: "CONFLICT", error: "response already contains different bytes" },
    { status: "AMBIGUOUS", download_id: 9, error: "several active calls expect it" },
    { status: "DUPLICATE", download_id: 9 },
    // Refused before it was stored: nothing of this call's is on disk.
    { status: "INVALID", error: "copied download changed" },
  ]) {
    reset();
    bind("ex-1");
    saved(9, ARCHIVE);
    browser.answers["download.completed"] = answer;

    await downloadCompletes(9);

    assert.deepEqual(commands(), ["download.completed"], answer.status);
    assert.deepEqual(finishedMarks(), {}, answer.status);
    assert.equal(session.data.get("externalcall:handoff:ex-1").monitoring, true, answer.status);
  }
});


test("a download that is not finished, or was already there at Go, is not even reported", async () => {
  reset();
  bind("ex-1", { downloadBaseline: [9] });
  saved(9, ARCHIVE);

  listeners.downloads({ id: 9, state: { current: "in_progress" } });
  await quiet();
  assert.deepEqual(commands(), []);

  // In the baseline, so not a new download; but it carries an answer's name,
  // so the folder watch is asked - and the folder watch, not this event,
  // decides whether anything landed.
  browser.answers["calls.collect"] = { finished: [], waiting: ["ex-1"], failed: [] };
  await downloadCompletes(9);
  assert.deepEqual(commands(), ["calls.collect"]);
  assert.deepEqual(finishedMarks(), {});
});


/* A browser restart forgets which call was in which tab. The call is still
 * out and its answer still arrives; only the binding is gone. */
test("an answer that lands with no call bound is handed to the folder watch", async () => {
  reset();
  saved(9, ARCHIVE);
  saved(10, "/home/u/Downloads/holiday-photos.zip");
  browser.answers["calls.collect"] = {
    finished: [{ exchange_id: "ex-7", status: "COMPLETE" }], waiting: [], failed: [],
  };

  await downloadCompletes(10);
  assert.deepEqual(commands(), [], "an unrelated download wakes nothing");

  await downloadCompletes(9);
  assert.deepEqual(commands(), ["calls.collect"]);
  assert.deepEqual(finishedMarks(), { "ex-7": "COMPLETE" });
});


/* A browser start and an extension reload both forget which call was in which
 * tab. Either way the folder is looked in once, straight away, for an archive
 * that landed while nothing was listening. */
test("the folder is looked in once when the browser starts or the extension reloads", async () => {
  for (const event of ["startup", "installed"]) {
    reset();
    browser.answers["calls.collect"] = {
      finished: [{ exchange_id: "ex-9", status: "COMPLETE" }], waiting: ["ex-10"], failed: [],
    };

    listeners[event]();
    await quiet();

    assert.deepEqual(commands(), ["calls.collect"], event);
    assert.deepEqual(finishedMarks(), { "ex-9": "COMPLETE" }, event);
    // One call is still out, so the watch is left running for it.
    assert.equal(browser.alarms.has("externalcall:watch"), true, event);
  }
});


test("the folder watch runs while a call is out and stops when none is", async () => {
  reset();
  browser.answers["call.go"] = {
    active: { request_id: "ec_aaaaaaaaaaaa", started_at: STARTED, download_baseline: [], observed_download_ids: [] },
    request_paths: ["/home/u/calls/ex-1/request/fixture_inputs.zip"],
    launch_prompt: "Open the attached archive.",
  };

  const started = await ask({ type: "GO", exchangeId: "ex-1", mode: "new", provider: "grok" });
  assert.equal(started.ok, true, started.error);
  assert.deepEqual(browser.alarms.get("externalcall:watch"), { periodInMinutes: 0.5 });

  // Still waiting: the watch keeps running and nothing is finished.
  browser.native = [];
  browser.answers["calls.collect"] = { finished: [], waiting: ["ex-1"], failed: [] };
  listeners.alarm({ name: "externalcall:watch" });
  await quiet();
  assert.deepEqual(commands(), ["calls.collect"]);
  assert.equal(browser.alarms.has("externalcall:watch"), true);
  assert.deepEqual(finishedMarks(), {});
  assert.equal(session.data.has("externalcall:handoff:ex-1"), true);

  // Landed: finished, recorded, unbound, and with nothing left the watch stops.
  browser.answers["calls.collect"] = {
    finished: [{ exchange_id: "ex-1", status: "COMPLETE" }], waiting: [], failed: [],
  };
  listeners.alarm({ name: "externalcall:watch" });
  await quiet();
  assert.deepEqual(finishedMarks(), { "ex-1": "COMPLETE" });
  assert.equal(session.data.has("externalcall:handoff:ex-1"), false);
  assert.equal(browser.alarms.has("externalcall:watch"), false);
  // It was still armed for an attachment that never came; that is released.
  assert.deepEqual(browser.detached, [31]);

  // Somebody else's alarm is not ours to answer.
  browser.native = [];
  listeners.alarm({ name: "something-else" });
  await quiet();
  assert.deepEqual(commands(), []);
});


/* If the check fails the archive is already filed, so nothing is lost: the
 * call stays running and bound, and the folder watch finishes it next time. */
test("a check that fails leaves the call running, to be tried again", async () => {
  reset();
  bind("ex-1");
  saved(9, ARCHIVE);
  browser.answers["download.completed"] = { status: "MOVED", exchange_id: "ex-1", stored_name: "fixture_ec_aaaaaaaaaaaa_outputs.zip" };
  browser.answers["call.done"] = new Error("call changed while validating; response evidence retained");

  await downloadCompletes(9);

  assert.deepEqual(commands(), ["download.completed", "call.done"]);
  assert.deepEqual(finishedMarks(), {});
  const handoff = session.data.get("externalcall:handoff:ex-1");
  assert.equal(handoff.monitoring, true);
  assert.equal(handoff.status, "CHECK_FAILED");
  assert.match(handoff.message, /landed but could not be checked yet: call changed while validating/);
  // Not reported as a download that failed to file: it was filed.
  assert.equal(session.data.has("lastDownloadFailure"), false);
});


test("the panel is told which calls finished, and moving them on forgets only those", async () => {
  reset();
  local.data.set("agent", "claude");
  local.data.set("externalcall:finished:ex-1", { exchangeId: "ex-1", status: "COMPLETE", at: STARTED });
  local.data.set("externalcall:finished:ex-2", { exchangeId: "ex-2", status: "INCOMPLETE", at: STARTED });
  browser.answers["status.snapshot"] = { root: "/r", ready: [], active: [], progress: [], recent: [] };

  const status = await ask({ type: "GET_STATUS" });
  assert.deepEqual(status.result.finished.map((mark) => [mark.exchangeId, mark.status]),
    [["ex-1", "COMPLETE"], ["ex-2", "INCOMPLETE"]]);

  browser.native = [];
  const moved = await ask({ type: "SHELVE", exchangeIds: ["ex-1"] });
  assert.equal(moved.ok, true);
  assert.deepEqual(finishedMarks(), { "ex-2": "INCOMPLETE" });
  assert.equal(local.data.get("agent"), "claude");
  // Moving a call to History is the panel's own bookkeeping. Nothing on disk
  // changes, so the companion is not asked to do anything.
  assert.deepEqual(commands(), []);
});
