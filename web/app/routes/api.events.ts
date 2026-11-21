import { rawCore } from "~/lib/api.server";
import { freshToken } from "~/lib/session.server";
import type { Route } from "./+types/api.events";

// Proxies the core-api SSE stream so the browser never holds the API token.
export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await freshToken(request);
  const upstream = await rawCore("/v1/me/events", { headers: { Authorization: `Bearer ${token}` }, signal: request.signal });
  return new Response(upstream.body, { headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-store", "X-Accel-Buffering": "no" } });
}
