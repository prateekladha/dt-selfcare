#!/usr/bin/env bash
# Local web app (your working copy, hot reload) against the PROD backend services, over kubectl port-forward.
#   ./deploy/scripts/dev-against-prod.sh      -> http://localhost:5173
# Real data, real assistant, real Razorpay test checkout. Only the web tier (React Router/BFF) runs locally:
# backend changes still go through tests + CI deploy (local backend code is never pointed at prod data).
# Actions are real: a payment or an add-on order you make here happens on the live demo account.
set -euo pipefail
export SUPPRESS_LABEL_WARNING=True
NS=selfcare
cd "$(dirname "$0")/../../web"

forward() {  # keep a port-forward alive (they drop on pod restarts / idle)
  local svc=$1 port=$2
  while true; do kubectl -n "$NS" port-forward "svc/$svc" "$port:8080" >/dev/null 2>&1 || true; sleep 1; done
}
forward core-api 18080 & P1=$!
forward payments-service 18081 & P2=$!
if [[ -n "${PREVIEW_ASSISTANT:-}" ]]; then   # unmerged agent code: see deploy/preview/assistant-preview.sh
  ( while true; do kubectl -n "$NS" port-forward pod/assistant-preview 18082:8080 >/dev/null 2>&1 || true; sleep 1; done ) & P3=$!
  echo "assistant -> PREVIEW pod (working-copy agent code)"
else
  forward assistant-service 18082 & P3=$!
fi
trap 'kill $P1 $P2 $P3 2>/dev/null; pkill -f "port-forward (svc/(core-api|payments-service|assistant-service)|pod/assistant-preview)" 2>/dev/null || true' EXIT

for i in $(seq 1 30); do curl -sf -o /dev/null http://127.0.0.1:18080/readyz && break; sleep 1; done
echo "prod services forwarded: core-api :18080, payments :18081, assistant :18082"
CORE_API_URL=http://127.0.0.1:18080 PAYMENTS_URL=http://127.0.0.1:18081 ASSISTANT_URL=http://127.0.0.1:18082 \
  npx react-router dev --port 5173
