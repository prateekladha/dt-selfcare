import { api, ApiError } from "~/lib/api.server";
import type { Route } from "./+types/api.pay";

export async function action({ request }: Route.ActionArgs) {
  const { purpose, bill_id, idempotency_key } = await request.json();
  try {
    const p = await api(request, "/v1/me/payments", { method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": String(idempotency_key) },
      body: JSON.stringify({ purpose, bill_id }) });
    return Response.json(p);
  } catch (e) {
    if (e instanceof ApiError) return Response.json({ detail: e.message }, { status: e.status });
    throw e;
  }
}
