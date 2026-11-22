// Production HTTP server for the SSR web app.
// TLS terminates at the ingress (Traefik); `trust proxy` makes Express honour X-Forwarded-Proto/Host
// so request URLs are https://<host> - required for React Router's CSRF origin check on form
// actions, secure cookies and absolute redirects.
import { createRequestHandler } from "@react-router/express";
import compression from "compression";
import express from "express";
import morgan from "morgan";

// Build version = React Router's manifest hash (the browser has the same value in
// window.__reactRouterManifest.version). Every response carries it, so an open tab notices a new
// deployment on its next request and loads the new version on the next click (VersionWatcher.tsx).
const build = await import("./build/server/index.js");
const VERSION = build.assets.version;

const app = express();
app.set("trust proxy", true);
app.disable("x-powered-by");
app.use(compression());
app.use((_, res, next) => {
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.setHeader("Referrer-Policy", "strict-origin-when-cross-origin");
  res.setHeader("X-Frame-Options", "DENY");
  res.setHeader("Strict-Transport-Security", "max-age=31536000");
  res.setHeader("X-App-Version", VERSION);
  // no script-src yet (SSR inline hydration scripts would need nonces); these directives can't break anything
  res.setHeader("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'self'; object-src 'none'");
  next();
});
// CSRF guard for state-changing JSON/resource routes. React Router checks Origin only for its own
// actions, not resource routes, and SameSite=Lax alone trusts every "same-site" host (any *.sslip.io).
// A browser always sends Sec-Fetch-Site and/or Origin on a POST; a cross-site one is refused.
app.use((req, res, next) => {
  if (req.method === "GET" || req.method === "HEAD" || !(req.path.startsWith("/api/") || req.path === "/logout")) return next();
  const site = req.get("sec-fetch-site");
  const origin = req.get("origin");
  const host = req.get("x-forwarded-host") ?? req.get("host");
  let crossOrigin = site !== undefined && site !== "same-origin";
  if (!crossOrigin && origin) { try { crossOrigin = new URL(origin).host !== host; } catch { crossOrigin = true; } }
  if (crossOrigin) return res.status(403).json({ detail: "cross-site request refused" });
  next();
});
app.get("/version", (_, res) => res.set("Cache-Control", "no-store").json({ version: VERSION }));
app.use("/assets", express.static("build/client/assets", { immutable: true, maxAge: "1y" }));
app.use(express.static("build/client", { maxAge: "1h", index: false }));
app.use(morgan(":method :url :status :response-time ms", { skip: (req) => req.url === "/healthz" || req.url === "/version" }));
app.all("/{*splat}", createRequestHandler({ build }));

// Safety net: a stray rejected promise must never take down a pod that serves everyone. Log it
// (it is a bug to fix) and keep serving. Streamed loader promises are marked handled at the source
// (streamed() in app/lib/api.server.ts).
process.on("unhandledRejection", (e) => console.error(JSON.stringify({ event: "unhandled_rejection", error: String(e) })));

const port = Number(process.env.PORT ?? 3000);
const server = app.listen(port, () => console.log(`web listening on :${port}`));
process.on("SIGTERM", () => server.close(() => process.exit(0)));
