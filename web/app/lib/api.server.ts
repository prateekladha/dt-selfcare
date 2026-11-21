import { redirect } from "react-router";
import { freshToken, sessions } from "./session.server";

const CORE = process.env.CORE_API_URL ?? "http://localhost:8080";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** Server-side call to core-api with the caller's token. 401 -> back to login. */
export async function api<T = unknown>(request: Request, path: string, init: RequestInit = {}): Promise<T> {
  const call = (token: string) => fetch(`${CORE}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      "x-request-id": request.headers.get("x-request-id") ?? crypto.randomUUID(),
      ...(init.headers ?? {}),
    },
    signal: init.signal ?? AbortSignal.timeout(15_000),
  });
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
  return fetch(`${CORE}${path}`, { ...init, signal: init.signal ?? AbortSignal.timeout(15_000) });
}

export const PRIVATE = { "Cache-Control": "private, no-store" };
