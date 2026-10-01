/* A call centre: two lists of calls, and every row opens where it sits.
 *
 * The panel used to be a launcher with a history drawer underneath it, which
 * meant one dropdown of prepared calls, one destination shared by all of them,
 * and one "last result" that belonged to whichever call finished most recently.
 * A call was not a thing you could point at. Here it is: the calls that need
 * something are on top, the ones that are over are below, and everything you
 * can do to a call is inside that call's own drawer.
 *
 * Three rules carried over intact, because they were the correct things about
 * every version so far: Done is held back until every expected file has landed
 * (Done stops monitoring, so anything still downloading at that moment is
 * lost), recovery never guesses which conversation a restart destroyed, and no
 * software here ever clicks Send.
 */

import {
  archiveVerdict,
  callCentre,
  describeStage,
  formatBytes,
  formatElapsed,
  downloadGuard,
  describeDownloadFailure,
} from "./lib/panel.js";

const el = (id) => document.querySelector(`#${id}`);

const nowList = el("now");
const nowCount = el("now-count");
const nowEmpty = el("now-empty");
const pastCard = el("past-card");
const pastList = el("past");
const pastCount = el("past-count");
const promptCard = el("prompt-card");
const promptText = el("prompt-text");
const copyPrompt = el("copy-prompt");
const health = el("health");
const companionLine = el("companion-line");
const errorLine = el("error");
const downloadFailure = el("download-failure");
const reloadButton = el("reload-button");

/* One drawer is open at a time, across both lists: the panel is a strip, and
 * two open drawers is the crowding the line design exists to avoid. */
let openId = null;
/* Destinations chosen but not yet used, per call. Not one shared value — that
 * was the real hazard in the old panel, where two calls running at once read
 * the same dropdown and the second one inherited the first one's answer. */
const destinations = new Map();
const providers = new Map();
/* Calls whose Done guard the operator has deliberately overridden. */
const forced = new Set();
let ticking = null;
let busy = false;

// Reloads the whole extension, the manual fix for a stale build that has
// stopped auto-capturing downloads. It also reloads this panel, so no refresh.
reloadButton.addEventListener("click", () => chrome.runtime.reload());

copyPrompt.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(promptText.textContent);
    copyPrompt.textContent = "Copied";
  } catch (_error) {
    copyPrompt.textContent = "Copy failed";
  }
});

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === "HANDOFF_STATUS") {
    showLaunchPrompt(message.handoff);
    refresh();
  }
});

/* The last destination and agent the operator picked, offered as the default
 * for the next call rather than as a decision already made. */
let defaultDestination = "new";
let defaultProvider = "gpt";
chrome.storage.local.get(["goMode", "agent"]).then((stored) => {
  if (stored.goMode === "current" || stored.goMode === "new") {
    defaultDestination = stored.goMode;
  }
  if (stored.agent === "claude" || stored.agent === "gpt") {
    defaultProvider = stored.agent;
  }
  refresh();
});

async function run(action) {
  setBusy(true);
  clearError();
  try {
    await action();
  } catch (error) {
    showError(error.message);
  } finally {
    setBusy(false);
    await refresh();
  }
}

let refreshGeneration = 0;
async function refresh() {
  const generation = ++refreshGeneration;
  let state;
  try {
    state = await send({ type: "GET_STATUS" });
    if (generation !== refreshGeneration) return;
  } catch (error) {
    health.className = "dot down";
    companionLine.textContent = "Companion unavailable";
    showError(error.message);
    return;
  }
  health.className = "dot up";
  companionLine.textContent = state.root ? shortRoot(state.root) : "Companion connected";
  clearError();

  const { now, past } = callCentre(state);
  renderNow(now);
  renderPast(past);
  renderDownloadFailure(state.lastDownloadFailure);
  scheduleTick(now);
}

/* ---------- the two lists ---------- */

function renderNow(calls) {
  const running = calls.filter((call) => call.state === "ACTIVE").length;
  const ready = calls.length - running;
  nowCount.textContent = [
    running ? `${running} running` : "",
    ready ? `${ready} ready` : "",
  ].filter(Boolean).join(" · ");
  nowCount.className = running ? "pill wait" : ready ? "pill ok" : "pill";
  nowEmpty.hidden = calls.length > 0;

  nowList.replaceChildren();
  for (const call of calls) {
    nowList.append(row(call, {
      label: call.state === "ACTIVE" ? "running" : "ready",
      tone: call.state === "ACTIVE" ? "wait" : "ok",
    }, () => flightDrawer(call)));
  }
}

function renderPast(rows) {
  pastCard.hidden = rows.length === 0;
  pastCount.textContent = rows.length === 1 ? "1 call" : `${rows.length} calls`;
  pastList.replaceChildren();
  for (const item of rows) {
    pastList.append(row(item, archiveVerdict(item), (drawer) => {
      archiveDrawer(drawer, item);
    }));
  }
}

