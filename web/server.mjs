// Production HTTP server for the SSR web app.
// TLS terminates at the ingress (Traefik); `trust proxy` makes Express honour X-Forwarded-Proto/Host
// so request URLs are https://<host> - required for React Router's CSRF origin check on form
// actions, secure cookies and absolute redirects.
import { createRequestHandler } from "@react-router/express";
import compression from "compression";
import express from "express";
import morgan from "morgan";

const app = express();
app.set("trust proxy", true);
app.disable("x-powered-by");
app.use(compression());
app.use((_, res, next) => {
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.setHeader("Referrer-Policy", "strict-origin-when-cross-origin");
  res.setHeader("X-Frame-Options", "DENY");
  res.setHeader("Strict-Transport-Security", "max-age=31536000");
  next();
});
app.use("/assets", express.static("build/client/assets", { immutable: true, maxAge: "1y" }));
app.use(express.static("build/client", { maxAge: "1h", index: false }));
app.use(morgan(":method :url :status :response-time ms", { skip: (req) => req.url === "/healthz" }));
app.all("/{*splat}", createRequestHandler({ build: await import("./build/server/index.js") }));

const port = Number(process.env.PORT ?? 3000);
const server = app.listen(port, () => console.log(`web listening on :${port}`));
process.on("SIGTERM", () => server.close(() => process.exit(0)));
