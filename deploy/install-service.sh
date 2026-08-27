#!/usr/bin/env bash
# Generates and installs the systemd unit from THIS machine's actual paths.
# Run as your normal login user (it calls sudo itself where needed):
#
#     ./deploy/install-service.sh
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
RUN_GROUP="$(id -gn)"
RUN_HOME="$HOME"
CONTENT_ROOT="${CF_CONTENT_ROOT:-$RUN_HOME/content-forge-data}"
PORT="${CF_PORT:-8080}"
MODEL="${CF_CLAUDE_MODEL:-sonnet}"
PUBLIC_HOST="${CF_PUBLIC_HOST:-contentforge.boblab.net}"
PUBLIC_PORT="${CF_PUBLIC_PORT:-8443}"

if [ "$RUN_USER" = "root" ]; then
  echo "!! Run this as your normal user, not root — the Claude CLI keeps its" >&2
  echo "   credentials in that user's \$HOME/.claude." >&2
  exit 1
fi

# --- preflight ------------------------------------------------------------
echo "==> app dir      : $APP_DIR"
echo "==> service user : $RUN_USER:$RUN_GROUP  (home $RUN_HOME)"
echo "==> content root : $CONTENT_ROOT"
echo "==> public host  : $PUBLIC_HOST:$PUBLIC_PORT"

if [ ! -x "$APP_DIR/.venv/bin/uvicorn" ]; then
  echo "==> creating virtualenv"
  python3 -m venv "$APP_DIR/.venv"
  "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
  "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
fi

CLAUDE_BIN="$(command -v claude || true)"
if [ -z "$CLAUDE_BIN" ]; then
  echo "!! 'claude' is not on your PATH. Install it, then re-run:" >&2
  echo "     curl -fsSL https://claude.ai/install.sh | bash" >&2
  exit 1
fi
echo "==> claude       : $CLAUDE_BIN"

# --- auth -----------------------------------------------------------------
# `claude setup-token` does NOT persist anything: it prints a token once and
# expects it supplied as CLAUDE_CODE_OAUTH_TOKEN. Systemd units in
# /etc/systemd/system are world-readable, so the token goes in a 0600 file
# that the unit references via EnvironmentFile= instead.
ENV_FILE="$RUN_HOME/.config/content-forge/env"

if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -f "$ENV_FILE" ] \
   && grep -q '^CLAUDE_CODE_OAUTH_TOKEN=' "$ENV_FILE"; then
  echo "==> token        : reusing $ENV_FILE"
else
  TOKEN="${CLAUDE_CODE_OAUTH_TOKEN:-}"
  if [ -z "$TOKEN" ]; then
    echo
    echo "This service needs a long-lived token. If you don't have one, run"
    echo "'claude setup-token' in another terminal (as $RUN_USER) and copy the result."
    echo "Paste it here, or press Enter to skip and configure it later:"
    read -rsp "  token: " TOKEN
    echo
  fi
  if [ -n "$TOKEN" ]; then
    mkdir -p "$(dirname "$ENV_FILE")"
    touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
    # replace any existing line, keep other vars
    if [ -s "$ENV_FILE" ]; then
      grep -v '^CLAUDE_CODE_OAUTH_TOKEN=' "$ENV_FILE" > "$ENV_FILE.new" || true
    else
      : > "$ENV_FILE.new"
    fi
    printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$TOKEN" >> "$ENV_FILE.new"
    mv "$ENV_FILE.new" "$ENV_FILE"; chmod 600 "$ENV_FILE"
    echo "==> token        : written to $ENV_FILE (0600)"
  else
    echo "!! No token supplied — the service will start but generation will fail." >&2
    echo "   Fix later with: ./deploy/set-token.sh" >&2
  fi
fi

# Verify using exactly the credential the service will use, and nothing else.
STORED_TOKEN=""
if [ -f "$ENV_FILE" ]; then
  STORED_TOKEN="$(sed -n 's/^CLAUDE_CODE_OAUTH_TOKEN=//p' "$ENV_FILE" | head -1)"