/* One row shape for both lists: a whole-width button that opens in place, and
 * a drawer built by whoever knows what this kind of call can do. */
function row(call, verdict, fill) {
  const item = document.createElement("li");
  item.className = "call-row";

  const head = document.createElement("button");
  head.type = "button";
  head.className = "row-head";
  head.setAttribute("aria-expanded", String(openId === call.id));

  const mark = document.createElement("span");
  mark.className = "row-mark";
  mark.textContent = openId === call.id ? "–" : "+";
  mark.setAttribute("aria-hidden", "true");

  const name = document.createElement("span");
  name.className = "row-name";
  name.textContent = call.subject;

  const state = document.createElement("span");
  state.className = `row-state pill ${verdict.tone}`.trim();
  state.textContent = verdict.label;

  head.append(mark, name, state);
  head.addEventListener("click", () => {
    openId = openId === call.id ? null : call.id;
    refresh();
  });
  item.append(head);

  if (openId === call.id) {
    const drawer = document.createElement("div");
    drawer.className = "row-drawer";
    drawer.dataset.exchange = call.id;
    /* A builder may return the nodes to append, or append them itself and
     * return nothing. `instanceof Node` rather than a truthiness test, because
     * the archive builder is async: a Promise is truthy, and appending one
     * writes "[object Promise]" into the drawer. */
    const built = fill(drawer);
    if (built instanceof Node) {
      drawer.append(built);
    }
    item.append(drawer);
  }
  return item;
}

/* ---------- a call that needs something ---------- */

function flightDrawer(call) {
  const body = document.createDocumentFragment();
  body.append(identity(call.id));

  if (call.state === "PREPARED") {
    body.append(
      detailList([
        ["request", call.requestId],
        ...(call.call?.attach_files ?? []).map((name) => ["sends", name]),
        ...(call.call?.expected_artifacts ?? []).map((name) => ["expects", name]),
      ]),
      destinationPicker(call.id),
      providerPicker(call.id),
      commandRow([
        primary("Go", () =>
          run(async () => {
            const handoff = await send({
              type: "GO",
              exchangeId: call.id,
              mode: destinationFor(call.id),
              provider: providerFor(call.id),
            });
            showLaunchPrompt(handoff);
          })),
      ]),
    );
    return body;
  }

  if (call.needsResume) {
    /* The companion still holds this call; the browser holds no handoff. What
     * a restart destroyed is the binding to a conversation, and that cannot be
     * guessed back — resuming a conductor call into a fresh conversation
     * silently discards the thread the mode existed to keep. So the
     * destination is chosen again here, deliberately, and Resume stays
     * disabled until it is. */
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = "The browser restarted. Which conversation this call was "
      + "in is gone — choose again.";
    const resume = primary("Resume attachment", () =>
      run(async () => {
        const handoff = await send({
          type: "RESUME",
          exchangeId: call.id,
          mode: destinationFor(call.id),
          provider: providerFor(call.id),
        });
        showLaunchPrompt(handoff);
      }));
    resume.disabled = !destinations.has(call.id);
    body.append(
      hint,
      destinationPicker(call.id, () => {
        resume.disabled = busy;
      }),
      providerPicker(call.id),
      commandRow([resume, secondary("Stop", { type: "STOP", exchangeId: call.id })]),
    );
    return body;
  }

  const stage = document.createElement("p");
  stage.className = "call-stage";
  stage.textContent = call.handoff?.message ?? describeStage(call.handoff?.status);
  const elapsed = document.createElement("span");
  elapsed.className = "pill";
  elapsed.dataset.since = call.progress?.started_at
    ?? call.handoff?.monitoringStartedAt
    ?? "";
  elapsed.textContent = formatElapsed(elapsed.dataset.since, Date.now());
  const head = document.createElement("div");
  head.className = "card-head";
  head.append(stage, elapsed);
  body.append(head);

  if (call.progress?.files?.length) {
    body.append(fileChecklist(call.progress.files));
  }

  /* A durable filing failure attributed to this call. The file is safe in the
   * downloads folder; what matters is saying so before Done stops monitoring,
   * and saying so even if the browser restarted since it happened. */
  const failures = call.record?.download_failures ?? [];
  if (failures.length) {
    body.append(warning(describeDownloadFailure({
      downloadId: failures[failures.length - 1].download_id,
      message: failures[failures.length - 1].message,
    })));
  }

  const guard = downloadGuard(call.progress, forced.has(call.id));
  if (guard.warning) {
    body.append(warning(guard.warning));
  }

  const done = primary(guard.doneLabel, () =>
    run(async () => {
      await send({ type: "DONE", exchangeId: call.id });
      forced.delete(call.id);
      // Its result is now in the archive, where the row it belongs to is.
      openId = call.id;
    }));
  done.disabled = guard.blockDone;
  body.append(commandRow([done, secondary("Stop", { type: "STOP", exchangeId: call.id })]));

  if (guard.blockDone) {
    const override = document.createElement("button");
    override.type = "button";
    override.className = "ghost";
    override.textContent = "Let me click Done anyway";
    override.addEventListener("click", () => {
      forced.add(call.id);
      refresh();
    });
    body.append(override);
  }
  return body;
}

