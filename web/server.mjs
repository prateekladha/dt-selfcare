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
  next();
});
app.get("/version", (_, res) => res.set("Cache-Control", "no-store").json({ version: VERSION }));
app.use("/assets", express.static("build/client/assets", { immutable: true, maxAge: "1y" }));
app.use(express.static("build/client", { maxAge: "1h", index: false }));
app.use(morgan(":method :url :status :response-time ms", { skip: (req) => req.url === "/healthz" || req.url === "/version" }));
app.all("/{*splat}", createRequestHandler({ build }));

const port = Number(process.env.PORT ?? 3000);
const server = app.listen(port, () => console.log(`web listening on :${port}`));
process.on("SIGTERM", () => server.close(() => process.exit(0)));
