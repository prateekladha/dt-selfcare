#!/usr/bin/env bash
# Keep the newest KEEP image versions per OCIR repository (CI pushes one per commit) plus anything
# currently deployed; delete the rest. Dry run by default:
#   deploy/scripts/ocir-prune.sh            # show what would be deleted
#   deploy/scripts/ocir-prune.sh --apply    # delete
set -euo pipefail
KEEP=${KEEP:-10}
APPLY=${1:-}
COMPARTMENT=$(grep -E '^compartment_ocid' "$(dirname "$0")/../terraform/terraform.tfvars" | cut -d'"' -f2)
IN_USE=$(kubectl get pods -A -o jsonpath='{range .items[*]}{range .spec.containers[*]}{.image}{"\n"}{end}{end}' 2>/dev/null \
         | grep -oE '[0-9a-f]{40}' | sort -u || true)
total=0
TMP=$(mktemp)
export SUPPRESS_LABEL_WARNING=True
for repo in $(oci artifacts container repository list --compartment-id "$COMPARTMENT" --all \
              --query 'data.items[]."display-name"' --raw-output 2>/dev/null | tr -d '[]",' ); do
  oci artifacts container image list --compartment-id "$COMPARTMENT" --repository-name "$repo" --all \
      --sort-by TIMECREATED --sort-order DESC --query 'data.items[].[id,version]' --output json > $TMP
  n=$(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))))" "$TMP")
  victims=$(python3 - "$KEEP" "$IN_USE" "$TMP" <<'PY'
import json, sys
keep, in_use = int(sys.argv[1]), set(sys.argv[2].split())
items = json.load(open(sys.argv[3]))
for i, (oid, ver) in enumerate(items):
    if i >= keep and ver not in in_use and ver != "latest":
        print(oid)
PY
)
  c=$(printf '%s\n' "$victims" | grep -c . || true)
  echo "$repo: $n versions, delete $c (keep newest $KEEP + deployed)"
  total=$((total + c))
  if [ "$APPLY" = "--apply" ]; then
    for id in $victims; do oci artifacts container image delete --image-id "$id" --force >/dev/null; done
  fi
done
rm -f $TMP
echo "total: $total image versions $([ "$APPLY" = "--apply" ] && echo deleted || echo 'would be deleted (dry run)')"
