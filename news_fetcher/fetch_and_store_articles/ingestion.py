# news_fetcher/fetch_and_store_articles/ingestion.py
"""
Per-batch article storage: the ingestion blocklists, dedupe, outlet
creation, embedding, grouping and classification for one list of
normalized article dicts (store_articles), plus the metrics and
serialization helpers it and the review pass share.
"""

import logging
from datetime import datetime, timedelta
import json
import os
import re
import threading
from aggregator import db, jobs
from aggregator.article_signals import (
    bias_bucket_for_score, is_media_oped, is_off_section_oped, is_roundup_article,
    low_value_article_reason,
)
from aggregator.html_safety import strip_title_tags
from aggregator.models import Article, OpinionPiece, Outlet, Story, Topic, IngestionBlock
from aggregator.outlet_origins import is_international, lean_abroad_for, origin_for
from news_fetcher import llm_client
from news_fetcher.allsides_lookup import get_allsides_score
from news_fetcher.bylines import clean_byline
from news_fetcher.outlet_bias_llm import get_outlet_bias_from_llm
from news_fetcher import wire_carriers
from news_fetcher.scraper import scrape_article
from news_fetcher.story_grouper import find_or_create_story
from news_fetcher.topic_classifier import classify_article
from news_fetcher.workpool import map_in_app
from .outlets import normalize_source_name, source_name_from_url

logger = logging.getLogger(__name__)


BLOCK_KIND_SOURCE = "source"
BLOCK_KIND_TITLE = "title_keyword"


def get_ingestion_blocks():
    """
    The ingestion blocklists -- DB-backed (IngestionBlock, admin-editable at
    /admin/ingestion-blocks) rather than two hardcoded lists.

    Returns (sources, title_keywords), both lowercased once here so the
    per-article checks don't re-lower on every comparison and an entry typed
    as "NFL.com" in the admin still matches. Loaded once per store_articles()
    batch, not per article: this is the hottest path in the fetch pipeline.
    """
    rows = IngestionBlock.query.filter_by(is_active=True).all()
    sources, title_keywords = [], []
    for row in rows:
        pattern = (row.pattern or "").strip().lower()
        if not pattern:
            continue
        if row.kind == BLOCK_KIND_SOURCE:
            sources.append(pattern)
        elif row.kind == BLOCK_KIND_TITLE:
            title_keywords.append(pattern)
    return sources, title_keywords


class IngestionBlocks:
    """The active ingestion blocks for one batch, counting which rule
    stopped each article so the Blocklist page can say what every rule
    caught (aggregator/blocklist.py). Patterns are lowercased once here, as
    in get_ingestion_blocks()."""

    def __init__(self):
        self.sources, self.title_keywords = [], []
        for row in IngestionBlock.query.filter_by(is_active=True).all():
            pattern = (row.pattern or "").strip().lower()
            if not pattern:
                continue
            if row.kind == BLOCK_KIND_SOURCE:
                self.sources.append((pattern, row.id))
            elif row.kind == BLOCK_KIND_TITLE:
                self.title_keywords.append((pattern, row.id))
        self.hits = {}

    def _match(self, rules, text, count):
        text = (text or "").lower()
        for pattern, block_id in rules:
            if pattern in text:
                if count:
                    self.hits[block_id] = self.hits.get(block_id, 0) + 1
                return True
        return False

    def blocks_url(self, url, count=True):
        return self._match(self.sources, url, count)

    def blocks_title(self, title, count=True):
        return self._match(self.title_keywords, title, count)

    def save_hits(self):
        """Add this batch's hits to today's counts. Doesn't commit."""
        from aggregator.blocklist import record_block_hits
        record_block_hits(self.hits)
        self.hits = {}


GROUPING_LOOKBACK_DAYS = int(os.getenv("MUCKSCRAPER_GROUPING_LOOKBACK_DAYS", "7"))


BIAS_BUCKETS = ("left", "lean_left", "center", "lean_right", "right", "unrated")


