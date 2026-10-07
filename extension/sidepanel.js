/* Three lists of calls, worked top to bottom: ready to send, running, over.
 *
 * A call is one line and the thing it needs is on that line. Sending is one
 * press of Go; the agent and the destination are two toggles above the list,
 * read at the moment of the press. A running call needs nothing pressed at
 * all: it finishes itself when its archive lands and changes colour where it
 * sits, and it stays there until it is moved to History, so a call that
 * finished while nobody was looking is still seen.
 *
 * Three rules carried over intact, because they were the correct things about
 * every version so far: recovery never guesses which conversation a restart
 * destroyed, the panel shows where files are and never what is in them, and no
 * software here ever clicks Send.
 */

import {
  archiveVerdict,
  callCentre,
  describeStage,
  formatBytes,
  formatElapsed,
  describeDownloadFailure,
} from "./lib/panel.js";
import { PROVIDER_IDS } from "./lib/target.js";

const el = (id) => document.querySelector(`#${id}`);

const readyList = el("ready");
const runningList = el("running");
const pastList = el("past");
const shelveAll = el("shelve-all");
const historyToggle = el("history-toggle");
const historyWrap = el("history-wrap");
const promptCard = el("prompt-card");
const promptText = el("prompt-text");
const copyPrompt = el("copy-prompt");
const health = el("health");
const errorLine = el("error");
const downloadFailure = el("download-failure");
const reloadButton = el("reload-button");

const AGENT_NAMES = { gpt: "GPT", claude: "Claude", grok: "Grok" };
const DESTINATIONS = [["new", "New tab"], ["current", "This chat"]];
const SAY = {
  lost: "the browser restarted and lost this call's conversation",
  waiting: "waiting for its archive",
  checking: "checking the archive",
  complete: "complete",
  incomplete: "came back with defects",
};
/* How often the open panel asks the companion to look in the Downloads folder.
 * The browser's own download event is faster than this; the look is for the
 * archive that event never reported. */
const COLLECT_EVERY_MS = 5000;
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* Where the next Go sends, shown on the two toggles and read when Go is
 * pressed. It is the last answer the operator gave, kept across sessions. */
const choice = { agent: "gpt", destination: "new" };
let historyOpen = false;
let openPast = null;
let justOpened = null;
const inspected = new Map();
/* Cards already on screen, so only a card that is new animates in and only a
 * card whose state changed blooms. Every refresh rebuilds the lists. */
let seen = new Set();
const lastPhase = new Map();
const lastCounts = {};
let inFlight = 0;
let busy = false;
let collecting = false;
let connectionError = "";
let actionError = "";

/* ---------- small builders ---------- */

function h(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "on") for (const [name, fn] of Object.entries(value)) node.addEventListener(name, fn);
    else if (key === "data") Object.assign(node.dataset, value);
    else if (value !== false && value !== null && value !== undefined) node.setAttribute(key, value === true ? "" : value);
  }
  for (const kid of kids.flat()) {
    if (kid !== null && kid !== undefined && kid !== false) node.append(kid);
  }
  return node;
}

/* Drawn node by node. Nothing in this panel is ever parsed from a string into
 * markup, so nothing a call returns can become markup either. */
const SVG_NS = "http://www.w3.org/2000/svg";
function drawing(viewBox, shapes) {
  const root = document.createElementNS(SVG_NS, "svg");
  root.setAttribute("viewBox", viewBox);
  root.setAttribute("aria-hidden", "true");
  for (const [tag, attributes] of shapes) {
    const shape = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes)) shape.setAttribute(name, value);
    root.append(shape);
  }
  return root;
}

