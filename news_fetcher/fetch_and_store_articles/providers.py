# news_fetcher/fetch_and_store_articles/providers.py
"""
The NewsAPI / GNews fetches that feed store_articles(), and the raw-payload
retention that runs alongside them.

Every request goes through news_fetcher/api_budget.py, so each key's daily
limit holds across restarts and both containers. The scheduler polls each
API on its own schedule (poll_news_apis_if_due, one request per active
scheduled fetch) and keeps what comes back as a RawArticlePayload; the next
pipeline run stores those articles (ingest_payloads). A manual fetch from
the admin requests and stores at once.
"""

import logging
from datetime import datetime, timedelta
import json
from aggregator import db
from news_fetcher import api_budget
from .ingestion import store_articles

logger = logging.getLogger(__name__)

NEWSAPI = api_budget.PROVIDERS["newsapi"]
GNEWS = api_budget.PROVIDERS["gnews"]
NEWSAPI_EVERYTHING_URL = "https://newsapi.org/v2/everything"
NEWSAPI_TOP_URL = "https://newsapi.org/v2/top-headlines"
GNEWS_SEARCH_URL = "https://gnews.io/api/v4/search"
GNEWS_TOP_URL = "https://gnews.io/api/v4/top-headlines"
# Payloads a poll stored that no run ingested within this long are left.
INGEST_MAX_AGE = timedelta(hours=36)


def _published(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None) if value else datetime.utcnow()
    except Exception:
        return datetime.utcnow()


def _articles_of(body):
    return len((body or {}).get("articles") or [])


# NewsAPI ----------------------------------------------------------------

def _newsapi_request(kind, mode="top", query=None, country="us", category=None, sources=None):
    """One budgeted NewsAPI request (the key goes in a header). Search
    (/everything) is a day behind on the free plan; top headlines are live."""
    if mode == "query" and query:
        logger.info(f"[NewsAPI] Fetching query: {query}")
        url, params = NEWSAPI_EVERYTHING_URL, {"q": query, "language": "en", "sortBy": "publishedAt", "pageSize": 100}
    elif sources:
        # NewsAPI refuses sources together with a country or category.
        logger.info(f"[NewsAPI] Fetching top headlines (sources={sources})")
        url, params = NEWSAPI_TOP_URL, {"sources": sources, "pageSize": 100}
    else:
        label = f"country={country}" if country else ""
        label += f" category={category}" if category else ""
        logger.info(f"[NewsAPI] Fetching top headlines ({label.strip()})")
        url, params = NEWSAPI_TOP_URL, {"pageSize": 100}
        if country:
            params["country"] = country
        if category:
            params["category"] = category
    key = api_budget.api_key(NEWSAPI)
    body = api_budget.request(NEWSAPI, kind, url, params=params, headers={"X-Api-Key": key},
                              key=key, items_of=_articles_of)
    if body is None:
        raise RuntimeError("NewsAPI refused the request's parameters (HTTP 400)")
    return body


def _normalize_newsapi(body):
    return [{
        "title":        a.get("title"),
        "content":      a.get("content") or "",
        "url":          a.get("url"),
        "source_name":  (a.get("source") or {}).get("name", "Unknown"),
        "published_at": _published(a.get("publishedAt")),
        "image_url":    a.get("urlToImage"),
        "author":       a.get("author"),
    } for a in (body.get("articles") or [])]


# GNews ------------------------------------------------------------------

def _gnews_request(kind, query=None, category=None):
    """One budgeted GNews request (the key goes in the query string)."""
    key = api_budget.api_key(GNEWS)
    if query:
        logger.info(f"[GNews] Fetching query: {query}")
        url, params = GNEWS_SEARCH_URL, {"q": query, "lang": "en", "max": 20}
    elif category:
        logger.info(f"[GNews] Fetching category: {category}")
        url, params = GNEWS_TOP_URL, {"category": category, "lang": "en", "country": "us", "max": 20}
    else:
        logger.info("[GNews] Fetching top headlines")
        url, params = GNEWS_TOP_URL, {"lang": "en", "country": "us", "max": 20}
    body = api_budget.request(GNEWS, kind, url, params={**params, "apikey": key}, key=key, items_of=_articles_of)
    if body is None:
        raise RuntimeError("GNews refused the request's parameters (HTTP 400)")
    return body


