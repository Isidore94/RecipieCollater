#!/usr/bin/env bash
# Roll back to the recorded prior release. Data restoration is explicit:
#   sudo deploy/rollback.sh
#   sudo deploy/rollback.sh --restore /mnt/backup/recipecollater/<backup-set>
set -euo pipefail

BASE=/opt/recipecollater
ETC=/etc/recipecollater
APP_USER=recipecollater
RESTORE_FROM=""
if [[ "${1:-}" == "--restore" ]]; then
  [[ "$#" -eq 2 ]] || { echo "usage: rollback.sh [--restore <backup_dir>]" >&2; exit 2; }
  RESTORE_FROM="${2:?usage: rollback.sh [--restore <backup_dir>]}"
elif [[ "$#" -ne 0 ]]; then
  echo "usage: rollback.sh [--restore <backup_dir>]" >&2
  exit 2
fi

[[ -f "$BASE/previous" ]] || { echo "No previous release recorded." >&2; exit 1; }
[[ -f "$ETC/env" ]] || { echo "Missing $ETC/env" >&2; exit 1; }
PREV="$(cat "$BASE/previous")"
CURRENT="$(readlink -f "$BASE/current")"
case "$PREV" in
  "$BASE"/releases/*) ;;
  *) echo "Recorded previous release is outside $BASE/releases: $PREV" >&2; exit 1 ;;
esac
[[ -d "$PREV" ]] || { echo "Previous release does not exist: $PREV" >&2; exit 1; }

set -a
# shellcheck disable=SC1091
. "$ETC/env"
set +a
DATA="${RC_DATA_DIR:-/var/lib/recipecollater}"
case "$DATA" in
  /*) ;;
  *) echo "RC_DATA_DIR must be an absolute path: $DATA" >&2; exit 2 ;;
esac
case "$DATA" in
  /|/var|/var/lib|/opt|/home) echo "Refusing unsafe RC_DATA_DIR: $DATA" >&2; exit 2 ;;
esac

atomic_switch() {
  local target="$1" temporary="$BASE/.rollback-current-$$"
  ln -s "$target" "$temporary"
  mv -Tf "$temporary" "$BASE/current"
}

ROLLBACK_SWITCHED=0
recover_on_error() {
  local status=$?
  trap - EXIT
  if [[ "$status" -ne 0 && "$ROLLBACK_SWITCHED" -eq 0 ]]; then
    echo "Rollback preparation failed; restarting the current release." >&2
    atomic_switch "$CURRENT" || true
    systemctl start recipecollater-web.service recipecollater-worker.service || true
  fi
  exit "$status"
}
trap recover_on_error EXIT

if [[ -n "$RESTORE_FROM" ]]; then
  [[ -d "$RESTORE_FROM" ]] || { echo "Backup set does not exist: $RESTORE_FROM" >&2; exit 1; }
fi

systemctl stop recipecollater-worker.service recipecollater-web.service

if [[ -n "$RESTORE_FROM" ]]; then
  RESTORE_TMP="${DATA}.restore.$$"
  FAILED_DATA="${DATA}.failed.$(date -u +%Y%m%dT%H%M%SZ)"
  [[ ! -e "$RESTORE_TMP" ]] || { echo "Restore scratch path exists: $RESTORE_TMP" >&2; exit 1; }
  install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$RESTORE_TMP"
  sudo -u "$APP_USER" env RC_DATA_DIR="$DATA" \
    "$CURRENT/.venv/bin/python" -m app.manage restore "$RESTORE_FROM" "$RESTORE_TMP"
  mv "$DATA" "$FAILED_DATA"
  mv "$RESTORE_TMP" "$DATA"
  chown -R "$APP_USER:$APP_USER" "$DATA"
  echo "Restored data. The failed data directory is preserved at $FAILED_DATA"
fi

atomic_switch "$PREV"
systemctl start recipecollater-web.service recipecollater-worker.service
ROLLBACK_SWITCHED=1

for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1/healthz >/dev/null 2>&1; then
    echo "Rolled back to $PREV"
    exit 0
  fi
  sleep 1
done

if [[ -z "$RESTORE_FROM" ]]; then
  RECORDED="$(cat "$BASE/previous-backup" 2>/dev/null || true)"
  echo "Old code could not start against the current schema." >&2
  echo "Re-run with an explicit data restore: deploy/rollback.sh --restore ${RECORDED:-<backup-set>}" >&2
else
  echo "Rollback restore completed, but health is still failing. Inspect the service journals." >&2
fi
exit 1
