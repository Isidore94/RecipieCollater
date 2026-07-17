# LAN setup — reaching RecipeCollater at `http://recipes.local` ($0, no domain)

RecipeCollater is **LAN-only**: plain HTTP, no domain, no certificates, no port
forwarding (architecture §9). This guide makes the N95 reachable at a friendly name for
every household device.

## 1. Give the N95 a stable IP (DHCP reservation)

In your router's DHCP settings, reserve the N95's current IP to its MAC address. Everything
below assumes the IP never changes.

## 2. Advertise `recipes.local` (mDNS via Avahi)

```sh
sudo apt install avahi-daemon
sudo systemctl enable --now avahi-daemon
```

Avahi publishes the machine's `.local` name automatically. If the hostname isn't already
`recipes`, set it:

```sh
sudo hostnamectl set-hostname recipes
sudo systemctl restart avahi-daemon
```

`recipes.local` now resolves on:
- **iPhone / iPad / Mac** — natively (Bonjour).
- **Windows 10+** — natively.
- **Android 12+** — natively. Older Android: use the reserved IP, or add a router DNS entry.

The reserved **IP address always works** as a fallback on any device.

### Optional: a router DNS entry
If your router supports local DNS records, add `recipes.home` (or similar) → the reserved
IP so even old Android resolves a friendly name. Keep `APP_BASE_URL` in `/etc/recipecollater/env`
matching whatever name you standardise on.

## 3. Port 80

The web service binds port 80 via `CAP_NET_BIND_SERVICE` (no root). Family devices use
`http://recipes.local` with no port suffix.

## 4. Onboard devices

1. Open `http://recipes.local` on the N95 (or any PC) — first run shows the **setup** page;
   create the admin account.
2. Admin → **Devices** → *Invite a device* → open the pairing link on the phone/PC, or type
   the 6-character code. On iPhone, then **Share → Add to Home Screen**.
3. For the Apple Shortcut ingest token (Phase 2), Admin → Devices → *Create ingest token*.

## 5. iOS "Local Network" prompt

The first time an iPhone Shortcut POSTs to the LAN, iOS shows a one-time **Local Network**
permission prompt — tap **Allow**, or ingestion silently fails with a misleading "offline"
error.

## Optional free upgrades (never required)

- **mkcert local CA** → trusted HTTPS on the LAN, which re-enables the native wake-lock API,
  a minimal app-shell service worker, and the Android share target. Set `RC_HTTPS=1` after.
- **Tailscale free tier** → reach the app away from home with a real `ts.net` certificate.

Neither needs a domain purchase. See the plan's `docs/02-architecture.md` §9.
