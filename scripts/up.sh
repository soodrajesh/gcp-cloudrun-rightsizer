#!/usr/bin/env bash
# Build and deploy (or update) the stack. Safe to re-run. Every Terraform change is planned,
# summarised, then applied from the saved plan file.
source "$(dirname "$0")/lib.sh"
need gcloud; need terraform
tf_init

if gcloud artifacts repositories describe rightsize --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  log "1/3 foundation already exists, skipping"
else
  log "1/3 foundation: APIs, registry, build identity, runtime identity"
  TF_VAR_image="" apply_plan
fi

log "2/3 build image with Cloud Build"
IMG="${IMAGE_BASE}/app:$(date +%Y%m%d-%H%M%S)"
built=0
for attempt in 1 2 3; do  # IAM grants for the build identity can take a minute to propagate
  if gcloud builds submit "$ROOT" --project "$PROJECT_ID" --region "$REGION" --config "$ROOT/cloudbuild.yaml" \
       --substitutions "_IMAGE=$IMG" --gcs-source-staging-dir "gs://${PROJECT_ID}-rs-build/source" \
       --service-account "projects/$PROJECT_ID/serviceAccounts/rs-build@${PROJECT_ID}.iam.gserviceaccount.com" \
       >/dev/null 2>"$ROOT/.build.err"; then built=1; break; fi
  warn "build attempt $attempt failed ($(tail -1 "$ROOT/.build.err" | cut -c1-120)); retrying in 30s"; sleep 30
done
rm -f "$ROOT/.build.err"
[ "$built" = 1 ] || die "image build failed"
ok "image $IMG"

log "3/3 deploy Cloud Run service"
TF_VAR_image="$IMG" apply_plan
URL="$($TF output -raw service_url)"
ok "service: $URL (private; invoker = $ADMIN_EMAIL)"
echo "Open the UI locally:  gcloud run services proxy rightsizer --region $REGION --project $PROJECT_ID --port 8081"
