#!/bin/bash
# One-time Knightbus setup on the shared GCP e2-micro. Idempotent.
# Does not touch other projects' containers, nginx sites, or certs.
# Usage: sudo bash deploy/gcp/bootstrap.sh <git-repo-url>
set -euo pipefail

APP_DIR=/opt/knightbus
REPO_URL="${1:?usage: bootstrap.sh <git-repo-url>}"
API_HOST=knightbus.34.123.154.175.sslip.io

command -v docker >/dev/null || { echo "docker missing" >&2; exit 1; }

if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
fi
mkdir -p "$APP_DIR/artifacts"

# CI user for training: SSH port-forward to loopback Postgres + write model artifacts.
# It has a normal login shell (scp/mv need command execution); limit its key
# with authorized_keys options, see deploy/gcp/README.md.
if ! id knightbus-ci >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash knightbus-ci
fi
# Writable by knightbus-ci (training upload), world-readable so the api
# container (bind mount ./artifacts:/app/artifacts) can load the model.
chown -R knightbus-ci:knightbus-ci "$APP_DIR/artifacts"
chmod 755 "$APP_DIR/artifacts"
install -d -m 700 -o knightbus-ci -g knightbus-ci /home/knightbus-ci/.ssh

# Deploy user for CI/CD: pulls ghcr.io images and restarts compose.
# docker group membership is root-equivalent; keep its key only in the
# GitHub "production" environment secret DEPLOY_SSH_KEY.
if ! id knightbus-deploy >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash --groups docker knightbus-deploy
fi
install -d -m 700 -o knightbus-deploy -g knightbus-deploy /home/knightbus-deploy/.ssh
# Deploy user owns the app dir (compose files, .images.env); artifacts stay with knightbus-ci.
find "$APP_DIR" -path "$APP_DIR/artifacts" -prune -o -exec chown knightbus-deploy:knightbus-deploy {} +
if [ -f "$APP_DIR/.env" ]; then
  chmod 600 "$APP_DIR/.env"
fi

if [ ! -e /etc/nginx/sites-enabled/knightbus ]; then
  cp "$APP_DIR/deploy/gcp/nginx-knightbus.conf" /etc/nginx/sites-available/knightbus
  ln -s /etc/nginx/sites-available/knightbus /etc/nginx/sites-enabled/knightbus
  if ! nginx -t; then
    # Never leave a broken site enabled on the shared nginx.
    rm -f /etc/nginx/sites-enabled/knightbus /etc/nginx/sites-available/knightbus
    echo "nginx -t failed; knightbus site removed, nginx untouched" >&2
    exit 1
  fi
  systemctl reload nginx
fi

# Independent guard so a rerun retries a failed cert.
if [ ! -e "/etc/letsencrypt/live/$API_HOST" ]; then
  certbot --nginx -d "$API_HOST" --non-interactive --agree-tos --register-unsafely-without-email --redirect
fi

echo "Next: add training key to /home/knightbus-ci/.ssh/authorized_keys, deploy key to /home/knightbus-deploy/.ssh/authorized_keys, create $APP_DIR/.env (owned by knightbus-deploy), then push to main or run deploy/update.sh"
