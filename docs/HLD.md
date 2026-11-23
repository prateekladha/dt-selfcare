# High-Level Design: Telecom Self-Care (Usage & Bill Explainer)

> Companion docs: [LLD.md](LLD.md) (APIs, schemas, algorithms) · [OPERATIONS.md](OPERATIONS.md) (how to look inside DB, Valkey, streams) · [LOADTEST.md](LOADTEST.md)

## 1. Context and goals

A multi-country mobile operator (DT-like: ~50M subscribers in 10 European countries) wants a self-care experience where a customer can:

- see their **bill trend** over 12 months, view and **download** any bill
- see the **unbilled amount** for the running month and **pay** (bill or advance), through a real payment gateway
- understand **why the bill changed** and the **top usage drivers**, also in plain language (AI)
- **upload or select usage** to see what it would cost
- get **real-time data alerts** (50/80/100% of allowance), exactly once

The bill is the product. Behind it sits a **real-time usage pipeline** that turns network usage records (CDRs) into running totals per subscriber and billing cycle.

### Non-functional requirements and capacity math

| Requirement | Target | Derivation / consequence |
|---|---|---|
| Subscribers | 50M across 10 countries | Country is a field on every record and config; one deployment per region |
| Usage records | ~5B/day | 50M × ~100 sessions/records per day |
| Throughput | **60k/s average, 150k/s evening peak** | 5B / 86,400 ≈ 58k/s; peak factor ~2.5 |
| Raw volume | ~1 TB/day | 5B × ~200 B; raw kept in cheap Object Storage (audit), aggregates in Valkey |
| Wire volume at peak | ~30 MB/s raw → **~6 MB/s gzip** | gzip on JSONL batches gives ~5× (measured ≥4× in tests) |
| OCI Streaming partitions | 1 MB/s write per partition | 6 MB/s → 8 partitions at 75%; **provision 12–16** (fixed at stream creation) |
| Alert latency | p95 < 1 min | Streaming path, not batch |
| Correctness | No double count, **exactly one alert per level per cycle** | Idempotent consumer (see §6) |
| App reads | ~10M app users/day, ~2,500 req/s peak | Read running totals (O(1)), never SUM raw rows |
| Data residency | GDPR: EU customer data stays in the EU | Per-region deployment; LLM provider per country by flag |
| Cost (case study) | Near zero | Always Free OCI services; one ~$1.5/day worker node |

## 2. System context (C4 level 1)

![System context: customers, mediation, operators, GitHub, Razorpay, LLM providers, Langfuse, legacy order systems](diagrams/context.png)

## 3. Containers (C4 level 2)

> The service split, LLM gateway, Temporal workflows, order/bss event streams, circuit breakers and
> bill-day preparation are described in **[PLATFORM.md](PLATFORM.md)**, which supersedes the
> "core-api (modular monolith)" box below. Payments, the assistant and the order adapter now run as
> separate services.

![Containers: usage pipeline, customer side, AI, orders, state and messaging](diagrams/architecture.png)

