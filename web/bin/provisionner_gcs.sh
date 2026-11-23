#!/usr/bin/env bash
# Creates the Cloud Storage bucket for transcript's videos and lets the
# existing service account (bin/provisionner_gcp.sh) use it. Idempotent.
#
#   gcloud auth login                                   # a human, once
#   web/bin/provisionner_gcs.sh transcript-509912 [bucket] [location]
#
# What it creates, and nothing else:
#   - the Cloud Storage API in the project (billing must be linked: the
#     script stops and says so otherwise);
#   - one bucket, in Paris by default, with:
#       * Autoclass down to Archive: an object nobody reads goes down to
#         Nearline, Coldline then Archive by itself, and back to Standard
#         when read, with no retrieval or early-deletion fee;
#       * uniform access and public access prevention: no ACL, no public
#         object, ever;
#       * 7 days of soft delete: a deleted video can still be restored;
#   - roles/storage.objectUser for the service account, on this bucket only.
set -euo pipefail

PROJET="${1:?projet GCP attendu}"
BUCKET="${2:-ekonum-transcript-reunions}"
LIEU="${3:-europe-west9}"
COMPTE="transcript-stockage@${PROJET}.iam.gserviceaccount.com"

g() { gcloud --project "$PROJET" --quiet "$@"; }
etape() { printf '\n→ %s\n' "$*"; }

etape "Facturation"
if [ "$(g billing projects describe "$PROJET" --format='value(billingEnabled)')" != "True" ]; then
  echo "Le projet n'est rattaché à aucun compte de facturation. Le rattacher :" >&2
  echo "  gcloud billing accounts list" >&2
  echo "  gcloud billing projects link $PROJET --billing-account=<ID>" >&2
  exit 1
fi

etape "API Cloud Storage"
g services enable storage.googleapis.com

etape "Bucket gs://${BUCKET} (${LIEU})"
if ! g storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1; then
  g storage buckets create "gs://${BUCKET}" \
    --location="$LIEU" \
    --enable-autoclass --autoclass-terminal-storage-class=ARCHIVE \
    --uniform-bucket-level-access \
    --public-access-prevention \
    --soft-delete-duration=7d
else
  g storage buckets update "gs://${BUCKET}" \
    --enable-autoclass --autoclass-terminal-storage-class=ARCHIVE \
    --uniform-bucket-level-access \
    --public-access-prevention \
    --soft-delete-duration=7d
fi

etape "Droit du compte de service, sur ce bucket seulement"
g storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${COMPTE}" --role=roles/storage.objectUser --format=none

cat <<FIN

Variable du stack (rien de secret), en plus des trois existantes :
  EKOVIDEO_GCS_BUCKET=${BUCKET}

Puis, dans le conteneur :
  python -m web.app.migration_gcs             # combien, et quel poids
  python -m web.app.migration_gcs --apply     # copier, vérifier, basculer
  python -m web.app.migration_gcs --purge-drive   # une semaine plus tard
FIN
