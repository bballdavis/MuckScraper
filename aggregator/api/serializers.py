"""What the API sends, field by field. Every response is built here from an
explicit list, so internal columns (scrape status and failure reasons,
grouping confidence, embeddings, raw payloads) never leave the server.

Dates are ISO 8601 UTC to the second ("2026-10-22T14:05:00Z"), the form
Swift's .iso8601 decoder reads.
"""
from datetime import datetime as dt
from urllib.parse import urlparse

from aggregator.article_copies import copy_label, is_copy_in_story
from aggregator.filters import summary_paragraph
from aggregator.html_safety import sanitize_html
from aggregator.opinion import leaning

DEK_MAX_CHARS = 280


def iso(value):
    if value is None:
        return None
    if isinstance(value, dt):
        return value.replace(microsecond=0).isoformat() + "Z"
    return value.isoformat()


def _http_url(url):
    """`url` when it is an absolute http(s) URL, else None."""
    if not url or len(url) > 2000:
        return None
    return url if urlparse(url).scheme in ("http", "https") else None


def _paragraphs(text):
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _dek(summary):
    """The summary's first paragraph, without a section label, cut at a word."""
    for line in _paragraphs(summary):
        text = summary_paragraph(line)
        if text:
            if len(text) > DEK_MAX_CHARS:
                text = text[:DEK_MAX_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "…"
            return text
    return None


def _article_leaning(article):
    score = article.bias_score
    if score is None and article.outlet:
        score = article.outlet.bias_score
    return leaning(score)


def coverage(story):
    counts = {"left": 0, "center": 0, "right": 0}
    for article in story.site_articles:
        side = _article_leaning(article)
        if side in counts:
            counts[side] += 1
    return counts


def _section(story):
    topics = sorted(story.topics, key=lambda t: (t.sort_order is None, t.sort_order or 0, t.name))
    return topics[0].name if topics else "News"


def _story_image(story, edition_story=None):
    if edition_story is not None and _http_url(edition_story.source_image_url):
        return edition_story.source_image_url
    for article in sorted(story.site_articles, key=lambda a: a.date or dt.min, reverse=True):
        if not article.is_roundup and _http_url(article.image_url):
            return article.image_url
    return None


def story_summary(story, edition_story=None):
    """A story card: the iOS StorySummary."""
    articles = story.site_articles
    return {
        "id": story.id,
        "section": _section(story),
        "headline": story.display_headline,
        "dek": _dek(story.summary),
        "image_url": _story_image(story, edition_story),
        "coverage": coverage(story),
        "source_count": len({a.outlet.name if a.outlet else a.source for a in articles}),
        "updated_at": iso(story.last_updated),
        "is_summarized": bool((story.summary or "").strip()),
        "has_analysis": bool((story.deep_report or "").strip()),
        "has_ai_headline": bool(story.has_ai_headline),
        "has_updates": bool(getattr(edition_story, "has_updates", False)),
    }


def outlet(o, fallback_name=None):
    if o is None:
        return {"id": None, "name": fallback_name or "Unknown", "leaning": "unrated"}
    return {"id": o.id, "name": o.name, "leaning": leaning(o.bias_score)}


def article_card(article):
    return {
        "id": article.id,
        "story_id": article.story_id,
        "title": article.title,
        "outlet": outlet(article.outlet, article.source),
        "leaning": _article_leaning(article),
        "url": _http_url(article.url),
        "published_at": iso(article.date),
        "image_url": None if article.is_roundup else _http_url(article.image_url),
        "summary": summary_paragraph(article.summary) or None,
        "copy_of": copy_label(article),
    }


def article_detail(article, full_text=False):
    out = article_card(article)
    out.update({
        "topics": [t.name for t in article.topics],
        "analysis": _paragraphs(article.deep_analysis),
        # Scraped text only when Settings › Mobile API allows it.
        "content_html": sanitize_html(article.content) if full_text and article.content else None,
    })
    return out


def opinion_piece(piece):
    return {
        "id": piece.id,
        "title": piece.title,
        "outlet": outlet(piece.outlet, piece.source),
        "leaning": leaning(piece.bias_score),
        "url": _http_url(piece.url),
        "published_at": iso(piece.date or piece.fetched_at),
        "stance": piece.stance or None,
        "story_id": piece.story_id,
    }


def by_leaning(items, side_of, serialize):
    out = {"left": [], "center": [], "right": [], "unrated": []}
    for item in items:
        out[side_of(item)].append(serialize(item))
    return out


def story_detail(story, opinion_pieces):
    articles = sorted(story.articles, key=lambda a: a.date or dt.min, reverse=True)
    out = story_summary(story)
    out.update({
        "title": story.title,
        "topics": [t.name for t in story.topics],
        "created_at": iso(story.created_at),
        "summary": _paragraphs(story.summary),
        "deep_report": _paragraphs(story.deep_report),
        "opinion_roundup": _paragraphs(story.opinion_roundup),
        "articles": by_leaning(articles, _article_leaning, article_card),
        "copies_hidden": sum(1 for a in articles if is_copy_in_story(a)),
        "opinion": by_leaning(
            sorted(opinion_pieces, key=lambda p: p.date or p.fetched_at or dt.min, reverse=True),
            lambda p: leaning(p.bias_score), opinion_piece),
    })
    return out


def edition_label(edition):
    return f"{edition.edition_type.title()} Edition — {edition.date.strftime('%B %d, %Y')}"


def edition_brief(edition, story_count=None):
    return {
        "id": edition.id,
        "label": edition_label(edition),
        "kind": edition.edition_type,
        "date": edition.date.isoformat(),
        "published_at": iso(edition.created_at),
        "story_count": story_count,
    }


def edition(edition_row, edition_stories):
    out = edition_brief(edition_row, len(edition_stories))
    out["stories"] = [story_summary(es.story, es) for es in edition_stories]
    return out


def topic(t):
    return {"id": t.id, "name": t.name, "icon": t.icon or None, "sort_order": t.sort_order}


def search_story_hit(story):
    return {
        "kind": "story",
        "id": story.id,
        "title": story.display_headline,
        "image_url": _story_image(story),
        "source_count": len({a.outlet.name if a.outlet else a.source for a in story.site_articles}),
        "updated_at": iso(story.last_updated),
    }


def search_article_hit(article):
    return {
        "kind": "article",
        "id": article.id,
        "story_id": article.story_id,
        "title": article.title,
        "image_url": None if article.is_roundup else _http_url(article.image_url),
        "outlet": outlet(article.outlet, article.source),
        "leaning": _article_leaning(article),
        "published_at": iso(article.date),
    }


def device(d):
    return {
        "id": d.id,
        "name": d.name,
        "scopes": d.scope_list,
        "created_at": iso(d.created_at),
        "expires_at": iso(d.expires_at),
    }


def newsbox_item(item):
    """One NewsBox entry: the iOS NewsBoxItem."""
    return {
        "story": story_summary(item.story),
        "origin": item.origin,
        "state": item.state,
        "progress": None,
        "is_unread": bool(item.is_unread),
        "auto_run_id": item.run_id,
        "added_at": iso(item.added_at),
        "ready_at": iso(item.ready_at),
        "error": item.error if item.state == "failed" else None,
    }


def schedule(settings, row):
    """The user's scheduled summaries: the iOS AutoSummarizeSettings."""
    return {
        **settings,
        "next_run_at": iso(row.next_run_at) if row is not None else None,
        "last_run_at": iso(row.last_run_at) if row is not None else None,
    }


def bookmark_version(snapshot):
    return {
        "version": snapshot.version,
        "status": snapshot.status,
        "captures": snapshot.captures,
        "size_bytes": int(snapshot.size_bytes or 0),
        "asset_count": snapshot.asset_count,
        "created_at": iso(snapshot.created_at),
        "finished_at": iso(snapshot.finished_at),
        "error": snapshot.error if snapshot.status in ("failed", "over_quota") or snapshot.captures in ("failed", "over_quota") else None,
    }


def bookmark(b, versions=False):
    """A saved story: the iOS Bookmark. `story` is null once the story is gone."""
    snaps = list(b.snapshots)
    out = {
        "id": b.id,
        "story_id": b.story_id,
        "title": b.title,
        "story": story_summary(b.story) if b.story is not None else None,
        "saved_at": iso(b.created_at),
        "latest": bookmark_version(snaps[-1]) if snaps else None,
        "version_count": len(snaps),
        "size_bytes": sum(int(s.size_bytes or 0) for s in snaps),
    }
    if versions:
        out["versions"] = [bookmark_version(s) for s in snaps]
    return out