def _normalize_gnews(body):
    return [{
        "title":        a.get("title"),
        "content":      a.get("content") or a.get("description") or "",
        "url":          a.get("url"),
        "source_name":  (a.get("source") or {}).get("name", "Unknown"),
        "published_at": _published(a.get("publishedAt")),
        "image_url":    a.get("image"),
    } for a in (body.get("articles") or [])]


NORMALIZERS = {"newsapi": _normalize_newsapi, "gnews": _normalize_gnews}


def _skipped(provider, topic_name, reason):
    return {"provider": provider, "topic_name": topic_name, "status": "skipped", "reason": reason,
            "input_articles": 0, "stored": 0}


def _save_payload(source, topic_name, body, ingested_at=None):
    from aggregator.models import RawArticlePayload
    db.session.add(RawArticlePayload(source=source, topic_name=topic_name, payload=json.dumps(body),
                                     ingested_at=ingested_at))


def _fetch_now(provider, topic_name, request):
    """Request, store the articles and keep the payload: a manual fetch."""
    p = api_budget.PROVIDERS[provider]
    if not api_budget.api_key(p):
        logger.warning(f"{p.key_setting} not set, skipping {p.name} fetch.")
        return _skipped(provider, topic_name, "missing_api_key")
    try:
        body = request()
    except api_budget.Unavailable as e:
        logger.info(f"[{p.name}] Skipping {topic_name}: {e}")
        return _skipped(provider, topic_name, str(e))
    except Exception as e:
        logger.error(f"[{p.name}] Error fetching {topic_name}: {e}")
        return {"provider": provider, "topic_name": topic_name, "status": "error", "reason": str(e),
                "input_articles": 0, "stored": 0}
    try:
        normalized = NORMALIZERS[provider](body)
        logger.info(f"[{p.name}] Fetched {len(normalized)} articles")
        metrics = store_articles(normalized, topic_name, provider=provider)
        _save_payload(provider, topic_name, body, ingested_at=datetime.utcnow())
        db.session.commit()
        metrics["status"] = "ok"
        return metrics
    except Exception as e:
        db.session.rollback()
        logger.error(f"[{p.name}] Error storing {topic_name}: {e}")
        return {"provider": provider, "topic_name": topic_name, "status": "error", "reason": str(e),
                "input_articles": 0, "stored": 0}


def fetch_newsapi(topic_name, mode="top", query=None, country="us", category=None, sources=None):
    """Fetch articles from NewsAPI and store them."""
    return _fetch_now("newsapi", topic_name,
                      lambda: _newsapi_request("manual", mode=mode, query=query, country=country,
                                               category=category, sources=sources))


def fetch_gnews(topic_name, query=None, category=None):
    """Fetch articles from GNews API and store them."""
    return _fetch_now("gnews", topic_name, lambda: _gnews_request("manual", query=query, category=category))


# Scheduled polls --------------------------------------------------------

def scheduled_fetches():
    """The active scheduled fetches (Settings › Scheduled fetches), as plain
    dicts: what each poll asks NewsAPI and GNews for."""
    from aggregator.models import ScheduledFetch
    rows = ScheduledFetch.query.filter_by(is_active=True).order_by(
        ScheduledFetch.sort_order.asc(), ScheduledFetch.id.asc()
    ).all()
    return [
        {
            "label":          row.label,
            "mode":           row.mode,
            "country":        row.newsapi_country,
            "category":       row.newsapi_category,
            "sources":        row.newsapi_sources,
            "query":          row.newsapi_query,
            "gnews_query":    row.gnews_query,
            "gnews_category": row.gnews_category,
        }
        for row in rows
    ]


