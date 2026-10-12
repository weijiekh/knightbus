#!/bin/bash
# Pull latest code and restart Knightbus containers.
# Run from the repo root, or pass the repo path as $1.
set -euo pipefail

APP_DIR="${1:-/opt/knightbus}"
cd "$APP_DIR"

# EC2 default; on GCP set COMPOSE_ARGS="-f docker-compose.yml -f deploy/gcp/docker-compose.gcp.yml"
COMPOSE_ARGS="${COMPOSE_ARGS:--f docker-compose.yml --profile web}"

echo "Pulling latest changes..."
git pull --ff-only

echo "Rebuilding and restarting containers..."
docker compose $COMPOSE_ARGS up --build -d

# Opt-in: this prunes the GLOBAL build cache, including other projects' on a shared host.
if [ "${PRUNE_BUILD_CACHE:-0}" = "1" ]; then
  docker builder prune -f
fi

echo "Done. Running containers:"
docker compose $COMPOSE_ARGS ps