/* ---------- a call that is over ---------- */

/* Filled in from the companion, because the archive row carries a verdict and
 * a date and nothing else — the rest is a file read this only pays for when a
 * row is actually opened. */
async function archiveDrawer(drawer, item) {
  drawer.append(identity(item.id));
  const pending = document.createElement("p");
  pending.className = "hint";
  pending.textContent = "Reading…";
  drawer.append(pending);

  let inspect;
  try {
    inspect = await send({ type: "INSPECT", exchangeId: item.id });
  } catch (error) {
    pending.className = "warn";
    pending.textContent = error.message;
    return;
  }
  if (drawer.dataset.exchange !== item.id) {
    return;
  }
  pending.remove();

  /* Delivery only. The responder's account of its own work and the hash
   * verdict were shown here as two more facts; they are in the validation
   * report on disk, and the panel's business is whether the files arrived. */
  const rows = [
    ["request", inspect.request_id],
    ["created", inspect.created_at],
  ];
  if (item.clonedFrom) {
    rows.push(["resend of", item.clonedFrom]);
  }
  if (inspect.repair_round > 0) {
    rows.push(["rounds", String(inspect.repair_round)]);
  }
  for (const file of inspect.response_files ?? []) {
    rows.push(["file", `${file.filename} · ${formatBytes(file.size)}`]);
  }
  if (inspect.paths?.main_response) {
    rows.push(["response", inspect.paths.main_response]);
  }
  rows.push(["folder", inspect.paths?.exchange ?? ""]);
  drawer.append(detailList(rows, "kv"));

  if (inspect.defects?.length) {
    drawer.append(defectList(inspect.defects, inspect.defects_omitted));
  }

  /* Nothing in here is urgent — a finished call is finished. Both actions are
   * outlined, so the only filled control in the panel stays the one immediate
   * thing to press in the list above. */
  const actions = [];
  if (inspect.state === "INCOMPLETE" || inspect.state === "COMPLETE") {
    actions.push(accent("Open correction round", () =>
      run(async () => {
        const handoff = await send({ type: "REPAIR", exchangeId: item.id });
        showPromptToCopy(handoff?.repairPrompt);
      })));
  }
  actions.push(accent("Prepare a copy", () =>
    run(async () => {
      const clone = await send({ type: "CLONE", exchangeId: item.id });
      // The copy is a prepared call, so it belongs to the list above — and
      // opening it there is the only way to say so without a sentence.
      openId = clone.exchange_id;
    })));
  drawer.append(commandRow(actions));

  const note = document.createElement("p");
  note.className = "hint";
  note.textContent = "A copy sends the same inputs as a new call. This one keeps "
    + "its answer.";
  drawer.append(note);
}

/* ---------- pieces ---------- */

function identity(exchangeId) {
  const id = document.createElement("div");
  id.className = "call-id";
  id.textContent = exchangeId;
  return id;
}

function detailList(rows, className = "detail") {
  const list = document.createElement("dl");
  list.className = className;
  for (const [term, value] of rows) {
    if (value === null || value === undefined) {
      continue;
    }
    const dt = document.createElement("dt");
    dt.textContent = term;
    const dd = document.createElement("dd");
    dd.textContent = value;
    list.append(dt, dd);
  }
  return list;
}

/* Per call, and only offered where it is about to be used. Destination and
 * agent are separate params: where the call lands, and which model it lands in. */
function destinationPicker(exchangeId, onChosen = () => {}) {
  const wrap = document.createDocumentFragment();
  const picker = document.createElement("select");
  picker.setAttribute("aria-label", "Where to send this call");
  for (const [value, label] of [
    ["new", "In a new tab"],
    ["current", "In the conversation I am in"],
  ]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    picker.append(option);
  }
  picker.value = destinationFor(exchangeId);

  const hint = document.createElement("p");
  hint.className = "hint";
  const describe = () => {
    const agent = providerFor(exchangeId) === "claude" ? "Claude" : "ChatGPT";
    hint.textContent = picker.value === "current"
      ? `Whichever ${agent} conversation is focused when you click Go.`
      : `Opens a new tab with a new ${agent} conversation. Nothing it has been `
        + "told before affects the answer.";
  };
  describe();

  picker.addEventListener("change", () => {
    destinations.set(exchangeId, picker.value);
    chrome.storage.local.set({ goMode: picker.value });
    defaultDestination = picker.value;
    describe();
    // Recovery holds its button until this has been answered deliberately.
    onChosen();
  });

  wrap.append(picker, hint);
  return wrap;
}

