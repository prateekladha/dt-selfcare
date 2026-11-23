# Low-Level Design

> See [HLD.md](HLD.md) for the architecture and [OPERATIONS.md](OPERATIONS.md) to inspect live data.

## 1. Repository and module map

| Path | Kind | Responsibility | Entrypoints |
|---|---|---|---|
| `libs/telco_common` | Python lib (pure + Valkey state) | Domain model, catalogue, rating engine, explainer, partitioning, codec, flags, usage state + `apply.lua`, demo population | — |
| `services/usage-collector` | FastAPI | Network-facing ingest: auth per source, validate, batch idempotency, partition, gzip, publish | `usage_collector.app:run` |
| `services/usage-aggregator` | Kafka consumers | `usage-aggregator` (apply), `archiver` (raw → Object Storage), `alert-relay` (outbox → topic) | `usage-aggregator`, `archiver`, `alert-relay` |
| `services/core-api` | FastAPI modular monolith (one image, several deployables) | auth, home BFF, bills, PDF, payments, insights/explainer, AI assistant + policy judge, orders (Temporal workflows), simulator, admin, system, SSE; deployables: payments-service, assistant-service, order-adapter, workflow-worker; workers/jobs: notifier, bill-run, explain-batch, db-init, prompt-seed, smoke-test, run-evals | `core-api`, `payments-service`, `assistant-service`, `order-adapter`, `workflow-worker`, `notifier`, `bill-run`, `explain-batch`, `db-init`, `prompt-seed`, `smoke-test`, `run-evals` |
| `services/network-emulator` | Typer CLI | Synthetic mediation: `backfill`, `live`, `load` | `network-emulator` |
| `web` | React Router SSR + Express | UI, session cookie, proxies to core-api | `node server.mjs` |
| `deploy/terraform` | IaC | VCN, OKE, ADB, Streaming, Object Storage, Vault, IAM, OCIR | — |
| `deploy/helm/selfcare` | Helm chart | All workloads, Valkey, flagd, Temporal, LLM gateway (LiteLLM), ExternalSecrets, ingress/TLS, jobs, smoke test | — |
| `deploy/scripts` | Bash | `bootstrap.sh`, `cluster-addons.sh`, `gh-secrets.sh`, `put-secrets.sh`, `auth-keys.sh` (JWT key pair, `session-secret`, `order-source-token`; `--rotate`) | — |

### Configuration (environment)

| Prefix / var | Used by | Meaning |
|---|---|---|
| `STREAM_BOOTSTRAP`, `STREAM_USERNAME`, `STREAM_PASSWORD`, `STREAM_USAGE_TOPIC`, `STREAM_ALERTS_TOPIC`, `STREAM_PARTITIONS` | collector, aggregator, notifier | Kafka-compatible OCI Streaming (SASL_SSL/PLAIN; username `tenancy/user/streampool-ocid`, password = auth token) |
| `APP_VALKEY_URL` | all Python | `redis://valkey:6379/0` |
| `APP_DB_DSN`, `APP_DB_USER` (SELFCARE), `APP_DB_PASSWORD` | core-api, notifier, bill-run | ADB TLS connect string, least-privilege app user |
| `APP_JWT_PRIVATE_KEY` (core-api only), `APP_JWT_PUBLIC_KEY` (all services), `APP_REQUIRE_JWT_KEYS` | core-api image | RS256 signing / verify-only; never fall back to a throwaway key in the cluster |
| `APP_DEMO_PASSWORD`, `APP_SEED_DEMO` | core-api | Demo seed |
| `APP_SOURCE_TOKENS` | collector | JSON `{source: token}` |
| `APP_ORDER_SOURCE_TOKEN` (optional, Vault `order-source-token`) | collector, core-api workflows | Dedicated source `core-orders` for add-on events; once set, the collector accepts `PLAN_CHANGED` only from it (otherwise from `order_sources`, default `emu-mediation-01`) |
| `APP_RAZORPAY_KEY_ID/_KEY_SECRET/_WEBHOOK_SECRET` | core-api | Gateway |
| `APP_LLM_GATEWAY_URL` (`http://llm-gateway:4000`), `APP_LLM_GATEWAY_KEY` | core-api image (all its deployables) | LLM gateway; provider keys (`DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`) exist only in the `llm-gateway` pod. `APP_DEEPSEEK_API_KEY` / `APP_MISTRAL_API_KEY` / `APP_SELFHOSTED_LLM_URL` remain for direct calls without a gateway (local runs) |
| `TEMPORAL_ADDRESS` (`temporal:7233`), `APP_COLLECTOR_URL` | core-api image | Order workflows; add-on events into the usage pipeline |
| `APP_ARCHIVE_BUCKET`, `APP_ARCHIVE_NAMESPACE`, `APP_OCI_AUTH=instance_principal` | archiver | Raw archive |
| `FLAGD_HOST=flagd`, `FLAGD_PORT=8013` | all Python | Feature flags |
| `CORE_API_URL`, `SESSION_SECRET` | web | Backend URL, cookie signing (its own `session-secret`, independent of the token keys) |
| Settings defaults | core-api | `jwt_ttl_s=900`, `ai_daily_limit_per_subscriber=20`, `bill_due_days=14`, `llm_timeout_s=30`, `deepseek_model=deepseek-chat`, `mistral_model=mistral-small-latest`, gateway aliases `llm_model_alias=billing-llm` / `llm_title_alias=title-llm` / `llm_judge_alias=judge-llm`, `judge_sample_rate=0.25`, `llm_max_concurrency=32` |

