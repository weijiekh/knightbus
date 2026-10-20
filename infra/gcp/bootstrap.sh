#!/usr/bin/env bash
#
# Knightbus: one-time bootstrap on the shared GCP e2-micro (krappa-haus).
# Mirrors caruma's infra/gcp/bootstrap.sh.
#
#   Usage:  ssh <app-user>@<vm-ip>
#           # one-time deploy key + clone: see "Deploy key" below
#           DOMAIN=knightbus.<vm-ip>.sslip.io SPA_ORIGIN=https://manabas.pages.dev \
#               sudo -E bash ~/knightbus/infra/gcp/bootstrap.sh
#
# Postgres, the API and the worker run in Docker (infra/gcp/compose.yaml) with
# memory caps. nginx and certbot stay on the host for TLS. The SPA lives on
# Cloudflare Pages, which proxies /v1/* here (frontend/functions/v1).
# DOMAIN is the VM's sslip.io name. It must be a subdomain such as
# knightbus.<vm-ip>.sslip.io, because the bare <vm-ip>.sslip.io host is served
# by caruma's nginx site.
#
# The API/worker images are private on GHCR. Bootstrap brings up Postgres; the
# first CI deploy pulls the images and starts the API and worker.
#
# APP_USER defaults to the account that invoked sudo. It owns the checkout
# (/home/$APP_USER/knightbus) and is the account CI deploys as.
#
# Deploy key (the repo is private). The VM's ~/.ssh/config already maps
# `Host github.com` to caruma's deploy key, so knightbus uses a host alias:
#   ssh-keygen -t ed25519 -N '' -C knightbus-deploy -f ~/.ssh/knightbus_deploy
#   # add ~/.ssh/knightbus_deploy.pub in GitHub: repo Settings -> Deploy keys
#   printf 'Host github.com-knightbus\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/knightbus_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
#   git clone git@github.com-knightbus:weijiekh/knightbus.git ~/knightbus
# This script generates the key and the alias itself if they are missing and
# stops with the public key to add, so doing it by hand first is optional.
#
# Idempotent: safe to re-run. Re-running does NOT overwrite an existing
# infra/gcp/.env (which holds the DB password) or re-issue TLS certificates.
#
# Not done here (caruma's bootstrap already did them for this shared host):
# firewall (ufw + VPC rules for 22/80/443) and swap. The VM has ~5 GB of
# swap, so caruma's top-up-to-4GB logic is dropped rather than copied.
#
set -euo pipefail

DOMAIN="${DOMAIN:-}"
SPA_ORIGIN="${SPA_ORIGIN:-}"
APP_USER="${APP_USER:-${SUDO_USER:-}}"
APP_HOME="/home/${APP_USER}"
REPO_DIR="${APP_HOME}/knightbus"
REPO_URL="${REPO_URL:-git@github.com-knightbus:weijiekh/knightbus.git}"
# CI deploys reset to origin/main; override only to bring a box up ahead of
# a merge.
REPO_BRANCH="${REPO_BRANCH:-main}"
STACK_DIR="${REPO_DIR}/infra/gcp"
CI_USER="knightbus-ci"
ARTIFACTS_DIR="${ARTIFACTS_DIR:-/opt/knightbus/artifacts}"

log()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m!!! %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mxxx %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run with sudo -E (needs -E to keep DOMAIN)."
[[ -n "$DOMAIN" ]] || die "Set DOMAIN, e.g. DOMAIN=knightbus.34.1.2.3.sslip.io sudo -E bash bootstrap.sh"
[[ -n "$SPA_ORIGIN" ]] || die "Set SPA_ORIGIN, e.g. SPA_ORIGIN=https://manabas.pages.dev"
[[ -n "$APP_USER" && "$APP_USER" != root ]] || die "Run via sudo from the app user, or set APP_USER."

as_app() { sudo -u "$APP_USER" -H bash -lc "$*"; }
compose() { (cd "$STACK_DIR" && docker compose "$@"); }

