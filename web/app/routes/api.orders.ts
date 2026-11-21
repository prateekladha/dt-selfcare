import { api, ApiError } from "~/lib/api.server";
import type { Route } from "./+types/api.orders";

// POST /api/orders            {addon_code}          -> propose (no change until approved)
// POST /api/orders?id=..&op=approve|decline        -> approve starts the durable workflow
// GET  /api/orders?id=..                           -> status + workflow stage
export async function action({ request }: Route.ActionArgs) {
  const url = new URL(request.url);
  const id = url.searchParams.get("id");
  const op = url.searchParams.get("op");
  try {
    if (id && (op === "approve" || op === "decline")) {
      return Response.json(await api(request, `/v1/me/orders/${encodeURIComponent(id)}/${op}`, { method: "POST" }));
    }
    const { addon_code } = await request.json();
    return Response.json(await api(request, "/v1/me/orders/addon", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ addon_code }) }));
  } catch (e) {
    if (e instanceof ApiError) return Response.json({ detail: e.message }, { status: e.status });
    throw e;
  }
}

export async function loader({ request }: Route.LoaderArgs) {
  const id = new URL(request.url).searchParams.get("id");
  try {
    return Response.json(await api(request, id ? `/v1/me/orders/${encodeURIComponent(id)}` : "/v1/me/orders"),
                         { headers: { "Cache-Control": "private, no-store" } });
  } catch (e) {
    if (e instanceof ApiError) return Response.json({ detail: e.message }, { status: e.status });
    throw e;
  }
}
