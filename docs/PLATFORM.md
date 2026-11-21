# Platform: services, LLM gateway, durable workflows, LLM observability

```mermaid
flowchart LR
  B[Browser] --> W[web SSR]
  W -->|/v1/me/*| C[core-api: billing, usage, home, orders, simulator]
  W -->|/v1/me/payments| P[payments-service]
  W -->|/v1/me/assistant| A[assistant-service]
  RZ[Razorpay] -->|webhook| P
  P -->|status API| RZ
  P -->|outbox: PAYMENT_SUCCEEDED| ST[(OCI Streaming)]
  ST -->|payment-events| N[notifier/billing consumer]
  N -->|bill_payments ledger| DB[(Autonomous DB)]
  A -->|tools via broker| DB
  A --> G[LLM gateway: LiteLLM]
  C -->|bill explanation| G
  G -->|primary| DS[DeepSeek]
  G -->|fallback| MI[Mistral EU]
  G -->|traces| LF[Langfuse]
  A -->|prompts| LF
  C -->|approve order| T[Temporal]
  T --> WW[workflow-worker]
  WW -->|TMF622 ProductOrder, timeouts + breaker| AD[order-adapter]
  AD -->|DE: REST / PL: SOAP, per-country rate limit| BSS[legacy order systems]
  AD -->|bss-events| ST
  ST -->|bss-events -> signal| WW
  C -->|status change + outbox, same tx| ST
  ST -->|order-events| N
  WW -->|PLAN_CHANGED| COL[usage-collector]
```

## Services and data ownership
| Service | Scales on | Owns | Talks to others via |
|---|---|---|---|
| usage-collector, usage-aggregator, raw-archiver, alert-relay | ingest rate, partitions | Valkey usage state, raw archive | OCI Streaming |
| **core-api** | page traffic | subscribers, bills, notifications, orders | REST; consumes `payment-events` (billing) |
| **payments-service** | checkouts | `payments` | Razorpay status API; emits `PAYMENT_SUCCEEDED` (transactional outbox) |
| **assistant-service** | concurrent LLM streams (SSE) | `context_docs`, `agent_traces` | LLM gateway; read-only tools |
| notifier | alerts and events | — | streams; Valkey pub/sub |
| workflow-worker | workflow activities | — | Temporal; order-adapter (HTTP); consumes `bss-events` |
| **order-adapter** | order submissions | legacy emulator state (`bss_orders`) | TMF622 API; publishes `bss-events` |
| llm-gateway | LLM request rate | provider keys | DeepSeek, Mistral, Langfuse |

- **Payments → billing is event-driven.** payments-service never writes billing tables.
  - Billing applies `PAYMENT_SUCCEEDED` through the `bill_payments` ledger (PK = payment id), so redeliveries do nothing.
  - The bill shows PAID within a few seconds of the capture (outbox relay → stream → consumer).
- **Honest limit:** the services still share one Autonomous DB, with table ownership as above. The assistant's read-only tools read billing tables directly. The target is a database per service, with tools calling core-api's read APIs. That's the next extraction step, gated on team topology.

## LLM gateway (LiteLLM)
- **Routing:** services call model alias `billing-llm`. The gateway routes it to **DeepSeek** and falls back to **Mistral (EU)** after errors, with 2 retries and `retry-after`.
- **Failing providers:** a failing deployment is cooled down for 30 s after 3 failures.
- **Rate limits** per deployment (rpm/tpm), configured in `values.yaml` under `llmGateway`.
- **Keys:** provider keys exist **only** in the gateway, synced from OCI Vault. Services hold a gateway key.
- **Tracing:** every call is sent to **Langfuse** (`success_callback`/`failure_callback`) with metadata: trace name, session id, pseudonymous user hash, stage.
- **Client behaviour:** the service-side client is **async** (`httpx.AsyncClient`), limits concurrency per pod (`llm_max_concurrency`) and retries the gateway with jittered backoff.

## Orders: events, adapters, bill-day spikes
- **`order-events` (Kafka/OCI Streaming).** Every order status change (PROPOSED → APPROVED → SUBMITTED →
  COMPLETED/FAILED, DECLINED) is written together with an `ORDER_STATUS_CHANGED` outbox row **in the same
  DB transaction** and relayed to the stream. The `order-projections` consumer updates the
  home-screen cache, the assistant's context and, on terminal states, the customer notification
  (idempotent: `alert_id = order:<id>:<status>`). Workflows never write notifications themselves.
- **`bss-events`.** The legacy result (TMF622 state) is published by the order-adapter. The worker's
  `order-workflows` consumer turns it into a workflow signal. Measured live: 0.7 s from legacy result to
  signal. If an event is lost, the workflow asks the adapter for the state every 45 s.
