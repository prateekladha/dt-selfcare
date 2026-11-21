import { createCookieSessionStorage, redirect } from "react-router";

const secret = process.env.SESSION_SECRET;
if (!secret && process.env.NODE_ENV === "production") {
  throw new Error("SESSION_SECRET must be set");
}

// The core-api JWT lives only in an httpOnly cookie: never readable by page JavaScript.
export const sessions = createCookieSessionStorage<{ token: string; sub: string }>({
  cookie: {
    name: "__session",
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    secrets: [secret ?? "dev-only-secret"],
    maxAge: 60 * 60,
  },
});

export async function requireToken(request: Request): Promise<string> {
  const session = await sessions.getSession(request.headers.get("Cookie"));
  const token = session.get("token");
  if (!token) throw redirect("/login");
  return token;
}
