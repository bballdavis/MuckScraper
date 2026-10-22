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
  checks. Content endpoints are read-only. The writes are a device's own
  pairing, sign-out and last-used time, and the user's own feed preferences,
  NewsBox and schedule. No request runs a model: summaries are queued as jobs
  for the scheduler container.
- **Scopes:** a device has `read`, and `summarize` for work that runs in the
  background: sending stories to NewsBox, saving bookmarks, and editing or
  running its schedule. New pairings get both;
  devices paired earlier were given `summarize` by migration `b7e2d4f9a1c3`.
- **Tokens:** a token is `msk_` followed by 32 random bytes. Only its SHA-256
  is stored. Tokens expire after a year, and pairing again issues a new one.
  Pairing codes are also stored hashed.
- **Rate limits** are counted in Postgres and shared by every worker. They
  apply per device and per IP, plus a tighter limit on pairing attempts per
  IP, stories sent to NewsBox per user per day and "Run now" per user per
  day. You can change them in Settings › Mobile API. A request over a limit
  gets `429` with `Retry-After`. Sending a story that is already in the
  user's NewsBox doesn't count, and a scheduled run adds at most 120 stories.
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
| `POST /stories/{id}/summarize` `{depth, origin}` | Adds the story to the user's NewsBox: `200` ready when already summarized, `202` pending when a summary was queued. `depth` is `summary` or `summary_analysis`; `origin` is `swiped` or `saved`. Needs `summarize`. |
| `GET /me/newsbox?state=&origin=&unread=&cursor=&limit=` | The user's NewsBox, newest first, with `unread_count` |
| `PATCH /me/newsbox/{story_id}` `{is_unread}`, `DELETE` | Mark one item read or unread, or remove it |
| `POST /me/newsbox/read` | Mark every item read |
| `GET`/`PUT /me/schedules` | Scheduled summaries: run times in the user's `timezone`, sections (Headlines in pages of 20, a topic by its top N), depth and rules. `PUT` needs `summarize`. |
| `POST /me/schedules/run` | Run the schedule once now. Needs `summarize`. |
| `GET /me/bookmarks` | Saved stories with their latest version, plus `used_bytes` and `quota_bytes` (null when unlimited) |
| `POST /me/bookmarks` `{story_id}` | Save a story: `202` while a new time-capsule version is written. It also goes to NewsBox as saved. Needs `summarize`. |
| `GET`/`DELETE /me/bookmarks/{id}` | One bookmark with all its versions, or delete it and its files |
| `POST /me/bookmarks/{id}/versions` | Save the story again as it stands now (a new version). Needs `summarize`. |
| `GET /me/bookmarks/{id}/versions/{n}` | The time capsule itself (below) |
| `GET /me/bookmarks/{id}/versions/{n}/assets/{name}` | A saved picture, page PDF or screenshot |
| `GET /me/bookmarks/{id}/export` | Every ready version as a zip of `v<n>/snapshot.json` and `v<n>/assets/` |

Lists are paginated with an opaque `cursor`: pass back the `next_cursor` from
the previous page, which is `null` on the last page. `limit` is 1 to 50
(default 20). Dates are ISO 8601 UTC to the second, for example
`2026-10-22T14:05:00Z`.

NewsBox items go `pending` → `in_progress` → `ready`, or `failed` with an
`error`. A story already summarized is `ready` at once. One job writes a
story's summary for everyone waiting on it, at lower priority than the
pipeline and the admin's AI tasks. An item is unread from when it turns
ready. Scheduled items have `origin: "auto"` and share an `auto_run_id`. The
scheduler checks every minute for schedules that are due.

### Bookmarks

A saved story is a time capsule. Each version keeps:
- the story: headline, original title, summary, multi-source analysis,
  opinion roundup, topics, and coverage by leaning
- every article: outlet and bias scores, link, date, summary, analysis, and
  the full scraped text (whatever the full-text setting says, since this is
  the user's own copy)
- the linked columns, with their text
- every picture: the story's, each article's and the ones inside the text
- a PDF and a screenshot of each article's page, from the optional capture
  container (`--profile capture`, `Dockerfile.capture`)

Pictures and pages are referenced as `assets/<name>`. Versions never change
after they are written, and a bookmark outlives its story: if the story is
merged away, `story` is null and the versions stay.

The scheduler writes the text and pictures. The capture container, which is
the only one with a browser, adds the pages. Both fetch only public http(s)
addresses (`aggregator/safe_fetch.py`), and every redirect and every request
the page makes is checked. Files live in the `bookmark_data` volume, which
the API mounts read-only. Each user has a storage limit (Settings › Mobile
API › Bookmark storage: a default, per-user overrides, blank for unlimited).
A version that would go over the limit stops with status `over_quota`.

## Not yet

These are in the plan but not built: push notifications when a run finishes (`notify_when_done` is stored only), a My devices
list on the profile page for self-service pairing, a read-only Postgres role
for the API, a request log with alerts, and App Attest.
