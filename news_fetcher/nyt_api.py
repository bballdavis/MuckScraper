# news_fetcher/nyt_api.py
"""The New York Times' free developer APIs, for paywalled NYT articles.

The scraper leaves a paywalled nytimes.com article headline-only (no crawler
user agent, no headless browser -- see news_fetcher/scraper.py). NYT's own
developer APIs give the abstract and the opening paragraph for free, under
its API terms, so a NYT article gets that much text to summarize instead of
the one-line RSS description. No NYT API gives the full text.

Needs NYT_API_KEY (a free key from developer.nytimes.com), set on Settings >
Source API keys or in the environment. Both it and NYT_API_DAILY_LIMIT are
read on every request, so a key saved in the admin takes effect without a
restart. Without a key nothing here makes a request.

The budget
----------
The key allows 5 requests a minute and 500 a day, shared across every NYT
API. Requests go through news_fetcher/api_budget.py, which keeps the
ledger shared with NewsAPI and GNews: a rolling 24-hour count under
NYT_API_DAILY_LIMIT, spacing, a pause after a 429, and the wire's poll
schedule (NYT_API_SCHEDULE), all editable on Settings › Source API keys.

What the requests buy, most articles per request first:
- Times Wire (`wire`): everything NYT has published, newest first, up to
  WIRE_PAGE_SIZE items a request. The scheduler polls it on NYT_API_SCHEDULE,
  more often in the morning and evening news peaks; one request usually
  covers every item since the last. Items are kept in nyt_wire_items, the
  news sections' ones go to the Breaking poller's headlines at once, and
  pipeline runs ingest them as articles (ingest_wire_items).
- Article Search (`search`): the lead paragraph, which the wire doesn't
  have, for ten wire items a request (search_lead_paragraphs).
- Article Search for one URL (`lookup`): only for a NYT article the wire
  never listed, such as an older one an admin rescrapes.

RESERVES keeps the cheaper-per-article kinds from starving the others.
"""

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

from news_fetcher import api_budget

logger = logging.getLogger(__name__)

ARTICLE_SEARCH_URL = "https://api.nytimes.com/svc/search/v2/articlesearch.json"
WIRE_URL = "https://api.nytimes.com/svc/news/v3/content/nyt/all.json"
SOURCE_NAME = "New York Times"

PROVIDER = api_budget.PROVIDERS["nyt"]
DAILY_CAP = PROVIDER.max_limit  # the key's own limit; NYT_API_DAILY_LIMIT can only go lower
# Calls left in the window that each kind leaves for the others: a wire
# poll stops with 25 left, so single lookups (an admin rescrape) still work;
# lead-paragraph searches stop with 150 left, so the wire polls keep going.
RESERVES = PROVIDER.reserves
WINDOW = api_budget.WINDOW

# The API caps a request at what it allows (20 in older docs, 500 now);
# asking for more costs nothing, and _wire_page_size remembers a refusal.
WIRE_PAGE_SIZE = 500
WIRE_FALLBACK_PAGE_SIZE = 20
# After downtime a poll pages back this many requests at most.
WIRE_MAX_PAGES = 3

# Sections whose items become articles and Breaking headlines. Opinion
# comes in through the NYT Opinion feed; lifestyle and games sections stay out.
NEWS_SECTIONS = {
    "u.s.", "us", "world", "politics", "business", "business day", "technology", "science",
    "health", "climate", "new york", "n.y. / region", "the upshot", "sports", "weather", "education",
}
SKIP_ITEM_TYPES = {"video", "audio", "podcast", "slideshow", "recipe", "crossword"}
INGEST_MAX_AGE = timedelta(hours=36)
INGEST_PER_RUN = 120
SEARCH_BATCH = 10  # one Article Search page
SEARCH_MAX_BATCHES_PER_RUN = 15
SEARCH_RETRY_FOR = timedelta(hours=12)
# When a whole batch comes back without a lead paragraph, the API no longer
# has the field; stop asking for this long.
SEARCH_NO_LEAD_PAUSE = timedelta(days=7)
_NO_LEAD_SETTING = "nyt_api_no_lead_paragraph_at"

# Article Search has filtered on `web_url` for years and on `url` since its
# 2025 revision. Try them in order and remember the first that works.
_URL_FILTER_FIELDS = ["url", "web_url"]

_state = {"filter_field": None, "wire_page_size": None}
_cache = {}
_CACHE_MAX_ENTRIES = 1000


