#!/bin/bash
# Run on the VM by CI as knightbus-deploy. Pulls prebuilt images and restarts.
# Usage: deploy.sh <api-image> <worker-image> <ghcr-user>   (GHCR token on stdin)
set -euo pipefail

API_IMAGE="${1:?api image}"
WORKER_IMAGE="${2:?worker image}"
GHCR_USER="${3:?ghcr user}"
APP_DIR="${APP_DIR:-/opt/knightbus}"
cd "$APP_DIR"

# Deployed tags live in .images.env so manual compose runs reuse the same images.
printf 'KNIGHTBUS_API_IMAGE=%s\nKNIGHTBUS_WORKER_IMAGE=%s\n' "$API_IMAGE" "$WORKER_IMAGE" > .images.env
compose=(docker compose --env-file .env --env-file .images.env
  -f docker-compose.yml -f deploy/gcp/docker-compose.gcp.yml)

# Job-scoped token from stdin; logged out right after the pull.
docker login ghcr.io -u "$GHCR_USER" --password-stdin >/dev/null
trap 'docker logout ghcr.io >/dev/null 2>&1 || true' EXIT
"${compose[@]}" pull api worker
docker logout ghcr.io >/dev/null

"${compose[@]}" up -d --no-build
# Drop superseded knightbus images only (other projects' images untouched).
docker images --format '{{.Repository}}:{{.Tag}}' \
  | grep -E '^ghcr\.io/.+/knightbus-(api|worker):' \
  | grep -vxF -e "$API_IMAGE" -e "$WORKER_IMAGE" -e "${API_IMAGE%:*}:latest" -e "${WORKER_IMAGE%:*}:latest" \
  | xargs -r docker rmi >/dev/null 2>&1 || true