def empty_store_metrics(topic_name, provider=None, input_articles=0):
    return {
        "topic_name": topic_name,
        "provider": provider,
        "input_articles": input_articles,
        "stored": 0,
        "new_outlets": 0,
        "stories_touched": 0,
        "skipped": {
            "missing_required": 0,
            "blocked_source": 0,
            "blocked_title": 0,
            "low_value_url": 0,
            "roundup": 0,
            "betting": 0,
            "advice_column": 0,
            "cartoon": 0,
            "opinion": 0,
            "promotional": 0,
            "duplicate_url": 0,
            "duplicate_title_outlet": 0,
            "unknown_outlet": 0,
            # A wire carrier's page that doesn't credit Reuters, from a
            # carrier feed (news_fetcher/wire_carriers.py).
            "not_wire_copy": 0,
            # A carrier's Reuters copy that filled the stored Reuters
            # headline instead of becoming an article of its own.
            "wire_text_filled": 0,
        },
        "scrape_statuses": {},
        "bias_buckets": {bucket: 0 for bucket in BIAS_BUCKETS},
        "bias_sources": {
            "allsides": 0,
            "ai": 0,
            "unrated": 0,
        },
    }


def merge_count_maps(target, source):
    for key, value in (source or {}).items():
        target[key] = target.get(key, 0) + value


def serialize_grouping_candidate_ids(candidate_story_ids):
    ids = []
    for story_id in candidate_story_ids or []:
        try:
            ids.append(int(story_id))
        except (TypeError, ValueError):
            continue
    return json.dumps(ids) if ids else None


def deserialize_grouping_candidate_ids(raw_value):
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
    except Exception:
        return []
    ids = []
    for story_id in parsed if isinstance(parsed, list) else []:
        try:
            ids.append(int(story_id))
        except (TypeError, ValueError):
            continue
    return ids


def truncate_db_string(value, max_length):
    if value is None:
        return None
    value = str(value)
    if len(value) <= max_length:
        return value
    return value[: max_length - 1] + "…"


def get_or_create_topic(topic_name):
    """Get existing topic or create a new one, handling race conditions."""
    topic = Topic.query.filter_by(name=topic_name).first()
    if not topic:
        try:
            topic = Topic(name=topic_name)
            db.session.add(topic)
            db.session.flush()
        except Exception:
            # Another process created it at the same time, roll back and fetch it
            db.session.rollback()
            topic = Topic.query.filter_by(name=topic_name).first()
    return topic


def normalize_url(url):
    """Strip query parameters from URL to detect duplicates."""
    try:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        # Keep only scheme, netloc, and path
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    except Exception:
        return url


def detect_duplicate_outlet_content(content, outlet_id, exclude_article_id=None):
    """
    Check if scraped content is near-identical to other articles from the same outlet.
    This catches login/error pages that return the same HTML for every blocked request.
    Returns (is_duplicate: bool, reason: str or None).
    """
    if not content or not outlet_id:
        return False, None

    from news_fetcher.scraper import sanitize_html
    import re

    def strip_to_text(html, max_chars=2000):
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:max_chars]

    clean_new = strip_to_text(content)
    if len(clean_new) < 100:
        return False, None

    from difflib import SequenceMatcher

    recent = Article.query.filter(
        Article.outlet_id == outlet_id,
        Article.content != None,
        Article.content != "",
    )
    if exclude_article_id:
        recent = recent.filter(Article.id != exclude_article_id)
    recent = recent.order_by(Article.id.desc()).limit(10).all()

    match_count = 0
    for article in recent:
        if not article.content:
            continue
        clean_existing = strip_to_text(article.content)
        if len(clean_existing) < 100:
            continue
        ratio = SequenceMatcher(None, clean_new, clean_existing).ratio()
        if ratio > 0.85:
            match_count += 1

    if match_count >= 2:
        reason = f"Bad scrape: content near-identical to {match_count} other articles from same outlet (login/error page)"
        return True, reason

    return False, None