def is_nyt_url(url):
    host = (urlparse(url or "").hostname or "").lower()
    return host == "nytimes.com" or host.endswith(".nytimes.com")


def _api_key():
    return api_budget.api_key(PROVIDER)


def daily_limit():
    return api_budget.daily_limit(PROVIDER)


def enabled():
    return api_budget.enabled(PROVIDER)


def _canonical(url):
    """The article URL without query string or fragment, as NYT indexes it."""
    parts = urlparse(url)
    return urlunparse((parts.scheme or "https", parts.netloc.lower(), parts.path, "", "", ""))


def _utcnow():
    return datetime.utcnow()


# Requests ---------------------------------------------------------------

_Unavailable = api_budget.Unavailable


def window_usage(now=None):
    """(calls in the last 24 hours, newest call time, newest 429 time)."""
    return api_budget.window_usage(PROVIDER, now)


def _get(kind, url, params, key, items_of):
    """One budgeted request. Returns the JSON body, or None on a 400 (a
    parameter NYT refuses). `items_of(body)` counts what it returned."""
    return api_budget.request(PROVIDER, kind, url, params={**params, "api-key": key}, key=key, items_of=items_of)


# Article Search ---------------------------------------------------------

def _search_docs(kind, field, urls, key):
    """Docs whose `field` is one of `urls` (one page, so ten at most), or
    None when NYT rejects the field."""
    quoted = " ".join(f'"{u}"' for u in urls)
    body = _get(kind, ARTICLE_SEARCH_URL, {"fq": f"{field}:({quoted})"}, key,
                lambda b: len((b.get("response") or {}).get("docs") or []))
    if body is None:
        return None
    return (body.get("response") or {}).get("docs") or []


def _docs_for(kind, urls, key):
    """{canonical url: doc} for whichever of `urls` Article Search has."""
    fields = [_state["filter_field"]] if _state["filter_field"] else _URL_FILTER_FIELDS
    wanted = set(urls)
    for field in fields:
        docs = _search_docs(kind, field, urls, key)
        if docs is None:
            continue
        found = {}
        for doc in docs:
            url = _canonical(doc.get("web_url") or doc.get("url") or "")
            if url in wanted:
                found[url] = doc
        if found:
            _state["filter_field"] = field
            return found
    return {}


def _text(value):
    return " ".join((value or "").split())


def _paragraphs(*texts):
    """The texts in order, without empties or one repeating another."""
    seen = []
    for text in map(_text, texts):
        if text and not any(text == other or text in other for other in seen):
            seen.append(text)
    return seen


def _doc_paragraphs(doc):
    return _paragraphs(doc.get("abstract"), doc.get("lead_paragraph"), doc.get("snippet"))


# Times Wire -------------------------------------------------------------

def _parse_time(value):
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _wire_image(item):
    media = [m for m in item.get("multimedia") or [] if isinstance(m, dict) and m.get("url")]
    if not media:
        return None
    return max(media, key=lambda m: (m.get("width") or 0) * (m.get("height") or 0))["url"]


def _wire_row(item, now):
    url = item.get("url") or ""
    title = _text(item.get("title"))
    if not is_nyt_url(url) or not title:
        return None
    return {
        "url": _canonical(url)[:2000],
        "title": title[:500],
        "abstract": _text(item.get("abstract")) or None,
        "section": (item.get("section") or "")[:100] or None,
        "subsection": (item.get("subsection") or "")[:100] or None,
        "byline": _text(item.get("byline"))[:300] or None,
        "item_type": (item.get("item_type") or "")[:50] or None,
        "image_url": (_wire_image(item) or "")[:2000] or None,
        "published_at": min(_parse_time(item.get("published_date")) or now, now),
        "first_seen_at": now,
    }


def is_news_item(row):
    if (row.get("item_type") or "").lower() in SKIP_ITEM_TYPES:
        return False
    sections = {(row.get("section") or "").lower(), (row.get("subsection") or "").lower()}
    return bool(sections & NEWS_SECTIONS)


def _store_wire_rows(rows):
    """Insert rows not stored yet; refresh the title and abstract of the
    rest. Returns the new rows."""
    from aggregator.models import NytWireItem, db
    if not rows:
        return []
    by_url = {r["url"]: r for r in rows}
    existing = {item.url: item for item in NytWireItem.query.filter(NytWireItem.url.in_(list(by_url))).all()}
    new = []
    for url, row in by_url.items():
        item = existing.get(url)
        if item is None:
            db.session.add(NytWireItem(**row))
            new.append(row)
        else:
            item.title = row["title"]
            item.abstract = row["abstract"] or item.abstract
            item.image_url = row["image_url"] or item.image_url
    db.session.commit()
    return new


