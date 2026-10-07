/* Calls that finished on their own and have not been moved to History yet.
 *
 * A call closes itself the moment its archive lands, which is usually while
 * nobody is looking at the panel. If it went straight into a folded History it
 * would change colour where no one could see it. So a finished call stays in
 * Running, coloured by how its delivery checked out, until the operator moves
 * it on.
 *
 * One key per call, never one list. Two calls can finish in the same moment
 * from two events, and a shared list is a read-modify-write that loses one of
 * them.
 */
export function createFinishedStore(storage, now = () => new Date().toISOString()) {
  const prefix = "externalcall:finished:";
  return {
    async read() {
      const all = await storage.get(null);
      return Object.fromEntries(Object.entries(all)
        .filter(([key]) => key.startsWith(prefix))
        .map(([key, value]) => [key.slice(prefix.length), value]));
    },
    async mark(exchangeId, status) {
      await storage.set({ [prefix + exchangeId]: { exchangeId, status, at: now() } });
    },
    async shelve(exchangeIds) {
      const keys = (Array.isArray(exchangeIds) ? exchangeIds : [])
        .filter((id) => typeof id === "string" && id)
        .map((id) => prefix + id);
      if (keys.length) await storage.remove(keys);
      return keys.length;
    },
  };
}
