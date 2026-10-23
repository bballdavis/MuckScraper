Hey! As promised, here's the feature request before I send any PRs your way. I started on this because I wanted an iPhone app (I'm calling it MuckScroller) that reads from MuckScraper, but I tried hard to keep the API itself generic so a script, a home dashboard, a different app or anything else can use the exact same endpoints. I've got a working version running on my fork, so this is a write-up of how it's shaped right now. Nothing here is set in stone, and I'd really like your take on it before I try to upstream any of it.

## The big idea

The web app has a lot of admin routes in it, so I didn't want to just open it up to the internet. Instead the API is its own tiny Flask app (`aggregator/api_app.py`) that registers **only** `/api/v1`. No web pages, no admin, no login form. It runs as a separate `api` container off the same image, behind a compose profile, so it's completely off unless you ask for it:

```
docker compose --profile api up -d api
```

It binds to `127.0.0.1:5050` by default. You put a tunnel or reverse proxy with TLS in front of it (Cloudflare Tunnel, Tailscale, Caddy, whatever you like), and the web app and admin stay on your network like they do today. There's a test that checks the API app only has `/api/v1/*` routes, so nothing sneaks in later.

## Two ways in: pairing a phone, or an API key

Every client ends up with the same thing: a bearer token (`msk_` plus 32 random bytes, only the SHA-256 is stored) sent as `Authorization: Bearer <token>`. There are just two ways to get one.

**Pairing a phone.** I really didn't want anyone typing a password into an app that talks to a server over the internet, so phones pair with a code:

1. In **Settings > API** you pick a user and hit **Create pairing code**.
2. You get a QR code and a short code. It works once and expires after 10 minutes.
3. The app scans it (or you type it), calls `POST /api/v1/devices/pair`, and gets back its own token.

