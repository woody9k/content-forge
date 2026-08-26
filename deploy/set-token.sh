#!/usr/bin/env bash
# Set or rotate the Claude subscription token used by the service.
#
#   ./deploy/set-token.sh              # prompts (hidden input)
#   ./deploy/set-token.sh <token>      # non-interactive
#
# Get a token with:  claude setup-token
# It prints once and is NOT saved anywhere by the CLI — that is what this
# script is for. Tokens last about a year; rerun this when it expires.
set -euo pipefail

ENV_FILE="$HOME/.config/content-forge/env"
CLAUDE_BIN="$(command -v claude || echo claude)"

TOKEN="${1:-}"
if [ -z "$TOKEN" ]; then
  read -rsp "Paste the token from 'claude setup-token': " TOKEN
  echo
fi
[ -n "$TOKEN" ] || { echo "No token given." >&2; exit 1; }

mkdir -p "$(dirname "$ENV_FILE")"
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
grep -v '^CLAUDE_CODE_OAUTH_TOKEN=' "$ENV_FILE" > "$ENV_FILE.new" 2>/dev/null || : > "$ENV_FILE.new"
printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$TOKEN" >> "$ENV_FILE.new"
mv "$ENV_FILE.new" "$ENV_FILE"; chmod 600 "$ENV_FILE"
echo "==> wrote $ENV_FILE (0600)"

echo "==> verifying…"
if env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
      CLAUDE_CODE_OAUTH_TOKEN="$TOKEN" \
      "$CLAUDE_BIN" -p --max-turns 1 "reply with OK" >/dev/null 2>&1; then
  echo "==> token accepted"
else
  echo "!! token rejected — expired or mistyped. Regenerate: claude setup-token" >&2
  exit 1
fi

if systemctl list-unit-files 2>/dev/null | grep -q '^content-forge\.service'; then
  echo "==> restarting service"
  sudo systemctl restart content-forge
  sleep 2
  curl -s "http://localhost:${CF_PORT:-8080}/health" && echo
fi