## 2. REST API

All customer routes require `Authorization: Bearer <JWT>` and are scoped to the token's subscriber. Personal responses carry `Cache-Control: private, no-store`.

### core-api

| Method & path | Request | Response | Errors |
|---|---|---|---|
| `POST /v1/auth/login` | `{subscriber_id, password}` | `{access_token, token_type, expires_in}` | 401 bad credentials · 429 >10 attempts per number or >20 failed logins per public client IP (/64 for IPv6) in 15 min |
| `GET /v1/me/home` | — | Snapshot: `as_of`, `subscriber{id,name,country,locale}`, `plan`, `usage{cycle,data_pct,version,data_mb,data_allowance_mb,voice_min,sms,intl_voice_min,roaming_*}`, `unbilled{cycle,currency,subtotal,vat,total,lines[]}`, `outstanding{amount,currency,bill_id}`, `last_bill`, `trend[≤12]`, `notifications[≤10]`, `features{ai_explanations,payments}` | 401, 404 |
| `GET /v1/me/bills?months=1..24` | — | `[BillSummary]` (`id, cycle, total, subtotal, vat, currency, status, amount_paid, outstanding, issued_at, due_date`) | 401 |
| `GET /v1/me/bills/{cycle}` | — | BillSummary + `lines[], usage, vat_rate, plan_code` | 404 |
| `GET /v1/me/bills/{cycle}/pdf` | — | `application/pdf` (embedded DejaVu Sans, seller block); the web route serves it `inline`, opened in a new tab | 404 |
| `GET /v1/me/bills/{cycle\|current}/explanation?lang=en\|de\|pl&ai=true\|false` | — | `{explanation, narrative, provider, partial}`. `ai=false` = deterministic only (fast SSR path, no budget used) | 404 |
| `POST /v1/me/notifications/{id}/read` | — | 204 | — |
| `GET /v1/me/events` | — | SSE: `notification` events, `ping` every 15 s | 401 |
| `GET /v1/me/usage/scenarios` | — | `[{key,title}]` | — |
| `GET /v1/me/usage/sample.csv` | — | CSV template | — |
| `POST /v1/me/usage/simulate?lang=` | multipart `file` (CSV/JSON, ≤1 MB) **or** form `scenario` | `{source, scenario_title, compared_to, baseline, bill, explanation, llm, status, sources}` | 422 invalid usage (friendly message per problem) |
| `POST /v1/me/payments` | header `Idempotency-Key` (8–64), `{purpose: BILL\|ADVANCE, bill_id?}` | 201 `{payment_id,status,amount,currency,charged_amount,charged_currency,fx_rate,purpose,bill_id,gateway,order_id,checkout{provider,key_id,order_id}}`; same key or an open order for the same bill/advance → same order. BILL amount = outstanding minus captured-but-unapplied payments; ADVANCE = usage so far minus advances paid this month | 404 bill not found/not yours · 409 nothing to pay / already paid · 503 payments disabled/not configured/breaker open/no fx rate · 502 gateway rejected |
| `GET /v1/me/payments/advance-due` | — | `{cycle, currency, usage_total, already_paid, amount}` | 401 |
| `POST /v1/me/payments/{id}/confirm` | `{gateway_payment_id, signature}` | payment (status `PAID`) or `{status: PENDING}` if not captured yet | 400 bad signature · 404 |
| `GET /v1/me/payments/{id}?refresh=true\|false` | — | `{payment_id,status,purpose,cycle,amount,currency,charged_*,fx_rate,gateway_status,status_check_delayed,…}`; `refresh=true` asks the gateway's status API unless PAID (stored status if the gateway is down) | 404 |
| `POST /v1/webhooks/razorpay` | raw body, `X-Razorpay-Signature` | `{ok, settled}` / `{ok, ignored}` | 401 bad signature · 503 not configured |
| `POST /v1/admin/bill-runs/{cycle}?settle_before=` | admin JWT | `{created, skipped_existing}` | 403, 409 cycle not over |
| `GET /v1/admin/pipeline` | admin JWT | per-partition offsets and outbox depth | 403 |
| `POST /v1/auth/refresh`, `POST /v1/auth/logout` | refresh token | rotated tokens / 204 (revokes the refresh token) | 401 |
| `GET /v1/me/bills/{cycle}/evidence` | — | raw CDRs re-summed from the archive, reconciled with Valkey and the bill | 429 >30 scans/hour |
| `GET /v1/me/usage/daily?cycle=` | — | per-day usage for the trend chart (current month by default) | 422 future month |
| `POST /v1/me/orders/addon` · `POST /v1/me/orders/plan` | `{addon_code}` · `{plan_code}` | 201 PROPOSED order with preview; retires the customer's earlier open offer of the same kind (EXPIRED) | 402 bill overdue · 422 not available |
| `GET /v1/me/orders/plans/available` | — | `{current, currency, plans[]}` (My plan page) | — |
| `POST /v1/me/orders/{id}/approve` · `/decline` | — | order (approve starts the Temporal workflow) | 404 · 410 offer expired · 503 workflow not started (rolled back) |
| `GET /v1/me/orders`, `GET /v1/me/orders/{id}` | — | orders / one order | 404 |
| `POST /v1/me/assistant/stream` (SSE) · `POST /v1/me/assistant/ask` | `{question, session_id?, lang?, cycle?}` | events `step`, `step_done`, `proposal`, `token`, `done` · final answer | 429 daily limit · 503 switched off / unavailable |
| `GET /v1/me/assistant/sessions`, `GET` / `DELETE /v1/me/assistant/sessions/{id}`, `GET /v1/me/assistant/traces` | — | chat list / one chat (messages with `at` times, sources, cards) / delete / recent traces | 404 |
| `GET /v1/system/overview`, `GET /v1/system/assistant-quality` | — | System page: cluster-wide aggregates; assistant quality trend, eval runs, judge calibration | 401 |
| `GET /healthz`, `GET /readyz`, `GET /metrics` | — | liveness / readiness (Valkey + DB) / Prometheus | 503 degraded |