fi
if [ -n "$STORED_TOKEN" ]; then
  echo "==> verifying auth…"
  if env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
        CLAUDE_CODE_OAUTH_TOKEN="$STORED_TOKEN" \
        "$CLAUDE_BIN" -p --max-turns 1 "reply with OK" >/dev/null 2>&1; then
    echo "==> auth         : OK (subscription token accepted)"
  else
    echo "!! The token was rejected — expired or mistyped." >&2
    echo "   Regenerate with 'claude setup-token', then: ./deploy/set-token.sh" >&2
  fi
fi

if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "!! ANTHROPIC_API_KEY is set in your shell. It outranks the OAuth token," >&2
  echo "   so your interactive 'claude' runs are billing the API. The unit below" >&2
  echo "   explicitly unsets it, so the service stays on your subscription." >&2
fi

# --- accounts ---------------------------------------------------------------
# The shared CF_ACCESS_TOKEN is gone: authentication is per-user now. Anything
# left in the env file is ignored, so strip it rather than leaving a dead
# secret lying about in a 0600 file.
if [ -f "$ENV_FILE" ] && grep -q '^CF_ACCESS_TOKEN=' "$ENV_FILE"; then
  grep -v '^CF_ACCESS_TOKEN=' "$ENV_FILE" > "$ENV_FILE.new" && \
    mv "$ENV_FILE.new" "$ENV_FILE" && chmod 600 "$ENV_FILE"
  echo "==> accounts     : removed the obsolete CF_ACCESS_TOKEN from $ENV_FILE"
fi

# Node installs live outside systemd's default PATH; make sure claude's dir is in.
SERVICE_PATH="$(dirname "$CLAUDE_BIN"):/usr/local/bin:/usr/bin:/bin:$RUN_HOME/.local/bin"

mkdir -p "$CONTENT_ROOT"

# --- write the unit -------------------------------------------------------
UNIT=/etc/systemd/system/content-forge.service
echo "==> writing $UNIT"
sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Content Forge — voice-matched article generation
Documentation=file://$APP_DIR/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple

User=$RUN_USER
Group=$RUN_GROUP
Environment=HOME=$RUN_HOME

WorkingDirectory=$APP_DIR

Environment=CF_CONTENT_ROOT=$CONTENT_ROOT
Environment=CF_PORT=$PORT
Environment=CF_HOST=0.0.0.0
Environment=CF_CLAUDE_MODEL=$MODEL
Environment=CF_CLAUDE_BIN=$CLAUDE_BIN
Environment=CF_MAX_CONCURRENT_JOBS=2
Environment=CF_CLAUDE_TIMEOUT=900
Environment=CF_PUBLIC_HOST=$PUBLIC_HOST
Environment=CF_PUBLIC_PORT=$PUBLIC_PORT
Environment=PATH=$SERVICE_PATH

# The subscription token, 0600, kept out of this world-readable unit.
EnvironmentFile=-$ENV_FILE
# An API key would outrank the OAuth token and silently switch to API billing.
UnsetEnvironment=ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN

ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $PORT

Restart=on-failure
RestartSec=5
TimeoutStopSec=20

NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
# '-' prefix = tolerate the path not existing yet (else systemd fails 226/NAMESPACE)
ReadWritePaths=$CONTENT_ROOT -$RUN_HOME/.claude -$RUN_HOME/.config/claude-code

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now content-forge
sleep 2

echo
if systemctl is-active --quiet content-forge; then
  echo "==> running: http://$(hostname -f 2>/dev/null || hostname):$PORT"
  curl -s "http://localhost:$PORT/health" && echo
else
  echo "!! service did not come up. Last 30 log lines:"
  sudo journalctl -u content-forge -n 30 --no-pager
  exit 1
fi
