import { redirect } from "react-router";
import type { Route } from "./+types/assistant";

/** The assistant is a panel on every page now; old links (/assistant, /assistant?c=<id>) open it on the overview. */
export function loader({ request }: Route.LoaderArgs) {
  const c = new URL(request.url).searchParams.get("c");
  return redirect(`/?assistant=${encodeURIComponent(c && /^[A-Za-z0-9_-]{1,64}$/.test(c) ? c : "new")}`);
}