const stroke = { fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round" };
const ICONS = {
  stop: () => drawing("0 0 14 14", [["rect", { x: "3", y: "3", width: "8", height: "8", rx: "2", fill: "currentColor" }]]),
  shelve: () => drawing("0 0 14 14", [["path", { d: "M7 2v6.5M4 6l3 3 3-3M2.5 12h9", ...stroke }]]),
  reload: () => drawing("0 0 14 14", [["path", { d: "M11.5 7a4.5 4.5 0 1 1-1.4-3.25M11.5 2.4v2.7H8.8", ...stroke }]]),
  chevron: () => drawing("0 0 14 14", [["path", { d: "M2.5 5l4.5 4.5L11.5 5", ...stroke, "stroke-width": "2" }]]),
};

function ring(phase) {
  const shapes = [["circle", { class: "arc", cx: "22", cy: "22", r: "16" }]];
  if (phase === "complete") shapes.push(["path", { class: "glyph", d: "M11 23l8 8 14-16" }]);
  if (phase === "incomplete") shapes.push(["path", { class: "glyph", d: "M13 13l18 18M31 13L13 31" }]);
  return h("div", { class: "ring", role: "img", "aria-label": SAY[phase], title: SAY[phase] },
    drawing("0 0 44 44", shapes));
}

function who(call, caption = "", hover = "") {
  return h("div", { class: "who" },
    call.project ? h("span", { class: "project" }, call.project) : null,
    h("span", { class: "name", title: call.subject }, call.subject),
    caption ? h("span", { class: "caption", title: hover || caption }, caption) : null);
}

function roundButton(icon, label, onClick) {
  return h("button", { class: "round small", type: "button", title: label, "aria-label": label, on: { click: onClick } },
    ICONS[icon]());
}

function pill(label, onClick, tone = "") {
  return h("button", { class: `pill ${tone}`.trim(), type: "button", on: { click: onClick } }, label);
}

/* ---------- the two toggles ---------- */

function buildToggle(root, options, read, write) {
  root.style.setProperty("--n", String(options.length));
  root.append(h("span", { class: "thumb", "aria-hidden": "true" }));
  const buttons = options.map(([value, label]) => {
    const button = h("button", { type: "button", role: "radio", data: { v: value } }, label);
    button.addEventListener("click", () => choose(value));
    root.append(button);
    return button;
  });
  const apply = () => {
    const index = Math.max(0, options.findIndex(([value]) => value === read()));
    root.style.setProperty("--i", String(index));
    buttons.forEach((button, i) => {
      button.setAttribute("aria-checked", String(i === index));
      button.tabIndex = i === index ? 0 : -1;
    });
  };
  const choose = (value) => { write(value); apply(); };
  root.addEventListener("keydown", (event) => {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const index = Math.max(0, options.findIndex(([value]) => value === read()));
    const next = (index + step + options.length) % options.length;
    choose(options[next][0]);
    buttons[next].focus();
  });
  return apply;
}

const applyAgent = buildToggle(
  el("seg-agent"),
  PROVIDER_IDS.map((id) => [id, AGENT_NAMES[id] ?? id]),
  () => choice.agent,
  (value) => {
    choice.agent = value;
    chrome.storage.local.set({ agent: value });
    tint();
  },
);

const applyDestination = buildToggle(
  el("seg-dest"),
  DESTINATIONS,
  () => choice.destination,
  (value) => {
    choice.destination = value;
    chrome.storage.local.set({ goMode: value });
  },
);

/* The whole surface takes the tint of the agent the next call goes to. */
function tint() {
  document.documentElement.dataset.agent = choice.agent;
}

/* ---------- fixed controls ---------- */

reloadButton.append(ICONS.reload());
el("history-chevron").append(ICONS.chevron());

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

historyToggle.addEventListener("click", () => {
  historyOpen = !historyOpen;
  chrome.storage.local.set({ historyOpen });
  applyHistoryOpen();
});

function applyHistoryOpen() {
  historyToggle.setAttribute("aria-expanded", String(historyOpen));
  historyWrap.classList.toggle("open", historyOpen);
  historyWrap.toggleAttribute("inert", !historyOpen);
}

shelveAll.addEventListener("click", () => {
  const cards = [...runningList.querySelectorAll(".card.complete, .card.incomplete")];
  shelve(cards.map((card) => card.dataset.id), cards);
});

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === "HANDOFF_STATUS") {
    showLaunchPrompt(message.handoff);
    refresh();
  }
});

chrome.storage.local.get(["goMode", "agent", "historyOpen"]).then((stored) => {
  if (stored.goMode === "current" || stored.goMode === "new") {
    choice.destination = stored.goMode;
  }
  if (PROVIDER_IDS.includes(stored.agent)) {
    choice.agent = stored.agent;
  }
  historyOpen = stored.historyOpen === true;
  applyAgent();
  applyDestination();
  applyHistoryOpen();
  tint();
  refresh().then(collect);
});

/* ---------- actions ---------- */

/* One action at a time. A second press while the first is still talking to the
 * companion is dropped rather than queued: the lists it was aimed at are about
 * to be rebuilt. */
