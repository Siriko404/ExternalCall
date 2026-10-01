import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { createHandoffStore } from "../lib/handoffs.js";

function fixture() {
  const data = {};
  const storage = {
    async get(key) {
      await Promise.resolve();
      return key === null ? structuredClone(data) : { [key]: structuredClone(data[key]) };
    },
    async set(values) { await Promise.resolve(); Object.assign(data, structuredClone(values)); },
    async remove(key) { await Promise.resolve(); delete data[key]; },
  };
  let generation = 0;
  return createHandoffStore(storage, () => String(++generation));
}

test("concurrent handoffs preserve independent calls", async () => {
  const store = fixture();
  await Promise.all(["A", "B", "C"].map(exchangeId =>
    store.write({ exchangeId, provider: "claude" }, { create: true })));
  assert.deepEqual(Object.keys(await store.read()).sort(), ["A", "B", "C"]);
});

test("late callback cannot resurrect a dropped handoff", async () => {
  const store = fixture();
  const old = await store.write({ exchangeId: "A" }, { create: true });
  await store.drop("A");
  assert.equal(await store.write({ ...old, status: "DOWNLOAD_MOVED" }), null);
  assert.deepEqual(await store.read(), {});
});

test("callback from an earlier resume cannot overwrite its replacement", async () => {
  const store = fixture();
  const old = await store.write({ exchangeId: "A", tabId: 1 }, { create: true });
  const current = await store.write({ exchangeId: "A", tabId: 2 }, { create: true });
  assert.equal(await store.write({ ...old, tabId: 1 }), null);
  assert.deepEqual((await store.read()).A, current);
  assert.equal(await store.current(old), false);
});

/* The download scout reads the same store every handoff write goes to. It
 * once read the retired `handoffs` session key instead, so completed
 * downloads never reached the companion: the panel checklist stayed empty,
 * Done stayed blocked, and the operator had to validate by hand. */
test("the download path reads handoffs from the store, not a retired key", async () => {
  const worker = await readFile(new URL("../service_worker.js", import.meta.url), "utf8");
  const body = worker.slice(
    worker.indexOf("async function submitCompletedDownload"),
    worker.indexOf("async function recordDownloadFailure"),
  );
  assert.match(body, /await readHandoffs\(\)/);
  assert.doesNotMatch(body, /get\("handoffs"\)/);
});