> Payments routes are served by `payments-service`, assistant routes by `assistant-service`, orders and the rest by `core-api` (same code base). No endpoint issues admin tokens today (role claim exists); bill runs run as the `bill-run` CronJob/CLI.

### usage-collector

| Method & path | Request | Response |
|---|---|---|
| `POST /v1/usage-batches` | headers `Authorization: Bearer <source token>`, `X-Source`; body `UsageBatch{batch_id, source, records[1..5000]}` | **202** `{status: accepted, records, messages}` · **200** `{status: duplicate}` · 401 bad token · 403 source mismatch · **409** batch in flight · **503** + `Retry-After: 5` stream unavailable · 422 validation |
| `GET /healthz`, `/readyz` (Valkey ping), `/metrics` | | |

`UsageRecord`: `record_id (8–128)`, `subscriber_id`, `country (DE|PL)`, `kind (USAGE|PLAN_CHANGED)`, `usage_type (DATA bytes|VOICE s|SMS count|ROAMING_DATA bytes|ROAMING_VOICE s|INTL_VOICE s)`, `quantity ≥0`, `roaming_zone (required for roaming)`, `addon_code (required for PLAN_CHANGED)`, `event_time (tz-aware, normalised to UTC)`. Billing cycle = `YYYY-MM` of **event time** (UTC).

