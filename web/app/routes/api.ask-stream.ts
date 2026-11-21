import { rawCore } from "~/lib/api.server";
import { freshToken } from "~/lib/session.server";
import type { Route } from "./+types/api.ask-stream";

// Streams the agent's Server-Sent Events from core-api to the browser (token never leaves the server).
export async function action({ request }: Route.ActionArgs) {
  const { token } = await freshToken(request);
  const { question, lang } = await request.json();
  const upstream = await rawCore("/v1/me/assistant/stream", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ question, lang }),
    signal: AbortSignal.timeout(120_000),
  });
  if (!upstream.ok || !upstream.body) {
    let detail = "The assistant is unavailable right now.";
    try { detail = (await upstream.json()).detail ?? detail; } catch {}
    return Response.json({ detail }, { status: upstream.status || 502 });
  }
  return new Response(upstream.body, { headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-store", "X-Accel-Buffering": "no" } });
}