def save_opinion_piece(title, url, source_name, published_at, content, image_url, seen_urls):
    """Add an op-ed/editorial to `opinion_pieces` (committed with the batch).

    Deliberately cheap: no scraping, embedding or LLM call, and an unknown
    outlet is left NULL rather than created (creating one can ask the LLM for
    its bias). A failure here is logged and never stops the news ingestion.
    """
    if url in seen_urls:
        return False
    seen_urls.add(url)
    # A savepoint, so a failure here rolls back only this row. Without it a
    # DB error (say, the table missing on an un-migrated install) aborts the
    # whole batch's transaction and every news article after it fails too.
    savepoint = db.session.begin_nested()
    try:
        if OpinionPiece.query.filter_by(url=url).first():
            savepoint.rollback()
            return False
        outlet = Outlet.query.filter_by(name=source_name).first()
        from news_fetcher.scraper import sanitize_html
        db.session.add(OpinionPiece(
            title=title,
            url=url,
            source=source_name,
            outlet_id=outlet.id if outlet else None,
            bias_score=outlet.bias_score if outlet else None,
            date=published_at,
            content=sanitize_html(f"<div>{content}</div>") if content else None,
            image_url=image_url,
        ))
        savepoint.commit()
        return True
    except Exception as e:
        savepoint.rollback()
        logger.warning(f"  [OpEd] Could not save opinion piece {url[:80]}: {e}")
        return False


_OPINION_TITLE_PREFIX = re.compile(r"^\s*opinion\s*[|:]\s*", re.IGNORECASE)


def store_opinion_feed(articles_data):
    """Store every entry of an Op/Ed feed (RssFeed bucket `op_ed`) as opinion.

    These feeds are opinion sections, so the URL check in is_opinion_article()
    is not needed and would miss outlets whose column URLs carry no /opinion/
    (Washington Times, National Review, ...). Everything else still applies:
    source and title blocks, and the other low-value reasons (betting,
    roundups, video, promo), and an entry from an outlet in
    OPED_FEED_SECTION_PATHS outside its opinion path is skipped (news the
    NYT Opinion feed carries), as is a video/audio/podcast page
    (is_media_oped()). Nothing here reaches `articles`.
    """
    metrics = {"input_articles": len(articles_data), "saved": 0, "skipped": {}}

    def skip(reason):
        metrics["skipped"][reason] = metrics["skipped"].get(reason, 0) + 1

    blocks = IngestionBlocks()
    seen_urls = set()
    for article in articles_data:
        title = strip_title_tags(article.get("title"))
        # Everything here is opinion already; Google News titles WaPo's
        # columns "Opinion | <headline>" where its own feed did not.
        title = _OPINION_TITLE_PREFIX.sub("", title or "")
        raw_url = article.get("url")
        if not title or not raw_url:
            skip("missing_required")
            continue
        url = normalize_url(raw_url)
        if blocks.blocks_url(url):
            skip("blocked_source")
            continue
        if blocks.blocks_title(title):
            skip("blocked_title")
            continue
        reason = low_value_article_reason(title, url)
        if reason and reason != "opinion":
            skip(reason)
            continue
        if is_off_section_oped(url):
            skip("not_opinion_section")
            continue
        if is_media_oped(url):
            skip("media")
            continue
        source_name = source_name_from_url(raw_url) or normalize_source_name(article.get("source_name", "Unknown"))
        if save_opinion_piece(title, url, source_name, article.get("published_at") or datetime.utcnow(),
                              article.get("content") or "", article.get("image_url"), seen_urls):
            metrics["saved"] += 1
        else:
            skip("not_saved")  # already stored, or the save failed (logged)
    db.session.commit()
    return metrics


def fill_wire_headline(title, scrape_result, carrier, carrier_url, days=4):
    """Give a stored Reuters headline the text a wire carrier republished.

    reuters.com is headline-only, so the Reuters article the site:reuters.com
    feed stored has no text. When it is there (same title, last few days,
    under MIN_TEXT_CHARS), it takes the carrier's text and keeps its own
    reuters.com link. Returns True when an article was filled.
    """
    if not scrape_result.content:
        return False
    outlet = Outlet.query.filter_by(name=wire_carriers.WIRE_OUTLET).first()
    if outlet is None:
        return False
    cutoff = datetime.utcnow() - timedelta(days=days)
    headline = (
        Article.query
        .filter(Article.outlet_id == outlet.id, Article.title == title, Article.fetched_at >= cutoff)
        .first()
    )
    if headline is None or len(headline.content or "") >= wire_carriers.MIN_TEXT_CHARS:
        return False
    headline.content = scrape_result.content
    headline.scrape_status = scrape_result.status
    headline.scrape_method = truncate_db_string(scrape_result.method, 255)
    headline.scrape_failure_reason = truncate_db_string(scrape_result.failure_reason, 1024)
    headline.scrape_http_status = scrape_result.http_status
    headline.text_via = carrier
    headline.text_via_url = carrier_url
    if not headline.image_url and scrape_result.image_url:
        headline.image_url = scrape_result.image_url
    # Embedded from the title alone when it was stored; now with its text,
    # as every other article is.
    embedding = llm_client.get_embeddings([_embed_text(title, scrape_result.content)])[0]
    if isinstance(embedding, str):
        embedding = json.loads(embedding)
    if embedding is not None:
        headline.embedding = embedding
    logger.info(f"  [Wire] Filled Reuters headline {headline.id} with text via {carrier}")
    return True