Screening quarantines individual records (rest of the batch accepted): invalid schema, `event_time` > 10 min in the future, unknown add-on code, unknown roaming zone, `quantity > 10¹³`, and `PLAN_CHANGED` from a source that isn't an order source. Each remaining record id is claimed with `SET rec:<id> pending NX EX 300`; a taken claim is a duplicate. After the broker ack the claims become `rec:<id>` for 7 days; if publishing fails they are released so the retry passes.

## 3. Data model (Autonomous DB, schema SELFCARE)

![Data model](diagrams/data-model.png)

| Table | Owner module | Constraints / indexes | Notes |
|---|---|---|---|
| subscribers | auth/CRM seed | PK id | Seeded from `telco_common.demo` when `APP_SEED_DEMO=true`; seed also writes Valkey `prof:` |
| bills | billing (bill run) | PK `sub:cycle`, UQ `(subscriber_id, cycle)`, IX `ix_bills_sub_cycle` | Insert-if-absent = idempotent bill run; immutable; `rated_counters` = the CDR totals it was priced from (late-usage adjustments) |
| payments | payments-service | UQ `gateway_order_id`, UQ `(subscriber_id, idempotency_key)` | `amount/currency` = ledger (bill currency); `charged_*` = what the gateway took; reconciler columns `gateway_status`, `check_attempts`, `next_check_at`, `last_checked_at`, `settled_by` |
| bill_payments | billing | PK `payment_id` | Which payment was credited to which bill (bill payments via `payment-events`, advances at the bill run); makes crediting idempotent |
| notifications | notifier | UQ `alert_id`, IX `(subscriber_id, created_at)` | UQ makes alert delivery idempotent |
| explanations | AI narrator (`services/ai.py`) | PK cache_key | `sha256(facts, lang, provider, model)`. The bill explanation (`agent/explain_llm.py`) is cached in Valkey instead: `exp:<sha256(sources, lang, model, prompt version, served prompt text, subject)>`, 30 days, so a new Langfuse prompt version never reuses old explanations |
| outbox | payments, orders → notifier relay | — | Written in the same transaction as settlement (`payment-events`) or an order status change (`order-events`); `published_at` set after broker ack |
| orders | orders (core-api, workflows) | PK `id` (= Temporal workflow id), IX `(subscriber_id, created_at)` | `kind` ADDON \| PLAN; status PROPOSED → APPROVED → SUBMITTED → COMPLETED/FAILED, DECLINED, EXPIRED; one open offer per kind (a new proposal expires the older open one: "Replaced by a newer offer") |
| bss_orders | order-adapter (legacy emulator) | PK `id`, UQ `order_id` | SUBMITTED / CONFIRMED / REJECTED |
| plan_changes | orders (tariff history) | PK `order_id`, IX `(subscriber_id, effective_at)` | Drives pro-rating and `plan_at` |
| context_docs | assistant (L1 spine) | PK `id`, IX `(subscriber_id, doc_type)` | `content_hash`, `embedding_json` + Oracle `VECTOR(384)` column (migration 0004) |
| agent_traces | assistant | PK `id` | One row per answer: status, answer, trace JSON, cost |
| chat_sessions / chat_messages | assistant-service | PK `id`; IX `(subscriber_id, updated_at)` / `(session_id, id)` | Chat history; messages carry `created_at` (served as `at`) and citations (`{sources, cards}`) |
| answer_judgements | assistant (sampled policy judge) | PK `trace_id` (= `agent_traces.id`), IX `created_at` | PASS / FAIL / ERROR, violated rule ids, judge model + version, cost; 25% of answers |
| eval_runs | `run-evals` | PK `id`, IX `(kind, created_at)` | `golden` or `calibration`; prompt and judge version, passed, metrics + cases JSON |
| schema_migrations | db.create_schema | — | Forward-only migrations `0002`–`0007` (settlement currency, backfill-notification purge, context-doc vector, `rated_counters`, payment reconciliation, `0007_expire_superseded_offers`) |