function providerPicker(exchangeId) {
  const wrap = document.createDocumentFragment();
  const picker = document.createElement("select");
  picker.setAttribute("aria-label", "Which agent to send this call to");
  for (const [value, label] of [
    ["gpt", "GPT"],
    ["claude", "Claude"],
  ]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    picker.append(option);
  }
  picker.value = providerFor(exchangeId);

  picker.addEventListener("change", () => {
    providers.set(exchangeId, picker.value);
    chrome.storage.local.set({ agent: picker.value });
    defaultProvider = picker.value;
    refresh();
  });

  wrap.append(picker);
  return wrap;
}

function destinationFor(exchangeId) {
  return destinations.get(exchangeId) ?? defaultDestination;
}

function providerFor(exchangeId) {
  return providers.get(exchangeId) ?? defaultProvider;
}

function commandRow(buttons) {
  const row = document.createElement("div");
  row.className = "row";
  row.append(...buttons);
  return row;
}

function primary(label, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "primary";
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

function accent(label, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "accent";
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

function secondary(label, message) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary";
  button.textContent = label;
  button.addEventListener("click", () => run(() => send(message)));
  return button;
}

function warning(text) {
  const line = document.createElement("p");
  line.className = "warn";
  line.textContent = text;
  return line;
}

function fileChecklist(files) {
  const list = document.createElement("ul");
  list.className = "files";
  for (const file of files) {
    const item = document.createElement("li");
    item.className = file.arrived ? "here" : "away";

    const tick = document.createElement("span");
    tick.className = "tick";
    tick.textContent = file.arrived ? "✓" : "○";

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = file.filename;

    const size = document.createElement("span");
    size.className = "size";
    size.textContent = file.arrived ? formatBytes(file.size) : "waiting";

    item.append(tick, name, size);
    list.append(item);
  }
  return list;
}

/* The diagnosis a correction round would send, shown before deciding whether to
 * open one — so the operator sees why, without a terminal. */
function defectList(defects, omitted) {
  const list = document.createElement("ul");
  list.className = "defects";
  for (const defect of defects) {
    const item = document.createElement("li");
    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = `${defect.kind} · ${defect.target}`;
    const why = document.createElement("span");
    why.className = "why";
    why.textContent = `expected ${defect.expected}; got ${defect.observed}`;
    item.append(kind, why);
    list.append(item);
  }
  if (omitted > 0) {
    const item = document.createElement("li");
    const why = document.createElement("span");
    why.className = "why";
    why.textContent = `and ${omitted} more — run defects --exchange for all of them`;
    item.append(why);
    list.append(item);
  }
  return list;
}

function renderDownloadFailure(failure) {
  const message = describeDownloadFailure(failure);
  downloadFailure.textContent = message;
  downloadFailure.hidden = message === "";
}

/* ---------- text the page would not take ---------- */

/* Only when typing it was attempted and failed.
 *
 * `true` means the line is already in the composer, and showing it again would
 * invite the operator to paste a second copy underneath the first. `null` means
 * it was never attempted, because the call is going into a conversation the
 * operator is already working in and that thread does not need telling. */
function showLaunchPrompt(handoff) {
  if (!handoff || handoff.launchInserted !== false) {
    return;
  }
  showPromptToCopy(handoff.launchPrompt);
}

function showPromptToCopy(text) {
  if (typeof text !== "string" || !text) {
    return;
  }
  promptText.textContent = text;
  promptCard.hidden = false;
  copyPrompt.textContent = "Copy";
}

/* ---------- plumbing ---------- */

function scheduleTick(calls) {
  clearInterval(ticking);
  if (!calls.some((call) => call.state === "ACTIVE")) {
    return;
  }
  // A live clock without a full refresh, so the elapsed pill stays honest
  // between status pushes.
  ticking = setInterval(() => {
    for (const pill of nowList.querySelectorAll("[data-since]")) {
      pill.textContent = formatElapsed(pill.dataset.since, Date.now());
    }
  }, 1000);
}

function setBusy(value) {
  busy = value;
  for (const button of document.querySelectorAll(".row-drawer button")) {
    button.disabled = value;
  }
}

function showError(message) {
  errorLine.textContent = message;
  errorLine.hidden = false;
}

function clearError() {
  errorLine.hidden = true;
  errorLine.textContent = "";
}

function shortRoot(root) {
  const parts = String(root).split(/[\\/]/).filter(Boolean);
  return parts.slice(-2).join("/") || root;
}

async function send(message) {
  const response = await chrome.runtime.sendMessage(message);
  if (!response?.ok) {
    throw new Error(response?.error ?? "Extension command failed");
  }
  return response.result;
}
