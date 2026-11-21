import { api, ApiError } from "~/lib/api.server";
import type { Route } from "./+types/api.evidence";

export async function loader({ request, params }: Route.LoaderArgs) {
  try {
    return Response.json(await api(request, `/v1/me/bills/${encodeURIComponent(params.cycle)}/evidence`), { headers: { "Cache-Control": "private, no-store" } });
  } catch (e) {
    if (e instanceof ApiError) return Response.json({ detail: e.message }, { status: e.status });
    throw e;
  }
}
