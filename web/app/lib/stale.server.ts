/** Last good home snapshot per customer, kept in the BFF's memory (per pod, LRU, max 1 h old).
 *  Used ONLY as a fallback when core-api is unavailable (circuit open / timeout / 5xx), so the customer
 *  still sees their account - clearly marked with the time it is from - instead of an error page. */
const MAX = 5_000, TTL_MS = 60 * 60 * 1000;
const store = new Map<string, { at: number; value: unknown }>();

export function subscriberOf(token: string): string | null {
  try {
    return JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString()).sub ?? null;
  } catch {
    return null;
  }
}

export function remember(sub: string | null, value: unknown) {
  if (!sub) return;
  store.delete(sub);
  store.set(sub, { at: Date.now(), value });
  if (store.size > MAX) store.delete(store.keys().next().value as string);  // evict least recently stored
}

export function recall<T>(sub: string | null): T | null {
  const hit = sub ? store.get(sub) : undefined;
  if (!hit || Date.now() - hit.at > TTL_MS) return null;
  return hit.value as T;
}
