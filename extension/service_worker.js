import {
  attachmentBasenames,
  buildFallbackAssignment,
  buildFileAssignment,
} from "./lib/attachment.js";
import {
  anyShouldObserveDownload,
  handoffForTab,
} from "./lib/downloads.js";
import { chooseTargetTab, normalizeProvider, providerBaseUrl, providerLabel } from "./lib/target.js";
import { createHandoffStore } from "./lib/handoffs.js";

const handoffStore = createHandoffStore(chrome.storage.session);


const NATIVE_HOST = "com.sina.externalcall";
/* A newly created conversation renders its composer some time after the tab
 * exists, so the launch line is retried rather than attempted once. Twenty
 * attempts at 500ms is ten seconds, past which a page that has not produced a
 * composer is a page worth telling the operator about. */
const COMPOSER_POLL_MS = 500;
const COMPOSER_ATTEMPTS_NEW_TAB = 20;


chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});


chrome.runtime.onStartup.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});


chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  handlePanelMessage(message)
    .then((result) => sendResponse({ ok: true, result }))
    .catch((error) => sendResponse({ ok: false, error: error.message }));
  return true;
});


chrome.debugger.onEvent.addListener((source, method, params) => {
  if (method !== "Page.fileChooserOpened") {
    return;
  }
  (async () => {
    const handoff = handoffForTab(await readHandoffs(), source.tabId);
    if (!handoff || !await handoffStore.current(handoff)) return;
    try {
      await completeAttachment(source, params, handoff);
    } catch (error) {
      if (!await handoffStore.current(handoff)) return;
      const saved = await writeHandoff({ ...handoff, status: "ERROR", message: error.message, armed: false });
      if (saved) await broadcastStatus(saved);
      await safeDetach(source.tabId);
    }
  })().catch(console.error);
});


chrome.downloads.onChanged.addListener((delta) => {
  submitCompletedDownload(delta).catch((error) => recordDownloadFailure(delta?.id, error));
});


async function handlePanelMessage(message) {
  switch (message?.type) {
    case "GET_STATUS":
      return getStatus();
    case "GO":
      return beginGo(message.exchangeId, message.mode, message.provider);
    case "RESUME":
      return resumeCall(message.exchangeId, message.mode, message.provider);
    case "STOP":
      return stopCall(message.exchangeId);
    case "DONE":
      return finishCall(message.exchangeId);
    case "REPAIR":
      return openRepairRound(message.exchangeId);
    case "CLONE":
      // Prepares the same request as a new exchange. It does not send it, and
      // it does not touch the finished call it copied.
      return nativeCommand("call.clone", { exchange_id: message.exchangeId });
    case "INSPECT":
      // Read-only recall for one exchange. The companion returns metadata,
      // the validation report, defects, and paths — never file contents.
      return nativeCommand("call.inspect", { exchange_id: message.exchangeId });
    default:
      throw new Error(`Unknown extension command: ${message?.type}`);
  }
}


async function readHandoffs() {
  return handoffStore.read();
}


async function writeHandoff(handoff, options) {
  return handoffStore.write(handoff, options);
}


async function dropHandoff(exchangeId) {
  return handoffStore.drop(exchangeId);
}


async function getStatus() {
  const [snapshot, stored, handoffs] = await Promise.all([
    nativeCommand("status.snapshot"),
    chrome.storage.session.get(["lastReport", "lastHandoff", "lastDownloadFailure"]),
    readHandoffs(),
  ]);
  const { ready, active, progress, recent } = snapshot;
  return {
    root: snapshot?.root ?? null,
    // Which expected files have actually landed, per running call. The panel
    // holds Done back until they all have, because Done stops monitoring.
    progress,
    recent,
    ready,
    // An empty array is truthy, so returning [] here disables Go in any caller
    // that tests `if (state.active)`. Absence must be falsy.
    active: Array.isArray(active) && active.length === 0 ? null : active,
    handoffs: Object.values(handoffs),
    lastReport: stored.lastReport ?? null,
    lastDownloadFailure: stored.lastDownloadFailure ?? null,
    canRepair: Boolean(
      stored.lastReport?.status === "INCOMPLETE"
      && Number.isInteger(stored.lastHandoff?.tabId),
    ),
    repairExchangeId: stored.lastHandoff?.exchangeId ?? null,
  };
}


