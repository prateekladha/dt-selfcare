import { createCookieSessionStorage, redirect } from "react-router";

const secret = process.env.SESSION_SECRET;
if (!secret && process.env.NODE_ENV === "production") {
  throw new Error("SESSION_SECRET must be set");
}
const CORE = process.env.CORE_API_URL ?? "http://localhost:8080";

// Access token (short-lived JWT) + refresh token (rotating, revocable) live only in an httpOnly cookie.
export const sessions = createCookieSessionStorage<{ token: string; refresh: string; sub: string }>({
  cookie: {
    name: "__session",
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    secrets: [secret ?? "dev-only-secret"],
    maxAge: 30 * 24 * 3600,  // matches the refresh-token lifetime: users stay signed in across deploys
  },
});

/** Rotated session cookies per request. A refresh rotates the refresh token (the old one dies after a 30 s
 *  grace), so EVERY response that refreshed must send the new cookie back, not just page loads; the root
 *  middleware (root.tsx) attaches it to resource-route responses too (API, PDF, SSE, orders...). */
const rotated = new WeakMap<Request, string>();
export const rotatedCookie = (request: Request) => rotated.get(request);

function expiresSoon(jwt: string, skewS = 120): boolean {
  try {
    const payload = JSON.parse(Buffer.from(jwt.split(".")[1], "base64url").toString());
    return payload.exp * 1000 - Date.now() < skewS * 1000;
  } catch {
    return true;
  }
}

async function logoutRedirect(session: Awaited<ReturnType<typeof sessions.getSession>>) {
  return redirect("/login", { headers: { "Set-Cookie": await sessions.destroySession(session) } });
}

/** A valid access token, refreshed transparently when it is about to expire (or `force`).
 * `setCookie` is returned when the session changed and must be sent back to the browser. */
export async function freshToken(request: Request, force = false): Promise<{ token: string; setCookie?: string }> {
  const session = await sessions.getSession(request.headers.get("Cookie"));
  const token = session.get("token");
  const refresh = session.get("refresh");
  if (!token) throw redirect("/login");
  if (!force && !expiresSoon(token)) return { token };
  if (!refresh) throw await logoutRedirect(session);
  const res = await fetch(`${CORE}/v1/auth/refresh`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refresh }), signal: AbortSignal.timeout(10_000),
  }).catch(() => null);
  if (!res) return { token };  // core-api briefly unreachable (e.g. mid-deploy): keep the current token
  if (!res.ok) throw await logoutRedirect(session);
  const pair = await res.json();
  session.set("token", pair.access_token);
  session.set("refresh", pair.refresh_token);
  const setCookie = await sessions.commitSession(session);
  rotated.set(request, setCookie);
  return { token: pair.access_token, setCookie };
}

export async function requireToken(request: Request): Promise<string> {
  return (await freshToken(request)).token;
}
