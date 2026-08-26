#!/usr/bin/env bash
# Dev / manual start. For the always-on box use the systemd unit instead.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "==> creating virtualenv"
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
fi

export CF_CONTENT_ROOT="${CF_CONTENT_ROOT:-$HOME/content-forge-data}"
mkdir -p "$CF_CONTENT_ROOT"

echo "==> content root: $CF_CONTENT_ROOT"
if ! command -v "${CF_CLAUDE_BIN:-claude}" >/dev/null 2>&1; then
  echo "!!  claude CLI not on PATH — generation will fail until it is."
elif ! claude -p --max-turns 1 "reply with OK" >/dev/null 2>&1; then
  echo "!!  claude CLI found but not authenticated — run: claude setup-token"
fi

exec ./.venv/bin/uvicorn app.main:app \
  --host "${CF_HOST:-0.0.0.0}" --port "${CF_PORT:-8080}" "$@"
