import { api, ApiError } from "~/lib/api.server";
import type { Route } from "./+types/api.chats";

// GET /api/chats            -> chat history (titles)
// GET /api/chats?id=..      -> one chat's messages
// POST /api/chats?id=..&op=delete
export async function loader({ request }: Route.LoaderArgs) {
  const id = new URL(request.url).searchParams.get("id");
  try {
    return Response.json(await api(request, id ? `/v1/me/assistant/sessions/${encodeURIComponent(id)}` : "/v1/me/assistant/sessions"),
                         { headers: { "Cache-Control": "private, no-store" } });
  } catch (e) {
    if (e instanceof ApiError) return Response.json({ detail: e.message }, { status: e.status });
    throw e;
  }
}

export async function action({ request }: Route.ActionArgs) {
  const id = new URL(request.url).searchParams.get("id");
  if (!id) return Response.json({ detail: "missing id" }, { status: 400 });
  try {
    await api(request, `/v1/me/assistant/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
    return Response.json({ ok: true });
  } catch (e) {
    if (e instanceof ApiError) return Response.json({ detail: e.message }, { status: e.status });
    throw e;
  }
}
