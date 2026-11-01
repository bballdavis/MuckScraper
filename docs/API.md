# API

An optional JSON API under `/api/v1`. The MuckScroller iPhone app reads it,
and so can a script, a dashboard or any other app with an API key. It runs as its own container with its own small Flask app
(`aggregator/api_app.py`) that registers **only** the API: no web pages, no
admin, no login. That makes it the one part of MuckScraper you can put on the
internet. The web app and admin stay on your network.

## Turning it on

1. Start the container: `docker compose --profile api up -d api`. It listens on
   `API_BIND:API_PORT`, which defaults to `127.0.0.1:5050`.
2. Put a tunnel or reverse proxy with TLS in front of that port (Cloudflare
   Tunnel, Tailscale, Caddy). Set `API_PROXY_HOPS` to the number of proxies in
   front of the API, so rate limits see the client's real address (`0` if the
   client connects directly). Behind Cloudflare, also set
   `API_CLIENT_IP_HEADER=CF-Connecting-IP`: otherwise everyone reaching the
   same Cloudflare edge shares one per-address limit. Only do that when the
   API can't be reached around Cloudflare, since a direct caller could send
   the header themselves.
3. In **Settings › API**, enter the public address
   (`https://api.example.com`), pick a user and press **Create pairing code**.
4. In MuckScroller, scan the QR code or type the code. The QR code holds a
   `muckscroller://pair?code=…&server=…&site=…` link, so scanning it also
   fills in the API's address and the site's (for the links the app shares). The code works once
   and expires after 10 minutes. The phone gets a device token; no password
   crosses the internet.

### API keys

For anything that can't scan a code (a script, a server, a home dashboard,
another app), turn on **API keys** in Settings › API. They are off by
default. Then fill in **API keys** on the same page: a name, the user the key
acts as, whether it can queue summaries, and when it expires (30 days, 90
days, a year or never). The key is shown once. Send it the same way a phone
sends its token:

```
curl -H "Authorization: Bearer msk_..." https://api.example.com/api/v1/stories
```

A key is the same kind of token a paired phone gets: same endpoints, scopes,
rate limits and sign-out. `GET /me` says which it is (`device.kind` is
`device` or `key`). Turning API keys off stops every key on its next request
(`401` with code `api_keys_off`) and leaves paired phones alone.

On the same page you can sign out one device or key, or all of them.
Disabling a user in Settings › Users signs out all of their devices and keys
on their next request.

## Security

- **Isolation:** the API process has only `/api/v1/*` routes, which a test
  checks. Content endpoints are read-only. The writes are a device's own
  pairing, sign-out and last-used time, and the user's own feed preferences,
  NewsBox and schedule. No request runs a model: summaries are queued as jobs
  for the scheduler container.
- **Scopes:** a device or key has `read`, and `summarize` for work that runs
  in the background: sending stories to NewsBox, saving bookmarks, and
  editing or running its schedule. New pairings get both, and a new key gets
  `summarize` only when you tick it;
  devices paired earlier were given `summarize` by migration `b7e2d4f9a1c3`.
- **Tokens:** a token is `msk_` followed by 32 random bytes. Only its SHA-256
  is stored. Paired devices' tokens expire after a year, and pairing again
  issues a new one; a key expires when you chose.
  Pairing codes are also stored hashed.