The schema is created at startup (`create_all` + migrations). The app user `SELFCARE` is created by the Helm `db-init` pre-install/pre-upgrade Job (grants: CREATE SESSION/TABLE/SEQUENCE/VIEW, quota 5G).

## 4. Valkey key schema

All per-partition keys carry the hash tag `{pN}` (N = stream partition), so one message's keys live in one cluster slot.

| Key | Type | Written by | TTL | Content |
|---|---|---|---|---|
| `off:{pN}` | string | apply.lua | — | Last applied stream offset of partition N |
| `seen:{pN}:<msg_id>` | string | apply.lua | 7 days | Message already applied |
| `alerts:{pN}` | stream (MAXLEN ~1,000,000) | apply.lua | — | Outbox: `alert_id, subscriber_id, country, cycle, level, used_bytes, allowance_bytes, pct`; consumer group `relay` |
| `u:{pN}:<sub>:<cycle>` | hash | apply.lua | 400 days | Counters `DATA`, `VOICE`, `SMS`, `INTL_VOICE`, `ROAMING_DATA:<zone>`, `ROAMING_VOICE:<zone>`, `addon:<code>`, `addon_bytes`; flags `alert:<level>` = offset that raised it |
| `prof:{pN}:<sub>` | hash | core-api seed | — | `plan_code, country, allowance_bytes` |
| `batch:<source>:<batch_id>` | string | collector | `inflight` 120 s → `done` 7 days | Batch idempotency (batch ids are unique per source only; unscoped keys written before the change are still honoured) |
| `rec:<record_id>` | string | collector screening | `pending` 300 s → 7 days | Record-level claim (`SET NX`) |
| `rl:login:<sub>` / `rl:login-fail-ip:<ip or /64>` / `rl:ai:<sub>` / `rl:ai-whatif:<sub>` | counter | core-api | 900 s / 900 s / 86,400 s / 86,400 s | Fixed-window limits: 10 attempts per account, 20 failed logins per public IP (non-public addresses are ignored), 20 bill explanations and 10 what-if explanations per day |
| `rl:ask:<sub>` / `rl:evidence:<sub>` | counter | assistant-service / core-api | 86,400 s / 3,600 s | 200 assistant questions a day (abuse guard), 30 CDR evidence re-scans an hour |
| `pay-lock:<sub>:<bill_id or advance>` | string | core-api | 15 s | Single flight when starting a payment |
| `dlq:{pN}` | list (trim 10,000) | aggregator | — | Undecodable (poison) messages and messages that can never apply |
| `notif:<sub>` | pub/sub channel | notifier → core-api SSE | — | Live notification push |

### `apply.lua` (one call per stream message)
1. `offset ≤ GET off:{p}` → return `stale_offset` (crash replay).
2. `EXISTS seen:{p}:msg` → `SET off:{p}=offset`, return `duplicate_message` (re-published chunk).
3. For each (subscriber, cycle) group: `HINCRBY` each field with the exact integer string as sent (no `tonumber`, which made doubles and failed on large values mid-message); `EXPIRE` 400 days.
4. `used = DATA + ROAMING_DATA:EU`; `allowance = prof.allowance_bytes + addon_bytes`.
5. For each level (from flag `usage-alert-thresholds`): if `pct ≥ level` and `HSETNX alert:<level>` succeeds → `XADD alerts:{p}` with `alert_id = <sub>:<cycle>:<level>:<offset>` (one id per arming, so a re-armed level alerts again; a replay gives the same id). If an add-on raised the allowance and `pct < level` → `HDEL alert:<level>` (re-arm).
6. `SET seen:{p}:msg EX 7d`; `SET off:{p}=offset`; return `applied, alerts`.

The consumer seeks to `off:{p}+1` on partition assignment. Broker group offsets are committed only for lag visibility. A message that raises anything other than a Valkey outage goes to `dlq:{p}` instead of crashing the pod. A tariff change updates `prof:` (allowance) and, after an upgrade, clears the alert levels the customer is now below (`UsageState.rearm_alerts`).