# One batch at a time groups and stores its articles. Feed jobs run side by
# side (aggregator/workers.py) and everything slow -- scraping, classifying,
# embedding -- happens before this lock, but grouping has to see every
# article stored before it: two feeds carrying the same wire story must land
# in one story, not race to make two. The scheduler is one process, so a
# thread lock is enough.
STORE_LOCK = threading.Lock()


def _embed_text(title, content):
    """Title plus a snippet of the feed text: what an article is embedded on."""
    from news_fetcher.story_grouper import strip_video_prefix
    from news_fetcher.summarizer import strip_html
    clean_title = strip_video_prefix(title)
    if content:
        snippet = strip_html(content)[:200].strip()
        return f"{clean_title}. {snippet}"
    return clean_title


def _prepare(candidate):
    """The network part of storing one article, run on the ingest pool:
    classify its title and scrape its page. No writes to the batch."""
    return {
        "topics": candidate["sections"] or classify_article(candidate["title"], candidate["content"]),
        # A wire carrier's page was already read in the first pass.
        "scrape": candidate.get("scrape") or scrape_article(candidate["url"], fallback_content=candidate["content"]),
    }


def store_articles(articles_data, topic_name, provider=None):
    """
    Store a list of normalized article dicts into the database,
    tagging them with the given topic.
    articles_data: list of dicts with keys:
        title, content, url, source_name, published_at, image_url

    In three passes: cheap checks (blocklists, low-value, duplicates); then
    the slow network work for what's left -- one batched embedding request,
    and classification and scraping on the shared ingest pool
    (news_fetcher/workpool.py); then, under STORE_LOCK, grouping and the
    writes, with the duplicate checks done again for anything another batch
    stored in the meantime.
    """
    metrics = empty_store_metrics(topic_name, provider=provider, input_articles=len(articles_data))
    stories_touched = set()

    # Same reasoning as the story pre-fetch below: every article in the batch
    # is tested against these, so load them once rather than per article.
    blocks = IngestionBlocks()
    # The sections each article's source serves (aggregator/sources.py).
    from aggregator.sources import IngestSections
    source_sections = IngestSections(topic_name)
    opinion_urls_seen = set()
    # Bylines that are just an outlet's name ("Washington Post" on a Toronto
    # Sun reprint) are dropped; loaded once per batch.
    outlet_names = {name.lower() for (name,) in db.session.query(Outlet.name) if name}

    candidates = []
    batch_urls = set()
    for article in articles_data:
        # Some outlets (National Review's culture pieces, e.g. "Not Ready for
        # <i>Primetime</i>") embed formatting tags in their RSS <title>. The
        # title is always shown as plain text (story cards, "In Brief",
        # <title>/OG tags), never through the sanitized-HTML content path, so
        # a literal "<i>" leaks straight onto the page instead of rendering.
        # Strip tags once here rather than at every render site.
        title        = strip_title_tags(article.get("title"))
        content      = article.get("content") or ""
        raw_url      = article.get("url")
        source_name  = source_name_from_url(raw_url) or normalize_source_name(article.get("source_name", "Unknown"))
        published_at = article.get("published_at", datetime.utcnow())
        image_url    = article.get("image_url")
        author       = clean_byline(article.get("author"), source_name, outlet_names)

        if not title or not raw_url:
            metrics["skipped"]["missing_required"] += 1
            continue

        url = normalize_url(raw_url)

        if blocks.blocks_url(url):
            logger.debug(f"Skipping blocked source: {url}")
            metrics["skipped"]["blocked_source"] += 1
            continue

        low_value_reason = low_value_article_reason(title, url)
        if low_value_reason == "opinion":
            # Still refused as news (and counted as skipped), but kept in the
            # separate Op/Ed table unless a title block applies.
            if not blocks.blocks_title(title, count=False):
                save_opinion_piece(title, url, source_name, published_at, content, image_url, opinion_urls_seen)
        if low_value_reason:
            logger.debug(f"Skipping low-value article ({low_value_reason}): {title} [{url}]")
            metrics["skipped"][low_value_reason if low_value_reason in metrics["skipped"] else "low_value_url"] += 1
            continue

        if blocks.blocks_title(title):
            logger.debug(f"Skipping blocked title: {title}")
            metrics["skipped"]["blocked_title"] += 1
            continue

        # Check for URL duplicate (normalized), in the database or earlier
        # in this batch.
        if url in batch_urls or Article.query.filter_by(url=url).first():
            logger.debug(f"Skipping duplicate URL: {title}")
            metrics["skipped"]["duplicate_url"] += 1
            continue

        # A wire carrier's page is read here rather than on the ingest pool:
        # whether it credits Reuters decides the outlet, and so the duplicate
        # check just below (news_fetcher/wire_carriers.py). Only the carrier
        # feeds' few dozen pages a run take this path.
        scrape_result = None
        text_via = None
        carrier = wire_carriers.carrier_name(url)
        if carrier:
            scrape_result = scrape_article(url, fallback_content=content)
            if scrape_result.credits_wire:
                text_via = carrier
                source_name = wire_carriers.WIRE_OUTLET
                author = clean_byline(article.get("author"), source_name, outlet_names)
                if fill_wire_headline(title, scrape_result, carrier, url):
                    metrics["skipped"]["wire_text_filled"] += 1
                    continue
            elif article.get("wire_only"):
                logger.debug(f"Skipping carrier page that isn't Reuters copy: {title} [{url}]")
                metrics["skipped"]["not_wire_copy"] += 1
                continue

        # Check for Title + Source duplicate (catch same article, different URL)
        outlet = Outlet.query.filter_by(name=source_name).first()
        if outlet and Article.query.filter_by(title=title, outlet_id=outlet.id).first():
            logger.debug(f"Skipping duplicate Title+Outlet: {title}")
            metrics["skipped"]["duplicate_title_outlet"] += 1
            continue

        if not outlet and source_sections.known_outlets_only:
            logger.debug(f"Skipping new outlet {source_name} (search keeps known outlets only): {title}")
            metrics["skipped"]["unknown_outlet"] += 1
            continue

        if is_roundup_article(title, raw_url):
            image_url = None
        batch_urls.add(url)
        candidates.append({
            "title": title, "content": content, "raw_url": raw_url, "url": url,
            "source_name": source_name, "published_at": published_at,
            "image_url": image_url, "author": author,
            "scrape": scrape_result, "text_via": text_via,
            # A source that serves sections tags its articles with them;
            # anything from a General source is classified in _prepare.
            "sections": source_sections.sections_for(article),
        })

    # Opinion pieces saved above, and the block rules' hit counts, are
    # committed now, so the slow part below doesn't hold the batch's
    # transaction open.
    blocks.save_hits()
    db.session.commit()

    if candidates:
        # One request for the batch's embeddings, then classify + scrape
        # side by side. Grouping needs the embedding, the article row needs
        # the rest, and none of it needs the lock.
        embeddings = llm_client.get_embeddings([_embed_text(c["title"], c["content"]) for c in candidates])
        prepared = map_in_app(_prepare, candidates, on_each=jobs.beat)
        for candidate, embedding, ready in zip(candidates, embeddings, prepared):
            # Ensure embedding is a list, not a string
            if isinstance(embedding, str):
                embedding = json.loads(embedding)
            candidate.update(ready, embedding=embedding)

        with STORE_LOCK:
            _store_prepared(candidates, metrics, stories_touched, outlet_names)

    metrics["stories_touched"] = len(stories_touched)
    logger.info(
        "Stored %s new articles for topic: %s (provider=%s, skipped=%s)",
        metrics["stored"],
        topic_name,
        provider or "unknown",
        metrics["skipped"],
    )
    return metrics