- **order-adapter: Temporal never calls a legacy system.** Activities call one internal API (TMF622
  ProductOrder). It picks the country adapter (DE REST, PL SOAP) and enforces each legacy system's capacity
  across all pods (Valkey 1-second window, `bss_max_rps`). Over capacity it answers **429 + Retry-After**,
  and the activity backs off (up to 5 min between tries, for up to 12 h). A bill-day spike becomes a
  durable queue in Temporal, not an outage of the legacy system. Temporal also caps the task queue's
  activity rate across workers.
- **Tariff change** (`PlanChangeWorkflow`) shares the same activities and adapters (`action: modify`).
  The switch applies from today; this month is billed on the new tariff (pro-rating is out of scope).
  Available from the `/plan` page and from the assistant (`propose_plan_change`, approval card).

## Timeouts and circuit breakers
Every service-to-service call has a **connect timeout** (1 s) and a **read budget**, plus a **circuit breaker**:
after 5 consecutive failures (timeout, connection error, 5xx) the circuit opens and fails fast; after a cool-down
one trial request decides whether it closes. 4xx and 429 don't trip it.

| Caller → callee | Connect / read | Breaker |
|---|---|---|
| web BFF → core-api, payments, assistant | fetch timeout 5 s (30 s explanation, 120 s assistant stream) | 5 fails → open 20 s → 503 page message |
| workflow activities → order-adapter | 1 s / 5 s | 5 fails → open 30 s → Temporal retries |
| workflow activities → usage-collector | 1 s / 10 s | Temporal retry policy |
| services → LLM gateway | per stage, with retries/backoff | gateway cooldown + fallback |

## Bill day
- The `bill-run` CronJob issues the bills (init container), then **`explain-batch`** runs: it warms every
  customer's home-screen cache and pre-generates the AI explanation at a fixed rate (`billDay.explainRps`).
  Customers opening the app get both from cache: no DB stampede, no LLM stampede.
- Read path measured live (one 1-OCPU node, 32 concurrent): **246 rps, 0 errors, p50 88 ms, p95 380 ms,
  cache hit 99.9%** (`deploy/loadtest/read-path-job.yaml`).
- Edge: Traefik rate limit per client IP (40 rps, burst 120); verified with a 400-request burst: 176 × 200, 224 × 429.

## Durable write actions (Temporal)
- **Broker tiers:** `read` tools execute; the **`propose`** tier (`propose_addon`) only creates a PROPOSED order with a preview. The customer approves in the UI (approval card in chat, or "Add data").
- **Approve** → `start_workflow(AddonPurchaseWorkflow, id=order_id)`. Because the workflow id is the order id, a double click can't start two workflows.
- **Workflow steps:**
  1. Validate.
  2. Submit to the legacy BSS (simulated: 20–60 s, ~10% rejections).
  3. Durable wait for its signal, re-checking with `bss_status` every 45 s in case the callback was lost; give up after 30 min.
  4. On confirmation, apply as `PLAN_CHANGED` through the **real usage pipeline**. The record id is `order-<id>`, so a retried activity is deduplicated by the collector.
  5. Complete and notify.
- **On rejection or timeout:** compensate (mark FAILED, notify; nothing was applied).
- **Durability:** a worker or server pod restart resumes from history; the server stores its SQLite DB on a volume. In production: Temporal Cloud or an HA cluster on Postgres/Cassandra, with the same code.
- **Tests:** `tests/test_workflow.py` runs 4 paths on Temporal's time-skipping test server:
  - confirmed
  - rejected
  - lost callback, found by polling
  - never confirmed

## Observability, evaluations, prompt versioning (Langfuse, EU cloud)
- **Traces:** every LLM call (grader, agent turns, repairs, explanation) carries cost, tokens, cached tokens and latency, grouped by session.
- **Prompt management:** `assistant-system` and `explainer-system` are fetched with label `production`, cached 5 min, with the repo default as fallback.
  - `prompt-seed` publishes the repo versions.
  - Rollback = move the label.
  - The active version is recorded in every trace (`prompt_version`).
- **Evaluations:** dataset `billing-assistant-golden` (9 questions over the demo population), run as experiment `assistant-golden` (`deploy/loadtest/evals-job.yaml`) with these evaluators:
  - `grounded`
  - `mentions`
  - `used_tools`
  - `cost_usd`

  Compare runs across prompt versions and models.

## Sessions
Sessions use rotating refresh tokens:
- **Access JWT:** 15 min.
- **Refresh token:** 30 days, stored only as a hash in Valkey, rotated on every use.
- **Parallel refreshes:** a 30 s grace window covers requests that refresh at the same time.
- **Logout** revokes the refresh token.

The web server refreshes transparently, so users stay signed in across deploys.