/* Which conversation the call lands in.
 *
 * "new" starts a fresh ChatGPT conversation — the bounded, self-contained call
 * the protocol is built around. "current" binds the conversation already open in
 * the focused tab, so a call can be delivered into a long-running thread that
 * has accumulated context on purpose. The two are different products: a call
 * sent into an existing thread is answered by a model that has already been
 * argued with, which is the point in conductor mode and a contaminant otherwise.
 *
 * The companion does not care. It takes tab_id as an opaque integer and
 * call.repair already hands it a tab that has been in use, so this is entirely a
 * question of which tab id we resolve here.
 */
async function resolveTargetTab(mode, provider) {
  const agent = normalizeProvider(provider);
  const [activeTab] = mode === "current"
    ? await chrome.tabs.query({ active: true, lastFocusedWindow: true })
    : [null];
  const choice = chooseTargetTab({
    mode,
    provider: agent,
    activeTab,
    handoffs: mode === "current" ? await readHandoffs() : {},
  });
  if (!choice.create) {
    return { id: choice.tabId };
  }
  const created = await chrome.tabs.create({ url: providerBaseUrl(agent), active: true });
  if (!Number.isInteger(created.id)) {
    throw new Error(`Chrome did not create a usable ${providerLabel(agent)} tab`);
  }
  return created;
}


async function beginGo(exchangeId, mode, provider) {
  const agent = normalizeProvider(provider);
  const label = providerLabel(agent);
  if (typeof exchangeId !== "string" || !exchangeId) {
    throw new Error("Select a prepared call first");
  }
  await chrome.storage.session.set({ lastDownloadFailure: null });
  const tab = await resolveTargetTab(mode, agent);
  const existingDownloads = await chrome.downloads.search({});
  let started = false;
  try {
    const result = await nativeCommand("call.go", {
      exchange_id: exchangeId,
      tab_id: tab.id,
      download_baseline: existingDownloads.map((item) => item.id),
    });
    started = true;
    /* The launch line is NOT typed here. It is held on the handoff and typed
     * the moment the operator's attachment lands (completeAttachment), so the
     * composer never sits with text that could be sent ahead of the archive.
     * A thread the operator is conducting (mode "current") is never typed
     * into at all. */
    const handoff = {
      ...handoffFrom(
        result,
        tab.id,
        exchangeId,
        mode === "current"
          ? "Delivering into the open conversation. Click Attach files there."
          : `Click Attach files in ${label}. The instruction is typed automatically once the attachment lands.`,
      ),
      provider: agent,
      launchPrompt: result.launch_prompt ?? null,
      launchInserted: null,
      pendingLaunchPrompt: mode === "current" ? null : result.launch_prompt ?? null,
    };
    const saved = await writeHandoff(handoff, { create: true });
    await armTab(tab.id);
    return saved;
  } catch (error) {
    await dropHandoff(exchangeId);
    await safeDetach(tab.id);
    if (started) {
      await nativeCommand("call.stop", { exchange_id: exchangeId }).catch(() => undefined);
    }
    throw error;
  }
}


/* One archive goes up with the prompt inside it, so a fresh conversation
 * receives an attachment and nothing said about it — which gets a model asking
 * what to do rather than doing it. The companion writes one line; this types it
 * and stops. The operator still reviews it and clicks Send.
 *
 * Called only AFTER the operator's attachment has landed (completeAttachment):
 * the composer then holds the archive and the instruction together, and there
 * is no window in which a stray Enter sends a bare prompt.
 *
 * Only for a fresh conversation. A thread the operator is already working in
 * has the context that makes the archive make sense, and typing a line into it
 * would put words in a conversation the operator is conducting. The text still
 * travels on the handoff, so the panel can offer it if it turns out to be
 * wanted.
 *
 * Failing to type it is not a failed call. The text goes to the panel with a
 * copy button instead, because the operator can paste it and carry on, and
 * aborting a call that has already started monitoring would cost more.
 */
async function typeLaunchPrompt(tabId, text, mode, provider) {
  if (mode === "current") {
    return { inserted: null, message: null };
  }
  const label = providerLabel(provider);
  try {
    await insertPromptIntoComposer(tabId, text, {
      attempts: COMPOSER_ATTEMPTS_NEW_TAB,
    });
    return {
      inserted: true,
      message: `Files attached, instruction typed into ${label}. Review, then click Send.`,
    };
  } catch (error) {
    return {
      inserted: false,
      message: `Copy the instruction below into ${label}, then click Send. `
        + `(${error.message})`,
    };
  }
}


