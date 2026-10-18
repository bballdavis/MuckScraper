# news_fetcher/google_news.py
"""Google News search feeds as an RSS source (2026-10-16).

Some outlets have no working feed of their own any more (CNN's rss.cnn.com
froze in 2023-24, feeds.apnews.com and feeds.reuters.com no longer resolve,
the WSJ feed stopped in January 2025). A Google News search feed limited to
one site (`https://news.google.com/rss/search?q=site:cnn.com+when:1d&...`)
still lists their stories, but every link is a coded
`news.google.com/rss/articles/<id>` address, not the publisher's.

`resolve_entries()` turns each link back into the real article address
before anything is stored, so the database, the aggregator and
muckscraper.news only ever see the publisher's URL. An entry that can't be
resolved is dropped for this run (it is still in the feed next run), never
stored with the Google address.

Resolving takes two small requests per link (the article page for a
signature and timestamp, then Google's batchexecute endpoint). Neither is a
documented API and Google publishes no quota, so: entries already stored
(same title from the same outlet in the last few days) are skipped without
a lookup, lookups are spaced out, and a feed stops resolving for the run
after a few failures in a row instead of retrying.
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

GOOGLE_NEWS_HOST = "news.google.com"
ARTICLE_PATH_RE = re.compile(r"news\.google\.com/(?:rss/)?articles/([A-Za-z0-9_-]+)")
SIGNATURE_RE = re.compile(r'data-n-a-sg="([^"]+)"')
TIMESTAMP_RE = re.compile(r'data-n-a-ts="([^"]+)"')
BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}
REQUEST_TIMEOUT = 15
LOOKUP_SPACING_SECONDS = 0.3
MAX_CONSECUTIVE_FAILURES = 3
# Google's when:1d is loose: a section-limited Economist search returned
# pieces from 2025 (2026-10-18), and WSJ's once brought a June puzzles page.
# Older entries are dropped before any lookup. 7 days, the grouping window: The
# Economist publishes weekly (Thursdays), so 3 days dropped most of its issue.
MAX_ENTRY_AGE_DAYS = 7
# Links resolved by this process, so a feed read more than once in a run (the
# enrichment passes re-read their feeds) doesn't repeat the lookups.
_RESOLVED_CACHE = {}
_RESOLVED_CACHE_MAX = 5000


def is_google_news_feed(feed_url):
    return GOOGLE_NEWS_HOST in (feed_url or "")


def article_id(link):
    """The coded id in a Google News article link, or None."""
    match = ARTICLE_PATH_RE.search(link or "")
    return match.group(1) if match else None


def strip_source_suffix(title, source_title, same_outlet=None):
    """Remove the " - <Outlet>" Google appends to every title.

    The suffix doesn't always spell the outlet the way <source> does ("- WSJ"
    with source "The Wall Street Journal"), so a last " - " segment is also
    removed when `same_outlet(segment, source_title)` says it names the same
    outlet. Any other " - " in a headline is left alone.
    """
    title = (title or "").strip()
    suffix = f" - {source_title}" if source_title else ""
    if suffix and title.endswith(suffix):
        return title[: -len(suffix)].rstrip()
    head, sep, tail = title.rpartition(" - ")
    if sep and head and same_outlet and len(tail) <= 40 and same_outlet(tail.strip(), source_title):
        return head.rstrip()
    return title


def parse_signature_page(html):
    """(signature, timestamp) from the article page, or None."""
    sig = SIGNATURE_RE.search(html or "")
    ts = TIMESTAMP_RE.search(html or "")
    if not (sig and ts):
        return None
    try:
        return sig.group(1), int(ts.group(1))
    except ValueError:
        return None


def build_batch_body(gid, timestamp, signature):
    inner = [
        "garturlreq",
        [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
          None, None, None, None, None, 0, 1],
         "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
        gid, timestamp, signature,
    ]
    payload = [[["Fbv4je", json.dumps(inner), None, "generic"]]]
    return "f.req=" + quote(json.dumps(payload))


def parse_batch_response(text):
    """The publisher URL from a batchexecute answer, or None."""
    try:
        chunk = json.loads(text.split("\n\n")[1])[:-2]
        url = json.loads(chunk[0][2])[1]
    except Exception:
        return None
    if isinstance(url, str) and url.startswith(("http://", "https://")) and GOOGLE_NEWS_HOST not in url:
        return url
    return None


def resolve_link(link, session):
    """Publisher URL for one Google News link, or None."""
    gid = article_id(link)
    if not gid:
        return None
    page = session.get(
        f"https://{GOOGLE_NEWS_HOST}/rss/articles/{gid}",
        headers=HEADERS, timeout=REQUEST_TIMEOUT,
    )
    if page.status_code != 200:
        logger.warning(f"  [Google News] Article page answered HTTP {page.status_code}")
        return None
    parsed = parse_signature_page(page.text)
    if not parsed:
        return None
    signature, timestamp = parsed
    answer = session.post(
        BATCH_URL,
        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        data=build_batch_body(gid, timestamp, signature),
        timeout=REQUEST_TIMEOUT,
    )
    if answer.status_code != 200:
        logger.warning(f"  [Google News] Lookup answered HTTP {answer.status_code}")
        return None
    return parse_batch_response(answer.text)


def resolve_entries(articles, already_stored=None, session=None, sleep=time.sleep, now=None):
    """Replace each article's Google link with the publisher's URL.

    `articles` are fetch_feed() dicts whose `title` and `source_name` have
    already been taken from the entry. `already_stored(title, source_name)`
    returns True for an entry we have, which is then dropped without a lookup.
    Entries published more than MAX_ENTRY_AGE_DAYS ago are dropped too.
    Returns (resolved articles, stats).
    """
    session = session or requests.Session()
    cutoff = (now or datetime.utcnow()) - timedelta(days=MAX_ENTRY_AGE_DAYS)
    stats = {"entries": len(articles), "too_old": 0, "already_stored": 0, "resolved": 0, "failed": 0,
             "stopped_early": False}
    resolved = []
    failures_in_a_row = 0
    for index, article in enumerate(articles):
        published = article.get("published_at")
        if published is not None and published < cutoff:
            stats["too_old"] += 1
            continue
        if already_stored and already_stored(article["title"], article["source_name"]):
            stats["already_stored"] += 1
            continue
        cached = _RESOLVED_CACHE.get(article_id(article["url"]))
        if cached:
            stats["resolved"] += 1
            resolved.append({**article, "url": cached})
            continue
        if failures_in_a_row >= MAX_CONSECUTIVE_FAILURES:
            stats["stopped_early"] = True
            stats["failed"] += 1
            continue
        if index:
            sleep(LOOKUP_SPACING_SECONDS)
        try:
            url = resolve_link(article["url"], session)
        except Exception as e:
            logger.warning(f"  [Google News] Lookup failed: {e}")
            url = None
        if url:
            if len(_RESOLVED_CACHE) >= _RESOLVED_CACHE_MAX:
                _RESOLVED_CACHE.clear()
            _RESOLVED_CACHE[article_id(article["url"])] = url
            failures_in_a_row = 0
            stats["resolved"] += 1
            resolved.append({**article, "url": url})
        else:
            failures_in_a_row += 1
            stats["failed"] += 1
    if stats["stopped_early"]:
        logger.warning(
            f"  [Google News] Stopped after {MAX_CONSECUTIVE_FAILURES} failed lookups in a row; "
            "the rest wait for the next run"
        )
    return resolved, stats
