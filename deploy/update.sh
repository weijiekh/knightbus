#!/bin/bash
# Pull latest code and restart Knightbus containers.
# Run from the repo root, or pass the repo path as $1.
set -euo pipefail

APP_DIR="${1:-/opt/knightbus}"
cd "$APP_DIR"

echo "Pulling latest changes..."
git pull --ff-only

echo "Rebuilding and restarting containers..."
docker compose up --build -d

echo "Done. Running containers:"
docker compose ps