def _wire_page_size():
    return _state["wire_page_size"] or WIRE_PAGE_SIZE


def poll_wire(now=None):
    """Read the Times Wire from the newest item back to the last one already
    stored (WIRE_MAX_PAGES requests at most), store the new items, and hand
    the news sections' ones to the Breaking poller. Returns a metrics dict."""
    from aggregator import breaking
    metrics = {"requests": 0, "items": 0, "new": 0, "headlines": 0}
    if not enabled():
        return {**metrics, "status": "skipped", "reason": "no key"}
    key = _api_key()
    offset = 0
    new_rows = []
    for _ in range(WIRE_MAX_PAGES):
        size = _wire_page_size()
        try:
            body = _get("wire", WIRE_URL, {"limit": size, "offset": offset}, key,
                        lambda b: len(b.get("results") or []))
        except _Unavailable as exc:
            logger.info("[NYT API] Wire poll stopped: %s", exc)
            metrics["status"] = "unavailable"
            break
        metrics["requests"] += 1
        if body is None:
            if size == WIRE_FALLBACK_PAGE_SIZE:
                break
            # NYT refused the page size: use the documented one from now on.
            _state["wire_page_size"] = WIRE_FALLBACK_PAGE_SIZE
            continue
        results = body.get("results") or []
        stamp = now or _utcnow()
        rows = [r for r in (_wire_row(item, stamp) for item in results) if r]
        new = _store_wire_rows(rows)
        new_rows.extend(new)
        metrics["items"] += len(results)
        # Stop at the first page that reaches items we already had, or at
        # the end of what the wire holds.
        if len(new) < len(rows) or len(results) < size:
            break
        offset += len(results)
    metrics["new"] = len(new_rows)
    headlines = [
        {"title": r["title"], "url": r["url"], "source_name": SOURCE_NAME, "published_at": r["published_at"],
         "image_url": r["image_url"], "content": r["abstract"]}
        for r in new_rows if is_news_item(r)
    ]
    if headlines:
        metrics["headlines"] = breaking.store_headlines(headlines, WIRE_URL, now)
    metrics.setdefault("status", "ok")
    logger.info("[NYT API] Wire: %(requests)s request(s), %(items)s items, %(new)s new, "
                "%(headlines)s Breaking headlines", metrics)
    return metrics


def wire_interval(now=None):
    """Minutes between wire polls at `now` (UTC) by the schedule alone."""
    return api_budget.scheduled_interval(PROVIDER, now)


def poll_wire_if_due(now=None):
    """The scheduler calls this every minute; it polls when the schedule's
    interval (stretched to fit the budget) has passed. Returns poll_wire's
    metrics, or None."""
    if not api_budget.poll_due(PROVIDER, now):
        return None
    return poll_wire(now)


# Pipeline runs ----------------------------------------------------------

def _no_lead_paragraph_since():
    from aggregator.models import AppSetting
    setting = AppSetting.query.filter_by(key=_NO_LEAD_SETTING).first()
    return _parse_time(setting.value) if setting and setting.value else None


def _set_no_lead_paragraph(now):
    from aggregator.models import AppSetting, db
    setting = AppSetting.query.filter_by(key=_NO_LEAD_SETTING).first() or AppSetting(key=_NO_LEAD_SETTING)
    setting.value = now.isoformat()
    db.session.add(setting)


def _ingest_candidates(now, limit):
    from aggregator.models import NytWireItem
    rows = (
        NytWireItem.query
        .filter(NytWireItem.ingested_at.is_(None), NytWireItem.published_at >= now - INGEST_MAX_AGE)
        .order_by(NytWireItem.published_at.desc())
        .limit(limit * 3)
        .all()
    )
    return [r for r in rows if is_news_item({"section": r.section, "subsection": r.subsection,
                                             "item_type": r.item_type})][:limit]