## 5. Streams and contracts

| Topic | Partitions | Key / partition | Payload | Consumer groups |
|---|---|---|---|---|
| `usage-events` | 4 (fixed) | explicit `crc32(subscriber_id) % 4` | gzip of newline-delimited `UsageRecord` JSON, ≤2,000 records, ≤900 KB; headers `msg_id = sha256(source:batch_id|partition|chunk)[:32]`, `batch_id` | `usage-aggregator`, `raw-archiver` |
| `usage-alerts` | 1 | key = subscriber_id; header `alert_id` | `{"alert_id":"<sub>:<cycle>:<level>:<offset>","subscriber_id","country","cycle","level","used_bytes","allowance_bytes","pct"}` | `notifier` |
| `payment-events` | 1 | key = subscriber_id | `{"type":"PAYMENT_SUCCEEDED","payment_id","subscriber_id","amount","currency","purpose","bill_id","gateway","gateway_payment_id"}` | `billing` (commits only applied events, re-reads a failed one); downstream: ERP, analytics |
| `order-events` | 1 | key = subscriber_id (via the DB outbox, same tx as the status change) | `{"type":"ORDER_STATUS_CHANGED","order_id","subscriber_id","kind","offering","status","detail","at"}` | `order-projections` (notifier: home cache, assistant context, notification on terminal states) |
| `bss-events` | 1 | key = order_id (published by order-adapter) | `{"type":"BSS_ORDER_RESULT","bss_order_id","order_id","state" (TMF622),"result","reason","at"}` | `order-workflows` (workflow-worker → Temporal signal) |

Retention: 48 h. Raw archive objects: `raw/usage-events/dt=YYYY-MM-DD/p=<p>/<first>-<last>.jsonl.gz` (flush at 4 MB or 60 s). Archive delivery is at-least-once, and reconciliation dedupes by `record_id`. Notifier skips alerts for cycles earlier than the current month (backfill replay).

## 6. Rating engine (`billing.py`, `catalog.py`)

| Plan | Country | Fee (net) | Data | Voice / SMS | Overage | Intl/min |
|---|---|---|---|---|---|---|
| DE-S Mobile S | DE (EUR, VAT 19%) | 19.95 | 10 GB | unlimited | THROTTLE | 0.29 |
| DE-M Mobile M | DE | 34.95 | 30 GB | unlimited | THROTTLE | 0.19 |
| DE-FLEX Mobile Flex | DE | 9.95 | 3 GB | 200 min / 100 SMS | CHARGE 0.009/MB, 0.09/min, 0.09/SMS | 0.39 |
| PL-S Mobile S | PL (PLN, VAT 23%) | 35.00 | 15 GB | unlimited | THROTTLE | 0.99 |
| PL-FLEX Mobile Flex | PL | 25.00 | 5 GB | 300 / 300 | CHARGE 0.035/MB, 0.29/min, 0.19/SMS | 1.49 |

Roaming (non-EU): DE Z2 0.0199/MB, 0.49/min; Z3 0.0999/MB, 1.99/min. PL Z2 0.086/MB, 1.99/min; Z3 0.43/MB, 8.99/min. Add-ons: DE-DATA-5GB 9.99, DE-DATA-20GB 19.99, PL-DATA-5GB 25.00.

Rules:
- Money is in integer minor units. Per-unit rates are Decimal. Each line rounds half-up.
- Data is rounded **up to the started MB** (1 MB = 10⁶ B), voice up to the started minute (aggregate seconds).
- **EU roam-like-at-home:** `ROAMING_DATA:EU` and `ROAMING_VOICE:EU` count against the domestic allowance at no extra charge.
- Unknown roaming zones (normally quarantined at ingest) are priced as the plan's most expensive zone instead of failing.
- Allowance = plan data + Σ add-on MB. Across a tariff switch, an add-on is charged in the segment it was bought in and its unused volume carries into the following segments. Overage is charged only when the policy is `CHARGE`. With `THROTTLE` the network slows down at 100% instead.
- VAT = round_half_up(subtotal × rate). Total = subtotal + VAT.
- The same `rate()` prices the unbilled running total, the issued bill and simulations. Bills and late-usage adjustments use the tariff in force in that month (`plan_at`).
- A customer who joined during the month and has no earlier bill pays fee and allowances from the join day (`first_billable_day`).
- **Overdue** = unpaid after the **end** of the due day (due dates are stored as 00:00 UTC). One rule for web pages, PDF, assistant tools and order blocking.

