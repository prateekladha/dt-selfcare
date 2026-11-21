import { redirect } from "react-router";
import { freshToken, sessions } from "./session.server";

const CORE = process.env.CORE_API_URL ?? "http://localhost:8080";
const PAYMENTS = process.env.PAYMENTS_URL ?? CORE;
const ASSISTANT = process.env.ASSISTANT_URL ?? CORE;

/** Each path goes to the service that owns it (payments-service, assistant-service, core-api). */
export function baseFor(path: string) {
  if (path.startsWith("/v1/me/payments") || path.startsWith("/v1/webhooks")) return PAYMENTS;
  if (path.startsWith("/v1/me/assistant")) return ASSISTANT;
  return CORE;
}

/** Per-service circuit breaker (BFF -> service). After 5 consecutive failures (timeouts, connection
 *  errors, 5xx) the circuit opens for 20 s: calls fail fast with 503 instead of tying up SSR workers on
 *  a struggling service. Then one trial request (half-open) decides whether it closes again. */
type Breaker = { failures: number; openedAt: number; halfOpen: boolean };
const breakers = new Map<string, Breaker>();
const FAIL_THRESHOLD = 5, RESET_MS = 20_000;
function breakerFor(base: string): Breaker {
  let b = breakers.get(base);
  if (!b) { b = { failures: 0, openedAt: 0, halfOpen: false }; breakers.set(base, b); }
  return b;
}
function admit(b: Breaker): boolean {
  if (b.failures < FAIL_THRESHOLD) return true;
  if (Date.now() - b.openedAt >= RESET_MS && !b.halfOpen) { b.halfOpen = true; return true; }
  return false;
}
function record(b: Breaker, ok: boolean) {
  if (ok) { b.failures = 0; b.halfOpen = false; return; }
  b.failures += 1; b.halfOpen = false;
  if (b.failures >= FAIL_THRESHOLD) b.openedAt = Date.now();
}
export function breakerStates() {
  return Object.fromEntries([...breakers].map(([k, b]) => [k, b.failures >= FAIL_THRESHOLD ? "OPEN" : "CLOSED"]));
}

/** Read budget per call; the assistant stream and LLM explanation get more (they wait for a model). */
function timeoutFor(path: string) {
  if (path.startsWith("/v1/me/assistant")) return 120_000;
  if (path.includes("/explanation")) return 30_000;
  return 5_000;
}

/** fetch with a timeout and the service's circuit breaker. */
async function guardedFetch(url: string, base: string, init: RequestInit, path: string): Promise<Response> {
  const b = breakerFor(base);
  if (!admit(b)) throw new ApiError(503, "This part of the service is temporarily unavailable. Please try again shortly.");
  try {
    const res = await fetch(url, { ...init, signal: init.signal ?? AbortSignal.timeout(timeoutFor(path)) });
    record(b, res.status < 500);
    return res;
  } catch (e) {
    record(b, false);
    throw new ApiError(504, "The service took too long to respond. Please try again.");
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** Server-side call to core-api with the caller's token. 401 -> back to login. */
export async function api<T = unknown>(request: Request, path: string, init: RequestInit = {}): Promise<T> {
  const base = baseFor(path);
  const call = (token: string) => guardedFetch(`${base}${path}`, base, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      "x-request-id": request.headers.get("x-request-id") ?? crypto.randomUUID(),
      ...(init.headers ?? {}),
    },
  }, path);
  let res = await call((await freshToken(request)).token);
  if (res.status === 401) res = await call((await freshToken(request, true)).token);  // expired: refresh once
  if (res.status === 401) {
    const session = await sessions.getSession(request.headers.get("Cookie"));
    throw redirect("/login", { headers: { "Set-Cookie": await sessions.destroySession(session) } });
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {}
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return (res.headers.get("content-type") ?? "").includes("json") ? ((await res.json()) as T) : (res as T);
}

export async function rawCore(path: string, init: RequestInit) {
  const base = baseFor(path);
  return guardedFetch(`${base}${path}`, base, init, path);
}

export const PRIVATE = { "Cache-Control": "private, no-store" };
