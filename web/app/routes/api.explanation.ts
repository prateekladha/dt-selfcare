import { api, ApiError, PRIVATE } from "~/lib/api.server";
import type { Route } from "./+types/api.explanation";

/** The bill explanation in another language, for the language tabs (only that section reloads). */
export async function loader({ request, params }: Route.LoaderArgs) {
  const cycle = params.cycle;
  const lang = new URL(request.url).searchParams.get("lang") ?? "en";
  if (!/^(\d{4}-(0[1-9]|1[0-2])|current)$/.test(cycle) || !["en", "de", "pl"].includes(lang)) {
    return Response.json({ detail: "not found" }, { status: 404 });   // never forwarded raw into an API path
  }
  try {
    return Response.json(await api(request, `/v1/me/bills/${cycle}/explanation?lang=${lang}`), { headers: PRIVATE });
  } catch (e) {
    if (e instanceof ApiError) return Response.json({ detail: e.message }, { status: e.status });
    throw e;
  }
}
