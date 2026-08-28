#!/usr/bin/env bash
# Nightly snapshot of everything that cannot be rebuilt from git.
#
# Two kinds of state live on this box and neither is in the repo:
#
#   auth.db   accounts, sessions, personal access tokens, OAuth grants
#   topics/   the articles and idea lists
#   voices/   writing samples and the built profiles — the expensive one,
#             since a profile is derived from someone's real writing
#
# auth.db is copied through SQLite's backup API rather than `cp`. The database
# runs in WAL mode, so a plain file copy taken mid-write yields a snapshot with
# committed transactions still sitting in the -wal file: it restores, and it is
# silently short. The backup API takes a consistent point-in-time copy of a
# live database instead.
#
# Local disk only. That covers the cases this actually protects against — a
# bad deploy, a mistaken delete, a corrupted profile — but NOT loss of the
# instance. For that these need to leave the box (S3 needs an IAM role, which
# this instance does not currently have).
set -euo pipefail

CONTENT_ROOT="${CF_CONTENT_ROOT:-/home/ubuntu/content-forge-data}"
DEST="${CF_BACKUP_DIR:-/home/ubuntu/backups}"
KEEP_DAYS="${CF_BACKUP_KEEP:-14}"
PY="${CF_PYTHON:-/opt/content-forge/.venv/bin/python}"
[ -x "$PY" ] || PY=python3

STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$DEST"
chmod 700 "$DEST"

# --- consistent copy of the account database ------------------------------
if [ -f "$CONTENT_ROOT/auth.db" ]; then
  "$PY" - "$CONTENT_ROOT/auth.db" "$WORK/auth.db" <<'PYEOF'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
d = sqlite3.connect(dst)
with d:
    s.backup(d)          # atomic snapshot of a live WAL database
s.close(); d.close()
PYEOF
  chmod 600 "$WORK/auth.db"
fi

# --- content --------------------------------------------------------------
for dir in topics voices; do
  if [ -d "$CONTENT_ROOT/$dir" ]; then
    cp -a "$CONTENT_ROOT/$dir" "$WORK/$dir"
  fi
done

ARCHIVE="$DEST/content-forge-$STAMP.tar.gz"
tar -czf "$ARCHIVE" -C "$WORK" .
# The archive contains password hashes and live token hashes, so it is no more
# readable than the database it came from.
chmod 600 "$ARCHIVE"

# --- prune ----------------------------------------------------------------
find "$DEST" -maxdepth 1 -name 'content-forge-*.tar.gz' -type f \
     -mtime "+$KEEP_DAYS" -delete

SIZE="$(du -h "$ARCHIVE" | cut -f1)"
COUNT="$(find "$DEST" -maxdepth 1 -name 'content-forge-*.tar.gz' | wc -l)"
echo "backup ok: $ARCHIVE ($SIZE); $COUNT kept, pruning older than ${KEEP_DAYS}d"