# ---------------------------------------------------------------------------
log "System packages"
# ---------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    curl git ca-certificates gnupg openssl \
    nginx certbot python3-certbot-nginx

# Docker from Docker's own repo: Debian's docker.io lacks the compose plugin.
# This shared host already has it; install only if missing.
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    # shellcheck disable=SC1091
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
systemctl enable --now docker
usermod -aG docker "$APP_USER"

# ---------------------------------------------------------------------------
log "Clone repository"
# ---------------------------------------------------------------------------
# The repo is private: clone over SSH with a read-only deploy key that only
# this repo trusts. First run prints the key and stops until it is added.
DEPLOY_KEY="${APP_HOME}/.ssh/knightbus_deploy"
if [[ ! -f "$DEPLOY_KEY" ]]; then
    as_app "mkdir -p ~/.ssh && chmod 700 ~/.ssh && \
        ssh-keygen -q -t ed25519 -N '' -C knightbus-deploy@\$(hostname) -f ${DEPLOY_KEY}"
fi
# A host alias, because `Host github.com` is already mapped to caruma's key.
if ! grep -q 'Host github.com-knightbus' "${APP_HOME}/.ssh/config" 2>/dev/null; then
    as_app "printf '\nHost github.com-knightbus\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/knightbus_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config && chmod 600 ~/.ssh/config"
fi
if ! as_app "ssh-keygen -F github.com >/dev/null 2>&1"; then
    as_app "ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null"
fi
if ! as_app "ssh -T git@github.com-knightbus 2>&1 | grep -q 'successfully authenticated'"; then
    warn "Add this key to GitHub: repo Settings -> Deploy keys (read-only):"
    cat "${DEPLOY_KEY}.pub"
    die "Then re-run bootstrap."
fi

if [[ -d "${REPO_DIR}/.git" ]]; then
    as_app "cd ${REPO_DIR} && git fetch -q origin ${REPO_BRANCH} && git checkout -q ${REPO_BRANCH} && git pull -q --ff-only"
else
    as_app "git clone -b ${REPO_BRANCH} ${REPO_URL} ${REPO_DIR}"
fi

# ---------------------------------------------------------------------------
log "Training user and model artifacts"
# ---------------------------------------------------------------------------
# knightbus-ci is for the weekly training workflow ONLY: an SSH tunnel to
# loopback Postgres plus scp of the model into ARTIFACTS_DIR. It must NOT be in
# the docker group (that would make its key root-equivalent on this shared
# host); deploys run as APP_USER instead.
if ! id "$CI_USER" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "$CI_USER"
fi
gpasswd -d "$CI_USER" docker >/dev/null 2>&1 || true
install -d -m 700 -o "$CI_USER" -g "$CI_USER" "/home/${CI_USER}/.ssh"
# Writable by knightbus-ci (training upload), world-readable so the api and
# worker containers (bind mount of this path) can load the model.
install -d -m 755 -o "$CI_USER" -g "$CI_USER" "$ARTIFACTS_DIR"
chmod 755 "$ARTIFACTS_DIR"

# ---------------------------------------------------------------------------
log "Environment file"
# ---------------------------------------------------------------------------
# One file for compose interpolation (POSTGRES_PASSWORD, API_TAG) and for the
# containers' settings. Lives beside compose.yaml, gitignored.
ENV_FILE="${STACK_DIR}/.env"
if [[ -f "$ENV_FILE" ]]; then
    warn "${ENV_FILE} exists, leaving it untouched."
else
    DB_PASS="$(openssl rand -hex 24)"
    install -o "$APP_USER" -g "$APP_USER" -m 600 /dev/null "$ENV_FILE"
    {
        echo "POSTGRES_PASSWORD=${DB_PASS}"
        echo "# Set by each CI deploy to the image it pulled."
        echo "API_TAG=latest"
        echo "APP_ENV=production"
        echo
        # GTFS and retention settings come from the checked-in example.
        # TRAINING_IN_WORKER is fixed to false in compose.yaml.
        grep -E '^(GTFS_|FEATURE_REFRESH_MINUTES|TRAINING_REFRESH_HOURS|REALTIME_RETENTION_DAYS|RAW_SNAPSHOT_RETENTION_DAYS)' \
            "${REPO_DIR}/.env.example"
    } > "$ENV_FILE"