- **Rate limits** are counted in Postgres and shared by every worker. They
  apply per device and per IP, plus a tighter limit on pairing attempts per
  IP, stories sent to NewsBox per user per day and "Run now" per user per
  day. You can change them in Settings › API. A request over a limit
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
| `GET`/`PUT /me/pinned-sources` | The outlets this reader keeps in the menu, in pin order (`{items: [outlet]}`). `PUT {outlet_id, pinned}` pins or unpins one and returns the list; up to 12, `404` for an unknown outlet (`aggregator/pinned_sources.py`). Each pinned outlet has a page of its own stories: `/stories?outlet=`. |
| `GET /outlets?q=&limit=` | Outlets with articles in the last 30 days, most articles first, for choosing a source to pin; each carries `pinned` for this reader. |
| `GET /topics` | Active topics, in sidebar order |
| `GET /editions?cursor=&limit=` | Published editions, newest first |
| `GET /editions/latest`, `/editions/{id}` | An edition and its ranked story cards |
| `GET /opinion?edition=` | The edition's Opinion page: stories with their columns by leaning |
| `GET /headlines?view=trending\|latest\|controversial&topic=&cursor=&limit=` | Headlines' live views, as on the web page (`aggregator/headline_views.py`). Trending (the default): 3+ outlets in the last 24 hours, by headline score. Latest: every story from the last 24 hours, newest first, each with a `group` (`Last hour`, `Last 6 hours`, `Earlier`). Controversial: 2+ left and 2+ right outlets in the last 48 hours, by framing gap and op-eds. Cards add a short `why` for beside the L/C/R pill, `newest_at`, `framing_gap` and `opeds`. `views` gives each view's label, description and count for the switcher; stories the user read are left out and counted in `hidden_read`. `topic` (a topic name, `404` if unknown) gives that section its own three views, keeping only stories mostly about it. |
| `GET /stories?topic=&outlet=&range=&single=&cursor=&limit=` | The story feed, filtered like All Stories. Omitted filters use the user's feed defaults. An `outlet` id lists the stories that outlet reported (never a copy of another outlet's article), newest of its own articles first, and adds `outlet` (with `pinned`) to the response; single-article stories stay unless `single=false`. A `topic` keeps the stories mostly about it (`aggregator/sections.py`), not every story one article tagged. |
| `GET /stories/{id}` | Summary, deep report, opinion roundup, articles and columns by leaning. `analysis_sections` is the deep report split as the story page lays it out: `[{label, kind, paragraphs, empty}]`, `kind` one of `lead`, `left`, `center`, `right`, `next`, `other`, `empty` marking a side with no sources. Articles from unrated outlets based outside the US are grouped under `international`, apart from `unrated`. |
| `GET /articles/{id}` | Title, outlet, leaning, summary, analysis, link, and scraped text when allowed. `is_summarizing` and `is_rating_bias` say whether its summary or lean rating is still being written. `lean_source` is `outlet` or where the article's own rating came from, with its `lean_reason`. |
| `POST /articles/{id}/summarize` | Needs the summarize scope. Queues the article's own summary (the web page's summary button): `200` when it already has one, `202` queued, `409 no_text` when MuckScraper couldn't read the article. Poll `/articles/{id}`. Counts against the daily summary limit. |
| `POST /articles/{id}/rate-bias` | Needs the summarize scope. Queues a rating of the article's own lean from its title and opening (`202`). Once done, its `leaning` and the story's L / C / R counts use it instead of the outlet's. Counts against the daily summary limit. |
| `GET /search?q=&kind=stories\|articles&range=&limit=` | Compact hits for as-you-type search |
| `POST /stories/{id}/summarize` `{depth, origin}` | Adds the story to the user's NewsBox: `200` ready when already summarized, `202` pending when a summary was queued. `depth` is `summary`, `analysis` (the multi-source analysis alone; the summary stands in when a story has one article) or `summary_analysis`; `origin` is `requested` (the default) or `saved`. Needs `summarize`. |
| `GET /breaking` | The Breaking News bar: up to three breaking RSS headlines, newest first, with `state` (`new`, `scraping`, `scraped`, `summarizing`, `summarized`, `failed`) and `story_id` once scraped |
| `POST /breaking/{id}/newsbox` `{depth}` | Send a breaking headline to NewsBox: it is scraped if it isn't in a story yet, then summarized. `200` ready, `202` pending. Needs `summarize`; counts against the daily NewsBox limit. |
| `GET /me/newsbox?state=&origin=&unread=&cursor=&limit=` | The user's NewsBox, newest first, with `unread_count`. Breaking headlines the poller would now refuse (`is_junk`: blocks, gambling, promos) are left out. |
| `PATCH /me/newsbox/{story_id}` `{is_unread}`, `DELETE` | Mark one item read or unread, or remove it |
| `DELETE /me/newsbox/items/{id}` | Remove one item by its NewsBox id, for a Breaking headline that has no story yet |
| `POST /me/newsbox/read` | Mark every item read |
| `GET /me/read?kind=story\|article&cursor=&limit=` | Stories and articles the user marked read, newest first, each with its story card or article card |
| `PUT`/`DELETE /me/read/stories/{id}`, `/me/read/articles/{id}` | Mark read (again moves it to the top) or unread. Shared with the web app: a read story is hidden from Headlines there and listed under Recently Read. Story cards in editions and `/stories` carry `is_read`, as do `/stories/{id}` and `/articles/{id}`. |
| `GET /me/deck?cursor=&limit=` | The Swipe deck: stories still missing a summary or analysis that the user hasn't queued or skipped. The latest edition's headlines come first by rank (`tier: "headlines"`), then the past week's other stories, newest first (`tier: "more"`). |
| `GET /me/skips?cursor=&limit=` | Stories the user passed on in Swipe, most recent first |
| `PUT`/`DELETE /me/skips/{story_id}` | Pass on a story, or undo it. Scheduled runs never summarize a skipped story for that user; asking from the story page still works. `/stories/{id}` carries `is_skipped`. |
| `GET`/`PUT /me/schedules` | Scheduled summaries: run times in the user's `timezone`, sections (each with an `amount` of stories, 1 to 100: the top N of the latest edition for Headlines, the N most-covered in the last day for a topic), depth and rules. `PUT` needs `summarize`. |
| `POST /me/schedules/run` | Run the schedule once now. Needs `summarize`. |
| `GET /me/bookmarks` | Saved stories with their latest version, plus `used_bytes` and `quota_bytes` (null when unlimited) |
| `POST /me/bookmarks` `{story_id}` | Save a story: `202` while a new time-capsule version is written. It also goes to NewsBox as saved. Needs `summarize`. |
| `GET`/`DELETE /me/bookmarks/{id}` | One bookmark with all its versions, or delete it and its files |
| `POST /me/bookmarks/{id}/versions` | Save the story again as it stands now (a new version). Needs `summarize`. |
| `GET /me/bookmarks/{id}/versions/{n}` | The time capsule itself (below) |
| `GET /me/bookmarks/{id}/versions/{n}/assets/{name}` | A saved picture, page PDF or screenshot |
| `GET /me/bookmarks/{id}/export` | Every ready version as a zip of `v<n>/snapshot.json` and `v<n>/assets/` |

