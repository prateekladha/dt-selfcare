# Monday walkthrough: Usage & Bill Explainer

**Live:** https://130.210.124.37.sslip.io · **Code:** github.com/prateekladha/dt-selfcare
**Login:** demo lines on the sign-in page; the shared password is in OCI Vault (`dt-selfcare-demo-password`).

---

## 1. Opening (1 min)
"I picked the Bill Explainer and built it as a production system, not a demo. Behind the bill
there's a real-time usage pipeline (the same design as a data-usage alert system), payments with a
real gateway, and an AI layer that isn't tied to one vendor. Everything runs on OCI, is deployed
by CI/CD, and costs about $1.5 a day."

## 2. Demo script (8–10 min)
| # | Show | Say |
|---|---|---|
| 1 | Sign in as **Jonas (+4915100000002)** | "Six demo customers, two countries. DE bills in EUR at 19% VAT, PL in PLN at 23%. Same code, per-country config." |
| 2 | Overview: data meter, **Unbilled so far**, **Bill pending** | "Every number on this page comes from one snapshot call, rendered on the server and stamped 'as of'. No widget shows data from a different moment. The unbilled amount is priced live from the stream totals by the same rating engine that produces the invoice." |
| 3 | **12-month trend** (hover, the outlined unpaid bar) | "Bills are immutable once issued. The outlined bar is unpaid." |
| 4 | **Why your bill increased** + **Top usage drivers** | "This part is deterministic: drivers ranked by money impact. October jumped because of Zone 2 roaming (calls and data in the US/CH/UK zone). EU roaming isn't charged, under EU roam-like-at-home rules." |
| 5 | **Explained in plain words** → switch EN / DE / PL | "Only this panel is AI, and it streams in after the page loads. The model gets the computed facts only, with no name or phone number. **Every number in its answer is checked against the facts**; one invented number and we discard it and show the template. A circuit breaker, a cache and a per-user daily budget control failures and cost." |
| 6 | **Download PDF** | "Generated from the same bill record. On bill day you'd pre-generate these to Object Storage behind a CDN." |
| 7 | **Pay bill** → Razorpay checkout → UPI `success@razorpay` | "One idempotency key per tap, so a double tap reuses the same order. We don't trust the browser's success callback: we verify the signature *and* ask Razorpay server-side whether the order was captured. The webhook and the browser can both report the payment; settlement happens once (conditional update), and the bill balance plus the outbox event are written in one transaction. Razorpay's UPI rail is INR-only, so a per-country flag charges the INR equivalent and records the rate. In Europe that flag points at Adyen or Stripe and charges EUR natively." |
| 8 | **Try your usage** → "2 weeks in the US", then upload `usage-sample.csv` | "This is the 'upload or select usage' part of the brief. It's priced by the same engine and compared with the last real bill, and it never touches real usage, so a customer can't change their own invoice this way." |
| 9 | Sign in as **Lena (+4915100000001)** | "Streamer: data grows every month. She crossed 50 and 80% this cycle. The alert came from the stream within seconds, exactly once, pushed to the open app over server-sent events." |
| 10 | GitHub Actions run + `docs/LOADTEST.md` | "Each push: tests → container images → Helm → in-cluster smoke test of every flow above → public check. Load test: 14.7k records/s sustained on one 1-OCPU node, no retries or duplicates." |

## 3. Architecture in 60 seconds
Network → **collector** (batch idempotency, partition by subscriber, gzip) → **OCI Streaming**
(Kafka API) → **aggregator** (one atomic Valkey Lua script per message: offset check, message
dedupe, totals, thresholds, alert outbox) → **relay** → alerts topic → **notifier** → app.
Customer side: **web** (server-rendered) → **core-api** (modular monolith: billing, payments,
insights, AI) → Autonomous DB. Raw usage goes to Object Storage for audit and reconciliation.

## 4. Questions they'll probably ask, with answers
**"Kafka is at-least-once. How do you avoid double counting and double alerts?"**
The offset is stored next to the state and updated in the same atomic script, so a crash replay
does nothing. Each message has a deterministic id, so a re-published batch does nothing. Each
threshold uses `HSETNX` per cycle, so an alert fires once. The alert is written to an outbox in the
same script, so state and alert can't diverge. Downstream dedupes on `alert_id`. This works on any
broker that delivers at least once, so it doesn't depend on Kafka transactions (which OCI
Streaming doesn't offer).

**"Why not store every record and run SUM queries?"**
60k writes/s plus SUMs over billions of rows on every app open is slow and expensive. Running
totals per subscriber per cycle are O(1) to read. Raw records go to cheap storage for audit and
batch reconciliation.

**"How does this scale to 50M subscribers and 150k events/s?"**
Along the partition axis. The collector is stateless; each aggregator owns a disjoint set of
partitions; Valkey keys are hash-tagged per partition, so it shards cleanly. Compression keeps us
under OCI's 1 MB/s per partition: 30 MB/s raw → ~6 MB/s → 12–16 partitions. Measured: ~0.7 cores
per 15k/s in the aggregator, so ~10–12 pods at peak. OCI partition counts are fixed at creation, so
we size for peak on day one.

