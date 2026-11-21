import { api, ApiError } from "~/lib/api.server";
import type { Route } from "./+types/api.pay-confirm";

export async function action({ request }: Route.ActionArgs) {
  const { payment_id, gateway_payment_id, signature } = await request.json();
  try {
    const p = await api(request, `/v1/me/payments/${encodeURIComponent(payment_id)}/confirm`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ gateway_payment_id, signature }) });
    return Response.json(p);
  } catch (e) {
    if (e instanceof ApiError) return Response.json({ detail: e.message }, { status: e.status });
    throw e;
  }
}
