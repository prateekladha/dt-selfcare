#!/usr/bin/env bash
# Start (or refresh) a preview assistant pod running THIS working copy's agent code, next to prod.
#   ./deploy/preview/assistant-preview.sh          start / re-copy code and restart
#   ./deploy/preview/assistant-preview.sh delete   remove it
# Then: PREVIEW_ASSISTANT=1 ./deploy/scripts/dev-against-prod.sh   (local web -> preview assistant)
# Same image, env, data and model as assistant-service, but NOT behind the Service: customers never reach it.
# Chats you have through it are real chats on the demo account (same DB), like using the site.
set -euo pipefail
export SUPPRESS_LABEL_WARNING=True
NS=selfcare; NAME=assistant-preview
cd "$(dirname "$0")/../.."
if [[ "${1:-}" == "delete" ]]; then kubectl -n $NS delete pod $NAME --ignore-not-found; exit 0; fi

kubectl -n $NS delete pod $NAME --ignore-not-found --wait=true >/dev/null
kubectl -n $NS get deploy assistant-service -o json | jq --arg n $NAME '
  .spec.template | .apiVersion="v1" | .kind="Pod" | .metadata={name:$n, labels:{app:$n}} |
  .spec.containers[0].command=["sh","-c","while [ ! -f /tmp/preview/ready ]; do sleep 1; done; exec python /tmp/preview/launch.py"] |
  del(.spec.containers[0].readinessProbe, .spec.containers[0].livenessProbe) | .spec.restartPolicy="Never"' \
  | kubectl -n $NS apply -f - >/dev/null
kubectl -n $NS wait --for=condition=Ready pod/$NAME --timeout=180s >/dev/null
kubectl -n $NS exec $NAME -- mkdir -p /tmp/preview
for f in deploy/preview/launch.py services/core-api/src/core_api/agent/{loop,tools,history}.py; do
  kubectl -n $NS cp "$f" "$NS/$NAME:/tmp/preview/$(basename "$f")"
done
kubectl -n $NS exec $NAME -- touch /tmp/preview/ready
for _ in $(seq 1 60); do kubectl -n $NS logs $NAME 2>/dev/null | grep -q "Uvicorn running" && break; sleep 2; done
kubectl -n $NS logs $NAME | grep -E "patched agent|Uvicorn running" || { kubectl -n $NS logs $NAME | tail -20; exit 1; }
echo "preview assistant ready. Now: PREVIEW_ASSISTANT=1 ./deploy/scripts/dev-against-prod.sh"
