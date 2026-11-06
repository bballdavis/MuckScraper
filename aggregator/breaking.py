"""Breaking news: RSS headlines before anything scrapes them.

The scheduler polls the general RSS feeds every few minutes
(news_fetcher/breaking_feed.py) and stores each headline as a FeedEntry,
with no scraping or model call. Entries several outlets report at once, or
that a feed marks BREAKING / LIVE, are flagged `is_breaking`; the Breaking
News bar on Headlines and /api/v1/breaking show the newest three.

"Scrape and summarize" (the bar's summary icon, or the app's, which sends
the headline to NewsBox) queues a "scrape_feed_entry" job: the entry goes
through the normal ingestion (scrape, outlet rating, story grouping) and
then the story's summary is queued like any NewsBox summary.
"""
import logging
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlunparse

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import joinedload

from aggregator import jobs
from aggregator.models import Article, FeedEntry, NewsBoxItem, db

logger = logging.getLogger(__name__)

SCRAPE_JOB = "scrape_feed_entry"
# Someone is waiting on it: ahead of NewsBox summaries (5), below the
# admin AI tasks (10).
SCRAPE_PRIORITY = 8

# How long a headline can count as breaking, from when it was published:
# how far back the bar and /api/v1/breaking reach. Was 3 hours; the bar sat
# empty for most of the night.
WINDOW = timedelta(hours=12)
BAR_SIZE = 3
# Entries are kept this long, then deleted.
KEEP = timedelta(days=3)
# A "summarizing" entry whose summary never came is offered again after this.
SUMMARY_TIMEOUT = timedelta(minutes=20)

# Why ingestion refused an entry (store_articles' skipped counters).
SKIP_NOTES = {
    "blocked_source": "Its site is on the ingestion block list.",
    "blocked_title": "Its title matches an ingestion block.",
    "opinion": "It's an opinion piece; it was filed under Opinion.",
    "missing_required": "The feed entry had no title or link.",
    "roundup": "Skipped as a roundup.",
    "betting": "Skipped as betting content.",
    "advice_column": "Skipped as an advice column.",
    "cartoon": "Skipped as a cartoon.",
    "promotional": "Skipped as promotional.",
    "low_value_url": "Skipped as a low-value page.",
    "unknown_outlet": "Its outlet isn't one we already carry.",
}


def normalize_url(url):
    """The same key ingestion stores Article.url under: no query or fragment."""
    try:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    except Exception:
        return url


# Storing (the poller) ---------------------------------------------------

def store_headlines(articles, feed_url, now=None):
    """Insert fetch-feed article dicts as FeedEntries, skipping URLs already
    stored and anything older than KEEP. Returns how many were new. Commits."""
    now = now or datetime.utcnow()
    rows = []
    for a in articles:
        title = (a.get("title") or "").strip()
        link = (a.get("url") or "").strip()
        if not title or not link or len(link) > 2000:
            continue
        published = a.get("published_at") or now
        if published > now:
            published = now
        if published < now - KEEP:
            continue
        rows.append({
            "url": normalize_url(link)[:2000],
            "link": link,
            "title": title[:500],
            "source_name": (a.get("source_name") or urlparse(link).netloc or "Unknown")[:200],
            "feed_url": (feed_url or "")[:2000] or None,
            "image_url": (a.get("image_url") or "")[:2000] or None,
            "content": (a.get("content") or "")[:4000] or None,
            "published_at": published,
            "first_seen_at": now,
        })
    if not rows:
        return 0
    result = db.session.execute(
        insert(FeedEntry.__table__).values(rows).on_conflict_do_nothing(index_elements=["url"])
    )
    db.session.commit()
    return result.rowcount or 0


def link_scraped(now=None):
    """Mark entries the regular pipeline has scraped since (same URL, or for
    Google News links the same title) as done. Commits."""
    now = now or datetime.utcnow()
    db.session.execute(db.text(
        "UPDATE feed_entries fe SET article_id = a.id, scrape_state = 'done' FROM articles a "
        "WHERE fe.article_id IS NULL AND fe.scrape_state IN ('new', 'failed', 'skipped') AND a.url = fe.url"
    ))
    db.session.execute(db.text(
        "UPDATE feed_entries fe SET article_id = a.id, scrape_state = 'done' FROM articles a "
        "WHERE fe.article_id IS NULL AND fe.scrape_state IN ('new', 'failed', 'skipped') "
        "AND a.title = fe.title AND a.fetched_at >= :since"
    ), {"since": now - KEEP - timedelta(days=1)})
    db.session.commit()