**Explainer (`explainer.py`):**
- Drivers are line-level deltas between the current and previous bill, sorted by |Δ| amount. Each has a quantity comparison as its detail.
- Top usage drivers are the non-plan lines by amount, with their share of variable charges.
- Usage changes (data, calls, international calls, non-EU roaming) are reported as a % change.
- Facts are sentences built only from these numbers. Direction is `UP`, `DOWN`, `SAME` or `FIRST_BILL`.

**AI grounding check (`agent/explain_llm.py`, `agent/loop.py`):**
1. Extract every number token from the sources (bill explanation: the cited sources of each item; assistant: all
   tool results of the turn).
2. Normalise each token by removing separators. Allow both the full number and its integer part.
3. Every number of two or more digits in the answer must be in that allowed set.
4. If any number fails, the answer is repaired (up to 2 times, the assistant through its tool loop); if it still
   fails, nothing unverified is shown (bill page: "not available", assistant: an apology).

## 7. Feature flags (flagd, `deploy/helm/selfcare/values.yaml`)

| Key | Variants (default **bold**) | Safe fallback if flagd is unreachable | Controls |
|---|---|---|---|
| `ai-explanations` | **on** / off | `false` | LLM narration at all (bill explanations and what-if simulations) |
| `ai-provider` | **deepseek** / mistral / selfhosted / template | `deepseek` | Which LLM (per country for GDPR) |
| `payments-enabled` | **on** / off | `true` | Kill switch for payments |
| `payment-gateway` | **razorpay** / cashfree | `razorpay` | Gateway adapter (only Razorpay implemented) |
| `payment-settlement` | native / **inr** (`fx: EUR 90.0, PLN 21.0`) | `native` | Gateway charge currency (UPI is INR-only) |
| `usage-alert-thresholds` | **standard** [50,80,100] / strict [50,75,90,100] | [50,80,100] | Alert levels (cached 30 s in the aggregator) |
| `throttle-at-100` | **on** / off | `true` | Reserved for the policy integration |
| `demo-accounts-panel` | on / **off** | `false` | `GET /v1/demo/accounts`: public per-demo-line summary (no longer shown on the login page) |

Every evaluation sends `targetingKey = subscriber`, attribute `country`.

## 8. Kubernetes resources (Helm chart `selfcare`, namespace `selfcare`)

| Resource | Replicas / schedule | Notes |
|---|---|---|
| Deployment `usage-collector` + Service + HPA 1–6 | 1 | 25m/192Mi |
| Deployment `usage-aggregator` | 2 | ≤ partitions |
| Deployment `raw-archiver`, `alert-relay`, `notifier` | 1 each | |
| Deployment `core-api` + Service + HPA 2–8 + PDB | 2 | readiness = Valkey + DB |
| Deployment `payments-service` + Service + HPA 2–6 + PDB | 2 | checkout, confirm, webhook, reconciler (core-api image) |
| Deployment `assistant-service` + Service + HPA 1–6 | 1 | tool-calling agent, SSE streams, sampled policy judge |
| Deployment `order-adapter` + Service + HPA 1–4 | 1 | TMF622 ProductOrder API, country adapters, `bss-events` |
| Deployment `workflow-worker` | 1 | Temporal worker + `bss-events` consumer |
| StatefulSet `temporal` (temporalio/temporal 1.10.0, dev server, SQLite on a 50Gi PVC) + Service :7233 / UI :8233 | 1 | Order workflows, per-country legacy task queues |
| Deployment `llm-gateway` (LiteLLM) + Service + HPA 2–4 + PDB | 2 | Aliases `billing-llm`, `judge-llm`, `title-llm` with fallbacks; the only holder of provider keys |
| Deployment `web` + Service + HPA 2–6 + PDB | 2 | UID 1000, Express `server.mjs` |
| Deployment `flagd` (v0.18.2) + Service :8013 | 1 | ConfigMap `flagd-flags`, hot reload |
| Deployment `network-emulator-live` | 1 | Live demo traffic every 60 s |
| StatefulSet `valkey` (valkey 8.1) + PVC 50Gi `oci-bv` | 1 | AOF everysec, `noeviction`, maxmemory 1gb |
| Job `db-init` | pre-install/upgrade hook | Creates SELFCARE user |
| Job `prompt-seed` | post-install/upgrade hook | Publishes the repo prompts to Langfuse (label `production`) when their text changed; never fails a deploy |
| Job `demo-backfill` | post-install hook | 12-month backfill → bill run (`--wait-quiet-seconds 30`, settle all but the last cycle) |
| CronJob `bill-run` | `0 2 1 * *` | Close the previous cycle (init container), then `explain-batch` warms caches and pre-generates explanations |
| SecretStore `oci-vault` + ExternalSecrets `selfcare-secrets`, `ocir-pull` (hooks), `selfcare-<third-party>` (from `thirdPartySecrets`) | | Instance-principal auth |
| Ingress `selfcare` (Traefik) + Issuer `letsencrypt` | | Routes `/` → web, `/v1/webhooks/razorpay` → payments-service; Traefik `ratelimit` middleware per client IP |
| Pod `selfcare-smoke` | `helm test` | 19 checks |

