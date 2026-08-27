#!/usr/bin/env bash
# Create an account and print a single-use invite link.
#
# There is no self-signup: this box answers on the public internet and every
# generation spends the owner's Claude subscription, so accounts are made by
# someone with a shell here. Hand the printed link to the person; they choose
# their own password and it is never seen by anyone else.
#
#   ./deploy/add-user.sh alice --admin     first account, or another operator
#   ./deploy/add-user.sh bob               an ordinary writer
#
# Other subcommands (list, invite, disable, enable, promote, demote, delete):
#   ./.venv/bin/python -m app.accounts --help
set -euo pipefail
cd "$(dirname "$0")/.."

if [ $# -lt 1 ]; then
  echo "usage: $0 <username> [--admin] [--name \"Display Name\"]" >&2
  exit 2
fi

PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3

# Run as the service user so the database is created with the right owner —
# otherwise the app cannot write to a root-owned auth.db.
if [ "$(id -un)" != "${CF_USER:-ubuntu}" ] && [ "$(id -u)" = "0" ]; then
  exec sudo -u "${CF_USER:-ubuntu}" -E "$PY" -m app.accounts add "$@"
fi
exec "$PY" -m app.accounts add "$@"
