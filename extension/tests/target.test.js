import assert from "node:assert/strict";
import test from "node:test";

import {
  PROVIDER_IDS,
  chooseTargetTab,
  normalizeProvider,
  providerBaseUrl,
  providerLabel,
} from "../lib/target.js";

const chatTab = { id: 7, url: "https://chatgpt.com/c/abc-123" };
const grokTab = { id: 9, url: "https://grok.com/c/def-456" };


test("the three sites are known and anything else falls back to ChatGPT", () => {
  assert.deepEqual([...PROVIDER_IDS], ["gpt", "claude", "grok"]);
  assert.equal(normalizeProvider("grok"), "grok");
  assert.equal(providerBaseUrl("grok"), "https://grok.com/");
  assert.equal(providerLabel("grok"), "Grok");
  assert.equal(normalizeProvider("nonsense"), "gpt");
  assert.equal(normalizeProvider(undefined), "gpt");
});


test("conductor mode binds the focused Grok conversation, and only a Grok one", () => {
  assert.deepEqual(
    chooseTargetTab({ mode: "current", provider: "grok", activeTab: grokTab }),
    { create: false, tabId: 9 },
  );
  // A ChatGPT tab is the wrong site when Grok was chosen, and the refusal names
  // the site whose permission would explain a withheld URL.
  assert.throws(
    () => chooseTargetTab({ mode: "current", provider: "grok", activeTab: chatTab }),
    /not a Grok conversation.*grok\.com permission/s,
  );
  assert.throws(
    () => chooseTargetTab({ mode: "current", provider: "gpt", activeTab: grokTab }),
    /not a ChatGPT conversation.*chatgpt\.com permission/s,
  );
});


test("the default opens a fresh conversation", () => {
  assert.deepEqual(chooseTargetTab({ mode: "new", activeTab: chatTab }), { create: true });
  // An absent or unrecognised mode must not silently bind a live tab.
  assert.deepEqual(chooseTargetTab({}), { create: true });
  assert.deepEqual(chooseTargetTab({ mode: "nonsense", activeTab: chatTab }), { create: true });
});


/* A new conversation is a new tab. This was once "improved" into reusing an
 * open ChatGPT tab and navigating it to the root — fewer tabs, and the wrong
 * behaviour: it takes over the window the operator was reading, to deliver a
 * call they asked to arrive somewhere new. The tab is the point.
 *
 * Nothing here may consult the tabs that happen to be open. A fresh
 * conversation does not depend on what else is on screen.
 */
test("a new conversation is a new tab, never one that is already open", () => {
  assert.deepEqual(
    chooseTargetTab({
      mode: "new",
      activeTab: chatTab,
      chatgptTabs: [chatTab, { id: 8, url: "https://chatgpt.com/" }],
    }),
    { create: true },
  );
});


test("conductor mode binds the focused ChatGPT conversation", () => {
  assert.deepEqual(
    chooseTargetTab({ mode: "current", activeTab: chatTab }),
    { create: false, tabId: 7 },
  );
});


/* The whole point of the guard: arming the debugger on a tab we cannot prove is
 * ChatGPT would attach it to an unrelated site and then hand that site files. */
test("a tab that cannot be proven to be ChatGPT is refused, never fallen back on", () => {
  for (const activeTab of [
    { id: 7, url: "https://example.com/" },
    { id: 7 }, // no host permission for this site, so Chrome withholds the URL
    { id: 7, url: "https://chatgpt.com.evil.test/c/1" },
    { id: 7, url: "http://chatgpt.com/" }, // downgraded scheme
    null,
    { url: "https://chatgpt.com/" }, // no usable tab id
  ]) {
    assert.throws(
      () => chooseTargetTab({ mode: "current", activeTab }),
      /not a ChatGPT conversation|No active tab/,
    );
  }
});


test("a conversation already running a call is not reused", () => {
  assert.throws(
    () => chooseTargetTab({
      mode: "current",
      activeTab: chatTab,
      handoffs: { "ex-1": { tabId: 7, exchangeId: "ex-1" } },
    }),
    /already running call ex-1/,
  );
});


test("a call running in a different tab does not block this one", () => {
  assert.deepEqual(
    chooseTargetTab({
      mode: "current",
      activeTab: chatTab,
      handoffs: { "ex-1": { tabId: 99, exchangeId: "ex-1" } },
    }),
    { create: false, tabId: 7 },
  );
});
