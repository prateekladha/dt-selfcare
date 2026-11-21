import { api, ApiError } from "~/lib/api.server";
import type { Route } from "./+types/api.orders";

// POST /api/orders            {addon_code}|{plan_code} -> propose (no change until approved)
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
    const body = await request.json();
    const [path, payload] = body.plan_code ? ["/v1/me/orders/plan", { plan_code: body.plan_code }]
                                           : ["/v1/me/orders/addon", { addon_code: body.addon_code }];
    return Response.json(await api(request, path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }));
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
