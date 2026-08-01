# 0006 — LAN-only, $0 infrastructure: plain HTTP at recipes.local

Date: backfilled 2026-08-01 · Canonical: D10 (`docs/00-overview.md`), CONVENTIONS §11

## Context
The platform must cost nothing to host and never expose family data to the
internet.

## Decision
Plain HTTP at `http://recipes.local` (DHCP reservation + mDNS), uvicorn directly
on port 80, no domain, certificates, reverse proxy, or VPN — and never
port-forwarded. `APP_BASE_URL` is the single source of URLs; `Host` is validated
against `RC_ALLOWED_HOSTS` (LAN-only is not DNS-rebinding protection); ingest
fetches enforce SSRF defense.

## Rationale
Documented in D10 as an owner requirement. Features needing a secure context get
free fallbacks (silent-video screen wake, copy-list-as-text); mkcert/Tailscale
are documented as optional free upgrades, deferred until real use demands them.
