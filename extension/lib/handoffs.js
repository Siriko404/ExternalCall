/* Per-call storage and generation checks for asynchronous browser callbacks. */
export function createHandoffStore(storage, makeGeneration = () => crypto.randomUUID()) {
  const prefix = "externalcall:handoff:";
  const pending = new Map();
  function serialize(id, action) {
    const previous = pending.get(id) ?? Promise.resolve();
    const next = previous.catch(() => undefined).then(action);
    pending.set(id, next);
    next.finally(() => {
      if (pending.get(id) === next) pending.delete(id);
    }).catch(() => undefined);
    return next;
  }
  return {
    async read() {
      const all = await storage.get(null);
      return Object.fromEntries(Object.entries(all)
        .filter(([key]) => key.startsWith(prefix))
        .map(([key, value]) => [key.slice(prefix.length), value]));
    },
    write(handoff, { create = false } = {}) {
      return serialize(handoff.exchangeId, async () => {
        const key = prefix + handoff.exchangeId;
        const existing = (await storage.get(key))[key];
        if (!create && (!existing || existing.generation !== handoff.generation)) return null;
        const value = create ? { ...handoff, generation: makeGeneration() } : handoff;
        await storage.set({ [key]: value });
        return value;
      });
    },
    drop(id) {
      return serialize(id, async () => {
        const key = prefix + id;
        const existing = (await storage.get(key))[key] ?? null;
        await storage.remove(key);
        return existing;
      });
    },
    async current(handoff) {
      const key = prefix + handoff.exchangeId;
      const existing = (await storage.get(key))[key];
      return Boolean(existing && existing.generation === handoff.generation);
    },
  };
}
