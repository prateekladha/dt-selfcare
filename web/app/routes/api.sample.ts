import { api } from "~/lib/api.server";
import type { Route } from "./+types/api.sample";

export async function loader({ request }: Route.LoaderArgs) {
  const res = await api<Response>(request, "/v1/me/usage/sample.csv");
  return new Response(await res.text(), { headers: { "Content-Type": "text/csv", "Content-Disposition": 'attachment; filename="usage-sample.csv"' } });
}