/* Resume re-arms a call whose handoff was lost — the panel offers it when the
 * companion still reports an active call and session storage holds no handoff,
 * which is what a browser restart leaves behind. That restart also destroys the
 * only record of which conversation the call was delivered into, so the
 * destination has to be resolved again rather than assumed: resuming a
 * conductor call into a fresh tab would silently discard the thread the mode
 * exists to keep.
 */
async function resumeCall(exchangeId, mode, provider) {
  const agent = normalizeProvider(provider);
  const label = providerLabel(agent);
  const tab = await resolveTargetTab(mode, agent);
  const existingDownloads = await chrome.downloads.search({});
  const payload = {
    tab_id: tab.id,
    download_baseline: existingDownloads.map((item) => item.id),
  };
  if (typeof exchangeId === "string" && exchangeId) {
    payload.exchange_id = exchangeId;
  }
  let resumedId = null;
  try {
    const result = await nativeCommand("call.resume", payload);
    resumedId = result.active.exchange_id;
    /* Same discipline as beginGo: the launch line waits for the attachment. */
    const handoff = {
      ...handoffFrom(
        result,
        tab.id,
        result.active.exchange_id,
        mode === "current"
          ? "Resumed into the open conversation. Click Attach files there."
          : `Resumed. Click Attach files in ${label} — the instruction is typed automatically once the attachment lands.`,
      ),
      provider: agent,
      launchPrompt: result.launch_prompt ?? null,
      launchInserted: null,
      pendingLaunchPrompt: mode === "current" ? null : result.launch_prompt ?? null,
    };
    const saved = await writeHandoff(handoff, { create: true });
    await armTab(tab.id);
    return saved;
  } catch (error) {
    if (resumedId) await dropHandoff(resumedId);
    await safeDetach(tab.id);
    throw error;
  }
}


function handoffFrom(result, tabId, exchangeId, message) {
  return {
    armed: true,
    tabId,
    exchangeId,
    subject: result.active.request_id,
    requestPaths: result.request_paths,
    attachmentNames: attachmentBasenames(result.request_paths),
    status: "WAITING_FOR_ATTACH_CLICK",
    message,
    monitoring: true,
    monitoringStartedAt: result.active.started_at,
    downloadBaseline: result.active.download_baseline,
    observedDownloadIds: result.active.observed_download_ids,
  };
}


async function armTab(tabId) {
  await chrome.debugger.attach({ tabId }, "1.3");
  await chrome.debugger.sendCommand(
    { tabId },
    "Page.enable",
    { enableFileChooserOpenedEvent: true },
  );
  await chrome.debugger.sendCommand(
    { tabId },
    "Page.setInterceptFileChooserDialog",
    { enabled: true },
  );
}


async function completeAttachment(source, params, handoff) {
  if (!handoff || !await handoffStore.current(handoff)) return;
  let assignment;
  try {
    assignment = buildFileAssignment(handoff, source, params);
  } catch (error) {
    if (!error.message.includes("backendNodeId")) {
      throw error;
    }
    // Some composers open the chooser from something other than a real
    // <input type=file>; find it ourselves.
    const nodeId = await findFileInputNode(source);
    assignment = buildFallbackAssignment(handoff, source, nodeId);
  }
  if (!await handoffStore.current(handoff)) return;
  await chrome.debugger.sendCommand(source, assignment.method, assignment.params);
  await chrome.debugger.sendCommand(
    { tabId: source.tabId },
    "Page.setInterceptFileChooserDialog",
    { enabled: false },
  );
  await safeDetach(source.tabId);

  /* The attachment has landed and the file-chooser session is detached, so
   * NOW is when the held launch line gets typed: the composer holds the
   * archive and the instruction together, and insertPromptIntoComposer can
   * attach its own debugger session without colliding with the armed one. */
  let updated = {
    ...handoff,
    armed: false,
    status: "ATTACHED",
    message: `${handoff.attachmentNames.length} files attached. Review them, then click Send.`,
  };
  if (!await handoffStore.current(handoff)) return;
  if (typeof handoff.pendingLaunchPrompt === "string" && handoff.pendingLaunchPrompt.trim()) {
    const launch = await typeLaunchPrompt(source.tabId, handoff.pendingLaunchPrompt, "new", handoff.provider);
    const label = providerLabel(handoff.provider);
    updated = {
      ...updated,
      launchInserted: launch.inserted,
      pendingLaunchPrompt: null,
      message: launch.inserted
        ? `Files attached, instruction typed into ${label}. Review, then click Send.`
        : launch.message,
    };
  }
  const saved = await writeHandoff(updated);
  if (saved) await broadcastStatus(saved);
}


