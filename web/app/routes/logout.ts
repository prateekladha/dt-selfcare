import { redirect } from "react-router";
import { sessions } from "~/lib/session.server";
import type { Route } from "./+types/logout";

export async function action({ request }: Route.ActionArgs) {
  const session = await sessions.getSession(request.headers.get("Cookie"));
  return redirect("/login", { headers: { "Set-Cookie": await sessions.destroySession(session) } });
}
export const loader = action;
