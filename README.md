# Telecom Self-Care: Usage & Bill Explainer

A self-care backend and web app for a multi-country mobile operator. It covers the whole path from
the network usage stream to an explained, payable bill. It's built to production standards and
deployed on Oracle Cloud (OCI), but sized to cost close to nothing.

**Design docs:** [HLD](docs/HLD.md) · [LLD](docs/LLD.md) · [AI agent L1–L4](docs/AI_AGENT.md) · [Payments & reconciliation](docs/PAYMENTS.md) · [Platform: services, LLM gateway, Temporal, Langfuse](docs/PLATFORM.md) · [CDR scenario coverage](docs/CDR_SCENARIOS.md) · [Operations runbook: see inside the DB, Valkey and the stream](docs/OPERATIONS.md) · [Load test](docs/LOADTEST.md) · Diagrams are PNGs with editable draw.io sources in [docs/diagrams](docs/diagrams)

> Case study: **"Usage & Bill Explainer"**: the user uploads or selects their usage and gets
> insights on their bill.

## What it does

| Case study ask | Where |
|---|---|
| Mock usage input (data, calls, roaming…) | `network-emulator` → `usage-collector` → OCI Streaming → `usage-aggregator` (real-time totals) |
| User uploads / selects usage → insights | **Try your usage** page → `POST /v1/me/usage/simulate` (CSV/JSON upload or scenarios) |
| Bill trends, last 12 months · view · download | Overview trend chart, bill detail page, PDF (`/v1/me/bills/{cycle}/pdf`, opens inline in a new tab; embedded font, seller block) |
| Bill pending: unbilled amount + tap to pay | *Unbilled so far* card (priced live from the stream) + *Bill pending* card → payment page `/pay/bill/<cycle>` or `/pay/advance` → real Razorpay checkout → result page `/pay/result` |
| Payment gateway | Razorpay (test mode): order → checkout → signature check + server-side capture check → webhook; the result page takes the status from the gateway. Idempotent end to end; advances are charged net of earlier advances and credited at the bill run. |
| Bill breakdown service | `telco_common.billing.rate`: plan fee, overage, roaming by zone (EU roam-like-at-home), international, add-ons, VAT per country |
| "Why your bill increased" / "Top usage drivers" | `telco_common.explainer.explain`: deterministic, ranked by impact |
| AI natural-language explanation | Provider-agnostic: DeepSeek (demo), Mistral (EU-hosted) or self-hosted open weights, chosen by a feature flag. Guardrails: facts-only input, **every number in the answer is checked against the facts** (repaired, or not shown), circuit breaker, cache, per-user budget. EN / DE / PL. |

Beyond the brief: real-time **50/80/100% data alerts sent exactly once** (pushed live to the open
app), multi-country (DE in EUR at 19% VAT, PL in PLN at 23% VAT), feature flags per country, and
a load generator. An assistant panel on every page answers from the customer's own records and
prepares add-ons, tariff changes and payments as approval cards; a golden set and a policy judge
calibrated against human labels gate every deploy ([AI agent](docs/AI_AGENT.md)). A **My plan** page
shows the current plan and compares it with the others.

## Architecture

> **Current architecture: [docs/PLATFORM.md](docs/PLATFORM.md).** It covers the microservices
> (core-api, payments-service, assistant-service, order-adapter), the LLM gateway (LiteLLM, DeepSeek → Mistral
> fallback), Temporal order workflows with per-country legacy task queues, `order-events` / `bss-events`
> on OCI Streaming, circuit breakers and what the customer sees when one opens, bill-day preparation,
> tariff-change pro-rating, and Langfuse tracing/prompts/evals. The diagram below is the usage pipeline,
> which is unchanged.

```
 Network (mediation)            OCI Streaming (Kafka API)                 Valkey (usage state)
 ┌───────────────────┐  HTTPS  ┌──────────────────┐   usage-events       ┌───────────────────────┐
 │ network-emulator  │───────▶│ usage-collector  │──(N partitions,──┐   │ u:{p}:<sub>:<cycle>   │
 │ (only synthetic   │ batch   │ validate, batch, │   by subscriber) │   │ off:{p}  seen:{p}:…   │
 │  part)            │ idem.   │ gzip, publish    │                  ▼   │ alerts:{p} (outbox)   │
 └───────────────────┘         └──────────────────┘        ┌────────────────────┐           ▲
                                                           │ usage-aggregator    │──Lua─────┘
                                                           │ exactly-once apply  │  atomic: totals +
                                         raw-archiver ◀────│ (offset in Valkey)  │  thresholds + outbox
                                   (Object Storage, audit) └────────────────────┘
                                                                     alert-relay ──▶ usage-alerts ──▶ notifier ──▶ SSE / app
 Browser ──▶ web (React Router SSR) ──▶ core-api (modular monolith) ──▶ Autonomous DB (bills, payments, outbox)
                                          │  billing · payments · insights · AI      └──▶ payment-events (outbox relay)
                                          ├──▶ Razorpay (orders, webhooks)
                                          └──▶ LLM (DeepSeek / Mistral EU / self-hosted) — guarded
```

### Key design decisions (and the trade-off behind each)

1. **Exactly-once alerts on an at-least-once broker.** OCI Streaming has no Kafka transactions.
   Instead, each message is applied by a single atomic Valkey Lua script that (a) checks the
   partition offset stored *next to the state* (so crash replays do nothing), (b) checks a
   deterministic message id (so a collector re-publish does nothing), (c) updates totals,
   (d) raises each threshold once per cycle (`HSETNX`), and (e) writes the alert to an outbox stream.
   All keys share the partition hash tag `{pN}`, so this stays atomic on a sharded Valkey cluster.
