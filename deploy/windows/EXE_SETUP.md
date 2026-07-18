# RecipeCollater — Windows desktop app

`RecipeCollater.exe` runs the whole thing on your mini PC: the web app your family opens
from their phones **and** the background worker that imports web/YouTube recipes. No Python
install, no terminals.

## First launch

1. Put `RecipeCollater.exe` in a permanent folder (e.g. `C:\RecipeCollater\`).
2. Copy `.env.example` beside it as `.env` and fill it in — at minimum your
   `RC_OPENAI_API_KEY` (or `RC_ANTHROPIC_API_KEY`). Set `RC_ALLOWED_HOSTS` / `APP_BASE_URL`
   to this PC's LAN name and IP, and keep `RC_DATA_DIR` pointed at your existing recipes if
   you have them.
3. Double-click `RecipeCollater.exe`. A small control window opens, starts the web server and
   the worker, and shows the LAN address (with **Open** and **Copy link**).
4. Open that address on a phone/PC on the same network to use the app.

The `.env` stays beside the exe and is never embedded in it.

## Port 80 vs a high port

`RC_PORT=80` gives clean URLs (`http://mini-pc`) but Windows requires admin to bind port 80,
so either run the exe as administrator or, simpler, set `RC_PORT=8765` in `.env` and use
`http://mini-pc:8765`. Whatever you choose, set `APP_BASE_URL` to match so the in-app
"Add to phone" instructions show the right address.

## Firewall (once)

So other devices can reach it, allow the port through Windows Firewall — run once in an
**admin** PowerShell (change 80 to your `RC_PORT`):

```powershell
New-NetFirewallRule -DisplayName RecipeCollater -Direction Inbound -Protocol TCP -LocalPort 80 -Action Allow
```

## Start automatically at logon

Easiest — run this once (from the repo, or point it at wherever you put the exe). It creates the
logon shortcut **and** the firewall rule:

```powershell
deploy\windows\install-autostart.ps1 -ExePath C:\RecipeCollater\RecipeCollater.exe -Port 80
```

(Run it in an **admin** PowerShell so the firewall part succeeds.) Or do it by hand: press
`Win+R`, type `shell:startup`, and drop a **shortcut** to `RecipeCollater.exe` in that folder.

Because it opens a window, Windows needs a signed-in desktop session — fine for a mini PC set to
auto-login. If the app must run with nobody logged in, install it as a service instead (e.g. with
NSSM, one service at `RecipeCollater.exe --web` and another at `RecipeCollater.exe --worker`).

## Bringing your existing recipes across

Your recipes live in the `data` folder. Point `RC_DATA_DIR` in `.env` at your current one (the
example already does), or copy that folder next to the exe. The app migrates the database
forward automatically on first start and snapshots it beforehand.

## Backups

Set `RC_BACKUP_DIR` in `.env` to a folder on a **second drive** (e.g. an external HDD bay at
`D:\RecipeCollaterBackups`). The worker writes a verified backup set there nightly - a consistent
database snapshot plus images and artifacts, each checksummed and test-restored - and keeps the
newest 14. The app refuses a backup destination on the same physical disk as your data, so a disk
failure can't take both. Make one immediately with:

```powershell
RecipeCollater.exe --backup
```

Keep the PC awake overnight (or the nightly run is skipped until it next wakes). Restore a set with
`RecipeCollater.exe --restore <backup-folder> <empty-target-folder>`.

## Command-line modes (for a service or debugging)

```
RecipeCollater.exe              the control window (starts web + worker)
RecipeCollater.exe --web        only the web server
RecipeCollater.exe --worker     only the ingestion worker
RecipeCollater.exe --smoke-test check the bundle serves, then exit (prints SMOKE OK)
```

## Rebuilding

From the repository (needs `uv`):

```powershell
deploy\windows\build_exe.ps1
```

The finished bundle is `dist\RecipeCollater.exe`.
