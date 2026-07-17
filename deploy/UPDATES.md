# Updating RecipeCollater (staged, health-checked, reversible)

Updates are **staged** and never mutate the live environment in place (architecture §10,
CONVENTIONS §12). `pip install -U` in a running environment is forbidden.

## How an update runs (`deploy/update.sh`)

```sh
sudo deploy/update.sh /path/to/checked-out-source <commit_sha>
```

1. **Stage** a new versioned release under `/opt/recipecollater/releases/<commit>/`.
2. **Build + test**: create a fresh uv environment *with* dev deps and run the full offline
   test suite. Any failure aborts with the live install untouched. Then slim to
   production deps (`uv sync --no-dev`).
3. **Backup**: take a verified backup of live data (`manage backup`).
4. **Rehearse** the migration on a *copy* of the live database and print the resulting
   schema version. A failed migration aborts before anything live changes.
5. **Smoke test**: boot the new code on a temporary port against the migrated copy and poll
   `/healthz`. Failure aborts; live is untouched.
6. **Switch**: only now migrate the live database (forward-only, already rehearsed), point
   `current` at the new release, and restart the services. Records the prior release in
   `/opt/recipecollater/previous`.

## Rolling back

```sh
sudo deploy/rollback.sh
```

Switches `current` back to the previous release and restarts.

**Application rollback ≠ data rollback.** A forward-only schema migration is not undone by
switching code. If the failed release migrated the schema, restore the database from the
backup taken in step 3 as a deliberate second action (see `RESTORE.md`).

## Hot dependencies (yt-dlp)

yt-dlp breaks when YouTube changes (Phase 2+). The app checks for a newer release weekly and
surfaces it in admin, but it is **never** auto-installed into the live environment — a bump
ships as a normal reviewed dependency change through this same staged flow.
