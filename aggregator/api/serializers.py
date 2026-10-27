"""What the API sends, field by field. Every response is built here from an
explicit list, so internal columns (scrape status and failure reasons,
grouping confidence, embeddings, raw payloads) never leave the server.

Dates are ISO 8601 UTC to the second ("2026-10-22T14:05:00Z"), the form
Swift's .iso8601 decoder reads.
"""
from datetime import datetime as dt
from urllib.parse import urlparse

from aggregator import newsbox
from aggregator.article_copies import copy_label, is_copy_in_story
from aggregator.filters import summary_paragraph
from aggregator.html_safety import sanitize_html
from aggregator.opinion import leaning
from aggregator.outlet_origins import is_international
from aggregator.outlet_reporting import reporting_for
from aggregator.story_analysis import parse as parse_analysis
from aggregator.story_image import image_focus

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


def analysis_sections(text):
    """The deep report split into its labelled sections, as the story page
    lays them out: kind is lead, left, center, right, next or other, and
    empty marks an L/C/R bucket that had no sources."""
    return [{"label": s["label"], "kind": s["kind"], "paragraphs": s["paragraphs"], "empty": s["empty"]}
            for s in parse_analysis(text)]


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


def _article_side(article):
    """The article's lean, or "international" for an unrated outlet based
    abroad (aggregator/outlet_origins.py), which stays off the L / C / R scale."""
    side = _article_leaning(article)
    if side == "unrated" and article.outlet is not None and article.outlet.is_international:
        return "international"
    return side


def coverage(story):
    """L / C / R counts, plus the articles from unrated outlets based abroad
    (the web's grey globe segment after Right)."""
    counts = {"left": 0, "center": 0, "right": 0, "international": 0}
    for article in story.site_articles:
        side = _article_side(article)
        if side in counts:
            counts[side] += 1
    return counts


def _section(story):
    """The story's topic that most of its articles carry, ties to the
    sidebar's order. A story collects every topic any of its articles was
    classified under, so the first one in sidebar order alone let a single
    Washington-angle piece put a Brazil election in US Politics."""
    topics = sorted(story.topics, key=lambda t: (t.sort_order is None, t.sort_order or 0, t.name))
    if not topics:
        return "News"
    ids = {t.id for t in topics}
    votes = {}
    for article in story.site_articles:
        for t in getattr(article, "topics", None) or []:
            if t.id in ids:
                votes[t.id] = votes.get(t.id, 0) + 1
    return max(topics, key=lambda t: votes.get(t.id, 0)).name


def _story_image(story, edition_story=None):
    if edition_story is not None and _http_url(edition_story.source_image_url):
        return edition_story.source_image_url
    return story.lead_image_url


def _story_focus(story, image_url):
    return image_focus(story.site_articles, image_url)


def _article_focus(article):
    return None if article.is_roundup else image_focus([article], article.image_url)


def _story_logo(story):
    """The newest outlet logo among the story's articles: what a card shows
    when `image_url` is null. Clients draw it fitted on a plain tile, not
    cropped like a photo (news_fetcher/outlet_logos.py)."""
    source = _story_source(story)
    return source.logo_url if source is not None and _http_url(source.logo_url) else None


def _story_source(story):
    """The outlet a card's fallback names: the one whose logo it shows, else
    the newest outlet. Clients set its name under the logo, or alone as a
    masthead when there's no logo (the web's card_image chain)."""
    outlets = [a.outlet for a in sorted(story.site_articles, key=lambda a: a.date or dt.min, reverse=True)
               if a.outlet is not None]
    return next((o for o in outlets if _http_url(o.logo_url)), outlets[0] if outlets else None)


def _story_source_name(story):
    source = _story_source(story)
    return source.name if source is not None else None


def story_summary(story, edition_story=None):
    """A story card: the iOS StorySummary."""
    articles = story.site_articles
    image_url = _story_image(story, edition_story)
    return {
        "id": story.id,
        "section": _section(story),
        "headline": story.display_headline,
        "dek": _dek(story.summary),
        "image_url": image_url,
        "image_focus": _story_focus(story, image_url),
        "logo_url": _story_logo(story),
        "source_name": _story_source_name(story),
        "coverage": coverage(story),
        "source_count": len({a.outlet.name if a.outlet else a.source for a in articles}),
        "updated_at": iso(story.last_updated),
        "is_summarized": bool((story.summary or "").strip()),
        "has_analysis": bool((story.deep_report or "").strip()),
        "can_analyze": newsbox.can_analyze(story),
        "has_ai_headline": bool(story.has_ai_headline),
        "has_updates": bool(getattr(edition_story, "has_updates", False)),
        "breaking": breaking_chip(story),
    }


