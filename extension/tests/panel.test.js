import assert from "node:assert/strict";
import test from "node:test";

import {
  describeDownloadFailure,
  describeStage,
  formatBytes,
  formatElapsed,
} from "../lib/panel.js";

test("byte sizes stay short enough for a narrow panel", () => {
  assert.equal(formatBytes(0), "0 B");
  assert.equal(formatBytes(999), "999 B");
  assert.equal(formatBytes(1536), "1.5 KB");
  assert.equal(formatBytes(1024 * 1024 * 3.25), "3.3 MB");
  assert.equal(formatBytes(1024 * 1024 * 40), "40 MB");
  assert.equal(formatBytes("not a number"), "");
});

test("elapsed time reads as a duration and never as a negative", () => {
  const now = Date.parse("2026-07-21T20:10:00Z");

  assert.equal(formatElapsed("2026-07-21T20:09:15Z", now), "45s");
  assert.equal(formatElapsed("2026-07-21T20:08:25Z", now), "1m 35s");
  assert.equal(formatElapsed("2026-07-21T18:40:00Z", now), "1h 30m");
  assert.equal(formatElapsed("2026-07-21T20:11:00Z", now), "0s");
  assert.equal(formatElapsed(undefined, now), "");
});

/* A card is one line, so what the operator must do next is a few words. The
 * site is named, because three calls can be waiting on three different sites. */
test("a running call says what it needs in a few words, naming the site", () => {
  assert.equal(
    describeStage({ status: "WAITING_FOR_ATTACH_CLICK", provider: "claude" }),
    "Click Attach in Claude",
  );
  assert.equal(
    describeStage({ status: "WAITING_FOR_ATTACH_CLICK", provider: "grok" }),
    "Click Attach in Grok",
  );
  assert.equal(describeStage({ status: "ATTACHED", launchInserted: true }), "Review, then Send");
  // The line could not be typed, so pasting it is the next thing to do.
  assert.equal(
    describeStage({ status: "ATTACHED", launchInserted: false }),
    "Paste the instruction, then Send",
  );
  assert.equal(describeStage({ status: "DOWNLOAD_MOVED" }), "Checking the archive");
});

test("a stage with no short form shows its own message rather than going blank", () => {
  assert.equal(
    describeStage({ status: "ERROR", message: "file chooser did not come from the bound tab" }),
    "file chooser did not come from the bound tab",
  );
  assert.equal(describeStage({ status: "SOMETHING_NEW" }), "SOMETHING_NEW");
  assert.equal(describeStage(null), "");
});

test("a download that could not be filed tells the operator how to recover", () => {
  const message = describeDownloadFailure({
    downloadId: 42,
    message: "Native companion failed",
    at: "2026-08-08T18:00:00Z",
  });

  assert.match(message, /Download 42/);
  assert.match(message, /Native companion failed/);
  // The recovery matters more than the cause: the bytes are safe, and the
  // folder watch collects them once they are in the downloads folder.
  assert.match(message, /still where the browser saved it/);
  assert.match(message, /downloads folder/);
  // There is no Done to click any more, so the advice must not mention one.
  assert.doesNotMatch(message, /Done/);
});

test("no failure renders nothing at all", () => {
  assert.equal(describeDownloadFailure(null), "");
  assert.equal(describeDownloadFailure({ downloadId: 1, message: "" }), "");
});