/* Fallback for a chooser event with no backendNodeId: the composer's hidden
 * <input type=file> is still on the page, so locate it by query. Claude exposes
 * #chat-input-file-upload-onpage; ChatGPT wires the last input most recently.
 */
async function findFileInputNode(source) {
  await chrome.debugger.sendCommand(source, "DOM.enable", {});
  const { root } = await chrome.debugger.sendCommand(source, "DOM.getDocument", {});
  const preferred = await chrome.debugger.sendCommand(
    source,
    "DOM.querySelector",
    { nodeId: root.nodeId, selector: "#chat-input-file-upload-onpage" },
  ).catch(() => null);
  if (Number.isInteger(preferred?.nodeId) && preferred.nodeId > 0) {
    return preferred.nodeId;
  }
  const { nodeIds } = await chrome.debugger.sendCommand(
    source,
    "DOM.querySelectorAll",
    { nodeId: root.nodeId, selector: "input[type=file]" },
  );
  if (!Array.isArray(nodeIds) || nodeIds.length === 0) {
    throw new Error("no <input type=file> found on the conversation page");
  }
  return nodeIds.at(-1);
}


async function openRepairRound(exchangeId) {
  const stored = await chrome.storage.session.get(["lastHandoff", "binding:" + exchangeId]);
  const handoffs = await readHandoffs();
  const handoff = (exchangeId ? handoffs[exchangeId] : null)
    ?? (exchangeId ? stored["binding:" + exchangeId] : stored.lastHandoff)
    ?? null;
  const tabId = handoff?.tabId;
  const target = exchangeId ?? handoff?.exchangeId;
  if (!Number.isInteger(tabId)) {
    throw new Error(`No bound ${providerLabel(handoff?.provider)} tab. Use Resume attachment first.`);
  }
  if (typeof target !== "string" || !target) {
    throw new Error("No exchange to repair");
  }
  if (handoff.exchangeId !== target || !["gpt", "claude"].includes(handoff.provider)) {
    throw new Error("No matching call/provider binding. Use Resume attachment first.");
  }
  const boundTab = await chrome.tabs.get(tabId);
  if (!boundTab.url || new URL(boundTab.url).hostname !== new URL(providerBaseUrl(handoff.provider)).hostname) {
    throw new Error("The bound tab is no longer on the selected provider. Use Resume first.");
  }
  const existingDownloads = await chrome.downloads.search({});
  const result = await nativeCommand("call.repair", {
    exchange_id: target,
    tab_id: tabId,
    download_baseline: existingDownloads.map((item) => item.id),
  });
  if (result.new_attempt) {
    const next = {
      ...handoffFrom(result, tabId, result.exchange_id, "Correction prepared. Click Attach files, review, then Send."),
      provider: handoff.provider,
      launchPrompt: result.prompt,
      pendingLaunchPrompt: result.prompt,
      launchInserted: null,
      repairRound: result.round,
    };
    try {
      const saved = await writeHandoff(next, { create: true });
      await armTab(tabId);
      await broadcastStatus(saved);
      return saved;
    } catch (error) {
      await dropHandoff(result.exchange_id);
      await safeDetach(tabId);
      await nativeCommand("call.stop", { exchange_id: result.exchange_id }).catch(() => undefined);
      throw error;
    }
  }

  let inserted = false;
  let insertError = "";
  try {
    inserted = await insertPromptIntoComposer(tabId, result.prompt);
  } catch (error) {
    insertError = error.message;
  }

  const updated = {
    ...handoff,
    exchangeId: result.exchange_id,
    tabId,
    armed: false,
    monitoring: true,
    monitoringStartedAt: result.active.started_at,
    downloadBaseline: result.active.download_baseline,
    observedDownloadIds: [],
    repairRound: result.round,
    repairPrompt: result.prompt,
    repairPromptPath: result.prompt_path,
    status: inserted ? "REPAIR_PROMPT_INSERTED" : "REPAIR_PROMPT_READY",
    message: inserted
      ? `Correction round ${result.round} typed into ${providerLabel(handoff?.provider)}. Review it, then click Send.`
      : `Correction round ${result.round} ready. Copy the prompt below into ${providerLabel(handoff?.provider)}, then click Send.`
        + (insertError ? ` (${insertError})` : ""),
  };
  const saved = await writeHandoff(updated, { create: true });
  await chrome.storage.session.set({ lastReport: null });
  await broadcastStatus(saved);
  return saved;
}


