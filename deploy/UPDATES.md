# Updating RecipeCollater (staged, health-checked, reversible)

Updates are **staged** and never mutate the live environment in place (architecture §10,
CONVENTIONS §12). `pip install -U` in a running environment is forbidden.

## How an update runs (`deploy/update.sh`)

```sh
sudo deploy/update.sh /path/to/checked-out-source <commit_sha>
```

1. **Stage** a root-owned release under `/opt/recipecollater/releases/<commit>/` and write its
   exact release marker.
2. **Build + test** from the committed lock: lint, format, strict types, and all offline tests.
3. **Snapshot** the live SQLite database through its online backup API. Never `cp` a hot WAL file.
   Only images/artifacts—not queue state or old backups—join the rehearsal data.
4. **Rehearse** migrations on that consistent snapshot.
5. **Smoke test** the exact release ID on a temporary port against the migrated rehearsal data.
6. **Maintenance window**: stop worker and web, then create and scratch-restore a backup on the
   configured external filesystem. No old code can write after this final backup.
7. **Cut over**: migrate live data, atomically rename a temporary `current` symlink into place,
   start both services, and require `/healthz` to report the expected web and worker release ID.

Any pre-cutover failure leaves the live application untouched. A cutover failure automatically
switches the application symlink back and records the exact backup set required if the schema also
needs an explicit data rollback.

## Rolling back

```sh
sudo deploy/rollback.sh
# If the old application cannot read the advanced schema:
sudo deploy/rollback.sh --restore /mnt/backup/recipecollater/<backup-set>
```

Switches `current` back to the previous release and restarts.

**Application rollback ≠ data rollback.** A forward-only schema migration is not undone by
switching code. Data restoration is therefore an explicit `--restore <backup-set>` choice. The
failed data directory is preserved rather than deleted (see `RESTORE.md`).

## Hot dependencies (yt-dlp)

yt-dlp breaks when YouTube changes (Phase 2+). The app checks for a newer release weekly and
surfaces it in admin, but it is **never** auto-installed into the live environment — a bump
ships as a normal reviewed dependency change through this same staged flow.