Also in the cluster, not drawn: **flagd** (OpenFeature flags, read by core-api, the collector, aggregators and notifier) and the **External Secrets Operator** (OCI Vault → Kubernetes Secrets via the node's instance principal). `payments-service`, `assistant-service`, `order-adapter` and `workflow-worker` run from the core-api image with different entrypoints.

**Why it's shaped this way:** the hot path (collector, aggregator) scales with event volume and is split out. The customer domains scale with requests and live in one deployable (`core-api`) with strict module boundaries, so any module can be extracted later without a rewrite.

## 4. OCI deployment

![OCI deployment](diagrams/oci-deployment.png)

All of it is defined in Terraform (`deploy/terraform`), with remote state in `dt-selfcare-tfstate`. The A1 (ARM, Always Free) shape was out of host capacity in Mumbai on the build day. `node_shape` is a variable, and switching back to A1 changes the image architecture automatically.

## 5. Key flows

### (a) Usage record → running total → 80% alert, exactly once → push to the open app

![Usage record to 80% alert, exactly once](diagrams/j3-usage-alert.png)

### (b) Bill run at cycle close

![Bill run at cycle close](diagrams/j4-bill-run.png)
Bills are immutable. Re-running the job is safe. Each month is priced on the tariff in force then (`plan_at`), not today's. A customer who joined mid-month (no earlier bill) pays fee and allowances only from the join day. Late records stay in the cycle counters; on every bill run the last 3 billed months are compared with the counters they were priced from (`rated_counters`), and any difference becomes one adjustment line on the new bill, applied once, never a mutation of an issued invoice.

### (c) Home page (server-rendered snapshot + streamed AI narrative)

![Overview page: one snapshot, slow parts streamed](diagrams/j2-home.png)

### (d) Payment

![Payment sequence](diagrams/j7-payment.png)

### (e) AI explanation with guardrails

![Bill explanation with guardrails](diagrams/j5-explanation.png)
Driver impacts are before VAT and totals include VAT; the prompt makes the model say which. The model's JSON is normalised to the shape the page renders (missing lists become empty).

### (f) Usage upload / scenario simulation
The customer uploads CSV/JSON (≤1 MB, read bounded; ≤10,000 rows, validated units and zones) or picks a scenario. Scenarios start from the counters the last bill was priced from (`rated_counters`): trips and calls are added on top (`mode: add`), a light or heavy month replaces that part of the month (`mode: replace`). `core-api` prices the result with the **same** `rate()` as the current cycle, explains it against the latest real bill, and narrates it with the same guardrails, told that it is a what-if estimate. The narration respects the `ai-explanations` kill switch and has its own quota (10 a day), so what-ifs can't use up the real bill's explanations. Bad files get a friendly 422 (wrong JSON shape, unsupported types, UTF-16 Excel, `;` delimiter, decimal comma, loose headers). **Nothing is written to Valkey or to bills**, so a customer can't change their own invoice this way.

## 6. Design decisions

| # | Decision | Alternatives considered | Why / trade-off |
|---|---|---|---|
| 1 | **OCI Streaming (classic)**, used through the Kafka API | OCI managed Kafka; self-run Kafka (Strimzi) | Billed per GB (cents for this scale); clients are plain Kafka, so moving later is a config change. Costs: 1 MB/s/partition, fixed partition count, no transactions → decisions 2, 5, 6 |
| 2 | **Idempotent consumer**: offset stored with state + message id dedupe + HSETNX alert flags + outbox, all in one Lua script | Kafka transactions / Flink checkpoints | Works on any at-least-once broker; atomic per message; independent of broker features |
| 3 | **Valkey hash tag `{pN}`** on every key of a partition | Per-subscriber keys without tags | The whole message is one atomic script even on a sharded cluster; each partition = one slot |
| 4 | **Batch-level idempotency** at ingest (`batch:<source>:<id>`; batch ids are only unique per source) | Per-record dedupe keys only | Batch keys are tiny. Record ids are also claimed atomically at screening (`SET NX`, 7 days; a Bloom filter at 5B/day). Re-published chunks are caught downstream by deterministic `msg_id` |
| 5 | **Explicit partition** = crc32(subscriber) % N | Kafka key hashing | Producer and state layout must agree exactly; stable because N is fixed on OCI |
| 6 | **gzip JSONL batches** (≤2,000 records/message) | One record per message | Fits under 1 MB/s/partition at peak with ~12 partitions instead of ~60 |
| 7 | **Modular monolith** `core-api` + separate hot path | Microservice per domain | Avoids distributed-system cost before teams or load need it; module boundaries keep extraction cheap |
| 8 | **SSR + one snapshot per page** (`as_of`, versions, `private, no-store`) | SPA + per-widget calls | No inconsistent first paint; personal data never on a CDN; locale and time-zone-pinned formatting means no hydration mismatch |
| 9 | **Provider-agnostic LLM**: DeepSeek (demo), Mistral EU or self-hosted vLLM (prod EU) | Single vendor | Cost and quality for the demo; GDPR in production by flipping a flag; same guardrails for every provider |
| 10 | **Deterministic explainer first, AI only rephrases** + number-grounding check | LLM computes the explanation | No hallucinated amounts reach customers; works when AI is down |
| 11 | **Razorpay + `payment-settlement` flag** (INR for UPI) | Stripe/Adyen | Razorpay test mode is instant in India; UPI is INR-only, so bills stay EUR/PLN and the gateway charges INR at a recorded rate. EU production: Adyen/Stripe adapter with `native` |
| 12 | **Autonomous DB (Always Free)** for system-of-record | Postgres in-cluster | Managed, backed up, free; portable SQLAlchemy Core schema |
| 13 | **flagd + OpenFeature** with safe in-code defaults | Env vars / hard-coded | Per-country/per-user targeting at runtime; if flagd is down, safe defaults apply (proven in practice: an old flagd version caused fallback to defaults without breaking anything) |
| 14 | **External Secrets + node instance principal** | Static creds in k8s / CI | No cloud credentials in the cluster; Vault stays the source of truth; rotation = new secret version |
| 15 | **E5.Flex node** (A1 was out of capacity) | Wait for A1 | Deadline; `node_shape` variable keeps A1 one change away |
| 16 | **Bills immutable**, late data → adjustment | Recompute issued bills | Auditability; matches telecom billing practice |

## 7. Scalability

| Component | Scales by | Limit avoided |
|---|---|---|
| usage-collector | Stateless replicas, HPA 1–6 on CPU | Per-record dedupe store |
| OCI Streaming | Partitions sized for compressed MB/s | Hot partitions (hash by subscriber, never by cell or region) |
| usage-aggregator | Replicas ≤ partitions; each owns disjoint partitions | Global locks, cross-partition transactions |
| Valkey | Cluster mode; one slot per partition | Cross-slot scripts |
| core-api / web | Stateless, HPA (2–8 / 2–6) | Backend round-trips: one snapshot call per page |
| Bills/PDF | Pre-generate on bill day to Object Storage + CDN (next step) | Bill-day thundering herd |

Measured: **14,721 records/s sustained on one 1-OCPU node**, 0 retries or duplicates ([LOADTEST.md](LOADTEST.md)). Extrapolated peak: ~10–12 aggregator pods and 12–16 partitions.

## 8. Resilience and failure modes

| What fails | What the user sees | Recovery |
|---|---|---|
| Aggregator pod crash mid-message | Nothing | New owner seeks to `off:{p}+1`; replayed offset is a no-op |
| Collector crash mid-publish | Network retries batch | `batch:` key not marked done → republish; duplicates dropped by `seen:{p}:msg_id` |
| Valkey unavailable | Usage/unbilled stale; home may fail | Aggregator blocks with backoff (never skips); AOF every second; rebuild from stream or raw archive |
| OCI Streaming unavailable | Alerts delayed | Collector answers 503 + Retry-After; sources retry |
| Poison or unappliable message | Nothing | Pushed to `dlq:{p}` list, partition continues (no crash loop) |
| Invalid record (unknown add-on code or roaming zone, implausible quantity) | Nothing | Quarantined at ingest; an unknown zone that slips through is priced as the plan's most expensive zone |
| One customer's bad data at bill run | Their bill is missing; everyone else's is issued | Per-customer isolation; failures logged and counted |
| notifier worker thread dies | Nothing (pod restarts) | main() exits when any worker thread dies, so Kubernetes restarts the pod |
| LLM provider down / slow | Answer from the fallback provider (Mistral EU) | Gateway retries, cooldown, fallback; per-pod concurrency limit |
| All LLM providers down / answer fails verification | "The explanation isn't available right now"; bill breakdown unaffected | No templates by design; numbers verified against sources or not shown |
| core-api down | Last good account view with "as of HH:MM" banner; auto-refresh | opossum breaker in the BFF; stale snapshot (≤ 1 h) or styled outage page |
| payments / assistant service down | Pay button / assistant replaced by a clear notice | Breaker per service; the rest of the app works |
| Legacy order system slow or at capacity (bill day) | "In line for the network system…", notification when done | Per-country Temporal task queue rate-limited to legacy capacity; durable |
| Razorpay down | "Try again shortly" | Breaker; idempotent retry, no double charge |
| Browser closes after paying | Bill marked paid anyway | Webhook path settles; conditional update makes it idempotent |
| flagd down | Safe defaults (AI off, payments on, thresholds 50/80/100) | Provider fallback values |
| Node loss | Brief outage (single node in the case study) | Production: ≥3 nodes across fault domains, PDBs already defined |

## 9. Security and privacy
- **Auth:** argon2id hashes, JWT (15 min) kept in an httpOnly, SameSite=Lax, Secure cookie by the web tier, never readable by page JS. Every customer route derives the subscriber from the token (`/v1/me/...`), so there's no IDOR by construction. Tokens are **RS256**: only core-api (the issuer) holds the private key; every other service gets the public key, which verifies but cannot sign, and only RS256 is accepted (no `alg` confusion). The web BFF signs its cookie with its **own** `session-secret`. Keys are generated straight into OCI Vault by `deploy/scripts/auth-keys.sh` (never printed, on disk or in Terraform state; `--rotate` for rotation).
- **Login limits:** 10 attempts per number per 15 min (brute force on one account) **and** 20 *failed* logins per client IP per 15 min (credential stuffing / password spraying across many accounts). Only failures count per IP, so many customers behind one NAT still sign in. The client IP is the ingress' `X-Real-Ip` (the ingress overwrites any client-sent value), passed by the BFF; IPv6 is keyed per /64. **Caveat, measured on prod:** the OCI flexible load balancer proxies TCP, so today the ingress sees the LB's private address, not the visitor's. Non-public addresses are ignored (otherwise every visitor would share one bucket and 20 failures by anyone would lock everyone out), so on this cluster only the per-account limit is active until the real address arrives: PROXY protocol on the LB listener or an OCI Network Load Balancer (preserves the source IP) is the next step.
- **CSRF:** React Router origin check on form actions (Express `trust proxy` so the https origin is seen correctly), plus an Express guard that refuses any non-GET `/api/*` or `/logout` request whose `Sec-Fetch-Site`/`Origin` is not same-origin (SameSite=Lax alone would trust every `*.sslip.io` host). Logout is POST-only. Strict **CSP**: `script-src 'nonce-<per request>' 'strict-dynamic'` plus `frame-ancestors 'none'; base-uri 'self'; object-src 'none'`. React Router's inline hydration and module scripts carry the nonce (`entry.server.tsx`); scripts they load (route chunks, Razorpay checkout) are trusted through `strict-dynamic`; an injected script or inline handler has no nonce and is blocked.
- **Abuse and cost limits per customer:** 20 fresh (uncached) AI explanations a day (what-if simulations have their own 10 a day), so one customer can't use up the shared daily LLM budget; 30 CDR evidence re-scans an hour; every `/bills/{cycle}` path is validated as a month before any work is done.
- **Payments ledger:** a second tab paying the same bill or advance gets the open order (single flight per bill/advance); captured-but-unapplied payments and earlier advances are subtracted from what is charged; `amount_paid` is incremented atomically in SQL.
- **Allowance can't be granted by the network:** add-on (`PLAN_CHANGED`) events are accepted only from order sources. With the dedicated `order-source-token` (Vault, created by `auth-keys.sh`), core-api sends them as source `core-orders` and only that source may; other sources' add-on records are quarantined.
- **Billing month = UTC calendar month (deliberate):** one global billing clock for usage keys, per-day fields, bill runs and the 12-month history. Usage between 00:00 and 01:00/02:00 local time on the 1st therefore lands in the previous month for DE/PL. Moving to local-time cycles is a per-market configuration (`CountryConfig.timezone`) **plus a data migration** of usage keys and history, so it is planned as its own change rather than patched in.
- **System page:** cluster-wide aggregates only. Quarantine samples show reasons only (raw entries carry other customers' record ids), layer errors are logged server-side and shown as "unavailable", and each layer is single flight and cached (5–30 s; the slow stream probe 15 s, the table counts 30 s), so page views can't exhaust the threadpool.
- **Closed gaps:** the web tier no longer shares the token secret (RS256, own `session-secret`); per-IP failed-login limit (in code; engages once the real client IP arrives, see below); `script-src` nonce CSP.
- **Known gaps, production next steps:** no NetworkPolicies (flannel doesn't enforce them: Calico or OCI VCN-native CNI, then default-deny); order-adapter trusts the cluster network (service token or mTLS); the real client IP at the ingress (PROXY protocol or OCI NLB) so the per-IP login limit engages, plus a bot check / WAF and the group IdP (MFA) in front of login; the System page shows cluster-wide aggregates to every customer (admin-only in production); the invoice has a seller block but no customer postal address yet; billing cycles are UTC months, not local time (see above). Infrastructure: run `db-init` as a pre-upgrade hook (a fresh install would wait on it); move the CI deploy secrets to the `production` environment restricted to `main`; scope the nodes' Vault policy to named secrets or use OKE Workload Identity (any pod can use the node identity today); PDBs need a second node (or surge) to drain the single node; readiness shouldn't depend on the shared DB; pin third-party actions and base images by digest.
- **Edge:** only `web` and `/v1/webhooks/razorpay` are public. Collector, Valkey, flagd and the rest of core-api are cluster-internal. HSTS, nosniff, frame-deny headers.
- **Secrets:** OCI Vault → External Secrets via instance principal. Third-party keys are added by a script with hidden input and never touch Terraform state.
- **Data minimisation for AI:** facts only, no name, number or email; outputs verified.
- **Payments:** signature verification on both paths, server-side capture check, webhook HMAC.
- **Workloads:** non-root, read-only root filesystems, all capabilities dropped, seccomp RuntimeDefault, DB app user limited to its own schema (created by the `db-init` hook).

## 10. Observability
Structured JSON logs with request ids. Prometheus metrics on every service, for example `aggregator_event_lag_seconds`, `aggregator_alerts_raised_total`, `collector_batches_total{result}`, `ai_narration_total{provider,outcome}`, `ai_tokens_total`, `circuit_breaker_open`, `http_request_seconds`. Each deploy runs a `helm test` smoke test (19 checks), Playwright e2e on the live site and the assistant eval gate (golden set + calibrated policy judge); 25% of production assistant answers are judged in the background, with the trend on the System page ([AI_AGENT.md](AI_AGENT.md)). Next step: OpenTelemetry traces and SLOs (alert p95, home p95, payment success).

## 11. Cost (case study)

| Item | Cost |
|---|---|
| OKE BASIC control plane, Autonomous DB, flexible LB, Vault (software key), Object Storage ≤20 GB, OCIR, NAT/Service GW | Always Free / no charge |
| OCI Streaming | per GB in/out + storage (cents) |
| Worker node VM.Standard.E5.Flex 1 OCPU / 16 GB + 50 GB boot + 50 GB Valkey volume | ≈ $1.5/day; `node_count=0` parks it |
| LLM | DeepSeek, cents per hundred explanations; cached |
| GitHub Actions | free (public repo) |

## 12. Multi-country and GDPR
- Country on every record; catalogue, VAT, currency and locale per country (`catalog.py`); flags target by `country`.
- One deployment (cell) per region; data never crosses regions. For the EU, the AI provider flag points to an EU-hosted or self-hosted model.
- A new country = config + a country adapter to its legacy billing/CRM behind TM Forum APIs. No code fork.

## 13. EM view
- **Teams:** Data Platform (collector, stream, aggregator, archive), Billing & Payments, Customer Experience (web, insights, AI), Platform/SRE (OKE, CI/CD, observability, cost).
- **MVP sequence:** bill view + PDF → unbilled + pay → deterministic explainer → AI narrative behind a flag → real-time alerts → country #2.
- **Metrics:** support-call deflection on billing questions, alert p95 latency, duplicate-alert rate (target 0), payment success rate, AI fallback rate and cost per 1k explanations, home p95.