fi

# ---------------------------------------------------------------------------
log "Postgres (container)"
# ---------------------------------------------------------------------------
compose up -d --wait db

# ---------------------------------------------------------------------------
log "nginx"
# ---------------------------------------------------------------------------
# Only this project's site is touched; caruma's stays as it is.
# Once certbot has added the 443 block, nginx.conf (HTTP only) must not
# overwrite it, so a site that already listens on 443 is left alone.
SITE=/etc/nginx/sites-available/knightbus
if grep -q 'listen 443' "$SITE" 2>/dev/null; then
    echo "  site already has TLS, leaving it untouched"
else
    BACKUP=""
    if [[ -f "$SITE" ]]; then
        BACKUP="$(mktemp)"
        cp "$SITE" "$BACKUP"
    fi
    sed "s/SERVER_NAME_PLACEHOLDER/${DOMAIN}/g" \
        "${STACK_DIR}/nginx.conf" > "$SITE"
    ln -sf "$SITE" /etc/nginx/sites-enabled/knightbus
    if ! nginx -t; then
        # Never leave a broken site enabled on the shared nginx. Restore the
        # previous one if there was one, else remove ours.
        if [[ -n "$BACKUP" ]]; then
            cp "$BACKUP" "$SITE"
            rm -f "$BACKUP"
            die "nginx -t failed; previous knightbus site restored."
        fi
        rm -f /etc/nginx/sites-enabled/knightbus "$SITE"
        die "nginx -t failed; knightbus site removed, nginx untouched."
    fi
    [[ -z "$BACKUP" ]] || rm -f "$BACKUP"
fi
systemctl reload nginx

# ---------------------------------------------------------------------------
log "TLS"
# ---------------------------------------------------------------------------
if [[ -d "/etc/letsencrypt/live/${DOMAIN}" ]]; then
    echo "  certificate already present, skipping issuance"
    # Re-attach it if the site was rewritten as HTTP only.
    if ! grep -q 'listen 443' /etc/nginx/sites-available/knightbus; then
        certbot install --nginx --cert-name "$DOMAIN" -d "$DOMAIN" \
            --redirect --non-interactive || \
            warn "certbot install failed. Re-run: sudo certbot install --nginx --cert-name ${DOMAIN} -d ${DOMAIN}"
    fi
elif getent hosts "$DOMAIN" >/dev/null; then
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
        --register-unsafely-without-email --redirect || \
        warn "certbot failed, API is on HTTP. Re-run: sudo certbot --nginx -d ${DOMAIN}"
else
    warn "${DOMAIN} does not resolve. API is on HTTP only."
    warn "Check the IP in DOMAIN, then: sudo certbot --nginx -d ${DOMAIN}"
fi

# ---------------------------------------------------------------------------
log "Done"
# ---------------------------------------------------------------------------
compose ps
cat <<SUMMARY

  API       https://${DOMAIN}/v1/health   (up after the first CI deploy)
  SPA       ${SPA_ORIGIN} (Cloudflare Pages, API_ORIGIN=https://${DOMAIN})
  Stack     cd ${STACK_DIR} && docker compose ps
  Logs      docker logs -f knightbus-api
  DB        docker exec -it knightbus-db psql -U postgres knightbus
  Model     ${ARTIFACTS_DIR} (written by the weekly training workflow)

  Next:
    1. Add the training public key to /home/${CI_USER}/.ssh/authorized_keys
       (restrict,port-forwarding,permitopen="127.0.0.1:5433" ssh-ed25519 ...).
    2. Set the DB_PASSWORD repo secret to POSTGRES_PASSWORD in ${ENV_FILE}.
    3. Push to main (or run the Deploy workflow) to pull the images.

SUMMARY
