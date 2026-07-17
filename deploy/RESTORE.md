# Restore & bare-machine recovery

A backup is trusted only after it has been **restored** (CONVENTIONS §14). Backups are
*sets*, not a lone DB file: a SQLite online-backup snapshot plus `images/` and
`artifacts/` trees, described by a checksum `manifest.json`.

## What a backup set contains

```
<backup_root>/<timestamp>-<id>/
  recipecollater.db     transactionally consistent SQLite snapshot
  images/               recipe images (empty until Phase 1)
  artifacts/            immutable ingestion artifacts (empty until Phase 2)
  manifest.json         schema/release version, complete file hashes, integrity + restore time
```

`<backup_root>` is `RC_BACKUP_DIR` and must already be mounted on a **different filesystem**
(USB/NAS). The app refuses a missing mount or a destination on the data filesystem.

## Verify a backup without restoring

```sh
/opt/recipecollater/current/.venv/bin/python -m app.manage verify-backup <backup_dir>
```

Requires the manifest to list every file exactly once, re-hashes each file, and runs
`PRAGMA integrity_check`. A newly created backup is called healthy only after its automatic scratch
restore has also succeeded.

## Restore into a data directory

```sh
# Restore into a fresh directory first, inspect, then swap.
python -m app.manage restore <backup_dir> /var/lib/recipecollater.restored
```

Restore refuses to run unless the backup verifies and the target is empty. To make it live:

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

The nightly worker immediately restores every new set into a private scratch directory, checks the
restored database, records `restore_tested_at`, and then prunes to the latest 14 sets. Phase 6 adds
weekly retention and surfaces backup/restore age in Admin.
