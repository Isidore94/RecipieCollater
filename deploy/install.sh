#!/usr/bin/env bash
# First-time install of RecipeCollater on the N95 (LAN-only, $0 infrastructure).
# Idempotent enough to re-run. Run as root.
#
#   sudo deploy/install.sh <source_dir> <commit_sha>
#
# Layout created:
#   /opt/recipecollater/releases/<commit>/   versioned release (code + .venv)
#   /opt/recipecollater/current              -> active release (symlink)
#   /var/lib/recipecollater/                 runtime data (db, images, artifacts, backups)
#   /etc/recipecollater/env                  non-secret config + provider keys (0600)
set -euo pipefail

SRC="${1:?usage: install.sh <source_dir> <commit_sha>}"
COMMIT="${2:?usage: install.sh <source_dir> <commit_sha>}"

APP_USER=recipecollater
BASE=/opt/recipecollater
DATA=/var/lib/recipecollater
ETC=/etc/recipecollater
RELEASE="$BASE/releases/$COMMIT"

echo "==> Ensuring service user and directories"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" "$DATA" "$BASE/releases"
install -d "$ETC"

if [[ ! -f "$ETC/env" ]]; then
  echo "==> Installing example env to $ETC/env (edit it before first real use)"
  install -m 0600 "$SRC/deploy/env.example" "$ETC/env"
fi

echo "==> Staging release $COMMIT"
rm -rf "$RELEASE"
mkdir -p "$RELEASE"
# Copy the tree without VCS/data cruft.
tar -C "$SRC" --exclude=.git --exclude=data --exclude=.venv -cf - . | tar -C "$RELEASE" -xf -

echo "==> Building isolated uv environment (production deps only)"
cd "$RELEASE"
uv sync --no-dev --frozen 2>/dev/null || uv sync --no-dev
chown -R "$APP_USER:$APP_USER" "$RELEASE"

echo "==> Applying migrations to the live database"
# shellcheck disable=SC1091
set -a; . "$ETC/env"; set +a
sudo -u "$APP_USER" env "RC_DATA_DIR=${RC_DATA_DIR:-$DATA}" \
  "$RELEASE/.venv/bin/python" -m app.manage migrate

echo "==> Switching 'current' symlink"
ln -sfn "$RELEASE" "$BASE/current"

echo "==> Installing systemd units"
install -m 0644 "$SRC/deploy/systemd/recipecollater-web.service" /etc/systemd/system/
install -m 0644 "$SRC/deploy/systemd/recipecollater-worker.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now recipecollater-web.service recipecollater-worker.service

echo "==> Done. See deploy/LAN.md to finish mDNS/DHCP setup so the family can reach"
echo "    http://recipes.local . Verify: curl -s http://127.0.0.1/healthz"
