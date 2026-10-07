import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";


test("manifest requests only the approved permissions", async () => {
  const manifest = JSON.parse(
    await readFile(new URL("../manifest.json", import.meta.url), "utf8"),
  );

  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual(manifest.permissions, [
    "sidePanel",
    "nativeMessaging",
    "debugger",
    "downloads",
    "storage",
    // Added deliberately. A call finishes itself when its archive lands, and
    // with the panel closed the only thing that can go and look in the
    // Downloads folder is an alarm. It grants access to nothing.
    "alarms",
  ]);
  /* Sending a call into the conversation already open means reading the focused
   * tab's URL first, and refusing anything that is not the chosen agent. Host
   * permissions scoped to chatgpt.com, claude.ai and grok.com buy exactly that:
   * `tab.url` is populated for those tabs and stays undefined for every other
   * site, so the check cannot see his other tabs and an unreadable URL is
   * already a refusal. The broad "tabs" permission would have worked too, and
   * would have exposed the lot. This stays pinned so widening it has to be
   * deliberate. */
  assert.deepEqual(manifest.host_permissions, [
    "https://chatgpt.com/*",
    "https://claude.ai/*",
    "https://grok.com/*",
  ]);
  assert.equal(manifest.content_scripts, undefined);
});
