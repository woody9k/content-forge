#!/usr/bin/env bash
# Phase 2 of standing up write.socialupkeep.com — run this ON THE NODE after
# `terraform apply` (which only does OS package prep via user_data). Clones
# the repo, wires up nginx + certbot, then hands off to the existing
# install-service.sh for the app itself.
#
#     ssh -i ~/.ssh/content-forge-kp ubuntu@<public-ip>
#     curl -fsSL https://raw.githubusercontent.com/woody9k/content-forge/main/deploy/bootstrap-node.sh | bash
#     # or, if the repo isn't public: scp this file over and run it directly.
set -euo pipefail

REPO="${CONTENT_FORGE_REPO:-https://github.com/woody9k/content-forge.git}"
DIR="/opt/content-forge"
DOMAIN="write.socialupkeep.com"

if [ "$(id -un)" = "root" ]; then
  echo "!! Run as the ubuntu user, not root." >&2
  exit 1
fi

echo "==> cloning $REPO"
if [ ! -d "$DIR/.git" ]; then
  sudo mkdir -p "$DIR"
  sudo chown -R "$(id -un):$(id -gn)" "$DIR"
  git clone "$REPO" "$DIR"
else
  git -C "$DIR" fetch --all --prune
  git -C "$DIR" pull --ff-only
fi

echo "==> claude CLI"
if ! command -v claude >/dev/null 2>&1; then
  curl -fsSL https://claude.ai/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
fi

# Two-phase nginx setup: the checked-in config's :443 block points at a cert
# that doesn't exist yet, so `nginx -t` fails if it's installed first. Bring
# up just the :80 challenge block, get the cert via webroot (doesn't touch
# nginx config, unlike `certbot --nginx`), then swap in the full config.
echo "==> installing nginx :80-only site for the ACME challenge"
sudo tee /etc/nginx/sites-available/write.conf >/dev/null <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
}
EOF
sudo ln -sf /etc/nginx/sites-available/write.conf /etc/nginx/sites-enabled/write.conf
sudo rm -f /etc/nginx/sites-enabled/default
sudo mkdir -p /var/www/certbot
sudo nginx -t
sudo systemctl reload nginx

echo "==> requesting Let's Encrypt cert for $DOMAIN"
echo "    (needs the Route53 A record to have propagated — retry if this fails)"
sudo certbot certonly --webroot -w /var/www/certbot -d "$DOMAIN" \
  --non-interactive --agree-tos -m "${LETSENCRYPT_EMAIL:-woody9k@gmail.com}"

echo "==> installing full nginx site (web UI + /mcp)"
sudo cp "$DIR/deploy/nginx-write.conf" /etc/nginx/sites-available/write.conf
sudo nginx -t
sudo systemctl reload nginx

echo
echo "==> now run, interactively, to set the Claude token + generate the access token:"
echo "    cd $DIR && CF_PUBLIC_HOST=$DOMAIN CF_PUBLIC_PORT=443 ./deploy/install-service.sh"
