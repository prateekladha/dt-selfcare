import { type RouteConfig, index, layout, route } from "@react-router/dev/routes";

export default [
  route("login", "routes/login.tsx"),
  route("logout", "routes/logout.ts"),
  route("healthz", "routes/healthz.ts"),
  layout("routes/_app.tsx", [
    index("routes/home.tsx"),
    route("bills", "routes/bills.tsx"),
    route("bills/:cycle", "routes/bill.tsx"),
    route("insights", "routes/insights.tsx"),
    route("plan", "routes/plan.tsx"),
    route("assistant", "routes/assistant.tsx"),
    route("system", "routes/system.tsx"),
  ]),
  route("bills/:cycle/pdf", "routes/bill-pdf.ts"),
  route("api/pay", "routes/api.pay.ts"),
  route("api/pay/confirm", "routes/api.pay-confirm.ts"),
  route("api/events", "routes/api.events.ts"),
  route("api/sample.csv", "routes/api.sample.ts"),
  route("api/evidence/:cycle", "routes/api.evidence.ts"),
  route("api/ask", "routes/api.ask.ts"),
  route("api/ask/stream", "routes/api.ask-stream.ts"),
  route("api/orders", "routes/api.orders.ts"),
  route("api/chats", "routes/api.chats.ts"),
] satisfies RouteConfig;