def search_lead_paragraphs(now=None, items=None):
    """Add Article Search's lead paragraph to wire items about to be
    ingested, ten per request. Returns how many items it searched."""
    from aggregator.models import db
    now = now or _utcnow()
    if not enabled():
        return 0
    paused = _no_lead_paragraph_since()
    if paused and now - paused < SEARCH_NO_LEAD_PAUSE:
        return 0
    pending = [i for i in (items if items is not None else _ingest_candidates(now, INGEST_PER_RUN))
               if i.searched_at is None]
    key = _api_key()
    searched = 0
    for start in range(0, min(len(pending), SEARCH_BATCH * SEARCH_MAX_BATCHES_PER_RUN), SEARCH_BATCH):
        batch = pending[start:start + SEARCH_BATCH]
        try:
            docs = _docs_for("search", [i.url for i in batch], key)
        except _Unavailable as exc:
            logger.info("[NYT API] Lead paragraphs stopped: %s", exc)
            break
        except Exception as exc:
            logger.info("[NYT API] Lead paragraph search failed: %s", exc)
            break
        for item in batch:
            doc = docs.get(item.url)
            if doc:
                item.lead_paragraph = _text(doc.get("lead_paragraph")) or None
                item.abstract = item.abstract or _text(doc.get("abstract")) or None
            # Search indexes an article a while after the wire lists it, so
            # a new one it doesn't have yet is tried again next run.
            if doc or now - item.published_at > SEARCH_RETRY_FOR:
                item.searched_at = now
        searched += len(batch)
        if docs and not any(_text(d.get("lead_paragraph")) for d in docs.values()):
            logger.info("[NYT API] Article Search returned no lead paragraphs; pausing those searches")
            _set_no_lead_paragraph(now)
            db.session.commit()
            break
        db.session.commit()
    return searched


def ingest_wire_items(now=None):
    """Store the news sections' wire items from the last INGEST_MAX_AGE as
    articles, through the normal ingestion (dedup, grouping, bias), after
    searching their lead paragraphs. Their text comes from the stored item
    (lookup_article), so this costs no per-article requests."""
    from aggregator.models import db
    from news_fetcher.fetch_and_store_articles import store_articles
    now = now or _utcnow()
    items = _ingest_candidates(now, INGEST_PER_RUN)
    if not items:
        return {"provider": "nyt_wire", "status": "ok", "input_articles": 0, "stored": 0, "searched": 0}
    searched = search_lead_paragraphs(now, items)
    articles = [{
        "title": i.title,
        "content": i.abstract or "",
        "url": i.url,
        "source_name": SOURCE_NAME,
        "published_at": i.published_at,
        "image_url": i.image_url,
        "author": i.byline,
    } for i in items]
    metrics = store_articles(articles, "Global News", provider="nyt_wire")
    for i in items:
        i.ingested_at = now
    db.session.commit()
    metrics.update(status="ok", searched=searched)
    return metrics


# The scraper's fallback -------------------------------------------------

def _stored_paragraphs(url):
    """The abstract and lead paragraph of a stored wire item, or None."""
    try:
        from aggregator.models import NytWireItem
        item = NytWireItem.query.filter_by(url=url).first()
    except Exception:
        return None
    if item is None:
        return None
    return _paragraphs(item.abstract, item.lead_paragraph) or None


def lookup_article(url):
    """The NYT abstract and opening paragraph for `url` as a list of
    paragraphs, or None when the key is unset, the URL isn't NYT's, the
    budget is spent, or NYT has no record of it. A wire item answers without
    a request; anything else costs one Article Search request."""
    if not enabled() or not is_nyt_url(url):
        return None
    canonical = _canonical(url)
    stored = _stored_paragraphs(canonical)
    if stored:
        return stored
    if canonical in _cache:
        return _cache[canonical]
    try:
        doc = _docs_for("lookup", [canonical], _api_key()).get(canonical)
    except _Unavailable as exc:
        logger.info("  [NYT API] Skipping %s: %s", canonical[:80], exc)
        return None
    except Exception as exc:
        logger.info("  [NYT API] Lookup failed for %s: %s", canonical[:80], exc)
        return None
    paragraphs = _doc_paragraphs(doc) if doc else None
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        _cache.clear()
    _cache[canonical] = paragraphs or None
    return _cache[canonical]


def usage_summary(now=None):
    """For Settings › Source API keys: the shared budget summary plus what
    the wire brought."""
    from aggregator.models import NytWireItem
    now = now or _utcnow()
    since = now - WINDOW
    return {
        **api_budget.usage_summary(PROVIDER, now),
        "wire_items": NytWireItem.query.filter(NytWireItem.first_seen_at > since).count(),
        "ingested": NytWireItem.query.filter(NytWireItem.ingested_at > since).count(),
    }
