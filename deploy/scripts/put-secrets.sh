#!/usr/bin/env bash
# Stores third-party secrets in OCI Vault WITHOUT echoing them or writing them to disk/state.
#   ./deploy/scripts/put-secrets.sh            (prompts for each value, input hidden)
# Re-running creates a new secret version (rotation); pods pick it up via External Secrets.
set -euo pipefail
export SUPPRESS_LABEL_WARNING=True
cd "$(dirname "$0")/../terraform"
CPT=$(terraform output -raw compartment_id)
VAULT=$(terraform output -raw vault_id)
KEY=$(terraform output -raw key_id)
NAME=dt-selfcare

put() {
  local name="$NAME-$1" prompt="$2" value
  read -r -s -p "$prompt (leave empty to skip): " value; echo
  [[ -z "$value" ]] && { echo "  skipped $name"; return; }
  local b64; b64=$(printf '%s' "$value" | base64)
  local id; id=$(oci vault secret list --compartment-id "$CPT" --vault-id "$VAULT" --name "$name" \
                 --lifecycle-state ACTIVE --query 'data[0].id' --raw-output 2>/dev/null || true)
  if [[ -n "$id" && "$id" != "null" ]]; then
    oci vault secret update-base64 --secret-id "$id" --secret-content-content "$b64" >/dev/null
    echo "  updated $name (new version)"
  else
    oci vault secret create-base64 --compartment-id "$CPT" --vault-id "$VAULT" --key-id "$KEY" \
        --secret-name "$name" --secret-content-content "$b64" >/dev/null
    echo "  created $name"
  fi
  unset value b64
}

put razorpay-key-id        "Razorpay TEST Key ID (rzp_test_...)"
put razorpay-key-secret    "Razorpay TEST Key Secret"
put razorpay-webhook-secret "Razorpay webhook secret (any strong string you also enter in the dashboard)"
put mistral-api-key        "Mistral API key"
echo "done - secrets sync into the cluster within ~1 minute"