def breaking_chip(story):
    """The Breaking chip's tooltip details (aggregator/breaking.py
    story_flags), or null when the story isn't breaking."""
    from aggregator import breaking
    details = breaking.story_breaking(story)
    if details is None:
        return None
    return {**details, "first_at": iso(details["first_at"])}


def outlet(o, fallback_name=None):
    if o is None:
        return {"id": None, "name": fallback_name or "Unknown", "leaning": "unrated", "logo_url": None,
                "country": None, "funding": None, "reporting": None}
    country = getattr(o, "country", None)
    # country: ISO code for outlets based outside the US (null = US), which
    # stay "unrated" unless AllSides rates them; funding: "public" | "state".
    return {"id": o.id, "name": o.name, "leaning": leaning(o.bias_score), "logo_url": _http_url(o.logo_url),
            "country": country if is_international(country) else None,
            "funding": getattr(o, "funding", None),
            # Hand-kept reliability grade (aggregator/outlet_reporting.py).
            "reporting": reporting_for(o.name)}


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
        "image_focus": _article_focus(article),
        "summary": summary_paragraph(article.summary) or None,
        "copy_of": copy_label(article),
    }


def article_detail(article, full_text=False):
    out = article_card(article)
    out.update({
        "topics": [t.name for t in article.topics],
        "analysis": _paragraphs(article.deep_analysis),
        # Scraped text only when Settings › API allows it.
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
    out = {"left": [], "center": [], "right": [], "international": [], "unrated": []}
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
        "analysis_sections": analysis_sections(story.deep_report),
        "opinion_roundup": _paragraphs(story.opinion_roundup),
        "articles": by_leaning(articles, _article_side, article_card),
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
    image_url = _story_image(story)
    return {
        "kind": "story",
        "id": story.id,
        "title": story.display_headline,
        "image_url": image_url,
        "image_focus": _story_focus(story, image_url),
        "logo_url": _story_logo(story),
        "source_name": _story_source_name(story),
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
        "image_focus": _article_focus(article),
        "outlet": outlet(article.outlet, article.source),
        "leaning": _article_leaning(article),
        "published_at": iso(article.date),
    }


def device(d):
    return {
        "id": d.id,
        "name": d.name,
        "kind": d.kind or "device",
        "scopes": d.scope_list,
        "created_at": iso(d.created_at),
        "expires_at": iso(d.expires_at),
    }


# How far along an unfinished NewsBox item is, by the step it waits on
# (newsbox.STEPS); the app draws one bar segment per step.
STEP_PROGRESS = {"scrape": 0.1, "group": 0.35, "summary": 0.55, "analysis": 0.8}


def feed_headline(entry):
    """A Breaking headline that isn't in a story yet."""
    return {
        "title": entry.title,
        "outlet": entry.source_name,
        "url": _http_url(entry.link),
        "published_at": iso(entry.published_at),
    }


def breaking_item(entry, state):
    """One headline in the Breaking News bar: the iOS BreakingItem."""
    story = entry.article.story if entry.article is not None else None
    return {
        "id": entry.id,
        **feed_headline(entry),
        "image_url": _http_url(entry.image_url),
        "outlet_count": entry.outlet_count,
        "state": state,
        "story_id": story.id if story is not None else None,
        "error": entry.scrape_note if state == "failed" else None,
    }


def newsbox_item(item):
    """One NewsBox entry: the iOS NewsBoxItem. `story` is null while a
    Breaking headline is still being scraped; `headline` stands in then."""
    unfinished = item.state in ("pending", "in_progress")
    return {
        "id": item.id,
        "story": story_summary(item.story) if item.story is not None else None,
        "headline": feed_headline(item.feed_entry) if item.story is None and item.feed_entry is not None else None,
        "origin": item.origin,
        "state": item.state,
        "step": item.step if unfinished else None,
        "progress": STEP_PROGRESS.get(item.step) if unfinished else None,
        "is_unread": bool(item.is_unread),
        "run_id": item.run_id,
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


def read_item(row):
    """One entry in the user's read list (aggregator/reads.py)."""
    kind = "story" if row.story_id else "article"
    return {
        "kind": kind,
        "id": row.story_id or row.article_id,
        "read_at": iso(row.read_at),
        "story": story_summary(row.story) if row.story is not None else None,
        "article": article_card(row.article) if row.article is not None else None,
    }


def story_skip(row):
    return {"story_id": row.story_id, "skipped_at": iso(row.skipped_at)}
