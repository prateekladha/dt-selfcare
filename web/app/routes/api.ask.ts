import { api, ApiError } from "~/lib/api.server";
import type { Route } from "./+types/api.ask";

export async function action({ request }: Route.ActionArgs) {
  const { question, lang } = await request.json();
  try {
    const res = await api(request, "/v1/me/assistant/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, lang }), signal: AbortSignal.timeout(90_000),
    });
    return Response.json(res, { headers: { "Cache-Control": "private, no-store" } });
  } catch (e) {
    if (e instanceof ApiError) return Response.json({ detail: e.message }, { status: e.status });
    throw e;
  }
}