Story cards (editions, `/stories`, `/headlines`, the deck, NewsBox, bookmarks) carry `image_url` and `image_focus` (`{x, y}` from 0 to 1, the photo's focal point to crop around, or null; article cards too), `logo_url` and `source_name` (the outlet a card names when it has no photo: under its logo, or alone as a masthead), `coverage`, `source_count` (distinct outlets), `is_summarized`, `has_analysis`, `can_analyze` (two or more outlets, so an analysis can be asked for), `has_ai_headline`, and `breaking` (the Breaking chip's details, or null). `section` is the topic most of the story's articles carry. Search hits carry `image_focus` and `source_name` too.

Article cards carry `text_via` (`{name, url}`, or null): a Reuters story whose text was read from a site licensed to republish it, such as Yahoo Finance, since reuters.com itself can't be read. `url` is the article's own link (reuters.com when the Reuters headline was stored first, else the carrier's page).

Outlets carry `country` (an ISO code, set only for outlets based outside the US) and `funding` (`public`, `state` or null). An outlet abroad takes its lean from AllSides, else from Media Bias/Fact Check or Ad Fontes Media where they rate it on the US scale; one none of them rates stays `unrated` and is never guessed. Story `coverage` counts those unrated articles as `international`, outside `left`/`center`/`right`.

Outlets also carry `reporting`: a hand-kept grade of how reliable the outlet's reporting is, separate from its lean, one of `very_high`, `high`, `mostly_factual`, `mixed`, `low`, or null when not graded (`aggregator/outlet_reporting.py`).

Lists are paginated with an opaque `cursor`: pass back the `next_cursor` from
the previous page, which is `null` on the last page. `limit` is 1 to 50
(default 20). Dates are ISO 8601 UTC to the second, for example
`2026-10-22T14:05:00Z`.

NewsBox items go `pending` → `in_progress` → `ready`, or `failed` with an
`error`. A story already summarized is `ready` at once. One job writes a
story's summary for everyone waiting on it, at lower priority than the
pipeline and the admin's AI tasks. An item is unread from when it turns
ready. An item's `origin` is `requested`, `saved` (from a bookmark),
`scheduled` or `breaking`. Scheduled items share a `run_id`. The
scheduler checks every minute for schedules that are due.

An unfinished item has a `step`: `scrape` → `group` → `summary` →
`analysis`, with `progress` from 0 to 1, for the app's progress bar. A
Breaking headline (`origin: "breaking"`) starts at `scrape` with `story`
null and `headline` (`title`, `outlet`, `url`, `published_at`) in its
place; once it is in a story, `story` is filled in, or the item merges into
the user's existing item for that story. Items have their own `id`.

### Breaking News

The scheduler reads the general RSS feeds every `BREAKING_POLL_MINUTES`
(default 5) and stores the headlines without scraping them. A headline is
breaking when three or more outlets carry the same event within 90 minutes,
or a feed marks it (BREAKING, LIVE, Developing, Live updates), and no story
before it already covered the event; it stays in the bar for twelve hours.

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
the API mounts read-only. Each user has a storage limit (Settings › API ›
Bookmark storage: a default, per-user overrides, blank for unlimited).
A version that would go over the limit stops with status `over_quota`.

## Not yet

These are in the plan but not built: push notifications when a run finishes (`notify_when_done` is stored only), a My devices
list on the profile page for self-service pairing, a read-only Postgres role
for the API, a request log with alerts, and App Attest.
