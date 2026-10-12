#!/bin/bash
# EC2 User Data / cloud-init script for Knightbus
# Pass this as User Data when launching an EC2 instance.
# Tested on: Amazon Linux 2023, Ubuntu 22.04+
set -euo pipefail

REPO_URL="https://github.com/yourusername/knightbus.git"
APP_DIR="/opt/knightbus"

# ---------- detect package manager ----------
if command -v dnf &>/dev/null; then
    PKG="dnf"
elif command -v apt-get &>/dev/null; then
    PKG="apt"
else
    echo "Unsupported OS — need dnf (Amazon Linux) or apt (Ubuntu/Debian)" >&2
    exit 1
fi

# ---------- install Docker ----------
if [ "$PKG" = "dnf" ]; then
    dnf update -y
    dnf install -y docker git
    # Docker Compose plugin
    mkdir -p /usr/local/lib/docker/cli-plugins
    COMPOSE_VERSION=$(curl -s https://api.github.com/repos/docker/compose/releases/latest | grep '"tag_name"' | cut -d'"' -f4)
    curl -SL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-$(uname -m)" \
        -o /usr/local/lib/docker/cli-plugins/docker-compose
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
else
    apt-get update -y
    apt-get install -y ca-certificates curl gnupg git
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi

# ---------- enable Docker ----------
systemctl enable docker
systemctl start docker

# ---------- clone repo ----------
git clone "$REPO_URL" "$APP_DIR"
cd "$APP_DIR"

# ---------- write .env ----------
# Copy the example and override with real credentials.
# IMPORTANT: Never commit secrets to the repo or this script.
cp .env.example .env

sed -i 's/^APP_ENV=.*/APP_ENV=production/' .env
# Option A: Edit .env manually after launch (SSH in and update values).
# Option B: Fetch from AWS Systems Manager Parameter Store:
#   Store your .env content as a SecureString parameter (e.g. /knightbus/env)
#   and attach an IAM instance profile with ssm:GetParameter permission.
#
#   aws ssm get-parameter --name /knightbus/env --with-decryption \
#       --query 'Parameter.Value' --output text > .env

# ---------- start services ----------
docker compose --profile web up --build -d

# ---------- systemd ensures Docker (and its containers) restart on boot ----------
# Docker is already enabled via systemctl above. Containers created with
# docker compose use the default restart policy from docker-compose.yml.
# To make containers auto-restart, add `restart: unless-stopped` to each
# service in docker-compose.yml (see deploy/README.md).

echo "Knightbus deployment complete."