/* Types text into the conversation composer and stops there. It never sends.
 *
 * This attaches and detaches its own debugger session, so it must not run while
 * a tab is armed: the second attach throws, and the detach in `finally` would
 * strip the file-chooser interception off a tab that was waiting for it. Both
 * callers run it before arming.
 *
 * `attempts` exists for a conversation that has just been created — the tab
 * exists before the page does, and the composer appears later still.
 */
async function insertPromptIntoComposer(tabId, text, { attempts = 1 } = {}) {
  if (typeof text !== "string" || !text.trim()) {
    throw new Error("no prompt text to insert");
  }
  await chrome.debugger.attach({ tabId }, "1.3");
  try {
    await chrome.debugger.sendCommand({ tabId }, "Runtime.enable", {});
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      const focused = await chrome.debugger.sendCommand(
        { tabId },
        "Runtime.evaluate",
        {
          expression: `(() => {
            const composer = document.querySelector("#prompt-textarea")
              ?? document.querySelector("div[contenteditable='true'][data-cds='Editor']")
              ?? document.querySelector("div.tiptap.ProseMirror")
              ?? document.querySelector("div[contenteditable='true']")
              ?? document.querySelector("textarea");
            if (!composer) { return false; }
            composer.focus();
            return true;
          })()`,
          returnByValue: true,
        },
      );
      if (focused?.result?.value === true) {
        await chrome.debugger.sendCommand({ tabId }, "Input.insertText", { text });
        return true;
      }
      if (attempt + 1 < attempts) {
        await new Promise((resolve) => setTimeout(resolve, COMPOSER_POLL_MS));
      }
    }
    throw new Error("the conversation composer was not found on the bound tab");
  } finally {
    await safeDetach(tabId);
  }
}


async function stopCall(exchangeId) {
  const handoffs = await readHandoffs();
  const handoff = exchangeId ? handoffs[exchangeId] : Object.values(handoffs)[0];
  if (Number.isInteger(handoff?.tabId)) {
    await safeDetach(handoff.tabId);
  }
  const payload = exchangeId ? { exchange_id: exchangeId } : {};
  const result = await nativeCommand("call.stop", payload);
  await dropHandoff(exchangeId ?? handoff?.exchangeId);
  return result;
}


async function finishCall(exchangeId) {
  const handoffs = await readHandoffs();
  const handoff = exchangeId ? handoffs[exchangeId] : Object.values(handoffs)[0];
  const target = exchangeId ?? handoff?.exchangeId;
  if (handoff) {
    await writeHandoff({ ...handoff, monitoring: false });
  }
  const payload = target ? { exchange_id: target } : {};
  const report = await nativeCommand("call.done", payload);
  // Keep the tab binding so a correction round can reuse the same conversation.
  const lastHandoff = handoff
    ? { tabId: handoff.tabId, exchangeId: handoff.exchangeId, provider: handoff.provider }
    : null;
  await chrome.storage.session.set({ lastReport: report, lastHandoff });
  if (lastHandoff) await chrome.storage.session.set({ ["binding:" + target]: lastHandoff });
  await dropHandoff(target);
  return report;
}


/* One completed download, decided from the download itself.
 *
 * This used to take two events. `chrome.downloads.onCreated` wrote the id into
 * a tracker in session storage, and only an id found in that tracker was
 * submitted when it completed. Both halves were a read-modify-write with no
 * lock, so two downloads created in the same moment both read the empty tracker
 * and the second write erased the first. The erased download then completed,
 * failed the tracker check, and was dropped in silence.
 *
 * That is what happened. Three calls ended with an archive parked in the
 * companion's pending pool and the main JSON's event never arriving, and the
 * operator had to run `validate` by hand to rescue files Chrome had already
 * written. A service worker evicted between the two events produced the same
 * loss without any race at all.
 *
 * So there is one event now. Everything the decision needs — start time,
 * filename, state — is on the item fetched here, and `shouldObserveDownload`
 * asks the same questions it always did. Nothing has to survive between two
 * callbacks, so nothing can be lost between them. Duplicate submissions are
 * fine: the companion holds the state lock and answers DUPLICATE for an id it
 * has already seen, which makes it the one authority rather than a second copy
 * of the bookkeeping.
 */
