import { redirect } from "react-router";
import { sessions } from "~/lib/session.server";
import type { Route } from "./+types/logout";

export async function action({ request }: Route.ActionArgs) {
  const session = await sessions.getSession(request.headers.get("Cookie"));
  const refresh = session.get("refresh");
  if (refresh) {  // revoke server-side so the refresh token can't be reused
    await fetch(`${process.env.CORE_API_URL ?? "http://localhost:8080"}/v1/auth/logout`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ refresh_token: refresh }),
    }).catch(() => null);
  }
  return redirect("/login", { headers: { "Set-Cookie": await sessions.destroySession(session) } });
}
// GET /logout just goes to the login page: a link or <img> on another site can't sign the customer out
export const loader = () => redirect("/");
