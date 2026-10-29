#!/usr/bin/env bash
# Shared helpers for up.sh / down.sh. Sourced, not executed.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m\xe2\x9c\x94 %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m\xe2\x9c\x98 %s\033[0m\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }

[ -f "$ROOT/deploy.env" ] && set -a && . "$ROOT/deploy.env" && set +a
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
ADMIN_EMAIL="${ADMIN_EMAIL:-$(gcloud config get-value account 2>/dev/null)}"
[ -n "$PROJECT_ID" ] || die "no project: set PROJECT_ID in deploy.env"

TF="terraform -chdir=$ROOT/terraform"
STATE_BUCKET="${PROJECT_ID}-tfstate"
IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/rightsize"
export TF_VAR_project_id="$PROJECT_ID" TF_VAR_region="$REGION" TF_VAR_admin_email="$ADMIN_EMAIL"

tf_init() { $TF init -input=false -backend-config="bucket=$STATE_BUCKET" >/dev/null; }

# plan -> show the resource list -> refuse unexpected destroys -> apply the saved plan file
apply_plan() {
  local plan; plan="$(mktemp -t rsplan.XXXXXX)"
  $TF plan -input=false -no-color -out="$plan" >"$plan.txt" 2>&1 || { cat "$plan.txt"; die "plan failed"; }
  grep -E '^Plan:|^No changes|^  # ' "$plan.txt" || true
  if grep -qE '^Plan: .* [1-9][0-9]* to destroy' "$plan.txt" && [ "${ALLOW_DESTROY:-0}" != 1 ]; then
    die "plan would destroy resources; review the list, then re-run with ALLOW_DESTROY=1 if intended"
  fi
  $TF apply -input=false -no-color "$plan" | grep -E 'Apply complete|Error' || true
  rm -f "$plan" "$plan.txt"
}
