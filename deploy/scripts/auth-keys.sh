#!/usr/bin/env bash
# Generates the platform's auth keys straight into OCI Vault. Values are never printed or written to
# disk or Terraform state.
#   jwt-private-key  RSA-2048 PEM: signs login tokens (RS256). Synced only into core-api.
#   jwt-public-key   its public half: every other service verifies tokens with it but can't create one.
#   session-secret   the web BFF's own cookie-signing secret (independent of the token keys).
#
#   ./deploy/scripts/auth-keys.sh            create whichever of the three is missing (safe to re-run)
#   ./deploy/scripts/auth-keys.sh --rotate   new versions of all three, then restart the workloads:
#       kubectl -n selfcare rollout restart deploy   (after External Secrets syncs, <= 5 min)
#     Rotation signs everyone out once: the old access tokens and cookies no longer verify.
set -euo pipefail
export SUPPRESS_LABEL_WARNING=True
cd "$(dirname "$0")/../terraform"
CPT=$(terraform output -raw compartment_id)
VAULT=$(terraform output -raw vault_id)
KEY=$(terraform output -raw key_id)
NAME=dt-selfcare
ROTATE=$([[ "${1:-}" == "--rotate" ]] && echo 1 || echo 0)

secret_id() {
  oci vault secret list --compartment-id "$CPT" --vault-id "$VAULT" --name "$NAME-$1" \
      --lifecycle-state ACTIVE --query 'data[0].id' --raw-output 2>/dev/null || true
}

store() {  # store <name> <value>
  local b64 id; b64=$(printf '%s' "$2" | base64 | tr -d '\n'); id=$(secret_id "$1")
  if [[ -n "$id" && "$id" != "null" ]]; then
    oci vault secret update-base64 --secret-id "$id" --secret-content-content "$b64" >/dev/null
    echo "  $NAME-$1: new version"
  else
    oci vault secret create-base64 --compartment-id "$CPT" --vault-id "$VAULT" --key-id "$KEY" \
        --secret-name "$NAME-$1" --secret-content-content "$b64" >/dev/null
    echo "  $NAME-$1: created"
  fi
}

exists() { local id; id=$(secret_id "$1"); [[ -n "$id" && "$id" != "null" ]]; }

# The key pair is written as a pair: never a private key whose public half differs in Vault.
if [[ $ROTATE == 1 ]] || ! exists jwt-private-key || ! exists jwt-public-key; then
  priv=$(openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 2>/dev/null)
  pub=$(printf '%s\n' "$priv" | openssl pkey -pubout 2>/dev/null)
  store jwt-private-key "$priv"
  store jwt-public-key "$pub"
  unset priv pub
else
  echo "  JWT key pair: already present (use --rotate for a new one)"
fi

if [[ $ROTATE == 1 ]] || ! exists session-secret; then
  s=$(openssl rand -base64 48 | tr -d '\n'); store session-secret "$s"; unset s
else
  echo "  session-secret: already present"
fi
echo "done - External Secrets syncs them into the cluster within 5 minutes"
