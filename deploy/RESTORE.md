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
restored database, records `restore_tested_at`, and then prunes to the latest 14 sets.

### Weekly restore smoke test

A backup only counts if it has been restored recently, so the worker also runs a fuller test every
Sunday at 04:15 (after the nightly backup). It can be run by hand at any time:

```sh
/opt/recipecollater/current/.venv/bin/python -m app.manage restore-test            # newest set
/opt/recipecollater/current/.venv/bin/python -m app.manage restore-test --backup <backup_dir>
```

It restores the set into a scratch directory under the data dir (not `/tmp`, which is RAM on some
installs), then checks, in order: the set verifies (every manifest checksum), `PRAGMA
integrity_check` passes on the restored database, the database opens and its recipe count equals
the manifest's `recipe_count`, the restored image count equals the manifest's, and up to 20
evenly spaced image files exist with matching SHA-256. The scratch directory is always deleted.
Sets written before `recipe_count` existed skip only the recipe comparison. Exit status is 0 for OK
and 1 for FAILED; a missing or unverifiable backup is a FAILED result, not a crash.

The outcome (timestamp, backup id, ok, error, counts) is written to
`<data_dir>/restore-test.json` - a file, not a database table, so it survives a database restore and
is readable with `cat` when the web app is down. **Admin -> Dashboard -> System** shows "Last
restore test", and turns red when the test has never run, failed, or is older than 7 days (the
roadmap budget). Red means: run the command above, read the error, and fix the backup path before
you need it.