2. **Batch-level idempotency at ingest.** Network systems re-send files. Deduplicating per batch
   (batch ids scoped per source) keeps the dedupe store tiny. Record ids are also claimed atomically
   at screening (`SET NX`, 7 days), so overlapping re-sends count once; at 5B/day that becomes a
   Bloom filter (plain per-record keys would need ~800 GB of RAM).
3. **Partitioned by subscriber, with an explicit partition number.** Each subscriber has exactly
   one state owner and their events stay in order. Partition counts are fixed on OCI, so the
   mapping is stable.
4. **Compression to stay within the per-partition limit.** OCI allows 1 MB/s per partition. At the
   evening peak (150k events/s × 200 B = 30 MB/s raw), gzip batches bring that to ~6 MB/s, i.e.
   ~8–12 partitions instead of ~60.
5. **Two speeds.** The real-time path drives alerts and the unbilled amount. The invoice is
   immutable once issued (`<subscriber>:<cycle>`), and late data becomes an adjustment.
   Raw usage is archived to Object Storage for reconciliation and disputes.
6. **AI is an enhancement, never a dependency.** The deterministic explainer always renders
   first (it's in the SSR HTML); the AI narrative streams in after. A flag off, a timeout, an open
   breaker or an answer that can't be verified leave the exact deterministic breakdown with "the
   explanation isn't available right now"; an unverified number is never shown.
7. **Modular monolith for the customer domains.** `core-api` keeps billing, payments and
   insights in separate modules, so each can be split into its own service when team size or
   load justifies it. The hot path (collector, aggregator) is already separate because it scales
   differently.
8. **Server-side rendering with one snapshot per page.** `/v1/me/home` returns one consistent
   snapshot (`as_of` plus version numbers). Personal responses are `private, no-store` and never
   cached on a CDN. Formatting is locale-pinned and in UTC, so server and client render identically.
9. **Payments settle exactly once.** An idempotency key per page visit, server-side capture
   confirmation, and the browser callback, the webhook and the reconciler all end in the same conditional
   `UPDATE … WHERE status<>'PAID'`. The payment and its outbox event go in the same transaction, and the bill
   is credited in the same request through the `bill_payments` ledger (PK = payment id), so it shows paid at
   once; the event's billing consumer is then a no-op, with a reconciler sweep as a last safety net.
10. **An order is "active" only when the meter says so.** An add-on travels collector → stream → aggregator
   like any network event; the workflow completes the order only after the aggregator's marker for that
   message exists (written in the same Lua call as the counters): read-your-writes, ~4 s from approval.

### Scaling to 50M subscribers / 10 countries
| Component | How it scales | Bottleneck we avoid |
|---|---|---|
| Collector | Stateless, HPA on CPU | Per-record dedupe store |
| Stream | Partitions sized to compressed MB/s | Hot partitions (we hash by subscriber, never by cell or region) |
| Aggregator | One consumer per partition | SUM queries over raw rows |
| Valkey | Cluster mode; one hash slot per partition | Cross-slot transactions |
| core-api / web | Stateless, HPA | Legacy back-ends: snapshot cache + events |
| Countries | Country on every record; catalogue/VAT/currency per country; one deployment per region (GDPR) | Country-specific forks of the code |

## Run & deploy

Everything runs on OCI. Every environment uses the same images and the same Helm chart.

```bash
deploy/scripts/bootstrap.sh            # compartment + Terraform state bucket (idempotent)
cd deploy/terraform && terraform init -backend-config=backend.hcl && terraform apply
deploy/scripts/cluster-addons.sh       # External Secrets, cert-manager, Traefik, metrics-server, CI RBAC
deploy/scripts/gh-secrets.sh           # CI credentials -> GitHub secrets
deploy/scripts/auth-keys.sh            # JWT RS256 key pair, web session-secret, order-source-token -> OCI Vault (--rotate)
deploy/scripts/put-secrets.sh          # Razorpay, DeepSeek, Mistral, Langfuse keys -> OCI Vault (input hidden)
# Delivery: main is protected. Open a PR -> pr-validation (lint, tests, web build, helm + kubeconform,
# terraform validate, image builds, gitleaks) must pass -> merge -> deploy-prod (images -> helm upgrade
# -> in-cluster smoke test -> Playwright e2e on the live site -> assistant eval gate: run-evals --gate --calibrate).
```

Cost: OKE basic control plane, Autonomous DB, load balancer, Vault, Object Storage and the
container registry are all Always Free. One worker node (E5.Flex 1 OCPU/16 GB, about $1.5/day;
A1 ARM when Mumbai has capacity) is the only real cost. Set `node_count=0` to park it.

## Repository
```
libs/telco_common        domain: records, catalogue, rating engine, explainer, usage state (Lua)
services/usage-collector ingest API (batch idempotency, partition-aware compressed publishing)
services/usage-aggregator stream consumer, raw archiver, alert relay
services/core-api        auth, home snapshot, bills, PDF, payments, AI assistant + policy judge/evals, orders
                         (Temporal workflows), simulator, notifier, bill run; one image, run as core-api,
                         payments-service, assistant-service, order-adapter, workflow-worker
services/network-emulator personas, 12-month backfill, live traffic, load generator
web/                     React Router (SSR) frontend
deploy/terraform         OCI: VCN, OKE, ADB, Streaming, Vault, IAM, OCIR, Object Storage
deploy/helm/selfcare     workloads, Valkey, flagd, Temporal, LLM gateway (LiteLLM), External Secrets, ingress/TLS, jobs
```
