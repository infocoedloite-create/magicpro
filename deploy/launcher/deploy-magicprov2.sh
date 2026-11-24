#!/usr/bin/env bash
# Deploy the Magic Pro website to Google Cloud project magicprov2, one step at a time.
#
#   bash deploy/launcher/deploy-magicprov2.sh check      # read-only: what exists, what's missing
#   bash deploy/launcher/deploy-magicprov2.sh apis       # 1. enable APIs (free)
#   bash deploy/launcher/deploy-magicprov2.sh firestore  # 2. create the Firestore database (free tier)
#   bash deploy/launcher/deploy-magicprov2.sh account    # 3. service account + roles (free)
#   (you) create the secrets in Secret Manager, see the README
#   bash deploy/launcher/deploy-magicprov2.sh secrets    # 4. let the service account read them (free)
#   bash deploy/launcher/deploy-magicprov2.sh deploy     # 5. build and deploy the Cloud Run service
#   bash deploy/launcher/deploy-magicprov2.sh domain     # 6. map magicpro.nacgenai.in (after the TXT verification)
#
# Every command names --project magicprov2; the local gcloud default project is not used or changed.
# Fill in the values below first. Run from the repository root (Cloud Shell or a PC with gcloud).
set -euo pipefail
cd "$(dirname "$0")/../.."

PROJECT=magicprov2
REGION=asia-southeast1          # Cloud Run domain mapping is not offered in asia-south1 (Mumbai)
SERVICE=magicpro-web
SA_NAME=magicpro-web
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"
DOMAIN=magicpro.nacgenai.in
SECRETS=(magicpro-session-secret google-client-secret razorpay-key-secret razorpay-webhook-secret vast-api-key pool-secret)

# ---- your values (not secret) ----
GOOGLE_CLIENT_ID="${GOOGLE_CLIENT_ID:-}"        # …apps.googleusercontent.com, from the OAuth client
RAZORPAY_KEY_ID="${RAZORPAY_KEY_ID:-}"          # rzp_test_… (test mode only)
ADMIN_EMAILS="${ADMIN_EMAILS:-}"                # your Google e-mail(s), comma separated
SUPPORT_EMAIL="${SUPPORT_EMAIL:-}"              # shown in the footer and legal pages (optional)

g() { gcloud --project "$PROJECT" "$@"; }

case "${1:-check}" in
check)
  echo "== project"; g projects describe "$PROJECT" --format='value(projectId,lifecycleState)'
  echo "== APIs enabled (of the ones needed)"
  g services list --enabled --format='value(config.name)' | grep -E '^(run|cloudbuild|artifactregistry|secretmanager|firestore)\.googleapis\.com$' || true
  echo "== Firestore"; g firestore databases list --format='value(name,locationId,type)' 2>/dev/null || echo "(none, or the API is off)"
  echo "== service account"; g iam service-accounts describe "$SA" --format='value(email)' 2>/dev/null || echo "(missing)"
  echo "== secrets"; for s in "${SECRETS[@]}"; do printf '%-26s ' "$s"; g secrets describe "$s" --format='value(name)' >/dev/null 2>&1 && echo ok || echo MISSING; done
  echo "== Cloud Run"; g run services describe "$SERVICE" --region "$REGION" --format='value(status.url)' 2>/dev/null || echo "(not deployed)"
  ;;
apis)
  g services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
    secretmanager.googleapis.com firestore.googleapis.com
  ;;
firestore)
  g firestore databases create --location="$REGION" --type=firestore-native
  ;;
account)
  g iam service-accounts describe "$SA" >/dev/null 2>&1 || g iam service-accounts create "$SA_NAME" --display-name "Magic Pro website"
  g projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role roles/datastore.user --condition=None >/dev/null
  echo "service account $SA can use Firestore"
  ;;
secrets)
  for s in "${SECRETS[@]}"; do
    g secrets add-iam-policy-binding "$s" --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor >/dev/null
    echo "$SA can read $s"
  done
  ;;
deploy)
  [ -n "$GOOGLE_CLIENT_ID" ] && [ -n "$ADMIN_EMAILS" ] || { echo "Set GOOGLE_CLIENT_ID and ADMIN_EMAILS first."; exit 1; }
  case "$RAZORPAY_KEY_ID" in rzp_test_*|"") ;; *) echo "Only a rzp_test_ key: payments stay in test mode."; exit 1;; esac
  g run deploy "$SERVICE" --source deploy/launcher --region "$REGION" --service-account "$SA" \
    --allow-unauthenticated --min-instances 0 --max-instances 1 --memory 512Mi --cpu 1 \
    --set-env-vars "^|^SITE_URL=https://$DOMAIN|STORE=firestore|FIRESTORE_PROJECT=$PROJECT|GOOGLE_CLIENT_ID=$GOOGLE_CLIENT_ID|RAZORPAY_KEY_ID=$RAZORPAY_KEY_ID|RAZORPAY_MODE=test|ADMIN_EMAILS=$ADMIN_EMAILS|SUPPORT_EMAIL=$SUPPORT_EMAIL|POOL_MIN_WARM=0" \
    --set-secrets "MAGIC_SESSION_SECRET=magicpro-session-secret:latest,GOOGLE_CLIENT_SECRET=google-client-secret:latest,RAZORPAY_KEY_SECRET=razorpay-key-secret:latest,RAZORPAY_WEBHOOK_SECRET=razorpay-webhook-secret:latest,VAST_API_KEY=vast-api-key:latest,POOL_SECRET=pool-secret:latest"
  g run services describe "$SERVICE" --region "$REGION" --format='value(status.url)'
  ;;
domain)
  g beta run domain-mappings create --service "$SERVICE" --domain "$DOMAIN" --region "$REGION"
  echo "Add this record at GoDaddy (DNS for nacgenai.in):"
  g beta run domain-mappings describe --domain "$DOMAIN" --region "$REGION" --format='table(status.resourceRecords[].type,status.resourceRecords[].name,status.resourceRecords[].rrdata)'
  ;;
*)
  sed -n '2,13p' "$0"; exit 1;;
esac