All pods: `enableServiceLinks: false`, non-root, read-only root FS, capabilities dropped, seccomp RuntimeDefault, `imagePullSecrets: ocir-pull`.

Cluster add-ons (`cluster-addons.sh`): External Secrets, cert-manager, metrics-server, Traefik (LB annotations flexible 10/10 Mbps, http→https redirect with ACME bypass), RoleBinding `admin` in `selfcare` for the CI user only.

## 9. CI/CD (`.github/workflows/pr.yml`, `.github/workflows/deploy.yml`)

![CI/CD pipeline](diagrams/cicd.png)

`main` is protected. **pr-validation** (`pr.yml`, every PR): ruff + pytest, web typecheck + build, helm lint + render +
kubeconform, terraform fmt + validate, image builds (6: core-api, usage-collector, usage-aggregator, network-emulator,
web, llm-gateway; no push), gitleaks. **deploy-prod** (`deploy.yml`, push to `main`): test → images ×6 → OCIR
(`:sha` + `:latest`) → when `DEPLOY_ENABLED`: helm upgrade → `helm test` smoke → public endpoint check → Playwright e2e
on the live site (+ Razorpay checkout) → assistant eval gate (`kubectl exec deploy/core-api -- run-evals --gate --calibrate`).

## 10. Testing strategy

| Layer | What | Where |
|---|---|---|
| Unit + API | **163 tests**: rating (rounding, VAT, roaming, add-ons, PLN), explainer, codec, partitioning, Lua apply (replay, re-publish, add-on re-arm, cycle rollover), collector publishing, API (auth, IDOR, idempotent payments, dual-path settlement, webhook signature, simulation, INR settlement), AI (grounding, cache, breaker, no PII), assistant tools and approval cards, policy judge, orders and workflows, pro-rating, PDF | `libs/*/tests`, `services/*/tests` (SQLite + fakeredis) |
| In-cluster smoke | 19 checks against live services after each deploy | `core_api.smoke`, `helm test` |
| Assistant eval gate | 17 golden questions (grounded, required facts, tool use, policy judge) + judge calibration against 32 human labels (kappa ≥ 0.6 to gate) | `core_api.evals` (`run-evals --gate --calibrate`), last deploy step |
| Load | 3k and 15k rec/s through the real stream | `deploy/loadtest/job.yaml`, [LOADTEST.md](LOADTEST.md) |
| Browser e2e | Playwright on the live site, desktop + mobile: wrong password, the customer journey (every page), Razorpay checkout (`@payment`), new deployment reaching an open tab; outage fallbacks (`@outage`) in the separate chaos workflow | `web/e2e/journey.spec.ts`, `web/e2e/outage.spec.ts` |

Known gap: SQLite doesn't enforce VARCHAR lengths, so a test asserts the cycle shape explicitly. Oracle-specific behaviour is covered by the smoke test.
