/* Which tab a call is delivered into.
 *
 * Kept pure and out of the service worker so the refusals can be tested. The
 * refusals are the reason this exists: "send into the conversation I am in"
 * means arming the debugger and intercepting a file chooser on a tab the
 * operator was already using, and doing that to the wrong tab would attach a
 * debugger to an unrelated site and hand it files.
 */

export const CHATGPT_URL = "https://chatgpt.com/";
export const CLAUDE_URL = "https://claude.ai/";

export function normalizeProvider(value) {
  return value === "claude" ? "claude" : "gpt";
}

export function providerBaseUrl(provider) {
  return normalizeProvider(provider) === "claude" ? CLAUDE_URL : CHATGPT_URL;
}

export function providerLabel(provider) {
  return normalizeProvider(provider) === "claude" ? "Claude" : "ChatGPT";
}

/* Returns {create: true} to open a fresh conversation, or {create: false,
 * tabId} to bind one already open. Throws with an operator-facing message when
 * the focused tab cannot safely be used.
 *
 * A new conversation is a new tab, deliberately. Reusing an open ChatGPT tab
 * was tried and reverted: it navigates away from whatever thread that tab was
 * showing, which is the operator's own window being taken over by a call they
 * expected to arrive somewhere new.
 */
export function chooseTargetTab({ mode, provider, activeTab, handoffs = {} } = {}) {
  const agent = normalizeProvider(provider);
  const baseUrl = providerBaseUrl(agent);
  const agentName = providerLabel(agent);
  if (mode !== "current") {
    return { create: true };
  }
  if (!activeTab || !Number.isInteger(activeTab.id)) {
    throw new Error(`No active tab to send into. Focus your ${agentName} conversation.`);
  }
  /* The extension holds host permissions for chatgpt.com and claude.ai, so `url`
   * is populated for those tabs and undefined everywhere else. An unreadable URL
   * is therefore a refusal and never a fallback — a missing URL is exactly the
   * case where we cannot prove the tab is safe to arm. */
  if (typeof activeTab.url !== "string" || !activeTab.url.startsWith(baseUrl)) {
    /* A withheld URL and a genuinely wrong site are indistinguishable from
     * here, and the operator staring straight at a conversation tab needs the
     * second cause named or the message reads as a lie. */
    throw new Error(
      agent === "claude"
        ? "The focused tab is not a Claude conversation. Open the conversation you "
          + "want this call delivered into, focus it, then click Go. If it already "
          + "is one, Chrome is withholding the tab's address because the extension's "
          + "claude.ai permission was declined — reload the extension and accept "
          + "the prompt."
        : "The focused tab is not a ChatGPT conversation. Open the conversation you "
          + "want this call delivered into, focus it, then click Go. If it already "
          + "is one, Chrome is withholding the tab's address because the extension's "
          + "chatgpt.com permission was declined — reload the extension and accept "
          + "the prompt.",
    );
  }
  const bound = Object.values(handoffs).find((item) => item?.tabId === activeTab.id);
  if (bound) {
    throw new Error(`That conversation is already running call ${bound.exchangeId}.`);
  }
  return { create: false, tabId: activeTab.id };
}
