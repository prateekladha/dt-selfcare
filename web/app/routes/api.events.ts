import { rawCore } from "~/lib/api.server";
import { freshToken } from "~/lib/session.server";
import type { Route } from "./+types/api.events";

// Proxies the core-api SSE stream so the browser never holds the API token.
export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await freshToken(request);
  let upstream: Response;
  try {
    upstream = await rawCore("/v1/me/events", { headers: { Authorization: `Bearer ${token}` }, signal: request.signal });
  } catch {
    return new Response(null, { status: 503 });   // breaker open: the client reconnects with backoff
  }
  if (!upstream.ok || !upstream.body) return new Response(null, { status: upstream.status || 502 });   // not a fake 200 stream
  return new Response(upstream.body, { headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-store", "X-Accel-Buffering": "no" } });
}
