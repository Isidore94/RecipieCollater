# Vendored front-end libraries

Self-hosted, no third-party CDN at runtime (CONVENTIONS §8 — the LAN app must work
offline). Pin the version and verify the SHA-256 when updating; updates go through the
normal reviewed dependency-bump process.

| File | Library | Version | SHA-256 |
|---|---|---|---|
| `htmx.min.js` | htmx | 2.0.4 | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |
| `alpine.min.js` | Alpine.js | 3.14.8 | `b600e363d99d95444db54acbfb2deffec9ae792aa99a09229bcda078e5b55643` |

Sources (fetch only when deliberately updating, never at runtime):
- htmx: `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js`
- Alpine: `https://cdn.jsdelivr.net/npm/alpinejs@3.14.8/dist/cdn.min.js`

Verify after downloading:

```sh
sha256sum htmx.min.js alpine.min.js
```
