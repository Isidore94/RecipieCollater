#!/usr/bin/env bash
# Staged, tested update with a consistent rehearsal DB and a stopped-service cutover.
# Usage: sudo deploy/update.sh <source_dir> <commit_sha>
set -euo pipefail

SRC="${1:?usage: update.sh <source_dir> <commit_sha>}"
COMMIT="${2:?usage: update.sh <source_dir> <commit_sha>}"
[[ -f "$SRC/pyproject.toml" && -d "$SRC/app" ]] || \
  { echo "Source is not a RecipeCollater checkout: $SRC" >&2; exit 2; }
[[ "$COMMIT" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "Unsafe release identifier: $COMMIT" >&2; exit 2; }

APP_USER=recipecollater
BASE=/opt/recipecollater
ETC=/etc/recipecollater
RELEASE="$BASE/releases/$COMMIT"
TMP_PORT=8099

[[ -f "$ETC/env" ]] || { echo "Missing $ETC/env" >&2; exit 2; }
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
: "${RC_BACKUP_DIR:?RC_BACKUP_DIR must point to a mounted external backup directory}"

[[ -L "$BASE/current" ]] || { echo "No active installation at $BASE/current" >&2; exit 2; }
[[ ! -e "$RELEASE" ]] || { echo "Release already exists: $RELEASE" >&2; exit 2; }

TMP_DATA="$(mktemp -d)"
SMOKE_PID=""
CUTOVER_STARTED=0
CUTOVER_COMPLETE=0
PREV="$(readlink -f "$BASE/current")"
BACKUP_PATH=""

atomic_switch() {
  local target="$1" temporary="$BASE/.current-$COMMIT-$$"
  rm -f "$temporary"
  ln -s "$target" "$temporary"
  mv -Tf "$temporary" "$BASE/current"
}

cleanup() {
  local status=$?
  trap - EXIT
  if [[ -n "$SMOKE_PID" ]]; then
    kill "$SMOKE_PID" 2>/dev/null || true
    wait "$SMOKE_PID" 2>/dev/null || true
  fi
  if [[ "$status" -ne 0 && "$CUTOVER_STARTED" -eq 1 && "$CUTOVER_COMPLETE" -eq 0 ]]; then
    echo "!! Cutover failed; switching the application back to $PREV" >&2
    atomic_switch "$PREV" || true
    systemctl restart recipecollater-web.service recipecollater-worker.service || true
    if [[ -n "$BACKUP_PATH" ]]; then
      echo "!! If the schema advanced, restore data explicitly from: $BACKUP_PATH" >&2
    fi
  fi
  rm -rf "$TMP_DATA"
  exit "$status"
}
trap cleanup EXIT

echo "==> [1/7] Staging root-owned release $COMMIT"
mkdir -p "$RELEASE"
tar -C "$SRC" --exclude=.git --exclude=data --exclude=.venv -cf - . | tar -C "$RELEASE" -xf -
printf '%s\n' "$COMMIT" > "$RELEASE/.release-id"
cd "$RELEASE"

echo "==> [2/7] Building the locked environment and running every offline gate"
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
env -u RC_BACKUP_DIR RC_DATA_DIR="$TMP_DATA/build-test-data" \
  RC_ALLOWED_HOSTS="testserver" RC_SETUP_TOKEN="deployment-test-only" uv run pytest
uv sync --no-dev --frozen

echo "==> [3/7] Creating a consistent rehearsal snapshot (never copying a hot WAL DB)"
install -d -o "$APP_USER" -g "$APP_USER" "$TMP_DATA"
sudo -u "$APP_USER" env RC_DATA_DIR="$DATA" \
  "$RELEASE/.venv/bin/python" -m app.manage snapshot-db "$TMP_DATA/recipecollater.db"
for tree in images artifacts; do
  if [[ -d "$DATA/$tree" ]]; then
    cp -a "$DATA/$tree" "$TMP_DATA/$tree"
  else
    install -d "$TMP_DATA/$tree"
  fi
done
chown -R "$APP_USER:$APP_USER" "$TMP_DATA"

echo "==> [4/7] Rehearsing migrations on the consistent snapshot"
sudo -u "$APP_USER" env RC_DATA_DIR="$TMP_DATA" \
  "$RELEASE/.venv/bin/python" -m app.manage migrate

echo "==> [5/7] Smoke-testing release $COMMIT on port $TMP_PORT"
sudo -u "$APP_USER" env RC_DATA_DIR="$TMP_DATA" APP_BASE_URL="http://127.0.0.1:$TMP_PORT" \
  RC_ALLOWED_HOSTS="127.0.0.1" "$RELEASE/.venv/bin/uvicorn" app.main:app \
  --host 127.0.0.1 --port "$TMP_PORT" --workers 1 --no-access-log &
SMOKE_PID=$!
SMOKE_OK=0
for _ in $(seq 1 30); do
  if BODY="$(curl -fsS "http://127.0.0.1:$TMP_PORT/healthz?include_worker=false" 2>/dev/null)" && \
     python3 -c 'import json,sys; d=json.loads(sys.argv[1]); raise SystemExit(d.get("release_id") != sys.argv[2] or d.get("status") != "ok")' "$BODY" "$COMMIT"; then
    SMOKE_OK=1
    break
  fi
  sleep 1
done
[[ "$SMOKE_OK" -eq 1 ]] || { echo "Temporary-port health check failed." >&2; exit 1; }
kill "$SMOKE_PID" 2>/dev/null || true
wait "$SMOKE_PID" 2>/dev/null || true
SMOKE_PID=""

echo "==> [6/7] Entering maintenance window and taking the final external backup"
systemctl stop recipecollater-worker.service recipecollater-web.service
CUTOVER_STARTED=1
BACKUP_OUTPUT="$(sudo -u "$APP_USER" env RC_DATA_DIR="$DATA" RC_BACKUP_DIR="$RC_BACKUP_DIR" \
  "$RELEASE/.venv/bin/python" -m app.manage backup)"
echo "$BACKUP_OUTPUT"
BACKUP_PATH="$(echo "$BACKUP_OUTPUT" | tail -n 1)"
[[ -d "$BACKUP_PATH" ]] || { echo "Backup command did not return a valid set." >&2; exit 1; }
printf '%s\n' "$PREV" > "$BASE/previous"
printf '%s\n' "$BACKUP_PATH" > "$BASE/previous-backup"

echo "==> [7/7] Migrating live data, atomically switching, and verifying web + worker"
sudo -u "$APP_USER" env RC_DATA_DIR="$DATA" \
  "$RELEASE/.venv/bin/python" -m app.manage migrate
atomic_switch "$RELEASE"
systemctl start recipecollater-web.service recipecollater-worker.service

FINAL_OK=0
for _ in $(seq 1 30); do
  if BODY="$(curl -fsS http://127.0.0.1/healthz 2>/dev/null)" && \
     python3 -c 'import json,sys; d=json.loads(sys.argv[1]); raise SystemExit(d.get("release_id") != sys.argv[2] or d.get("status") != "ok")' "$BODY" "$COMMIT"; then
    FINAL_OK=1
    break
  fi
  sleep 1
done
[[ "$FINAL_OK" -eq 1 ]] || { echo "Post-switch health check failed." >&2; exit 1; }
CUTOVER_COMPLETE=1
echo "==> Update to $COMMIT complete. Rollback backup: $BACKUP_PATH"
