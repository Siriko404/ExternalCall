/* Pure panel logic, kept out of the DOM so it can be tested.
 *
 * callCentre is the important one. The panel is three lists, in the order the
 * operator works through them: calls ready to send, calls that are running,
 * and calls that are over. It decides which call is in which, and gathers
 * everything a row needs from the separate sources the companion and the
 * browser report.
 */

import { providerLabel } from "./target.js";

/* What a running call needs from the operator right now, in a few words. The
 * card is one line, so this is the short form; the full message stays on the
 * handoff and is what the card shows on hover. */
const CAPTIONS = {
  WAITING_FOR_ATTACH_CLICK: (handoff) => `Click Attach in ${providerLabel(handoff.provider)}`,
  ATTACHED: (handoff) => (handoff.launchInserted === false
    ? "Paste the instruction, then Send"
    : "Review, then Send"),
  REPAIR_PROMPT_INSERTED: () => "Correction typed. Review, then Send",
  REPAIR_PROMPT_READY: () => "Paste the correction, then Send",
  DOWNLOAD_MOVED: () => "Checking the archive",
  DOWNLOAD_INVALID: () => "Checking the archive",
};

export function describeStage(handoff) {
  if (!handoff) {
    return "";
  }
  const caption = CAPTIONS[handoff.status];
  return caption ? caption(handoff) : String(handoff.message ?? handoff.status ?? "");
}


/* Which of the five things a call in the Running list is doing.
 *
 * lost       the companion holds it, the browser no longer knows its tab
 * waiting    sent or about to be; its archive has not landed
 * checking   the archive is in and is being validated
 * complete   it finished and the delivery checked out
 * incomplete it finished and the delivery has defects
 *
 * "checking" is read from the archive, not from a timer: the companion reports
 * the archive as arrived, or the browser has just reported filing it. A check
 * that failed goes back to waiting, where the card says why and can still be
 * stopped; "checking" has no controls, so it must never be a place to get stuck.
 */
function activePhase(handoff, progress) {
  if (handoff === null) {
    return "lost";
  }
  if (handoff.status === "CHECK_FAILED") {
    return "waiting";
  }
  if (progress?.complete || handoff.status === "DOWNLOAD_MOVED" || handoff.status === "DOWNLOAD_INVALID") {
    return "checking";
  }
  return "waiting";
}


/* A call appears once. `recent` is every exchange including the prepared and
 * the running ones, so History is what is left after the two lists above it
 * have taken what they need.
 *
 * A finished call stays in Running until the operator moves it on, because it
 * finished on its own and has to be seen changing colour. `finished` is the
 * record of those; it is honoured only for an exchange the companion still
 * reports as finished, so a call sent again or corrected in place goes back to
 * being whatever it now is.
 *
 * "lost" is per call, not per panel. A browser restart destroys the binding
 * between a call and its conversation while the companion still holds the
 * call; with several calls running, one can be lost and another not.
 */
export function callCentre(state = {}) {
  const handoffById = new Map(
    (state.handoffs ?? []).map((handoff) => [handoff.exchangeId, handoff]),
  );
  const progressById = new Map(
    (state.progress ?? []).map((item) => [item.exchange_id, item]),
  );
  const recentById = new Map(
    (state.recent ?? []).map((row) => [row.exchange_id, row]),
  );

  const ready = (state.ready ?? []).map((call) => ({
    id: call.exchange_id,
    subject: call.subject || call.exchange_id,
    project: call.project ?? null,
    call,
  }));

  const active = (state.active ?? []).map((record) => {
    const progress = progressById.get(record.exchange_id) ?? null;
    const handoff = handoffById.get(record.exchange_id) ?? null;
    return {
      id: record.exchange_id,
      subject: progress?.subject
        || recentById.get(record.exchange_id)?.subject
        || handoff?.subject
        || record.request_id
        || record.exchange_id,
      project: progress?.project ?? recentById.get(record.exchange_id)?.project ?? null,
      phase: activePhase(handoff, progress),
      since: progress?.started_at ?? record.started_at ?? handoff?.monitoringStartedAt ?? null,
      record,
      progress,
      handoff,
    };
  });

  const taken = new Set([...ready, ...active].map((item) => item.id));
  const settled = (state.finished ?? [])
    .map((mark) => recentById.get(mark?.exchangeId))
    .filter((row) => row && !taken.has(row.exchange_id)
      && (row.state === "COMPLETE" || row.state === "INCOMPLETE"))
    .map((row) => ({
      id: row.exchange_id,
      subject: row.subject || row.exchange_id,
      project: row.project ?? null,
      phase: row.state === "COMPLETE" ? "complete" : "incomplete",
      since: null,
      record: null,
      progress: null,
      handoff: null,
    }));

  /* By exchange id, which begins with its timestamp: a card keeps its place
   * when it changes colour instead of jumping to the end of the list. */
  const running = [...active, ...settled]
    .sort((a, b) => String(a.id).localeCompare(String(b.id)));
  for (const item of running) {
    taken.add(item.id);
  }

  const past = (state.recent ?? [])
    .filter((row) => !taken.has(row.exchange_id))
    .map((row) => ({
      id: row.exchange_id,
      subject: row.subject || row.exchange_id,
      project: row.project ?? null,
      state: row.state ?? "",
      requestId: row.request_id ?? null,
      createdAt: row.created_at ?? null,
      clonedFrom: row.cloned_from ?? null,
      repairRound: row.repair_round ?? 0,
    }));

  return { ready, running, past };
}


/* What a finished row says about itself: the delivery, and nothing else.
 *
 * The panel reports on the pipe, not on the answer. Delivery is the half this
 * end of the exchange can prove - every promised file arrived, and each one
 * matched the hash declared for it. Whether the work is any good is in the
 * response, which the operator reads; a panel that grades it is guessing with
 * the responder's own marking.
 *
 * An earlier version demoted a COMPLETE delivery to amber when the responder
 * called its own work PARTIAL. That is the thing this must not do: it turns an
 * intact delivery worth reading into a row that looks like a failure, which is
 * the exact misreading that started all of this.
 */
export function archiveVerdict(row = {}) {
  return {
    label: row.state || "—",
    tone: row.state === "COMPLETE" ? "ok" : row.state === "INCOMPLETE" ? "bad" : "",
  };
}

export function formatBytes(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) {
    return "";
  }
  if (value < 1024) {
    return `${value} B`;
  }
  const units = ["KB", "MB", "GB"];
  let scaled = value / 1024;
  let unit = 0;
  while (scaled >= 1024 && unit < units.length - 1) {
    scaled /= 1024;
    unit += 1;
  }
  return `${scaled < 10 ? scaled.toFixed(1) : Math.round(scaled)} ${units[unit]}`;
}

export function formatElapsed(startedAt, nowMs) {
  const started = Date.parse(startedAt ?? "");
  if (!Number.isFinite(started)) {
    return "";
  }
  const seconds = Math.max(0, Math.round((nowMs - started) / 1000));
  if (seconds < 60) {
    return `${seconds}s`;
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`;
  }
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}


export function describeDownloadFailure(failure) {
  // The operator needs the recovery, not the stack trace: the bytes are safe
  // where the browser saved them, and the call is still waiting for them.
  if (!failure || typeof failure.message !== "string" || failure.message === "") {
    return "";
  }
  const which = Number.isInteger(failure.downloadId)
    ? `Download ${failure.downloadId}`
    : "A download";
  return (
    `${which} was not filed into its call: ${failure.message}. ` +
    "The file is still where the browser saved it. If that is your downloads " +
    "folder it is collected from there; otherwise move it there."
  );
}