def _store_prepared(candidates, metrics, stories_touched, outlet_names):
    """Group and write prepared articles; the caller holds STORE_LOCK."""
    # Pre-fetch recent stories once for the whole batch. This is the hottest
    # fetch path: every new article compares against this pool, so keep the
    # lookback configurable while we diagnose runtime trends. Loaded under
    # the lock, so it includes what other batches stored a moment ago.
    # No selectinload(Story.articles) here on purpose: the pgvector query in
    # find_matching_story_with_metadata() does the real embedding-match work
    # in SQL and never touches the ORM relationship, so eager-loading every
    # recent story's articles up front (~40s/run at ~3,440 stories) paid for
    # a collection almost nothing in the batch actually reads. What does read
    # story.articles -- the title_overlap_review/embedding_review snippet
    # lookups (a handful of candidates per call) and this loop's own
    # story.articles.append() below -- now lazy-loads per story touched,
    # which is bounded by batch size rather than the whole lookback pool.
    cutoff = datetime.utcnow() - timedelta(days=GROUPING_LOOKBACK_DAYS)
    recent_stories = (
        Story.query
        .filter(Story.created_at >= cutoff)
        .all()
    )
    logger.info(
        "  [Grouper] Loaded %s recent stories from the last %s day(s) for matching",
        len(recent_stories),
        GROUPING_LOOKBACK_DAYS,
    )

    for candidate in candidates:
        jobs.beat()
        title, content, url = candidate["title"], candidate["content"], candidate["url"]
        source_name, image_url = candidate["source_name"], candidate["image_url"]
        scrape_result = candidate["scrape"]
        classified_topic_names = candidate["topics"]

        # Another feed's batch may have stored it while this one was
        # scraping.
        if Article.query.filter_by(url=url).first():
            metrics["skipped"]["duplicate_url"] += 1
            continue
        outlet = Outlet.query.filter_by(name=source_name).first()
        if outlet and Article.query.filter_by(title=title, outlet_id=outlet.id).first():
            metrics["skipped"]["duplicate_title_outlet"] += 1
            continue

        logger.info(f"Processing: {title}")

        if not outlet:
            country, funding = origin_for(source_name, url)
            as_score = get_allsides_score(source_name)
            if as_score is not None:
                logger.info(f"  New outlet {source_name}: AllSides rating {as_score}")
                bias_score = as_score
                bias_source = "allsides"
                allsides_bias_score = as_score
            elif is_international(country):
                # No LLM guess for outlets based abroad: MBFC / Ad Fontes's
                # US-scale lean if they give one, else none
                # (aggregator/outlet_origins.py).
                lean = lean_abroad_for(source_name)
                logger.info(f"  New outlet {source_name}: based in {country}, lean {lean}")
                bias_score = float(lean) if lean else None
                bias_source = "mbfc_adfontes" if lean else "international"
                allsides_bias_score = None
            else:
                logger.info(f"  New outlet {source_name}: no AllSides rating, asking Ollama...")
                bias_score = get_outlet_bias_from_llm(source_name)
                bias_source = "ai" if bias_score is not None else None
                allsides_bias_score = None

            outlet = Outlet(
                name=source_name,
                url=url,
                description="N/A",
                bias_score=bias_score,
                allsides_bias_score=allsides_bias_score,
                bias_source=bias_source,
                country=country,
                funding=funding,
            )
            db.session.add(outlet)
            db.session.flush()
            metrics["new_outlets"] += 1

        article_embedding = candidate["embedding"]
        story, match = find_or_create_story(title, db, Story, recent_stories,
                                            article_embedding=article_embedding,
                                            article_content=content)
        stories_touched.add(story.id)

        # Add new story to recent_stories so subsequent articles
        # in this same batch can match against it
        if story not in recent_stories:
            recent_stories.append(story)

        # The source's sections, or the classifier's (both found before the lock)
        from aggregator.models import Topic as TopicModel
        for classified_name in classified_topic_names:
            classified_topic = TopicModel.query.filter_by(name=classified_name).first()
            if not classified_topic:
                classified_topic = TopicModel(name=classified_name)
                db.session.add(classified_topic)
                db.session.flush()
            if classified_topic not in story.topics:
                story.topics.append(classified_topic)

        scraped_content = scrape_result.content
        if scraped_content:
            # Check if this looks like a duplicate login/error page across the outlet
            is_dup, dup_reason = detect_duplicate_outlet_content(scraped_content, outlet.id)
            if is_dup:
                logger.warning(f"  [Scraper] {dup_reason} — clearing content and blocking domain for {url[:60]}")
                from news_fetcher.scraper import add_to_blocklist
                add_to_blocklist(url, dup_reason)
                scraped_content = None
                scrape_result.content = None
                scrape_result.status = "blocked"
                scrape_result.failure_reason = dup_reason

        # A feed without an image often still links a page with an og:image.
        if not image_url and scrape_result.image_url and not is_roundup_article(title, candidate["raw_url"]):
            image_url = scrape_result.image_url

        if scraped_content:
            final_content = scraped_content
        else:
            from news_fetcher.scraper import sanitize_html
            final_content = sanitize_html(f"<div>{content}</div>") if content else ""

        new_article = Article(
            title=title,
            content=final_content,
            source=source_name,
            author=candidate["author"] or clean_byline(scrape_result.author, source_name, outlet_names),
            outlet_id=outlet.id,
            # story_id is deliberately NOT set here -- the append below establishes
            # it. Setting the FK here as well makes the collection's first lazy
            # load autoflush this row, so the load returns it AND the append adds
            # the same object again, double-counting len(story.articles).
            url=url,
            date=candidate["published_at"],
            fetched_at=datetime.utcnow(),
            bias_score=outlet.bias_score,
            image_url=image_url,
            embedding=article_embedding,
            scrape_status=scrape_result.status,
            scrape_method=truncate_db_string(scrape_result.method, 255),
            scrape_failure_reason=truncate_db_string(scrape_result.failure_reason, 1024),
            scrape_http_status=scrape_result.http_status,
            text_via=candidate.get("text_via"),
            text_via_url=url if candidate.get("text_via") else None,
            grouping_match_method=truncate_db_string(getattr(match, "method", None), 32),
            grouping_confidence=getattr(match, "confidence", None),
            grouping_candidate_story_ids=serialize_grouping_candidate_ids(
                getattr(match, "candidate_story_ids", None)
            ),
            grouping_needs_review=bool(getattr(match, "needs_review", False)),
        )

        # IMPORTANT: append BEFORE session.add, and keep the FK out of the
        # constructor above. Appending is what makes the new article visible to
        # find_matching_story for subsequent articles in this SAME loop iteration;
        # doing it while the article is still transient means the collection's
        # first lazy load has nothing pending to flush, so the article lands in
        # story.articles exactly once and the >= 2 guard below is honest.
        story.articles.append(new_article)
        db.session.add(new_article)

        # Tag the article with its own topics, not all of the story's: the
        # API's section is the topic most of a story's articles carry
        # (aggregator/api/serializers.py _section), so one off-angle article
        # can't relabel the story.
        for t in story.topics:
            if t.name in classified_topic_names and t not in new_article.topics:
                new_article.topics.append(t)

        # Headlines are no longer generated here. They run in one batch pass
        # (generate_headlines_for_stale_stories) after grouping settles, so the
        # quality model loads once per run instead of being swapped in and out
        # around every grouping/classification call. This story is now stale by
        # definition -- it just gained an article -- and the pass finds it via
        # headline_generated_at being older than the newest article.
        if len(story.articles) < 2:
            # For single-article stories, ensure story headline is cleared
            # so the UI falls back to story.title (original article title)
            story.headline = None
            story.headline_generated_at = None

        metrics["stored"] += 1
        metrics["scrape_statuses"][scrape_result.status] = (
            metrics["scrape_statuses"].get(scrape_result.status, 0) + 1
        )
        bias_bucket = bias_bucket_for_score(outlet.bias_score)
        metrics["bias_buckets"][bias_bucket] += 1
        bias_source = outlet.bias_source or "unrated"
        metrics["bias_sources"][bias_source] = metrics["bias_sources"].get(bias_source, 0) + 1

    db.session.commit()