def _poll(provider, fetches):
    """One request per scheduled fetch, each kept as a payload for the next
    pipeline run. Stops when the budget runs out."""
    p = api_budget.PROVIDERS[provider]
    metrics = {"requests": 0, "articles": 0, "errors": 0, "status": "ok"}
    for fetch in fetches:
        try:
            if provider == "newsapi":
                body = _newsapi_request("poll", mode=fetch["mode"], query=fetch["query"],
                                        country=fetch["country"], category=fetch["category"],
                                        sources=fetch.get("sources"))
            else:
                body = _gnews_request("poll", query=fetch["gnews_query"], category=fetch["gnews_category"])
        except api_budget.Unavailable as e:
            logger.info(f"[{p.name}] Poll stopped: {e}")
            metrics["status"] = "unavailable"
            break
        except Exception as e:
            logger.warning(f"[{p.name}] Poll of {fetch['label']} failed: {e}")
            metrics["errors"] += 1
            continue
        metrics["requests"] += 1
        metrics["articles"] += _articles_of(body)
        _save_payload(provider, fetch["label"], body)
        db.session.commit()
    return metrics


def poll_news_apis_if_due(now=None, fetches=None):
    """The scheduler calls this every minute: poll NewsAPI and GNews, each
    when its schedule says (stretched to fit its daily limit at one request
    per scheduled fetch). Returns {provider: metrics} for those polled."""
    fetches = scheduled_fetches() if fetches is None else fetches
    polled = {}
    for provider in ("newsapi", "gnews"):
        if api_budget.poll_due(provider, now, requests_per_poll=len(fetches)):
            polled[provider] = _poll(provider, fetches)
    if not polled:
        return {"skipped": "not due"}
    return polled


def ingest_payloads(now=None):
    """Store the articles of every polled payload no run has ingested yet,
    one store_articles call per scheduled fetch and provider (several polls'
    articles merged by URL). Returns {topic: {"providers": {provider: metrics}}}."""
    from aggregator.models import RawArticlePayload
    now = now or datetime.utcnow()
    pending = (RawArticlePayload.query
               .filter(RawArticlePayload.ingested_at.is_(None), RawArticlePayload.fetched_at >= now - INGEST_MAX_AGE)
               .order_by(RawArticlePayload.fetched_at.desc())
               .all())
    groups = {}
    for payload in pending:
        groups.setdefault((payload.topic_name, payload.source), []).append(payload)
    topics = {}
    for (topic_name, source), payloads in groups.items():
        normalize = NORMALIZERS.get(source)
        articles = {}
        if normalize:
            # Newest payload first, so an article keeps its latest version.
            for payload in payloads:
                try:
                    body = json.loads(payload.payload)
                except ValueError:
                    continue
                for article in normalize(body):
                    if article.get("url"):
                        articles.setdefault(article["url"], article)
        try:
            metrics = store_articles(list(articles.values()), topic_name, provider=source)
            metrics["status"] = "ok"
            metrics["payloads"] = len(payloads)
        except Exception as e:
            db.session.rollback()
            logger.error(f"[{source}] Error storing {topic_name}: {e}")
            metrics = {"provider": source, "topic_name": topic_name, "status": "error", "reason": str(e),
                       "input_articles": 0, "stored": 0}
        for payload in payloads:
            payload.ingested_at = now
        db.session.commit()
        topics.setdefault(topic_name, {"topic_name": topic_name, "providers": {}})["providers"][source] = metrics
    return topics


def cleanup_old_payloads():
    """Delete raw API payloads older than 30 days."""
    from aggregator.models import RawArticlePayload
    cutoff = datetime.utcnow() - timedelta(days=30)
    old = RawArticlePayload.query.filter(RawArticlePayload.fetched_at < cutoff).all()
    if old:
        logger.info(f"Deleting {len(old)} raw payloads older than 30 days...")
        for payload in old:
            db.session.delete(payload)
        db.session.commit()
        logger.info("Cleanup complete.")
    else:
        logger.info("No old payloads to clean up.")


def fetch_and_store_articles(topic_name, mode="top", query=None,
                              country="us", category=None, sources=None,
                              gnews_query=None, gnews_category=None):
    """
    Main entry point. Fetches from both NewsAPI and GNews for a given topic.
    """
    newsapi_metrics = fetch_newsapi(topic_name, mode=mode, query=query,
                                    country=country, category=category, sources=sources)
    gnews_metrics = fetch_gnews(topic_name, query=gnews_query, category=gnews_category)
    cleanup_old_payloads()
    return {
        "topic_name": topic_name,
        "providers": {
            "newsapi": newsapi_metrics,
            "gnews": gnews_metrics,
        },
    }
