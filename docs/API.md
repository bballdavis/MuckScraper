# Mobile API (for the MuckScroller app)

An optional JSON API under `/api/v1` that the MuckScroller iPhone app reads.
It runs as its own container with its own small Flask app
(`aggregator/api_app.py`) that registers **only** the API: no web pages, no
admin, no login. That makes it the one part of MuckScraper you can put on the
internet. The web app and admin stay on your network.

## Turning it on

1. Start the container: `docker compose --profile api up -d api`. It listens on
   `API_BIND:API_PORT`, which defaults to `127.0.0.1:5050`.
2. Put a tunnel or reverse proxy with TLS in front of that port (Cloudflare
   Tunnel, Tailscale, Caddy). Set `API_PROXY_HOPS` to the number of proxies in
   front of the API, so rate limits see the phone's real address (`0` if the
   phone connects directly).
3. In **Settings › Mobile API**, enter the public address
   (`https://api.example.com`), pick a user and press **Create pairing code**.
4. In MuckScroller, scan the QR code or type the code. The code works once
   and expires after 10 minutes. The phone gets a device token; no password
   crosses the internet.

On the same page you can sign out one device or all of them. Disabling a user
in Settings › Users signs out all of their devices on their next request.

## Security

- **Isolation:** the API process has only `/api/v1/*` routes, which a test
  checks. Content endpoints are read-only. The only writes are a device's own
  pairing, sign-out and last-used time, and the user's own feed preferences.
- **Tokens:** a token is `msk_` followed by 32 random bytes. Only its SHA-256
  is stored. Tokens expire after a year, and pairing again issues a new one.
  Pairing codes are also stored hashed.
- **Rate limits** are counted in Postgres and shared by every worker. They
  apply per device and per IP, plus a tighter limit on pairing attempts per
  IP. You can change all three in Settings › Mobile API. A request over a
  limit gets `429` with `Retry-After`.
- **Input:** every parameter is validated, and an unknown parameter is a
  `400`. Bodies are capped at 16 KB, and queries time out after 5 seconds.
- **Output:**
  - Responses are built field by field (`aggregator/api/serializers.py`).
    Scrape status, failure reasons, grouping data and model or host names
    never leave the server.
  - Errors are JSON (`{"error": {"code", "message"}}`) and never include a
    traceback.
  - Every response sets HSTS, `nosniff`, `X-Frame-Options: DENY` and
    `Cache-Control: private`. GETs carry an `ETag` and answer
    `If-None-Match` with `304`.
- **Full article text** is off by default: the app gets summaries and the
  publisher's link, as signed-out web visitors do. Turn on **Send full
  article text** to include the sanitized scraped text.
- **Off switch:** turning the API off answers every request except
  `/health` with `503`. It takes effect within 15 seconds, with no restart.

## Endpoints

The OpenAPI 3.1 spec is served at `/api/v1/openapi.json`. Every request
except `/health`, `/openapi.json` and `/devices/pair` needs
`Authorization: Bearer <token>`.

| Endpoint | Returns |
|---|---|
| `GET /health` | `{"ok": true, "version": "1.0"}` |
| `POST /devices/pair` `{code, device_name}` | `201` `{token, device, user}`. The token is shown once. |
| `GET /me` | The user and this device |
| `DELETE /me/devices/current` | Signs this device out |
| `GET`/`PUT /me/preferences` | `start_page`, `range`, `hide_single` (the profile page's feed defaults) |
| `GET /topics` | Active topics, in sidebar order |
| `GET /editions?cursor=&limit=` | Published editions, newest first |
| `GET /editions/latest`, `/editions/{id}` | An edition and its ranked story cards |
| `GET /opinion?edition=` | The edition's Opinion page: stories with their columns by leaning |
| `GET /stories?topic=&range=&single=&cursor=&limit=` | The story feed, filtered like All Stories. Omitted filters use the user's feed defaults. |
| `GET /stories/{id}` | Summary, deep report, opinion roundup, articles and columns by leaning |
| `GET /articles/{id}` | Title, outlet, leaning, summary, analysis, link, and scraped text when allowed |
| `GET /search?q=&kind=stories\|articles&range=&limit=` | Compact hits for as-you-type search |

Lists are paginated with an opaque `cursor`: pass back the `next_cursor` from
the previous page, which is `null` on the last page. `limit` is 1 to 50
(default 20). Dates are ISO 8601 UTC to the second, for example
`2026-10-22T14:05:00Z`.

## Not yet

These are in the plan but not built: swipe-to-summarize with job status,
NewsBox, bookmarks with saved copies, auto-summarize schedules, a My devices
list on the profile page for self-service pairing, a read-only Postgres role
for the API, a request log with alerts, and App Attest.