async function run(action, leaving = []) {
  if (busy) return;
  busy = true;
  actionError = "";
  showErrors();
  if (!reducedMotion) {
    for (const card of leaving) card?.classList.add("leave");
  }
  try {
    await action();
  } catch (error) {
    actionError = error.message;
  } finally {
    busy = false;
    await refresh();
  }
}

function shelve(exchangeIds, cards) {
  if (!exchangeIds.length) return;
  run(async () => {
    await send({ type: "SHELVE", exchangeIds });
    historyToggle.classList.remove("nudge");
    void historyToggle.offsetWidth;
    historyToggle.classList.add("nudge");
  }, cards);
}

let refreshGeneration = 0;
async function refresh() {
  const generation = ++refreshGeneration;
  let state;
  try {
    state = await send({ type: "GET_STATUS" });
    if (generation !== refreshGeneration) return;
  } catch (error) {
    health.className = "led down";
    setHealthLabel("Companion unavailable");
    connectionError = error.message;
    showErrors();
    return;
  }
  health.className = "led up";
  setHealthLabel(state.root ? `Connected · ${shortRoot(state.root)}` : "Companion connected");
  connectionError = "";
  showErrors();

  const { ready, running, past } = callCentre(state);
  const keys = new Set();
  renderReady(ready, keys);
  renderRunning(running, keys);
  renderPast(past);
  renderDownloadFailure(state.lastDownloadFailure);
  seen = keys;
}

/* ---------- 1. ready to send ---------- */

function renderReady(calls, keys) {
  readyList.replaceChildren();
  let fresh = 0;
  for (const call of calls) {
    const key = `ready:${call.id}`;
    keys.add(key);
    const card = h("li", { class: "card", data: { id: call.id } });
    const go = h("button", { class: "round go", type: "button", "aria-label": `Send ${call.subject}` }, "Go");
    go.addEventListener("click", () =>
      run(async () => {
        const handoff = await send({
          type: "GO",
          exchangeId: call.id,
          mode: choice.destination,
          provider: choice.agent,
        });
        showLaunchPrompt(handoff);
      }, [card]));
    card.append(h("div", { class: "line" }, who(call), go));
    if (!seen.has(key)) {
      card.classList.add("enter");
      card.style.setProperty("--d", `${fresh++ * 45}ms`);
    }
    readyList.append(card);
  }
  el("ready-empty").hidden = calls.length > 0;
  setCount("ready-count", calls.length, calls.length ? "accent" : "");
}

/* ---------- 2. running ---------- */

function renderRunning(calls, keys) {
  runningList.replaceChildren();
  let fresh = 0;
  for (const call of calls) {
    const key = `running:${call.id}`;
    keys.add(key);
    const card = runningCard(call);
    if (!seen.has(key)) {
      card.classList.add("enter");
      card.style.setProperty("--d", `${fresh++ * 45}ms`);
    } else if (lastPhase.get(call.id) !== call.phase) {
      card.classList.add("changed");
    }
    lastPhase.set(call.id, call.phase);
    runningList.append(card);
  }
  const over = calls.filter((call) => call.phase === "complete" || call.phase === "incomplete").length;
  inFlight = calls.length - over;
  el("running-empty").hidden = calls.length > 0;
  setCount("running-count", inFlight, inFlight ? "amber" : "");

  if (over > 0 && shelveAll.hidden) {
    shelveAll.hidden = false;
    shelveAll.classList.add("appear");
  }
  if (over === 0) {
    shelveAll.hidden = true;
    shelveAll.classList.remove("appear");
  }
}

