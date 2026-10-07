function validDownloadId(value) {
  return Number.isInteger(value) && value >= 0;
}

function parseTime(value) {
  if (typeof value !== "string") {
    return Number.NaN;
  }
  return Date.parse(value);
}

export function shouldObserveDownload(active, downloadItem) {
  if (!active?.monitoring || !validDownloadId(downloadItem?.id)) {
    return false;
  }

  const startedAt = parseTime(active.monitoringStartedAt);
  const downloadStartedAt = parseTime(downloadItem.startTime);
  if (!Number.isFinite(startedAt) || !Number.isFinite(downloadStartedAt)) {
    return false;
  }
  if (downloadStartedAt < startedAt) {
    return false;
  }

  const baseline = new Set(active.downloadBaseline ?? []);
  const observed = new Set(active.observedDownloadIds ?? []);
  return !baseline.has(downloadItem.id) && !observed.has(downloadItem.id);
}

function handoffList(handoffs) {
  if (Array.isArray(handoffs)) {
    return handoffs;
  }
  return handoffs && typeof handoffs === "object" ? Object.values(handoffs) : [];
}

/**
 * Several calls can be monitoring at once. A download is worth submitting when
 * any of them could legitimately own it; the companion decides which one does.
 */
export function anyShouldObserveDownload(handoffs, downloadItem) {
  return handoffList(handoffs).some((one) => shouldObserveDownload(one, downloadItem));
}

/* Whether a saved file carries the name the companion generates for a call's
 * answer: an attempt id, optionally led by a project, ending in _outputs.zip.
 * Chrome's "(1)" suffix for a repeated name is still that name.
 *
 * A download that no bound call claims is normally none of this extension's
 * business. One with this name is the exception: it is some call's answer, and
 * after a browser restart no call is bound to anything.
 */
export function isOutputsArchiveName(path) {
  if (typeof path !== "string") {
    return false;
  }
  const name = path.split(/[\\/]/).at(-1);
  return /^(?:[a-z0-9][a-z0-9_-]{0,31}_)?ec_[0-9a-f]{12}_outputs(?: \(\d+\))?\.zip$/i.test(name);
}

export function handoffForTab(handoffs, tabId) {
  return handoffList(handoffs).find((one) => one?.tabId === tabId) ?? null;
}
