#!/usr/bin/env bash
# First-boot OS prep only. Does NOT deploy the app — Claude/access tokens
# need a human to enter them, so app deploy stays a manual
# `deploy/bootstrap-node.sh` + `deploy/install-service.sh` run over SSH,
# same two-phase pattern as the rest of this repo's deploy scripts.
set -euo pipefail
exec > >(tee /var/log/content-forge-user-data.log) 2>&1

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  nginx certbot python3-certbot-nginx \
  python3 python3-venv python3-pip git curl ca-certificates unattended-upgrades

mkdir -p /var/www/certbot

systemctl enable --now nginx
systemctl enable --now unattended-upgrades

echo "user_data complete: $(date -u)"
