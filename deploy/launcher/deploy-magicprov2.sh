#!/usr/bin/env bash
# Deploy the Magic Pro website to Google Cloud project magicprov2, one step at a time.
#
#   bash deploy/launcher/deploy-magicprov2.sh check      # read-only: what exists, what's missing
#   bash deploy/launcher/deploy-magicprov2.sh apis       # 1. enable APIs (free)
#   bash deploy/launcher/deploy-magicprov2.sh firestore  # 2. create the Firestore database (free tier)
#   bash deploy/launcher/deploy-magicprov2.sh account    # 3. service account + roles (free)
#   (you) create the secrets in Secret Manager, see the README
#   bash deploy/launcher/deploy-magicprov2.sh secrets    # 4. let the service account read them (free)
#   bash deploy/launcher/deploy-magicprov2.sh deploy     # 5. FIRST deploy of a new service only (sets every env var)
#   bash deploy/launcher/deploy-magicprov2.sh domain     # 6. map magicpro.nacgenai.in (after the TXT verification)
#   bash deploy/launcher/deploy-magicprov2.sh redeploy   # code update: rebuild magicpro-web AND magicpro-desk,
#                                                        #   keeping every env var and secret they already have
#
# Two services run this same code and share Firestore, secrets, Google sign-in and Razorpay:
#   magicpro-web   magicpro.nacgenai.in       public site + THE admin dashboard (streaming and desktop keys)
#   magicpro-desk  magicpro-desk.nacgenai.in  what the desktop app talks to (activation, updates, download)
# To ship new code to both, use `redeploy`, never `deploy`: `deploy` replaces all env vars (it would
# switch the live site's payments to test mode), so it refuses to touch a service that already exists.
#
# Every command names --project magicprov2; the local gcloud default project is not used or changed.
# Fill in the values below first. Run from the repository root (Cloud Shell or a PC with gcloud).
set -euo pipefail
cd "$(dirname "$0")/../.."

PROJECT=magicprov2
REGION=asia-southeast1          # Cloud Run domain mapping is not offered in asia-south1 (Mumbai)
SERVICE="${SERVICE:-magicpro-web}"              # the desktop-licensing site: SERVICE=magicpro-desk
SA_NAME=magicpro-web
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"
DOMAIN="${DOMAIN:-magicpro.nacgenai.in}"        # desktop-licensing site: DOMAIN=magicpro-desk.nacgenai.in
# the names the live project actually uses (Razorpay: rzp_key_id / rzp_key_secret / rzp_webhook_secret)
SECRETS=(magicpro-session-secret google-client-secret rzp_key_id rzp_key_secret rzp_webhook_secret vast-api-key pool-secret desktop-license-key smtp-password)

# ---- your values (not secret) ----
GOOGLE_CLIENT_ID="${GOOGLE_CLIENT_ID:-}"        # …apps.googleusercontent.com, from the OAuth client
ADMIN_EMAILS="${ADMIN_EMAILS:-}"                # your Google e-mail(s), comma separated
SUPPORT_EMAIL="${SUPPORT_EMAIL:-}"              # shown in the footer and legal pages (optional)
# Magic Pro Desktop sign-in codes are e-mailed over SMTP (Brevo, SendGrid, Resend, Zoho, SES or a
# Gmail/Workspace app password); the password is the secret smtp-password
SMTP_HOST="${SMTP_HOST:-}"                      # e.g. smtp-relay.brevo.com
SMTP_PORT="${SMTP_PORT:-587}"                   # 587 (STARTTLS) or 465 (SSL)
SMTP_USER="${SMTP_USER:-}"
MAIL_FROM="${MAIL_FROM:-}"                      # e.g. "Magic Pro <no-reply@nacgenai.in>"
# Magic Pro Desktop: the release public key from desktop/desktop.json (tools/desktop_keys.py), so the
# admin page refuses manifests that aren't signed with your release key
DESKTOP_RELEASE_PUBKEY="${DESKTOP_RELEASE_PUBKEY:-$(sed -n 's/.*"release_pubkey": *"\([^"]*\)".*/\1/p' "$(dirname "$0")/../../desktop/desktop.json" 2>/dev/null)}"

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
  # first deploy of a NEW service only: it sets every env var, so on a running service it would reset
  # the live payment settings. Existing services get new code with `redeploy`.
  if g run services describe "$SERVICE" --region "$REGION" --format='value(metadata.name)' >/dev/null 2>&1; then
    echo "$SERVICE already exists. To ship new code use: bash $0 redeploy  (keeps all env vars and secrets)."
    echo "To change one setting: gcloud run services update $SERVICE --project $PROJECT --region $REGION --update-env-vars KEY=value"
    exit 1
  fi
  [ -n "$GOOGLE_CLIENT_ID" ] && [ -n "$ADMIN_EMAILS" ] || { echo "Set GOOGLE_CLIENT_ID and ADMIN_EMAILS first."; exit 1; }
  # a new service starts with payments in test mode; switching to live is a deliberate, separate step
  # (RAZORPAY_MODE=live plus RAZORPAY_LIVE_CONFIRM, see payments.py)
  g run deploy "$SERVICE" --source deploy/launcher --region "$REGION" --service-account "$SA" \
    --allow-unauthenticated --min-instances 0 --max-instances 1 --memory 512Mi --cpu 1 \
    --set-env-vars "^|^SITE_URL=https://$DOMAIN|STORE=firestore|FIRESTORE_PROJECT=$PROJECT|GOOGLE_CLIENT_ID=$GOOGLE_CLIENT_ID|RAZORPAY_MODE=test|ADMIN_EMAILS=$ADMIN_EMAILS|SUPPORT_EMAIL=$SUPPORT_EMAIL|POOL_MIN_WARM=0|DESKTOP_RELEASE_PUBKEY=$DESKTOP_RELEASE_PUBKEY|SMTP_HOST=$SMTP_HOST|SMTP_PORT=$SMTP_PORT|SMTP_USER=$SMTP_USER|MAIL_FROM=$MAIL_FROM" \
    --set-secrets "MAGIC_SESSION_SECRET=magicpro-session-secret:latest,GOOGLE_CLIENT_SECRET=google-client-secret:latest,RAZORPAY_KEY_ID=rzp_key_id:latest,RAZORPAY_KEY_SECRET=rzp_key_secret:latest,RAZORPAY_WEBHOOK_SECRET=rzp_webhook_secret:latest,VAST_API_KEY=vast-api-key:latest,POOL_SECRET=pool-secret:latest,DESKTOP_LICENSE_KEY=desktop-license-key:latest,SMTP_PASSWORD=smtp-password:latest"
  g run services describe "$SERVICE" --region "$REGION" --format='value(status.url)'
  ;;
redeploy)
  # new code only: rebuild from source; every env var, secret and setting of each service is kept
  for svc in ${SERVICES:-magicpro-web magicpro-desk}; do
    echo "== $svc"
    g run deploy "$svc" --source deploy/launcher --region "$REGION"
  done
  ;;
domain)
  g beta run domain-mappings create --service "$SERVICE" --domain "$DOMAIN" --region "$REGION"
  echo "Add this record at GoDaddy (DNS for nacgenai.in):"
  g beta run domain-mappings describe --domain "$DOMAIN" --region "$REGION" --format='table(status.resourceRecords[].type,status.resourceRecords[].name,status.resourceRecords[].rrdata)'
  ;;
*)
  sed -n '2,13p' "$0"; exit 1;;
esac