async function submitCompletedDownload(delta) {
  if (delta?.state?.current !== "complete" || !Number.isInteger(delta.id)) {
    return;
  }
  const stored = await chrome.storage.session.get("handoffs");
  const handoffs = stored.handoffs ?? {};
  const matches = await chrome.downloads.search({ id: delta.id });
  const item = matches[0];
  if (!item || item.state !== "complete" || !item.filename) {
    return;
  }
  if (!anyShouldObserveDownload(handoffs, item)) {
    return;
  }
  const result = await nativeCommand("download.completed", {
    id: item.id,
    filename: item.filename,
    state: "complete",
    url: item.url,
    finalUrl: item.finalUrl,
    mime: item.mime,
    startTime: item.startTime,
    endTime: item.endTime,
  });
  const owner = result.exchange_id ? handoffs[result.exchange_id] : null;
  if (!owner) {
    return;
  }
  const updated = {
    ...owner,
    observedDownloadIds: [...new Set([...(owner.observedDownloadIds ?? []), item.id])],
    status: `DOWNLOAD_${result.status}`,
    message: downloadMessage(result),
  };
  const saved = await writeHandoff(updated);
  if (saved) await broadcastStatus(saved);
}


/* A download that fails to file itself is the one failure nobody can see. The
 * file sits in the downloads folder, the panel shows nothing, and validation
 * later reports it missing with no hint that anything went wrong. These
 * listeners used to discard the error entirely.
 *
 * It is recorded, not retried. A retry could file an ambiguous filename into the
 * wrong exchange, and filename is the only thing attribution has to go on.
 *
 * Recorded twice, deliberately. Session storage is immediate but dies with the
 * browser — which is exactly when the operator most needs the warning. The
 * companion writes the same fact durably and attributes it to the call whose
 * expected filename it matches, so the panel sees it after a restart and a
 * waiting agent sees it at all. When the failure was the native channel
 * itself, the second write fails too; session storage is then the honest
 * remainder, which is why it is written first.
 */
async function recordDownloadFailure(downloadId, error) {
  try {
    await chrome.storage.session.set({
      lastDownloadFailure: {
        downloadId: Number.isInteger(downloadId) ? downloadId : null,
        message: error?.message ?? String(error),
        at: new Date().toISOString(),
      },
    });
    await broadcastStatus(null);
  } catch (_ignored) {
    // Session storage is the last place left to report to.
  }
  try {
    let filename = null;
    if (Number.isInteger(downloadId)) {
      const matches = await chrome.downloads.search({ id: downloadId });
      filename = matches[0]?.filename ?? null;
    }
    await nativeCommand("download.failure.record", {
      download_id: Number.isInteger(downloadId) ? downloadId : null,
      filename,
      message: error?.message ?? String(error),
    });
  } catch (_ignored) {
    // The durable record is best-effort by nature: if the companion is the
    // broken piece, there is nowhere durable left to write.
  }
}


async function safeDetach(tabId) {
  if (!Number.isInteger(tabId)) {
    return;
  }
  try {
    await chrome.debugger.detach({ tabId });
  } catch (_error) {
    // Detaching an already-detached target is harmless.
  }
}


async function nativeCommand(command, payload = {}) {
  const response = await chrome.runtime.sendNativeMessage(NATIVE_HOST, {
    protocol_version: 1,
    command,
    payload,
  });
  if (!response?.ok) {
    throw new Error(response?.error?.message ?? "Native companion failed");
  }
  return response.result;
}


async function broadcastStatus(handoff) {
  try {
    await chrome.runtime.sendMessage({ type: "HANDOFF_STATUS", handoff });
  } catch (_error) {
    // The side panel may be closed; session storage remains authoritative.
  }
}


function downloadMessage(result) {
  switch (result.status) {
    case "MOVED":
      return `${result.stored_name} moved to this call's response folder.`;
    case "PENDING":
      return "Download saved as a candidate; waiting for the main JSON to name it.";
    case "IGNORED":
      return "Download left untouched because it is not part of this call.";
    case "AMBIGUOUS":
    case "INVALID":
    case "CONFLICT":
      return result.error ?? "Download was not collected.";
    default:
      return `Download status: ${result.status}.`;
  }
}
