#!/usr/bin/env bash
# First-time install of RecipeCollater on the N95. Run as root.
# Usage: sudo deploy/install.sh <source_dir> <commit_sha>
set -euo pipefail

SRC="${1:?usage: install.sh <source_dir> <commit_sha>}"
COMMIT="${2:?usage: install.sh <source_dir> <commit_sha>}"
[[ -f "$SRC/pyproject.toml" && -d "$SRC/app" ]] || \
  { echo "Source is not a RecipeCollater checkout: $SRC" >&2; exit 2; }
[[ "$COMMIT" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "Unsafe release identifier: $COMMIT" >&2; exit 2; }

APP_USER=recipecollater
BASE=/opt/recipecollater
ETC=/etc/recipecollater
RELEASE="$BASE/releases/$COMMIT"

atomic_switch() {
  local target="$1" temporary="$BASE/.current-$COMMIT-$$"
  ln -s "$target" "$temporary"
  mv -Tf "$temporary" "$BASE/current"
}

echo "==> Ensuring service user and root-owned release directories"
id -u "$APP_USER" >/dev/null 2>&1 || \
  useradd --system --home /var/lib/recipecollater --shell /usr/sbin/nologin "$APP_USER"
install -d -o root -g root -m 0755 "$BASE" "$BASE/releases" "$ETC"

if [[ -e "$BASE/current" || -L "$BASE/current" ]]; then
  echo "An installation already exists; use deploy/update.sh instead." >&2
  exit 2
fi
if [[ -e "$RELEASE" ]]; then
  echo "Release already exists: $RELEASE" >&2
  exit 2
fi

if [[ ! -f "$ETC/env" ]]; then
  ENV_TMP="$(mktemp)"
  cp "$SRC/deploy/env.example" "$ENV_TMP"
  install -o root -g root -m 0600 "$ENV_TMP" "$ETC/env"
  rm -f "$ENV_TMP"
fi
if ! grep -q '^RC_SETUP_TOKEN=.' "$ETC/env"; then
  SETUP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  printf '\nRC_SETUP_TOKEN=%s\n' "$SETUP_TOKEN" >> "$ETC/env"
  echo "==> First-run setup token (save this until the admin is created): $SETUP_TOKEN"
fi
chown root:root "$ETC/env"
chmod 0600 "$ETC/env"

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
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$DATA"

echo "==> Staging root-owned release $COMMIT"
mkdir -p "$RELEASE"
tar -C "$SRC" --exclude=.git --exclude=data --exclude=.venv -cf - . | tar -C "$RELEASE" -xf -
printf '%s\n' "$COMMIT" > "$RELEASE/.release-id"

echo "==> Building locked production environment"
cd "$RELEASE"
uv sync --no-dev --frozen

echo "==> Applying initial migrations"
sudo -u "$APP_USER" env RC_DATA_DIR="$DATA" \
  "$RELEASE/.venv/bin/python" -m app.manage migrate

atomic_switch "$RELEASE"
install -m 0644 "$SRC/deploy/systemd/recipecollater-web.service" /etc/systemd/system/
install -m 0644 "$SRC/deploy/systemd/recipecollater-worker.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now recipecollater-web.service recipecollater-worker.service

echo "==> Waiting for web + worker health"
for _ in $(seq 1 30); do
  if BODY="$(curl -fsS http://127.0.0.1/healthz 2>/dev/null)" && \
     python3 -c 'import json,sys; d=json.loads(sys.argv[1]); raise SystemExit(d.get("release_id") != sys.argv[2] or d.get("status") != "ok")' "$BODY" "$COMMIT"; then
    echo "==> Install complete: http://recipes.local"
    exit 0
  fi
  sleep 1
done
echo "Install staged but health did not become ready; inspect systemctl status and journalctl." >&2
exit 1
