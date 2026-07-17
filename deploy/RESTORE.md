# Restore & bare-machine recovery

A backup is trusted only after it has been **restored** (CONVENTIONS §14). Backups are
*sets*, not a lone DB file: a `VACUUM INTO` database snapshot plus `images/` and
`artifacts/` trees, described by a checksum `manifest.json`.

## What a backup set contains

```
<backup_root>/<timestamp>-<id>/
  recipecollater.db     VACUUM INTO snapshot
  images/               recipe images (empty until Phase 1)
  artifacts/            immutable ingestion artifacts (empty until Phase 2)
  manifest.json         schema version, app version, per-file SHA-256, integrity flag
```

`<backup_root>` is `RC_BACKUP_DIR` (should be a **different physical device** — USB/NAS).

## Verify a backup without restoring

```sh
/opt/recipecollater/current/.venv/bin/python -m app.manage verify-backup <backup_dir>
```

Re-hashes every file against the manifest and runs `PRAGMA integrity_check`. Exit 0 = healthy.

## Restore into a data directory

```sh
# Restore into a fresh directory first, inspect, then swap.
python -m app.manage restore <backup_dir> /var/lib/recipecollater.restored
```

Restore refuses to run unless the backup verifies. To make it live:

```sh
sudo systemctl stop recipecollater-web recipecollater-worker
sudo mv /var/lib/recipecollater /var/lib/recipecollater.old
sudo mv /var/lib/recipecollater.restored /var/lib/recipecollater
sudo chown -R recipecollater:recipecollater /var/lib/recipecollater
sudo systemctl start recipecollater-web recipecollater-worker
curl -fsS http://127.0.0.1/healthz
```

## Bare-machine recovery (new N95)

1. Install the OS; install `uv`, `avahi-daemon`.
2. `sudo deploy/install.sh <source_dir> <commit_sha>` (creates user, release, env, services).
3. Stop the services, restore the newest healthy backup into `/var/lib/recipecollater`
   (steps above), start the services.
4. Re-run `deploy/LAN.md` steps (DHCP reservation, hostname) so `recipes.local` resolves.
5. Re-pair devices from Admin → Devices if their cookies were lost.

## Restore-test cadence

The nightly worker creates a backup; a restore **smoke test** must run on a schedule (a
scheduled restore into a scratch directory + `verify-backup`). Admin surfaces the last
healthy backup age and the last restore-test age; treat >48 h since backup or >7 days since a
restore test as red (roadmap "Measurable budgets").
