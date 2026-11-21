import { api } from "~/lib/api.server";
import type { Route } from "./+types/bill-pdf";

export async function loader({ request, params }: Route.LoaderArgs) {
  const res = await api<Response>(request, `/v1/me/bills/${params.cycle}/pdf`);
  return new Response(res.body, { headers: {
    "Content-Type": "application/pdf", "Cache-Control": "private, no-store",
    "Content-Disposition": `attachment; filename="bill-${params.cycle}.pdf"` } });
}