function runningCard(call) {
  const card = h("li", { class: `card run ${call.phase}`, data: { id: call.id } });
  const stop = () => roundButton("stop", `Stop ${call.subject}`, () =>
    run(() => send({ type: "STOP", exchangeId: call.id }), [card]));

  if (call.phase === "lost") {
    /* The companion still holds this call; the browser holds no handoff. What
     * a restart destroyed is the binding to a conversation, and that cannot be
     * guessed back — resuming a conductor call into a fresh conversation
     * silently discards the thread the mode existed to keep. So there is no
     * Resume that reads a default: each button is the answer, given for this
     * call, at the moment it is pressed. */
    const resume = (mode) =>
      run(async () => {
        const handoff = await send({
          type: "RESUME",
          exchangeId: call.id,
          mode,
          provider: choice.agent,
        });
        showLaunchPrompt(handoff);
      });
    card.append(
      h("div", { class: "line" }, ring(call.phase), who(call, "Browser restarted. Where does it resume?"), stop()),
      h("div", { class: "tools" },
        pill("Resume in a new tab", () => resume("new"), "accent"),
        pill("Resume in this chat", () => resume("current"), "accent")),
    );
    return card;
  }

  if (call.phase === "complete" || call.phase === "incomplete") {
    const tail = [];
    if (call.phase === "incomplete") {
      tail.push(pill("Correct", () =>
        run(async () => {
          const handoff = await send({ type: "REPAIR", exchangeId: call.id });
          showPromptToCopy(handoff?.repairPrompt);
          showLaunchPrompt(handoff);
          // The correction is a call of its own now; this one has been seen.
          await send({ type: "SHELVE", exchangeIds: [call.id] });
        }), "bad"));
    }
    tail.push(roundButton("shelve", `Move ${call.subject} to history`, () => shelve([call.id], [card])));
    card.append(h("div", { class: "line" }, ring(call.phase), who(call), tail));
    return card;
  }

  const clock = h("span", { class: "clock", data: { since: call.since ?? "" } },
    formatElapsed(call.since, Date.now()));
  card.append(h("div", { class: "line" },
    ring(call.phase),
    who(call, describeStage(call.handoff), call.handoff?.message ?? ""),
    clock,
    call.phase === "waiting" ? stop() : null));

  /* A durable filing failure attributed to this call. The file is safe where
   * the browser saved it; what matters is saying so, and saying so even if the
   * browser restarted since it happened. */
  const failures = call.record?.download_failures ?? [];
  if (failures.length) {
    card.append(h("p", { class: "warn" }, describeDownloadFailure({
      downloadId: failures[failures.length - 1].download_id,
      message: failures[failures.length - 1].message,
    })));
  }
  return card;
}

/* ---------- 3. history ---------- */

function renderPast(rows) {
  pastList.replaceChildren();
  for (const item of rows) {
    const verdict = archiveVerdict(item);
    const open = openPast === item.id;
    const row = h("li", {},
      // A button, not a list item, so it is reachable by keyboard.
      h("button", {
        class: "past-row",
        type: "button",
        "aria-expanded": String(open),
        title: verdict.label,
        on: {
          click: () => {
            openPast = open ? null : item.id;
            justOpened = openPast;
            refresh();
          },
        },
      },
        h("span", { class: `pip ${verdict.tone}`.trim(), role: "img", "aria-label": verdict.label }),
        h("span", { class: "name", title: item.subject }, item.subject),
        item.project ? h("span", { class: "project" }, item.project) : null));
    if (open) {
      const detail = h("div", { class: "detail", data: { exchange: item.id } });
      if (justOpened === item.id) {
        detail.classList.add("enter");
        justOpened = null;
      }
      row.append(detail);
      // Filled in from the companion. It appends into the drawer itself, and
      // what it returns is a Promise that is deliberately never appended.
      void fillDetail(detail, item);
    }
    pastList.append(row);
  }
  el("past-empty").hidden = rows.length > 0;
  setCount("history-count", rows.length, "");
}

/* The row carries a verdict and nothing else — the rest is a file read this
 * only pays for when a row is actually opened. What was read last time is
 * shown at once, so a refresh does not blank an open drawer. */
async function fillDetail(detail, item) {
  const known = inspected.get(item.id);
  if (known) {
    detail.replaceChildren(...detailNodes(item, known));
  } else {
    detail.replaceChildren(h("p", { class: "hint" }, "Reading…"));
  }

  let inspect;
  try {
    inspect = await send({ type: "INSPECT", exchangeId: item.id });
  } catch (error) {
    if (detail.isConnected) detail.replaceChildren(h("p", { class: "warn" }, error.message));
    return;
  }
  if (!detail.isConnected || detail.dataset.exchange !== item.id) {
    return;
  }
  if (!known || JSON.stringify(known) !== JSON.stringify(inspect)) {
    detail.replaceChildren(...detailNodes(item, inspect));
  }
  inspected.set(item.id, inspect);
}

