/* Pure panel logic, kept out of the DOM so it can be tested.
 *
 * downloadGuard is the important one. Clicking Done stops monitoring, so a file
 * still in flight at that moment is never collected and has to be copied into
 * the exchange by hand afterwards. The guard turns that into something visible:
 * Done stays disabled while an expected file is missing, and enabling it is a
 * deliberate act rather than an accident of timing.
 */

const STAGES = {
  WAITING_FOR_ATTACH_CLICK: "Waiting for you to click Attach files in the conversation.",
  ATTACHED: "Files attached. Review them, then click Send.",
  COLLECTING: "Collecting downloads.",
  REPAIR_PROMPTED: "Correction prompt is in the composer. Click Send.",
};

export function describeStage(status) {
  return STAGES[status] ?? String(status ?? "Running.");
}


/* The panel is two lists of calls: the ones that need something, and the ones
 * that are over. This decides which is which, and gathers everything a row
 * needs from the four separate sources the companion and the browser report.
 *
 * A call appears once. `recent` is every exchange including the prepared and
 * the running ones, so the archive is what is left after the top list has
 * taken what it needs — otherwise a running call is in the panel twice, once
 * as work and once as history.
 *
 * needsResume is per call, not per panel. A browser restart destroys the
 * binding between a call and its conversation while the companion still holds
 * the call; with several calls running, one can be lost and another not.
 */
export function callCentre(state = {}) {
  const handoffById = new Map(
    (state.handoffs ?? []).map((handoff) => [handoff.exchangeId, handoff]),
  );
  const progressById = new Map(
    (state.progress ?? []).map((item) => [item.exchange_id, item]),
  );

  const now = [
    ...(state.active ?? []).map((record) => {
      const progress = progressById.get(record.exchange_id) ?? null;
      const handoff = handoffById.get(record.exchange_id) ?? null;
      return {
        id: record.exchange_id,
        subject: progress?.subject
          || handoff?.subject
          || record.request_id
          || record.exchange_id,
        state: "ACTIVE",
        requestId: record.request_id ?? null,
        record,
        progress,
        handoff,
        needsResume: handoff === null,
      };
    }),
    ...(state.ready ?? []).map((call) => ({
      id: call.exchange_id,
      subject: call.subject || call.exchange_id,
      state: "PREPARED",
      requestId: call.request_id ?? null,
      call,
    })),
  ];

  const upstairs = new Set(now.map((item) => item.id));
  const past = (state.recent ?? [])
    .filter((row) => !upstairs.has(row.exchange_id))
    .map((row) => ({
      id: row.exchange_id,
      subject: row.subject || row.exchange_id,
      state: row.state ?? "",
      requestId: row.request_id ?? null,
      createdAt: row.created_at ?? null,
      clonedFrom: row.cloned_from ?? null,
      repairRound: row.repair_round ?? 0,
    }));

  return { now, past };
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

export function downloadGuard(progress, forced = false) {
  // Without progress data we cannot know, so we must not block the operator.
  if (!progress || !Array.isArray(progress.files) || progress.files.length === 0) {
    return { blockDone: false, warning: "", doneLabel: "Done and validate" };
  }
  const missing = progress.files.filter((file) => !file.arrived).map((file) => file.filename);
  if (missing.length === 0) {
    return {
      blockDone: false,
      warning: "",
      doneLabel: `Done and validate (${progress.files.length}/${progress.files.length})`,
    };
  }
  const noun = missing.length === 1 ? "file has" : "files have";
  return {
    blockDone: !forced,
    warning:
      `${missing.length} ${noun} not arrived yet: ${missing.join(", ")}. ` +
      "Done stops monitoring, so anything still downloading will not be collected.",
    doneLabel: `Done and validate (${progress.received}/${progress.expected})`,
  };
}


export function describeDownloadFailure(failure) {
  // The operator needs the recovery, not the stack trace: the bytes are safe in
  // the downloads folder and the exchange is still waiting for them.
  if (!failure || typeof failure.message !== "string" || failure.message === "") {
    return "";
  }
  const which = Number.isInteger(failure.downloadId)
    ? `Download ${failure.downloadId}`
    : "A download";
  return (
    `${which} was not filed into its call: ${failure.message}. ` +
    "The file is still in your downloads folder. Copy it into the exchange's " +
    "response folder, or download it again before clicking Done."
  );
}
