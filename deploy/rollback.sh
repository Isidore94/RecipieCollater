#!/usr/bin/env bash
# Application rollback: switch 'current' back to the previous release and restart.
#
#   sudo deploy/rollback.sh
#
# IMPORTANT (architecture §10): application rollback and DATA rollback are separate.
# A release that applied a forward-only schema migration cannot be undone by switching
# code alone. If the new release migrated the schema, restore the database from the
# backup taken during the update (see deploy/RESTORE.md) as a deliberate second step.
set -euo pipefail

BASE=/opt/recipecollater

if [[ ! -f "$BASE/previous" ]]; then
  echo "No previous release recorded ($BASE/previous missing)." >&2
  exit 1
fi
PREV="$(cat "$BASE/previous")"
if [[ ! -d "$PREV" ]]; then
  echo "Recorded previous release does not exist: $PREV" >&2
  exit 1
fi

echo "==> Rolling back 'current' to $PREV"
ln -sfn "$PREV" "$BASE/current"
systemctl restart recipecollater-web.service recipecollater-worker.service
sleep 2
curl -fsS "http://127.0.0.1/healthz" >/dev/null && echo "==> Rolled back. If the schema was migrated, restore data per deploy/RESTORE.md."