def prune(now=None):
    """Delete entries older than KEEP that no unfinished NewsBox item needs,
    then the NewsBox items left with neither a story nor an entry: a failed
    Breaking headline whose entry is gone has nothing to show (the API leaves
    it out too; me_routes._listed)."""
    now = now or datetime.utcnow()
    result = db.session.execute(db.text(
        "DELETE FROM feed_entries WHERE first_seen_at < :cutoff AND id NOT IN ("
        "SELECT feed_entry_id FROM newsbox_items WHERE feed_entry_id IS NOT NULL "
        "AND state IN ('pending', 'in_progress'))"
    ), {"cutoff": now - KEEP})
    db.session.execute(db.text(
        "DELETE FROM newsbox_items WHERE story_id IS NULL AND feed_entry_id IS NULL"))
    db.session.commit()
    return result.rowcount


# Reading ----------------------------------------------------------------

def bar_entries(limit=BAR_SIZE, now=None):
    """The newest breaking entries, one per event, for the bar and the API."""
    now = now or datetime.utcnow()
    return (FeedEntry.query
            .filter(FeedEntry.is_breaking.is_(True), FeedEntry.published_at >= now - WINDOW)
            .options(joinedload(FeedEntry.article).joinedload(Article.story))
            .order_by(FeedEntry.published_at.desc(), FeedEntry.id.desc())
            .limit(limit).all())


# Breaking stories (the chip on story cards) ---------------------------

# Outlets the chip's tooltip names, at most.
CHIP_OUTLETS = 8


def story_flags(now=None):
    """{story_id: chip details} for stories that grew out of a breaking event:
    one of the story's articles was scraped from an entry in an event the
    poller flagged in the last WINDOW. The details are what the chip's
    tooltip says: who reported it first and when, how many outlets carried
    it, and those outlets with their lean. Read once per request (g)."""
    from flask import g, has_request_context
    if now is None and has_request_context() and "breaking_story_flags" in g:
        return g.breaking_story_flags
    flags = _story_flags(now or datetime.utcnow())
    if now is None and has_request_context():
        g.breaking_story_flags = flags
    return flags


def _story_flags(now):
    from aggregator.models import Outlet
    from aggregator.opinion import leaning
    heads = {e.id: e for e in FeedEntry.query.filter(FeedEntry.is_breaking.is_(True),
                                                     FeedEntry.published_at >= now - WINDOW)}
    if not heads:
        return {}
    rows = (db.session.query(FeedEntry.cluster_id, FeedEntry.source_name, FeedEntry.published_at, Article.story_id)
            .outerjoin(Article, Article.id == FeedEntry.article_id)
            .filter(FeedEntry.cluster_id.in_(list(heads)))
            .order_by(FeedEntry.published_at, FeedEntry.id).all())
    names = {r.source_name.lower() for r in rows}
    leans = {o.name.lower(): leaning(o.bias_score)
             for o in Outlet.query.filter(db.func.lower(Outlet.name).in_(names))}
    events = {}
    for cluster_id, source, _published, story_id in rows:
        event = events.setdefault(cluster_id, {"sources": {}, "stories": set()})
        event["sources"].setdefault(source, leans.get(source.lower(), "unrated"))
        if story_id is not None:
            event["stories"].add(story_id)
    flags = {}
    # Oldest first, so a story in two events keeps the newer one.
    for head in sorted(heads.values(), key=lambda e: e.published_at):
        event = events.get(head.id)
        if not event or not event["stories"]:
            continue
        sources = event["sources"]
        lean = {side: 0 for side in ("left", "center", "right", "unrated")}
        for side in sources.values():
            lean[side] += 1
        details = {
            "first_outlet": head.source_name,
            "first_at": head.published_at,
            "outlets": max(head.outlet_count or 1, len(sources)),
            # Under the outlet threshold it was flagged by its feed
            # ("BREAKING:", "Live updates").
            "flagged_by_feed": (head.outlet_count or 1) < 3,
            "sources": [{"name": n, "leaning": side} for n, side in list(sources.items())[:CHIP_OUTLETS]],
            "lean": lean,
        }
        for story_id in event["stories"]:
            flags[story_id] = details
    return flags


def story_breaking(story):
    """The chip details for one story, or None."""
    return story_flags().get(story.id) if story is not None and story.id else None


