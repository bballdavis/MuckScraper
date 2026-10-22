# news_fetcher/nyt_api.py
"""The New York Times' free Article Search API, for paywalled NYT articles.

The scraper leaves a paywalled nytimes.com article headline-only (no crawler
user agent, no headless browser -- see news_fetcher/scraper.py). NYT's own
developer API gives the abstract and the article's opening paragraph for free,
under its API terms, so a NYT article gets that much text to summarize instead
of the one-line RSS description.

Needs NYT_API_KEY (a free key from developer.nytimes.com), set on Settings >
Source API keys or in the environment. Both it and NYT_API_DAILY_LIMIT are
read on every lookup, so a key saved in the admin takes effect without a
restart. Without a key, lookup_article() always returns None.

The key allows 5 requests a minute and 500 a day, shared across every NYT API.
Calls are spaced NYT_API_MIN_INTERVAL_SECONDS apart and stop for the day at
NYT_API_DAILY_LIMIT; both counters are per process, which is fine because only
the scheduler container ingests articles.
"""

import logging
import os
import threading
import time
from datetime import date
from urllib.parse import urlparse, urlunparse

import requests

logger = logging.getLogger(__name__)

NYT_API_MIN_INTERVAL_SECONDS = 12.5
ARTICLE_SEARCH_URL = "https://api.nytimes.com/svc/search/v2/articlesearch.json"

# Article Search has filtered on `web_url` for years and on `url` since its
# 2025 revision. Try them in order and remember the first that finds anything,
# so a working filter costs one request per article.
_URL_FILTER_FIELDS = ["url", "web_url"]

_lock = threading.Lock()
_state = {"last_call": 0.0, "day": None, "calls_today": 0, "filter_field": None, "rejected_key": None}
_cache = {}
_CACHE_MAX_ENTRIES = 1000


def is_nyt_url(url):
    host = (urlparse(url or "").hostname or "").lower()
    return host == "nytimes.com" or host.endswith(".nytimes.com")


def _setting(name, default=""):
    """A saved admin setting, else the env var, else `default`."""
    try:
        from news_fetcher import saved_settings
    except ImportError:  # before the admin key store (provider-settings) is merged
        return os.environ.get(name, default)
    return saved_settings.get(name, default)


def _api_key():
    return (_setting("NYT_API_KEY") or "").strip()


def _daily_limit():
    try:
        return int(_setting("NYT_API_DAILY_LIMIT", "450") or 450)
    except ValueError:
        return 450


def enabled():
    key = _api_key()
    return bool(key) and key != _state["rejected_key"]


def _canonical(url):
    """The article URL without query string or fragment, as NYT indexes it."""
    parts = urlparse(url)
    return urlunparse((parts.scheme or "https", parts.netloc.lower(), parts.path, "", "", ""))


def _take_request_slot(daily_limit):
    """Wait for the rate limit. Returns False once today's budget is spent."""
    with _lock:
        today = date.today()
        if _state["day"] != today:
            _state["day"] = today
            _state["calls_today"] = 0
        if _state["calls_today"] >= daily_limit:
            return False
        wait = _state["last_call"] + NYT_API_MIN_INTERVAL_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _state["last_call"] = time.monotonic()
        _state["calls_today"] += 1
        return True


class _Unavailable(Exception):
    """The API can't answer right now (budget spent, rate limited); not cached."""


def _search(field, url, key):
    """Docs matching `field:("url")`, or None when NYT rejects the field."""
    daily_limit = _daily_limit()
    if not _take_request_slot(daily_limit):
        raise _Unavailable(f"daily budget of {daily_limit} requests spent")
    response = requests.get(
        ARTICLE_SEARCH_URL,
        params={"fq": f'{field}:("{url}")', "api-key": key},
        timeout=15,
    )
    if response.status_code == 429:
        raise _Unavailable("rate limited (429)")
    if response.status_code in (401, 403):
        # A bad or revoked key: stop spending requests on it until a
        # different key is saved.
        _state["rejected_key"] = key
        raise _Unavailable(f"key rejected (HTTP {response.status_code})")
    if response.status_code == 400:
        return None
    response.raise_for_status()
    return (response.json().get("response") or {}).get("docs") or []


def _doc_for(url, key):
    fields = [_state["filter_field"]] if _state["filter_field"] else _URL_FILTER_FIELDS
    for field in fields:
        for doc in _search(field, url, key) or []:
            if _canonical(doc.get("web_url") or doc.get("url") or "") == url:
                _state["filter_field"] = field
                return doc
    return None


def _paragraphs(doc):
    """Abstract, then the opening paragraph, without repeats."""
    seen = []
    for key in ("abstract", "lead_paragraph", "snippet"):
        text = " ".join((doc.get(key) or "").split())
        if text and not any(text == other or text in other for other in seen):
            seen.append(text)
    return seen


def lookup_article(url):
    """The NYT API's abstract and opening paragraph for `url` as a list of
    paragraphs, or None when the key is unset, the URL isn't NYT's, the daily
    budget is spent, or the API has no record of it."""
    if not enabled() or not is_nyt_url(url):
        return None
    canonical = _canonical(url)
    if canonical in _cache:
        return _cache[canonical]
    try:
        doc = _doc_for(canonical, _api_key())
    except _Unavailable as exc:
        logger.info("  [NYT API] Skipping %s: %s", canonical[:80], exc)
        return None
    except Exception as exc:
        logger.info("  [NYT API] Lookup failed for %s: %s", canonical[:80], exc)
        return None
    paragraphs = _paragraphs(doc) if doc else None
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        _cache.clear()
    _cache[canonical] = paragraphs or None
    return _cache[canonical]
