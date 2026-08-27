#!/usr/bin/env bash
# Manage Content Forge accounts from the server.
#
# There is no self-signup: this box answers on the public internet and every
# generation spends the owner's Claude subscription, so accounts are made by
# someone with a shell here. Creating one prints a single-use invite link;
# hand it to the person and they choose their own password, which nobody else
# ever sees.
#
#   ./deploy/add-user.sh alice --admin      create (the bare form)
#   ./deploy/add-user.sh invite alice       reissue a link, voiding the old one
#   ./deploy/add-user.sh list
#   ./deploy/add-user.sh disable bob        also drops their live sessions
#   ./deploy/add-user.sh promote alice
#   ./deploy/add-user.sh --help
#
# Anything that is not a known subcommand is treated as a username to add, so
# the common case stays a one-liner.
set -euo pipefail
cd "$(dirname "$0")/.."

# Adopt the running service's environment. Without this the CLI falls back to
# config.py's defaults and disagrees with the service about two things that
# matter: which database holds the accounts, and what URL to build the invite
# link from — printing a link pointing at a host that does not serve this
# instance.
if command -v systemctl >/dev/null 2>&1 \
   && systemctl cat content-forge >/dev/null 2>&1; then
  for kv in $(systemctl show -p Environment content-forge --value); do
    case "$kv" in
      CF_*) export "$kv" ;;
    esac
  done
fi

PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3

if [ $# -lt 1 ]; then
  echo "usage: $0 <username> [--admin] [--name \"Display Name\"]" >&2
  echo "       $0 <add|invite|list|disable|enable|promote|demote|delete> ..." >&2
  exit 2
fi

case "${1:-}" in
  add|invite|list|disable|enable|promote|demote|delete|-h|--help)
    exec "$PY" -m app.accounts "$@" ;;
  *)
    exec "$PY" -m app.accounts add "$@" ;;
esac
