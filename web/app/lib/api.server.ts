import { redirect } from "react-router";
import CircuitBreaker from "opossum";
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

/** One opossum circuit breaker per downstream service (BFF -> core-api / payments / assistant).
 *  - each call has a timeout (5 s; 30 s for the LLM explanation; 120 s for the assistant stream);
 *  - failures = timeouts, connection errors, 5xx (4xx is the caller's problem and doesn't count);
 *  - the circuit OPENS when >= 50% of at least 5 calls in a 10 s window fail; while open, calls fail
 *    fast (no request leaves the BFF) and the page falls back (stale snapshot / degraded UI);
 *  - after 20 s one trial call is let through (HALF_OPEN): success closes it, failure re-opens it. */
class UpstreamError extends Error {
  constructor(public res: Response) { super(`upstream ${res.status}`); }
}
type Call = { url: string; init: RequestInit; path: string };
const breakers = new Map<string, CircuitBreaker<[Call], Response>>();
const SERVICE_NAMES: Record<string, string> = {};
SERVICE_NAMES[CORE] = "core"; SERVICE_NAMES[PAYMENTS] = PAYMENTS === CORE ? "core" : "payments";
SERVICE_NAMES[ASSISTANT] = ASSISTANT === CORE ? "core" : "assistant";

function breakerFor(base: string) {
  let b = breakers.get(base);
  if (!b) {
    b = new CircuitBreaker(async ({ url, init, path }: Call) => {
      const res = await fetch(url, { ...init, signal: init.signal ?? AbortSignal.timeout(timeoutFor(path)) });
      if (res.status >= 500) throw new UpstreamError(res);   // counts as a failure
      return res;
    }, { timeout: false, errorThresholdPercentage: 50, volumeThreshold: 5, rollingCountTimeout: 10_000, resetTimeout: 20_000,
         name: SERVICE_NAMES[base] ?? base });
    const name = SERVICE_NAMES[base] ?? base;
    b.on("open", () => console.warn(JSON.stringify({ event: "circuit_open", service: name })));
    b.on("halfOpen", () => console.warn(JSON.stringify({ event: "circuit_half_open", service: name })));
    b.on("close", () => console.warn(JSON.stringify({ event: "circuit_closed", service: name })));
    breakers.set(base, b);
  }
  return b;
}

/** Services whose circuit is OPEN right now -> the UI degrades those features up front.
 *  Half-open is NOT degraded: the feature is shown again and the customer's next action is the trial call
 *  (otherwise a breaker on a service the page never calls by itself would never close). */
export function degradedServices(): string[] {
  return [...breakers].filter(([, b]) => b.opened).map(([base]) => SERVICE_NAMES[base] ?? base);
}

/** Read budget per call; the assistant stream and LLM explanation get more (they wait for a model). */
function timeoutFor(path: string) {
  if (path.startsWith("/v1/me/assistant")) return 120_000;
  if (path.includes("/explanation")) return 30_000;
  return 5_000;
}

/** fetch through the service's circuit breaker; returns the upstream response (incl. 5xx) or throws ApiError. */
async function guardedFetch(url: string, base: string, init: RequestInit, path: string): Promise<Response> {
  try {
    return await breakerFor(base).fire({ url, init, path });
  } catch (e) {
    if (e instanceof UpstreamError) return e.res;
    if ((e as { code?: string })?.code === "EOPENBREAKER")
      throw new ApiError(503, "This part of the service is temporarily unavailable. Please try again shortly.");
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
