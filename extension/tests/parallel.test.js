import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  anyShouldObserveDownload,
  handoffForTab,
} from "../lib/downloads.js";


const numbers = {
  exchangeId: "2026-07-21_090000_numbers",
  tabId: 101,
  monitoring: true,
  monitoringStartedAt: "2026-07-21T09:00:00.000Z",
  downloadBaseline: [1, 2],
  observedDownloadIds: [],
};

const claims = {
  exchangeId: "2026-07-21_090100_claims",
  tabId: 102,
  monitoring: true,
  monitoringStartedAt: "2026-07-21T09:01:00.000Z",
  downloadBaseline: [1, 2, 3],
  observedDownloadIds: [],
};

const both = { [numbers.exchangeId]: numbers, [claims.exchangeId]: claims };


test("a download is observed when any running call could own it", () => {
  assert.equal(
    anyShouldObserveDownload(both, { id: 9, startTime: "2026-07-21T09:02:00.000Z" }),
    true,
  );
});


test("a download inside every baseline is not observed", () => {
  assert.equal(
    anyShouldObserveDownload(both, { id: 1, startTime: "2026-07-21T09:02:00.000Z" }),
    false,
  );
});


test("a download in one call's baseline can still belong to the other", () => {
  // Id 3 predates the claims call but not the numbers call, which started first.
  assert.equal(
    anyShouldObserveDownload(both, { id: 3, startTime: "2026-07-21T09:00:30.000Z" }),
    true,
  );
});


test("a download predating every call is not observed", () => {
  assert.equal(
    anyShouldObserveDownload(both, { id: 9, startTime: "2026-07-21T08:59:00.000Z" }),
    false,
  );
});


test("a download is submitted while any call is still monitoring", () => {
  const item = { id: 9, startTime: "2026-07-21T09:02:00.000Z" };
  const stopped = {
    [numbers.exchangeId]: { ...numbers, monitoring: false },
    [claims.exchangeId]: claims,
  };
  assert.equal(anyShouldObserveDownload(stopped, item), true);

  const allStopped = {
    [numbers.exchangeId]: { ...numbers, monitoring: false },
    [claims.exchangeId]: { ...claims, monitoring: false },
  };
  assert.equal(anyShouldObserveDownload(allStopped, item), false);
});


test("a file chooser is matched to the call bound to that tab", () => {
  assert.equal(handoffForTab(both, 102).exchangeId, claims.exchangeId);
  assert.equal(handoffForTab(both, 999), null);
});


test("the side panel drives Done and Stop per exchange", async () => {
  const panel = await readFile(new URL("../sidepanel.js", import.meta.url), "utf8");
  const worker = await readFile(new URL("../service_worker.js", import.meta.url), "utf8");

  // Matched loosely across lines: what matters is that both commands carry an
  // exchange id, not how the call is wrapped.
  assert.match(panel, /"DONE",[\s\S]{0,40}exchangeId/);
  assert.match(panel, /"STOP",[\s\S]{0,40}exchangeId/);
  assert.match(worker, /nativeCommand\("status\.snapshot"\)/);
  assert.match(worker, /exchange_id: exchangeId/);
});
