#!/usr/bin/env bash
# Staged, health-checked update with safe rollback (architecture §10).
# Never mutates the live environment with `pip install -U`; builds a new versioned
# release, tests it offline, rehearses the migration on a copy, smoke-tests the new
# code on a temporary port against a copy, and only then migrates live + switches.
#
#   sudo deploy/update.sh <source_dir> <commit_sha>
set -euo pipefail

SRC="${1:?usage: update.sh <source_dir> <commit_sha>}"
COMMIT="${2:?usage: update.sh <source_dir> <commit_sha>}"

APP_USER=recipecollater
BASE=/opt/recipecollater
DATA=/var/lib/recipecollater
ETC=/etc/recipecollater
RELEASE="$BASE/releases/$COMMIT"
TMP_PORT=8099
TMP_DATA="$(mktemp -d)"
trap 'rm -rf "$TMP_DATA"' EXIT

run_as() { sudo -u "$APP_USER" env RC_DATA_DIR="$1" "$RELEASE/.venv/bin/python" -m app.manage "${@:2}"; }

echo "==> [1/6] Staging release $COMMIT"
rm -rf "$RELEASE"; mkdir -p "$RELEASE"
tar -C "$SRC" --exclude=.git --exclude=data --exclude=.venv -cf - . | tar -C "$RELEASE" -xf -
cd "$RELEASE"

echo "==> [2/6] Building env WITH dev deps and running the offline test suite"
uv sync
uv run pytest -q
# Slim to production dependencies for the running services.
uv sync --no-dev
chown -R "$APP_USER:$APP_USER" "$RELEASE"

echo "==> [3/6] Verified backup of live data before touching it"
sudo -u "$APP_USER" env RC_DATA_DIR="$DATA" "$RELEASE/.venv/bin/python" -m app.manage backup

echo "==> [4/6] Rehearsing migration on a copy of the live database"
cp -a "$DATA/." "$TMP_DATA/"
chown -R "$APP_USER:$APP_USER" "$TMP_DATA"
run_as "$TMP_DATA" migrate
run_as "$TMP_DATA" schema-version

echo "==> [5/6] Smoke-testing the new code on port $TMP_PORT against the migrated copy"
sudo -u "$APP_USER" env RC_DATA_DIR="$TMP_DATA" APP_BASE_URL="http://127.0.0.1:$TMP_PORT" \
  "$RELEASE/.venv/bin/uvicorn" app.main:app --host 127.0.0.1 --port "$TMP_PORT" --workers 1 &
SMOKE_PID=$!
trap 'kill "$SMOKE_PID" 2>/dev/null || true; rm -rf "$TMP_DATA"' EXIT
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$TMP_PORT/healthz" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
kill "$SMOKE_PID" 2>/dev/null || true
if [[ "${ok:-0}" != "1" ]]; then
  echo "!! Health check failed — aborting. Live install is UNCHANGED." >&2
  exit 1
fi

echo "==> [6/6] Health OK. Migrating live DB, switching 'current', restarting services"
PREV="$(readlink -f "$BASE/current" || true)"
echo "$PREV" > "$BASE/previous"   # remembered for rollback
sudo -u "$APP_USER" env RC_DATA_DIR="$DATA" "$RELEASE/.venv/bin/python" -m app.manage migrate
ln -sfn "$RELEASE" "$BASE/current"
systemctl restart recipecollater-web.service recipecollater-worker.service
sleep 2
curl -fsS "http://127.0.0.1/healthz" >/dev/null && echo "==> Update to $COMMIT complete."