![Pairing a device](https://raw.githubusercontent.com/bballdavis/MuckScraper/issue-screenshots/redesign/settings-api-pairing.png)

**API keys.** For anything that can't scan a code, there's a plain API key option. It's **off by default** behind an "API keys" switch, so a stock install only lets paired phones in. Turn it on and you can create a key with a name, the user it acts as, whether it can queue summaries, and an expiry (30 days, 90 days, a year or never). The key is shown once, with a ready-to-copy curl example:

```
curl -H "Authorization: Bearer msk_..." https://api.example.com/api/v1/stories
```

![API keys](https://raw.githubusercontent.com/bballdavis/MuckScraper/issue-screenshots/redesign/settings-api-keys.png)

A key is the same kind of token a paired phone gets, so it hits the same endpoints with the same scopes, rate limits and sign-out button. Flipping the switch back off stops every key on its next request and leaves phones alone.

Paired tokens last a year. You can sign out any device or key, or all of them, from the same page, and disabling a user in Settings > Users signs out everything they have on its next request.

Each token has scopes: `read` for content, and `summarize` for anything that queues background work (NewsBox, bookmarks, schedules). Paired phones get both, and a key only gets `summarize` if you tick the box.

## Keeping it generic

Since the first client was my app, I went back through it to make sure nothing was shaped around one app's screens:

- Field names describe the data, not a UI: `headline`, `dek`, `coverage`, `leaning`, `source_count`, `updated_at` and so on.
- NewsBox item origins are `requested`, `saved`, `scheduled` and `breaking`, not gesture names.
- Schedule sections all count stories the same way (`amount` is 1 to 100 for every section).
- Lists use an opaque `cursor`, `limit` is 1 to 50, dates are ISO 8601 UTC, and errors are always `{"error": {"code", "message"}}`.
- There's a hand-kept OpenAPI 3.1 spec at `/api/v1/openapi.json`, with a test that fails if a route is missing from it, so anyone can generate a client in whatever language they like.

## Rate limits and safety rails

Rate limits are counted in Postgres so every worker shares them, no Redis needed. They're all editable in Settings > API, and these are the defaults I landed on:

| Limit | Default |
|---|---|
| Requests per minute, per device or key | 300 |
| Requests per minute, per IP | 600 |
| Pairing attempts per hour, per IP | 10 |
| Stories sent to NewsBox per day, per user | 100 |
| "Run now" on a schedule per day, per user | 10 |
| Bookmark snapshots per day, per user | 30 |

The per-device number looks high, but a client scrolling a feed and prefetching story details easily fires 20 to 30 requests in a burst. Over a limit you get a `429` with `Retry-After`.

A few other things I tried to be careful about:

- Every parameter is validated and an unknown one is a `400`. Bodies are capped at 16 KB and queries time out after 5 seconds.
- Responses are built field by field, so scrape status, failure reasons, grouping internals and model or host names never leave the server.
- Errors never include a traceback.
- HSTS, `nosniff`, `X-Frame-Options: DENY` and `Cache-Control: private` on everything, plus `ETag`/`304` on GETs.
- **Full article text is off by default.** Clients get summaries and the publisher's link, same as a signed-out web visitor. There's a toggle if you want to send the scraped text to your own devices.
- No request ever runs a model. Anything that needs the LLM gets queued as a job for the scheduler.
- There's an off switch in Settings that answers everything except `/health` with `503`, within 15 seconds, no restart.
- `API_PROXY_HOPS` and `API_CLIENT_IP_HEADER` (for example `CF-Connecting-IP`) so the per-IP limits see the client's real address behind a proxy.

## What's exposed

Read side, mostly mirroring what the web app already shows:

| Endpoint | What it gives you |
|---|---|
| `GET /health`, `GET /openapi.json` | Health check and the OpenAPI 3.1 spec |
| `GET /me`, `GET`/`PUT /me/preferences` | The user, this device or key, and the feed defaults from the profile page |
| `GET /topics` | Active topics in sidebar order |
| `GET /editions`, `/editions/latest`, `/editions/{id}` | Published editions and their ranked stories |
| `GET /opinion?edition=` | An edition's Opinion page |
| `GET /stories?topic=&range=&single=` | The story feed, filtered like All Stories |
| `GET /stories/{id}`, `GET /articles/{id}` | Story detail (summary, deep report, opinion roundup, coverage by leaning) and article detail |
| `GET /search?q=&kind=` | Compact hits for as-you-type search |
| `GET /breaking` | Up to three breaking RSS headlines (more on that below) |

Then a handful of per-user features:

- **NewsBox.** A personal reading queue. Ask for a story with `POST /stories/{id}/summarize` and it lands in your NewsBox with a summary, an analysis or both. If the story is already summarized it's ready right away, otherwise a low-priority job writes it, and one job serves everyone waiting on the same story. Items go `pending` > `in_progress` > `ready` with a step and progress value so a client can show a little progress bar. Endpoints: `GET /me/newsbox`, `PATCH`/`DELETE /me/newsbox/{id}`, `POST /me/newsbox/read`.
- **Schedules.** "Summarize the top 20 Headlines and the top 5 Sci/Tech stories at 7am and 6pm" kind of thing, stored per account in the user's timezone. The scheduler checks every minute for ones that are due. `GET`/`PUT /me/schedules`, `POST /me/schedules/run`.
- **Bookmarks.** Saving a story keeps a full versioned copy: the story, every article's text and analysis, the linked columns, the pictures, and (if you run the optional `capture` container) a PDF and screenshot of each article page. It's basically a time capsule so a saved story survives link rot or getting regrouped. Each user has a storage limit you set in Settings. `GET`/`POST /me/bookmarks`, versions, assets, and a zip export.
- **Breaking news.** A light poller reads the general RSS feeds every few minutes and flags a headline as breaking when 3+ outlets carry the same event within 90 minutes, or the feed itself says BREAKING/LIVE/Developing. No LLM involved. You can send one to NewsBox, which scrapes and summarizes it. `GET /breaking`, `POST /breaking/{id}/newsbox`. (The web app gets the same bar on Headlines.)

## Managing it in Settings

Everything lives on one page under **Settings > API**: on/off, the full-text toggle, the API keys switch, the public address that goes into the QR code, every rate limit, pairing codes, API keys, the list of devices and keys with sign-out buttons, and bookmark storage limits (a default plus per-user overrides, blank for unlimited).

## Not built yet

Push notifications when a scheduled run finishes, a "My devices" page so people can pair and make keys for themselves, a read-only Postgres role for the API, a request log with alerts, and App Attest.

The full write-up is in `docs/API.md` on my fork's `dev` branch: https://github.com/bballdavis/MuckScraper/blob/dev/docs/API.md