def chip_details(story, now=None):
    """story_breaking plus what the web chip's tooltip prints: "42 min ago"
    and the lean shares in percent for its bar."""
    details = story_breaking(story)
    if details is None:
        return None
    minutes = max(1, int(((now or datetime.utcnow()) - details["first_at"]).total_seconds() // 60))
    ago = f"{minutes} min ago" if minutes < 60 else f"{minutes // 60} hr ago"
    total = sum(details["lean"].values()) or 1
    shares = [(side, round(100 * n / total)) for side, n in details["lean"].items() if n]
    return {**details, "ago": ago, "shares": shares}


def story_of(entry):
    return entry.article.story if entry.article is not None else None


def entry_state(entry, now=None):
    """What the bar shows: new (can be scraped), scraping, summarizing,
    summarized (filled icon, the title opens the story), scraped (in a story
    but no summary yet; can be summarized) or failed (can be retried)."""
    now = now or datetime.utcnow()
    story = story_of(entry)
    if story is not None and (story.summary or "").strip():
        return "summarized"
    if entry.scrape_state == "queued":
        return "scraping"
    if story is not None:
        if (entry.scrape_state == "summarizing" and entry.requested_at
                and now - entry.requested_at < SUMMARY_TIMEOUT):
            return "summarizing"
        return "scraped"
    if entry.scrape_state in ("failed", "skipped"):
        return "failed"
    return "new"


WORKING = ("scraping", "summarizing")
ACTIONABLE = ("new", "scraped", "failed")


# Queueing ---------------------------------------------------------------

def request(entry, depth="summary", user_id=None):
    """Scrape the entry if it has no article yet, then summarize its story.
    With `user_id` it also goes in that user's NewsBox (the app). Returns
    the NewsBoxItem, or None. Commits."""
    from aggregator import newsbox
    item = newsbox.queue_feed_entry(user_id, entry, depth) if user_id else None
    story = story_of(entry)
    now = datetime.utcnow()
    if story is not None:
        if user_id is None:
            newsbox.queue_summary(story, depth)
        if not newsbox.has_parts(story, depth):
            entry.scrape_state, entry.requested_at = "summarizing", now
        db.session.commit()
        return item
    if entry.scrape_state != "queued":
        entry.scrape_state, entry.requested_at, entry.scrape_note = "queued", now, None
        db.session.flush()
        jobs.enqueue(SCRAPE_JOB, {"entry_id": entry.id, "depth": depth}, priority=SCRAPE_PRIORITY)
    db.session.commit()
    return item


def _waiting_items(entry_id):
    return NewsBoxItem.query.filter(NewsBoxItem.feed_entry_id == entry_id, NewsBoxItem.story_id.is_(None),
                                    NewsBoxItem.state.in_(("pending", "in_progress"))).all()


def _set_step(entry_id, step):
    for item in _waiting_items(entry_id):
        item.state, item.step = "in_progress", step
    db.session.commit()


def _scrape(entry):
    """Run the entry through ingestion. Returns (article id, None) or
    (None, why not)."""
    from news_fetcher import google_news
    from news_fetcher.fetch_and_store_articles import store_articles
    data = {"title": entry.title, "content": entry.content or "", "url": entry.link,
            "source_name": entry.source_name, "published_at": entry.published_at, "image_url": entry.image_url}
    if google_news.is_google_news_feed(entry.feed_url or "") or "news.google.com" in entry.link:
        resolved, _ = google_news.resolve_entries([data])
        if not resolved:
            return None, "Couldn't find the publisher's link behind the Google News entry."
        data = resolved[0]
    # Keeps the feed's sections (aggregator/sources.py).
    data["feed_url"] = entry.feed_url
    metrics = store_articles([data], "Global News", provider="rss_breaking")
    found = (Article.query.filter_by(url=normalize_url(data["url"])).first()
             or Article.query.filter(Article.title == entry.title,
                                     Article.fetched_at >= datetime.utcnow() - KEEP).first())
    if found is not None:
        return found.id, None
    reasons = [k for k, v in (metrics.get("skipped") or {}).items() if v]
    return None, SKIP_NOTES.get(reasons[0], "Ingestion skipped it.") if reasons else "Nothing was stored."


def run_scrape_job(payload):
    """Job handler: scrape the entry, then hand its story to the summaries."""
    from aggregator import newsbox
    entry = FeedEntry.query.get(payload.get("entry_id"))
    if entry is None:
        return
    depth = payload.get("depth") if payload.get("depth") in newsbox.DEPTHS else "summary"
    _set_step(entry.id, "scrape")
    if entry.article_id is None:
        article_id, note = _scrape(entry)
        if article_id is None:
            entry.scrape_state, entry.scrape_note = "skipped", note[:300]
            for item in _waiting_items(entry.id):
                item.state, item.step, item.error = "failed", None, note[:300]
            db.session.commit()
            return
        entry.article_id = article_id
        db.session.commit()
    story = story_of(entry)
    if story is None:
        raise RuntimeError("The article was stored but not grouped into a story.")
    _set_step(entry.id, "group")
    items = _waiting_items(entry.id)
    for item in items:
        newsbox.attach_story(item, story)
    if not items:
        newsbox.queue_summary(story, depth)
    entry.scrape_state = "done" if newsbox.has_parts(story, depth) else "summarizing"
    entry.requested_at = datetime.utcnow()
    db.session.commit()


def fail_scrape_job(payload, message):
    entry = FeedEntry.query.get(payload.get("entry_id"))
    if entry is None:
        return
    entry.scrape_state, entry.scrape_note = "failed", (message or "Failed.")[:300]
    for item in _waiting_items(entry.id):
        item.state, item.step, item.error = "failed", None, (message or "Failed.")[:300]
    db.session.commit()


jobs.register(SCRAPE_JOB, run_scrape_job, fail_scrape_job)