**"Hot partitions?"**
We hash by subscriber, never by cell tower or region. One heavy user is still only one key's
worth of events.

**"Late or out-of-order records? A record arriving after the bill was issued?"**
Totals are sums, so order doesn't matter. Cycle assignment uses event time, not arrival time.
Issued bills are immutable, so late usage stays in the cycle counters and becomes an adjustment
on the next bill via reconciliation against the raw archive.

**"Customer buys a 5 GB add-on mid-cycle?"**
A `PLAN_CHANGED` event goes into the same subscriber partition, so it's ordered with their usage.
The allowance grows, and alert levels they're now below are re-armed.

**"Valkey dies?"**
The append-only file is synced every second to a block volume. The worst case (losing the last
second) is fixed by replaying from the stream offsets, and full rebuilds come from the raw archive.
`noeviction` because silently losing a running total would be a billing bug.

**"Why server-side rendering? Why not a single-page app?"**
To avoid showing inconsistent or empty states during the first paint. The real consistency
guarantee is the single snapshot plus `Cache-Control: private, no-store` (personal data never goes
on a CDN, which is how users end up seeing each other's data) plus version numbers, so the client
never replaces newer data with older data.

**"Why DeepSeek? GDPR?"**
The AI layer is provider-agnostic, chosen per country by a feature flag. The demo uses the
DeepSeek API (cheap, strong, synthetic data, minimal facts only). For EU production the flag
routes to Mistral (EU-hosted) or to open weights (DeepSeek or Mistral) on vLLM in an EU OCI
region, so customer data never leaves the EU. The guardrails are the same whichever model answers.

**"What if the LLM hallucinates a number?"**
Every number in the answer has to appear in the facts we sent. If any doesn't, the answer is
discarded and the template is shown. This is enforced in code and covered by a test.

**"Payment gateway down?"**
The circuit breaker opens and we fail fast with "try again shortly". There's no double charge on
retry thanks to the idempotency key. A per-country flag can switch gateways, and payments can be
switched off with a kill-switch flag.

**"Why a modular monolith and not microservices?"**
The hot path is already separate because it scales differently. The customer domains share one
deployable with strict module boundaries until team topology or load justifies splitting them.
That avoids paying distributed-systems costs before they buy anything.

**"How do you add a new country?"**
Catalogue, VAT, currency and locale config, plus flag targeting by `country`. In DT's real
landscape there would also be a country adapter to the local billing/CRM system behind the
standard TM Forum APIs. No code fork.

**"Security?"**
JWT in an httpOnly cookie, never readable by page JS. Every route is scoped to the token's
subscriber (no subscriber id in URLs, so no IDOR). Argon2id passwords, rate-limited login.
Secrets live in OCI Vault and are synced by node instance principal (no static cloud credentials
in the cluster). The DB app user can only touch its own schema. Pods run non-root with read-only
root filesystems. Only the web app and the payment webhook are public; the collector, Valkey and
flagd are internal.

## 5. What I'd do next (shows judgement)
1. KEDA autoscaling on consumer lag; a dedicated node pool for the hot path.
2. A chaos test that kills an aggregator mid-batch and verifies totals and alert counts against
   the raw archive, to measure the exactly-once claim.
3. A daily reconciliation job: real-time totals vs. raw archive vs. issued bills, with alerting
   on drift.
4. OIDC with DT's identity provider instead of local login; refresh tokens.
5. Multi-region cells (one per EU region) with data residency; country adapters to legacy
   billing behind TMF APIs.
6. Observability: OpenTelemetry traces end to end, SLOs (alert latency p95, home p95, payment
   success rate).

## 6. EM angle: teams, MVP, metrics
- **Teams:** Data Platform (stream, aggregator, archive); Billing & Payments; Customer Experience
  (web/app, insights, AI); Platform/SRE (OKE, CI/CD, observability).
- **MVP order:** bill view + PDF → unbilled + pay → explainer (deterministic) → AI narrative behind
  a flag → real-time alerts.
- **Measure:** % of "why is my bill high" support calls deflected, alert p95 latency, duplicate
  alert rate (target 0), payment success rate, AI fallback rate, cost per 1k explanations.

## 7. Facts worth having to hand
- **Built in one day.** 33 automated tests, plus a 15-check smoke test on every deploy.
- **Cost:** ~$1.5/day. Everything is Always Free except the worker node.
- **Where things live:**
  - IaC: `deploy/terraform`
  - Helm chart: `deploy/helm/selfcare`
  - CI/CD: `.github/workflows/ci.yml`
- **Honest limits:**
  - The network feed is emulated.
  - The cluster has a single node.
  - The demo uses a local login rather than DT's identity provider.
  - The demo uses Razorpay in INR, where production in Europe would settle in EUR.
