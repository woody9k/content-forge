#!/usr/bin/env bash
# Issue/renew the contentforge.boblab.net cert from the BobLab step-ca.
# Reuses the existing private key + CSR in this directory, so the key never
# leaves the box and nginx only ever sees a new fullchain.pem.
set -euo pipefail

CA_URL="https://ca.boblab.net:9000"
CA_FP="7c77ded27075271e6647e9cb76be44b6e595784100d0d8379c9393704ceb1f5b"
DIR="/etc/ssl/contentforge"

# One-time trust bootstrap for the step client.
if [ ! -f "$HOME/.step/config/defaults.json" ]; then
  step ca bootstrap --ca-url "$CA_URL" --fingerprint "$CA_FP"
fi

# Sign the standing CSR.  --provisioner is whichever JWK/admin provisioner the
# CA exposes; `step ca provisioner list` on the CA host will name it.
step ca sign "$DIR/contentforge.csr" "$DIR/fullchain.pem" \
    --ca-url "$CA_URL" \
    --not-after 8760h \
    "$@"

chmod 0644 "$DIR/fullchain.pem"
openssl x509 -in "$DIR/fullchain.pem" -noout -subject -issuer -dates

nginx -t && systemctl reload nginx
echo "==> contentforge.boblab.net certificate installed and nginx reloaded"
