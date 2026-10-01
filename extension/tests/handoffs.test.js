import assert from "node:assert/strict";
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
