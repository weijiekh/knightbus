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

# Restricted CI user: SSH port-forward to loopback Postgres + write model artifacts.
if ! id knightbus-ci >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash knightbus-ci
fi
# Writable by knightbus-ci (training upload), world-readable so the api
# container (bind mount ./artifacts:/app/artifacts) can load the model.
chown -R knightbus-ci:knightbus-ci "$APP_DIR/artifacts"
chmod 755 "$APP_DIR/artifacts"
install -d -m 700 -o knightbus-ci -g knightbus-ci /home/knightbus-ci/.ssh

if [ ! -e /etc/nginx/sites-enabled/knightbus ]; then
  cp "$APP_DIR/deploy/gcp/nginx-knightbus.conf" /etc/nginx/sites-available/knightbus
  ln -s /etc/nginx/sites-available/knightbus /etc/nginx/sites-enabled/knightbus
  nginx -t
  systemctl reload nginx
  certbot --nginx -d "$API_HOST" --non-interactive --agree-tos --register-unsafely-without-email --redirect
fi

echo "Next: add CI public key to /home/knightbus-ci/.ssh/authorized_keys, create $APP_DIR/.env, then run deploy/update.sh"
