# Operations: inspecting the running system

Everything below runs against the live OCI deployment (region `ap-mumbai-1`, compartment `dt-selfcare`).

## 0. Prerequisites
```bash
kubectl config use-context dt-selfcare          # created by deploy/scripts/cluster-addons.sh
kubectl -n selfcare get pods                    # every workload
export SUPPRESS_LABEL_WARNING=True              # quieter oci cli
VAULT=$(grep vaultId deploy/helm/values-oci.yaml | awk '{print $2}')
CPT=$(cd deploy/terraform && terraform output -raw compartment_id)
```
Read any secret value yourself (it's never printed by scripts):
```bash
secret() { oci secrets secret-bundle get-secret-bundle-by-name --secret-name "dt-selfcare-$1" --vault-id "$VAULT" \
  --query 'data."secret-bundle-content".content' --raw-output | base64 -d; echo; }
secret demo-password      # web login for the demo lines
secret db-app-password    # DB user SELFCARE
```

## 1. Database (Autonomous DB `dtselfcare`)

**Console:** OCI Console → Oracle Database → Autonomous Database → compartment **dt-selfcare** → **dt-selfcare-adb** → **Database actions → SQL**. The ACL only allows our VCN, so add your IP temporarily under *Network → Access control list* if the console SQL tool needs it. Sign in as **SELFCARE** with `secret db-app-password`.

Useful queries:
```sql
-- 1. Customers and plans
SELECT id, name, country, plan_code FROM subscribers ORDER BY id;

-- 2. 12-month bill history for the traveller persona
SELECT cycle, currency, total/100 AS total, amount_paid/100 AS paid, status, due_date
FROM bills WHERE subscriber_id = '4915100000002' ORDER BY cycle;

-- 3. Unpaid bills (Bill pending)
SELECT subscriber_id, cycle, (total - amount_paid)/100 AS outstanding, currency, due_date
FROM bills WHERE status <> 'PAID' ORDER BY due_date;

-- 4. Bill line items (stored JSON)
SELECT b.cycle, j.code, j.label, j.quantity, j.unit, j.amount/100 AS amount
FROM bills b, JSON_TABLE(b.payload, '$.lines[*]' COLUMNS (code VARCHAR2(40) PATH '$.code',
  label VARCHAR2(80) PATH '$.label', quantity NUMBER PATH '$.quantity', unit VARCHAR2(10) PATH '$.unit',
  amount NUMBER PATH '$.amount')) j
WHERE b.id = '4915100000002:2026-10';

-- 5. Payments, incl. INR settlement for UPI (bill currency vs charged currency)
SELECT id, subscriber_id, purpose, bill_id, amount/100 AS bill_amt, currency,
       charged_amount/100 AS charged, charged_currency, fx_rate, gateway_order_id, status, created_at
FROM payments ORDER BY created_at DESC;

-- 6. Idempotency at work: one row per (subscriber, idempotency_key)
SELECT subscriber_id, idempotency_key, COUNT(*) FROM payments GROUP BY subscriber_id, idempotency_key;

-- 7. Usage alerts delivered (exactly one per subscriber/cycle/level and arming; alert_id = sub:cycle:level:offset)
SELECT subscriber_id, alert_id, kind, title, created_at FROM notifications ORDER BY created_at DESC;

-- 8. AI explanation cache: which provider answered
SELECT subscriber_id, cycle, provider, SUBSTR(text, 1, 120) AS preview, created_at
FROM explanations ORDER BY created_at DESC;

-- 9. Transactional outbox backlog (payment events not yet published)
SELECT id, topic, event_key, created_at, published_at FROM outbox ORDER BY id DESC;

-- 10. Schema migrations applied
SELECT * FROM schema_migrations;

-- 11. Which payment was credited to which bill (bill payments via payment-events, advances at the bill run)
SELECT bp.payment_id, bp.bill_id, bp.amount/100 AS amount, bp.applied_at, p.purpose
FROM bill_payments bp LEFT JOIN payments p ON p.id = bp.payment_id ORDER BY bp.applied_at DESC;

-- 12. Captured but not yet credited (should be empty after ~2 min for bills; advances wait for the bill run)
SELECT id, purpose, bill_id, amount/100 AS amount, updated_at FROM payments
WHERE status = 'PAID' AND id NOT IN (SELECT payment_id FROM bill_payments);

-- 13. Orders (one open offer per kind; older offers EXPIRED "Replaced by a newer offer")
SELECT id, subscriber_id, kind, addon_code, status, detail, source, created_at FROM orders ORDER BY created_at DESC;

-- 14. Assistant quality: sampled policy-judge verdicts and eval-gate runs
SELECT trace_id, verdict, violations, reason, judge_version, created_at FROM answer_judgements ORDER BY created_at DESC;
SELECT kind, prompt_version, judge_version, passed, created_at FROM eval_runs ORDER BY created_at DESC;
```

## 2. Valkey (usage state)
```bash
kubectl -n selfcare exec -it valkey-0 -- valkey-cli
```
The partition of a subscriber is `crc32(id) % 4`. Demo lines: 4915100000001→3, 4915100000002→1, 4915100000003→3, 4915100000004→0, 48600000001→2, 48600000002→0. To compute any other:
```bash
python3 -c "import zlib,sys; print(zlib.crc32(sys.argv[1].encode()) % 4)" 4915100000002
```
Inside `valkey-cli`:
```text
MGET off:{p0} off:{p1} off:{p2} off:{p3}            # last applied offset per partition
HGETALL u:{p1}:4915100000002:2026-11                 # running counters for this cycle (+ alert:<level> flags)
HGETALL prof:{p1}:4915100000002                      # plan_code, country, allowance_bytes
XLEN alerts:{p3}                                     # outbox depth
XRANGE alerts:{p3} - + COUNT 5                       # recent alerts raised
XINFO GROUPS alerts:{p3}                             # relay group: pending / last delivered
SCAN 0 MATCH u:{p1}:* COUNT 100                      # usage keys on a partition
SCAN 0 MATCH batch:* COUNT 20                        # ingest idempotency keys (batch:<source>:<batch_id>)
SCAN 0 MATCH rec:* COUNT 20                          # record-id claims (pending while publishing, then 7 days)
TTL u:{p1}:4915100000002:2026-11                     # ~400 days
LLEN dlq:{p0}                                        # poison / unappliable messages (should be 0)
DBSIZE
INFO memory
```
Values are bytes (data) or seconds (voice). `ROAMING_DATA:EU` counts toward the allowance, `ROAMING_DATA:Z2` is charged.

## 3. OCI Streaming (Kafka API)
**Console:** Analytics & AI → Messaging → Streaming → compartment **dt-selfcare** → stream pool **dt-selfcare-pool** → stream **usage-events** (4 partitions) / **usage-alerts** / **payment-events** / **order-events** / **bss-events** → **Load messages**. Usage messages are **gzip** (binary in the console), and alerts are plain JSON.

CLI (alerts are readable directly):
```bash
STREAM=$(oci streaming admin stream list --compartment-id "$CPT" --name usage-alerts --query 'data[0].id' --raw-output)
EP=$(oci streaming admin stream get --stream-id "$STREAM" --query 'data."messages-endpoint"' --raw-output)
CUR=$(oci streaming stream cursor create-cursor --stream-id "$STREAM" --endpoint "$EP" --partition 0 --type TRIM_HORIZON --query 'data.value' --raw-output)
oci streaming stream message get --stream-id "$STREAM" --endpoint "$EP" --cursor "$CUR" --limit 5 \
  --query 'data[].value' --raw-output | while read v; do echo "$v" | base64 -d; echo; done
```
For `usage-events`, decode a message with `base64 -d | gunzip` to see the JSONL `UsageRecord`s. Consumer groups: `usage-aggregator`, `raw-archiver`, `notifier` (usage-alerts), `billing` (payment-events), `order-projections` (order-events), `order-workflows` (bss-events). The aggregator's truth offset is `off:{pN}` in Valkey; compare it with the stream's latest offset for lag.

## 4. Raw archive (Object Storage)
```bash
NS=$(oci os ns get --query data --raw-output)
oci os object list -bn dt-selfcare-usage-raw -ns "$NS" --prefix raw/usage-events/ --query 'data[-5:].name'
oci os object get -bn dt-selfcare-usage-raw -ns "$NS" --name '<object name>' --file - | gunzip -c | head
```
Lifecycle moves objects to Archive after 30 days and deletes them after 400.

## 5. Logs, metrics, tests
```bash
kubectl -n selfcare logs deploy/usage-aggregator --tail=50          # partitions_assigned, poison_message
kubectl -n selfcare logs deploy/notifier | grep notified            # alerts delivered
kubectl -n selfcare logs deploy/notifier | grep -E 'worker_thread_died|payment_apply_failed|outbox_delivery_failed'
kubectl -n selfcare logs deploy/payments-service | grep -E 'reconcile_failed|payments_applied_by_sweep'
kubectl -n selfcare logs job/<bill-run job> -c bill-run | grep -E 'bill_failed|"failed"'  # customers the run couldn't bill (the rest are billed)
kubectl -n selfcare logs -l app=core-api | grep -E 'llm_|ungrounded' # AI fallbacks
kubectl -n selfcare logs -l app=core-api | grep unhandled_error      # server errors with request_id
kubectl -n selfcare logs -l app=web --tail=50                        # SSR access log
kubectl -n selfcare port-forward deploy/core-api 8080 & curl -s localhost:8080/metrics | grep -E '^(ai_|http_|circuit)'
kubectl -n selfcare port-forward deploy/usage-aggregator 9000 & curl -s localhost:9000/metrics | grep aggregator_
helm test selfcare -n selfcare --logs                                # 19-check smoke test
kubectl -n selfcare exec deploy/core-api -- run-evals --gate --calibrate  # assistant eval gate (last deploy step)
```
Key metrics:
- `aggregator_messages_total{result}`, `aggregator_records_total`, `aggregator_alerts_raised_total`, `aggregator_event_lag_seconds`, `aggregator_apply_seconds`
- `collector_batches_total{result}`, `collector_publish_seconds`
- `relay_alerts_published_total`, `notifier_notifications_total`, `notifier_duplicates_total`, `outbox_published_total`
- `ai_narration_total{provider,outcome}`, `ai_tokens_total`, `circuit_breaker_open{name}`, `http_request_seconds`

Load test: edit `--rate` in `deploy/loadtest/job.yaml` (replace `REGISTRY`, `IMAGE_TAG` and `RATE`), then run `kubectl -n selfcare apply -f -`.

## 6. Feature flags (runtime, no redeploy)
```bash
kubectl -n selfcare edit cm flagd-flags     # flagd watches the file and hot-reloads
```
Change `defaultVariant`:
- `ai-provider` → `template` (no LLM) or `mistral`
- `usage-alert-thresholds` → `strict` (50/75/90/100)
- `payments-enabled` → `off` (kill switch)
- `payment-settlement` → `native` (charge the bill currency; needs international payments on the gateway account) or `inr` (demo default: Razorpay test mode is INR-only for us)

Durable change: edit `flags:` in `deploy/helm/selfcare/values.yaml` and push. CI redeploys.

## 7. Secrets and cost
- Add or rotate third-party keys: `./deploy/scripts/put-secrets.sh`. Then restart the pods that read them as env vars: `kubectl -n selfcare rollout restart deploy/llm-gateway` (DeepSeek, Mistral, Langfuse: provider keys live only in the gateway) and `deploy/payments-service deploy/assistant-service deploy/core-api` (Razorpay, Langfuse prompt keys).
- Auth keys (JWT RS256 key pair, web `session-secret`, `order-source-token`): `./deploy/scripts/auth-keys.sh` creates whichever is missing; `--rotate` makes new versions of all, then `kubectl -n selfcare rollout restart deploy` once External Secrets has synced (≤ 5 min). Rotation signs everyone out once.
- Park the platform (DB, streams and data are kept):
  ```bash
  cd deploy/terraform && terraform apply -var node_count=0
  ```
  Use `node_count=1` to resume.