function detailNodes(item, inspect) {
  /* Delivery only. The responder's account of its own work and the hash
   * verdict are in the validation report on disk, and the panel's business is
   * whether the files arrived. */
  const rows = [
    ["exchange", item.id],
    ["result", archiveVerdict(item).label],
    ["request", inspect.request_id],
    ["created", inspect.created_at],
  ];
  if (item.clonedFrom) {
    rows.push(["resend of", item.clonedFrom]);
  }
  if (inspect.repair_round > 0) {
    rows.push(["rounds", String(inspect.repair_round)]);
  }
  for (const name of inspect.attach_files ?? []) {
    rows.push(["sent", name]);
  }
  for (const file of inspect.response_files ?? []) {
    rows.push(["file", `${file.filename} · ${formatBytes(file.size)}`]);
  }
  if (inspect.paths?.main_response) {
    rows.push(["response", inspect.paths.main_response]);
  }
  rows.push(["folder", inspect.paths?.exchange ?? ""]);

  const nodes = [detailList(rows)];
  if (inspect.defects?.length) {
    nodes.push(defectList(inspect.defects, inspect.defects_omitted));
  }

  const actions = [];
  if (inspect.state === "INCOMPLETE" || inspect.state === "COMPLETE") {
    actions.push(pill("Open correction round", () =>
      run(async () => {
        const handoff = await send({ type: "REPAIR", exchangeId: item.id });
        showPromptToCopy(handoff?.repairPrompt);
        showLaunchPrompt(handoff);
      })));
  }
  actions.push(pill("Prepare a copy", () =>
    run(async () => {
      // Prepares the same inputs as a new call in the Ready list. It is not
      // sent: Go is a separate press, and this call keeps its answer.
      await send({ type: "CLONE", exchangeId: item.id });
    })));
  nodes.push(h("div", { class: "tools" }, actions));
  return nodes;
}

/* ---------- pieces ---------- */

function detailList(rows) {
  const list = h("dl", { class: "kv" });
  for (const [term, value] of rows) {
    if (value === null || value === undefined || value === "") {
      continue;
    }
    list.append(h("dt", {}, term), h("dd", {}, value));
  }
  return list;
}

/* The diagnosis a correction round would send, shown before deciding whether to
 * open one — so the operator sees why, without a terminal. */
function defectList(defects, omitted) {
  const list = h("ul", { class: "defects" });
  for (const defect of defects) {
    list.append(h("li", {},
      h("span", { class: "kind" }, `${defect.kind} · ${defect.target}`),
      h("span", { class: "why" }, `expected ${defect.expected}; got ${defect.observed}`)));
  }
  if (omitted > 0) {
    list.append(h("li", {},
      h("span", { class: "why" }, `and ${omitted} more — run defects --exchange for all of them`)));
  }
  return list;
}

function renderDownloadFailure(failure) {
  const message = describeDownloadFailure(failure);
  downloadFailure.textContent = message;
  downloadFailure.hidden = message === "";
}

function setCount(id, value, tone) {
  const node = el(id);
  node.textContent = String(value);
  node.className = tone ? `count ${tone}` : "count";
  if (lastCounts[id] !== undefined && lastCounts[id] !== value) {
    void node.offsetWidth;
    node.classList.add("bump");
  }
  lastCounts[id] = value;
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

/* Ask the companion to look in the Downloads folder. It finishes whatever has
 * landed, and the service worker tells every open panel when it does. A count
 * that disagrees with this panel means a call ended some other way - from a
 * terminal, say - and the lists are stale. */
async function collect() {
  if (collecting || busy) return;
  collecting = true;
  try {
    const result = await send({ type: "COLLECT" });
    const pending = (result?.waiting?.length ?? 0) + (result?.failed?.length ?? 0);
    if (pending !== inFlight) await refresh();
  } catch (_error) {
    // A companion that cannot be reached is already reported by refresh.
  } finally {
    collecting = false;
  }
}

setInterval(() => {
  if (inFlight > 0) collect();
}, COLLECT_EVERY_MS);

// A live clock without a full refresh, so elapsed time stays honest between
// status pushes.
setInterval(() => {
  for (const clock of runningList.querySelectorAll("[data-since]")) {
    clock.textContent = formatElapsed(clock.dataset.since, Date.now());
  }
}, 1000);

function showErrors() {
  const message = connectionError || actionError;
  errorLine.textContent = message;
  errorLine.hidden = message === "";
}

function setHealthLabel(text) {
  health.title = text;
  health.setAttribute("aria-label", text);
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
