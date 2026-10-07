import assert from "node:assert/strict";
import test from "node:test";

import { createFinishedStore } from "../lib/finished.js";
import { callCentre } from "../lib/panel.js";


function memoryStorage() {
  const data = new Map();
  return {
    data,
    async get(keys) {
      if (keys === null) return Object.fromEntries(data);
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


/* Two calls can finish in the same moment, each from its own event in its own
 * turn of the service worker. One shared list would be read twice and written
 * twice, and the second write would erase the first. */
test("two calls finishing together are both recorded", async () => {
  const storage = memoryStorage();
  const store = createFinishedStore(storage, () => "2026-10-07T10:00:00.000Z");

  await Promise.all([store.mark("ex-1", "COMPLETE"), store.mark("ex-2", "INCOMPLETE")]);

  assert.deepEqual(await store.read(), {
    "ex-1": { exchangeId: "ex-1", status: "COMPLETE", at: "2026-10-07T10:00:00.000Z" },
    "ex-2": { exchangeId: "ex-2", status: "INCOMPLETE", at: "2026-10-07T10:00:00.000Z" },
  });
  assert.equal(storage.data.size, 2, "one key per call");
});


test("moving calls to history forgets those calls and no others", async () => {
  const storage = memoryStorage();
  storage.data.set("goMode", "new");
  const store = createFinishedStore(storage);
  await store.mark("ex-1", "COMPLETE");
  await store.mark("ex-2", "COMPLETE");
  await store.mark("ex-3", "INCOMPLETE");

  assert.equal(await store.shelve(["ex-1", "ex-3", "never-finished"]), 3);

  assert.deepEqual(Object.keys(await store.read()), ["ex-2"]);
  // Everything else the panel keeps in the same storage is left alone.
  assert.equal(storage.data.get("goMode"), "new");
  assert.equal(await store.shelve(undefined), 0);
  assert.equal(await store.shelve([]), 0);
});


/* A call finishes on its own, usually while nobody is watching. If it went
 * straight into a folded History it would change colour where no one could
 * see it, so it stays in Running, in its new colour, until it is moved. */
test("a call that finished on its own stays in Running until it is moved", () => {
  const state = {
    recent: [
      { exchange_id: "ex-good", subject: "good one", project: "dalio", state: "COMPLETE" },
      { exchange_id: "ex-bad", subject: "faulty one", state: "INCOMPLETE" },
      { exchange_id: "ex-old", subject: "seen long ago", state: "COMPLETE" },
    ],
    finished: [{ exchangeId: "ex-good" }, { exchangeId: "ex-bad" }],
  };

  const { running, past } = callCentre(state);
  assert.deepEqual(
    running.map((call) => [call.id, call.phase, call.project]),
    [["ex-bad", "incomplete", null], ["ex-good", "complete", "dalio"]],
  );
  assert.deepEqual(past.map((row) => row.id), ["ex-old"]);

  // Moved: the same calls, no longer marked, are History.
  const moved = callCentre({ ...state, finished: [] });
  assert.deepEqual(moved.running, []);
  assert.deepEqual(moved.past.map((row) => row.id), ["ex-good", "ex-bad", "ex-old"]);
});


/* The mark is a note, not the truth. The companion says what state a call is
 * in, and a mark for a call that is no longer finished - sent again, corrected
 * in place, stopped, deleted - must not paint it as finished. */
test("a stale mark never overrides what the companion reports", () => {
  const { ready, running, past } = callCentre({
    ready: [{ exchange_id: "ex-resent", subject: "prepared again" }],
    active: [{ exchange_id: "ex-reopened" }],
    handoffs: [{ exchangeId: "ex-reopened", status: "ATTACHED" }],
    recent: [
      { exchange_id: "ex-resent", state: "PREPARED" },
      { exchange_id: "ex-reopened", state: "ACTIVE" },
      { exchange_id: "ex-stopped", state: "STOPPED" },
    ],
    finished: [
      { exchangeId: "ex-resent" },
      { exchangeId: "ex-reopened" },
      { exchangeId: "ex-stopped" },
      { exchangeId: "ex-deleted" },
      null,
    ],
  });

  assert.deepEqual(ready.map((call) => call.id), ["ex-resent"]);
  assert.deepEqual(running.map((call) => [call.id, call.phase]), [["ex-reopened", "waiting"]]);
  assert.deepEqual(past.map((row) => row.id), ["ex-stopped"]);
});


/* The state a running card shows is read from the archive, never from a timer:
 * checking means the archive is in. */
test("a running call is checking exactly when its archive is in", () => {
  const phase = (handoff, progress) => callCentre({
    active: [{ exchange_id: "ex" }],
    handoffs: handoff ? [{ exchangeId: "ex", ...handoff }] : [],
    progress: progress ? [{ exchange_id: "ex", ...progress }] : [],
  }).running[0].phase;

  assert.equal(phase({ status: "WAITING_FOR_ATTACH_CLICK" }), "waiting");
  assert.equal(phase({ status: "ATTACHED" }, { complete: false }), "waiting");
  assert.equal(phase({ status: "ATTACHED" }, { complete: true }), "checking");
  assert.equal(phase({ status: "DOWNLOAD_MOVED" }), "checking");
  assert.equal(phase({ status: "DOWNLOAD_INVALID" }), "checking");
  // A download that was not filed has not landed.
  assert.equal(phase({ status: "DOWNLOAD_CONFLICT" }), "waiting");
  // A check that failed is shown as waiting, where the card can still be
  // stopped and says why, even though the archive is in.
  assert.equal(phase({ status: "CHECK_FAILED" }, { complete: true }), "waiting");
  assert.equal(phase(null, { complete: true }), "lost");
});


/* Cards are ordered by exchange id, which begins with a timestamp, so a card
 * changes colour where it sits instead of jumping to the end of the list. */
test("a card keeps its place when it finishes", () => {
  const before = callCentre({
    active: [{ exchange_id: "2026-10-07_100000_a" }, { exchange_id: "2026-10-07_100100_b" }, { exchange_id: "2026-10-07_100200_c" }],
    handoffs: ["a", "b", "c"].map((x, i) => ({ exchangeId: `2026-10-07_1${String(i).padStart(2, "0")}${i === 0 ? "000" : i === 1 ? "100" : "200"}_${x}` })),
  });
  const after = callCentre({
    active: [{ exchange_id: "2026-10-07_100000_a" }, { exchange_id: "2026-10-07_100200_c" }],
    recent: [{ exchange_id: "2026-10-07_100100_b", state: "COMPLETE" }],
    finished: [{ exchangeId: "2026-10-07_100100_b" }],
  });

  assert.deepEqual(after.running.map((call) => call.id), before.running.map((call) => call.id));
  assert.equal(after.running[1].phase, "complete");
});
