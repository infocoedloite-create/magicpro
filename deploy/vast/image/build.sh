#!/usr/bin/env bash
# Build the pool node image in Cloud Build and push it to Artifact Registry (project magicprov2).
# Costs a few cents of Cloud Build time plus Artifact Registry storage (~$0.10/GB-month, image ~20 GB).
#   1. stage the models from a working node (det/recognizer, swappers, enhancers, batched, and the
#      TensorRT cache, which only fits the GPU model it was built on):
#        scp -r -P <ssh port> root@<node ip>:/workspace/magicpro/models ./models
#   2. REGION=asia-south1 TAG=v1 bash deploy/vast/image/build.sh
set -euo pipefail
cd "$(dirname "$0")/../../.."
PROJECT="${PROJECT:-magicprov2}"; REGION="${REGION:-asia-south1}"; TAG="${TAG:-$(git rev-parse --short HEAD)}"
IMAGE="$REGION-docker.pkg.dev/$PROJECT/magicpro/node:$TAG"
test -f models/det_10g.onnx || { echo "Stage models/ first (see the top of this script)."; exit 1; }
gcloud artifacts repositories describe magicpro --location "$REGION" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud artifacts repositories create magicpro --repository-format docker --location "$REGION" --project "$PROJECT"
gcloud builds submit --project "$PROJECT" --config deploy/vast/image/cloudbuild.yaml --substitutions "_IMAGE=$IMAGE" .
echo "Built $IMAGE  -> set POOL_IMAGE=$IMAGE on the launcher"
