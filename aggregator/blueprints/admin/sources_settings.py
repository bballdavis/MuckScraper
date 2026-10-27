"""Settings › Sections, Sources and Blocklist (Philip, 2026-10-27).

Sections are the one list the sidebar, the app, the classifier and the
sources hang off (aggregator/sources.py). The Sources page lists every feed
once per URL, with its roles, the sections it serves and how its last read
went; the NewsAPI/GNews searches keep their own editor (Scheduled fetches)
and are summarized here. The Blocklist merges the ingestion blocks and the
scrape blocklist into one list (aggregator/blocklist.py).
"""
import logging
from datetime import datetime, timedelta

from flask import abort, jsonify, redirect, render_template, request, url_for
from sqlalchemy import func

from aggregator import db, feed_order
from aggregator.blocklist import CAUGHT_DAYS, EFFECT_NO_SCRAPE, EFFECT_SKIP, blocklist_rows
from aggregator.feed_health import health_note, health_of
from aggregator.models import (
    Article, FeedEntry, FeedSection, IngestionBlock, Outlet, RssFeed, ScheduledFetch,
    ScrapeBlocklist, Story, Topic, article_topics,
)
from aggregator.permissions import ROLE_ADMIN, role_required
from aggregator.sections import forget_section_labels, in_section
from aggregator.sources import ROLE_HINTS, ROLES, section_source_counts, sections_by_feed, set_feed_sections

from . import admin

logger = logging.getLogger(__name__)

# A section with fewer sources or articles a day than this is flagged thin.
THIN_SOURCES = 2
THIN_ARTICLES_PER_DAY = 10


def _ordered_topics():
    return Topic.query.order_by(Topic.sort_order.asc().nullslast(), Topic.name.asc()).all()


# --- Sections -------------------------------------------------------------------------

def _section_coverage(topics, now):
    """{topic id: {sources, articles, stories, lean, thin}}: feeds and searches
    serving the section, its articles fetched and stories started in the last
    24 hours, and those articles' outlets by lean."""
    since = now - timedelta(hours=24)
    sources = section_source_counts()
    articles = dict(
        db.session.query(article_topics.c.topic_id, func.count(Article.id))
        .join(Article, Article.id == article_topics.c.article_id)
        .filter(Article.fetched_at >= since)
        .group_by(article_topics.c.topic_id)
    )
    lean_rows = (
        db.session.query(article_topics.c.topic_id, Outlet.bias_score, func.count(Article.id))
        .join(Article, Article.id == article_topics.c.article_id)
        .join(Outlet, Outlet.id == Article.outlet_id)
        .filter(Article.fetched_at >= since)
        .group_by(article_topics.c.topic_id, Outlet.bias_score)
    )
    leans = {}
    for topic_id, score, count in lean_rows:
        side = "unrated" if score is None else "left" if score < 2.5 else "right" if score > 3.5 else "center"
        bucket = leans.setdefault(topic_id, {"left": 0, "center": 0, "right": 0, "unrated": 0})
        bucket[side] += count
    out = {}
    for topic in topics:
        stories = Story.query.filter(in_section(topic), Story.created_at >= since).count()
        n_sources = sources.get(topic.id, 0)
        n_articles = articles.get(topic.id, 0)
        out[topic.id] = {
            "sources": n_sources,
            "articles": n_articles,
            "stories": stories,
            "lean": leans.get(topic.id, {"left": 0, "center": 0, "right": 0, "unrated": 0}),
            "thin": topic.is_active and topic.name != "Other"
                    and (n_sources < THIN_SOURCES or n_articles < THIN_ARTICLES_PER_DAY),
        }
    return out


@admin.route("/sections")
@admin.route("/topics")
@role_required(ROLE_ADMIN)
def topics_page():
    topics = _ordered_topics()
    return render_template("sections.html", topics=topics,
                           coverage=_section_coverage(topics, datetime.utcnow()),
                           thin_sources=THIN_SOURCES, thin_articles=THIN_ARTICLES_PER_DAY)


@admin.route("/sections/add", methods=["POST"])
@role_required(ROLE_ADMIN)
def add_topic():
    name = request.form.get("name", "").strip()[:60]
    if name and not Topic.query.filter(func.lower(Topic.name) == name.lower()).first():
        max_order = db.session.query(func.max(Topic.sort_order)).scalar()
        db.session.add(Topic(
            name=name,
            display_name=request.form.get("display_name", "").strip()[:80] or None,
            description=request.form.get("description", "").strip()[:300] or None,
            sort_order=(max_order if max_order is not None else -1) + 1,
            is_active=True,
        ))
        db.session.commit()
        forget_section_labels()
        logger.info(f"[Sections] Added section: {name}")
    return redirect(url_for("admin.topics_page"))


@admin.route("/sections/<int:topic_id>/update", methods=["POST"])
@role_required(ROLE_ADMIN)
def update_topic(topic_id):
    topic = Topic.query.get_or_404(topic_id)
    topic.display_name = request.form.get("display_name", "").strip()[:80] or None
    topic.description = request.form.get("description", "").strip()[:300] or None
    db.session.commit()
    forget_section_labels()
    return redirect(url_for("admin.topics_page", _anchor=f"topic-{topic.id}"))


@admin.route("/sections/<int:topic_id>/active", methods=["POST"])
@role_required(ROLE_ADMIN)
def set_topic_active(topic_id):
    """The row's switch: show or hide one section without touching the rest."""
    topic = Topic.query.get_or_404(topic_id)
    topic.is_active = bool((request.get_json(silent=True) or {}).get("active"))
    db.session.commit()
    return jsonify({"id": topic.id, "active": topic.is_active})


@admin.route("/sections/reorder", methods=["POST"])
@role_required(ROLE_ADMIN)
def reorder_topics():
    """Drag and drop: the page posts every section id in its new order."""
    payload = request.get_json(silent=True) or {}
    try:
        ids = [int(i) for i in payload.get("ids", [])]
    except (TypeError, ValueError):
        return jsonify({"error": "ids must be integers"}), 400
    topics = {t.id: t for t in Topic.query.all()}
    if sorted(ids) != sorted(topics):
        return jsonify({"error": "ids must list every section once"}), 400
    for position, topic_id in enumerate(ids):
        topics[topic_id].sort_order = position
    db.session.commit()
    return jsonify({"ids": ids})


# --- Sources ---------------------------------------------------------------------------

def _feed_groups(now, order=None):
    """One entry per feed URL: its rows (one per role), sections, health and
    24-hour counts from the Breaking poller's headlines."""
    since = now - timedelta(hours=24)
    seen = dict(
        db.session.query(FeedEntry.feed_url, func.count(FeedEntry.id))
        .filter(FeedEntry.first_seen_at >= since).group_by(FeedEntry.feed_url)
    )
    stored = dict(
        db.session.query(FeedEntry.feed_url, func.count(FeedEntry.article_id))
        .filter(FeedEntry.first_seen_at >= since).group_by(FeedEntry.feed_url)
    )
    sections = sections_by_feed()
    groups = {}
    for feed in RssFeed.query.order_by(RssFeed.id.asc()).all():
        group = groups.get(feed.url)
        if group is None:
            group = groups[feed.url] = {
                "url": feed.url, "rows": [], "label": feed.label, "fallback_url": feed.fallback_url,
                "sections": sections.get(feed.url, []),
                "headlines": seen.get(feed.url, 0), "stored": stored.get(feed.url, 0),
            }
        group["rows"].append(feed)
        group["label"] = group["label"] or feed.label
        group["fallback_url"] = group["fallback_url"] or feed.fallback_url
    for group in groups.values():
        rows = group["rows"]
        group["roles"] = [r.bucket for r in rows]
        group["enabled"] = any(r.enabled for r in rows)
        # The freshest read stands for the URL.
        latest = max(rows, key=lambda r: r.last_read_at or datetime.min)
        group["health"] = health_of(latest) if group["enabled"] else "unknown"
        group["health_note"] = health_note(latest) if group["enabled"] else "Switched off"
        group["last_read_at"] = latest.last_read_at
        group["name"] = group["label"] or feed_order.feed_name(rows[0])
        group["feed"] = rows[0]
        group["id"] = rows[0].id
    # Listed in the order each run fetches them (aggregator/feed_order.py).
    by_first_row = {g["feed"].id: g for g in groups.values()}
    return [by_first_row[f.id] for f in feed_order.sort_feeds([g["feed"] for g in groups.values()], order)]


@admin.route("/sources")
@admin.route("/rss-feeds")
@role_required(ROLE_ADMIN)
def rss_feeds_page():
    now = datetime.utcnow()
    scan_order = feed_order.current()
    feeds = _feed_groups(now, scan_order)
    searches = ScheduledFetch.query.order_by(ScheduledFetch.sort_order.asc(), ScheduledFetch.id.asc()).all()
    health_counts = {}
    for feed in feeds:
        health_counts[feed["health"]] = health_counts.get(feed["health"], 0) + 1
    return render_template(
        "sources.html", feeds=feeds, searches=searches, topics=_ordered_topics(),
        roles=ROLES, role_hints=ROLE_HINTS, health_counts=health_counts,
        scan_order=scan_order, scan_orders=feed_order.CHOICES,
    )


@admin.route("/sources/order", methods=["POST"])
@admin.route("/rss-feeds/order", methods=["POST"])
@role_required(ROLE_ADMIN)
def save_rss_scan_order():
    feed_order.save(request.form.get("order", ""))
    return redirect(url_for("admin.rss_feeds_page"))


def _form_roles():
    return [r for r in request.form.getlist("roles") if r in ROLES] or ["general"]


def _form_sections():
    return [int(t) for t in request.form.getlist("sections") if t.isdigit()]


@admin.route("/sources/feeds/add", methods=["POST"])
@role_required(ROLE_ADMIN)
def add_rss_feed():
    url = request.form.get("url", "").strip()
    label = request.form.get("label", "").strip() or None
    fallback_url = request.form.get("fallback_url", "").strip() or None
    if url.startswith(("http://", "https://")):
        for role in _form_roles():
            if not RssFeed.query.filter_by(url=url, bucket=role).first():
                db.session.add(RssFeed(url=url, bucket=role, label=label, enabled=True, fallback_url=fallback_url))
        set_feed_sections(url, _form_sections())
        db.session.commit()
        logger.info(f"[Sources] Added feed: {url}")
    return redirect(url_for("admin.rss_feeds_page"))


@admin.route("/sources/feeds/<int:feed_id>/update", methods=["POST"])
@role_required(ROLE_ADMIN)
def update_rss_feed(feed_id):
    """Edits every row for the feed's URL: label, fallback, on/off, roles
    (a role taken away deletes that row) and sections."""
    url = RssFeed.query.get_or_404(feed_id).url
    rows = {r.bucket: r for r in RssFeed.query.filter_by(url=url)}
    label = request.form.get("label", "").strip() or None
    fallback_url = request.form.get("fallback_url", "").strip() or None
    # The row's switch saves on its own (set_rss_feed_active); the editor
    # leaves the state alone unless a form sends it.
    if "enabled" in request.form:
        enabled = request.form.get("enabled") == "on"
    else:
        enabled = any(r.enabled for r in rows.values()) if rows else True
    roles = _form_roles()
    for role, row in rows.items():
        if role not in roles:
            db.session.delete(row)
    for role in roles:
        row = rows.get(role)
        if row is None:
            row = RssFeed(url=url, bucket=role)
            db.session.add(row)
        row.label, row.fallback_url, row.enabled = label, fallback_url, enabled
    set_feed_sections(url, _form_sections())
    db.session.commit()
    first = RssFeed.query.filter_by(url=url).order_by(RssFeed.id).first()
    return redirect(url_for("admin.rss_feeds_page", _anchor=f"feed-{first.id}" if first else None))


@admin.route("/sources/feeds/<int:feed_id>/active", methods=["POST"])
@role_required(ROLE_ADMIN)
def set_rss_feed_active(feed_id):
    url = RssFeed.query.get_or_404(feed_id).url
    active = bool((request.get_json(silent=True) or {}).get("active"))
    for row in RssFeed.query.filter_by(url=url):
        row.enabled = active
    db.session.commit()
    return jsonify({"id": feed_id, "active": active})


@admin.route("/sources/feeds/<int:feed_id>/delete", methods=["POST"])
@role_required(ROLE_ADMIN)
def delete_rss_feed(feed_id):
    url = RssFeed.query.get_or_404(feed_id).url
    RssFeed.query.filter_by(url=url).delete()
    FeedSection.query.filter_by(feed_url=url).delete()
    db.session.commit()
    return redirect(url_for("admin.rss_feeds_page"))


# --- Blocklist -------------------------------------------------------------------------

@admin.route("/blocklist")
@role_required(ROLE_ADMIN)
def blocklist_page():
    rows = blocklist_rows()
    return render_template("blocklist.html", rules=rows, caught_days=CAUGHT_DAYS,
                           active_count=sum(1 for r in rows if r["active"]))


@admin.route("/ingestion-blocks", defaults={"slug": None})
@admin.route("/ingestion-blocks/<slug>")
@role_required(ROLE_ADMIN)
def ingestion_blocks_page(slug):
    return redirect(url_for("admin.blocklist_page"))


@admin.route("/blocklist/add", methods=["POST"])
@role_required(ROLE_ADMIN)
def add_block_rule():
    effect = request.form.get("effect", "")
    match = request.form.get("match", "")
    pattern = request.form.get("pattern", "").strip().lower()
    reason = request.form.get("reason", "").strip()
    if not pattern or not reason:
        return redirect(url_for("admin.blocklist_page"))
    if effect == EFFECT_NO_SCRAPE:
        domain = pattern.split("//")[-1].split("/")[0].removeprefix("www.")
        entry = ScrapeBlocklist.query.filter_by(domain=domain).first()
        if entry is None:
            db.session.add(ScrapeBlocklist(domain=domain, reason=reason, is_permanent=True))
        else:
            # Taking an automatic block over by hand makes it permanent.
            entry.reason, entry.is_permanent, entry.expires_at = reason, True, None
    elif effect == EFFECT_SKIP and match in ("url", "title"):
        kind = "source" if match == "url" else "title_keyword"
        if not IngestionBlock.query.filter_by(kind=kind, pattern=pattern).first():
            db.session.add(IngestionBlock(kind=kind, pattern=pattern, note=reason, is_active=True))
    else:
        abort(400)
    db.session.commit()
    logger.info(f"[Blocklist] Added {effect} rule: {pattern}")
    return redirect(url_for("admin.blocklist_page"))


@admin.route("/blocklist/ingestion/<int:block_id>/update", methods=["POST"])
@role_required(ROLE_ADMIN)
def update_ingestion_block(block_id):
    block = IngestionBlock.query.get_or_404(block_id)
    pattern = request.form.get("pattern", "").strip().lower()
    clash = IngestionBlock.query.filter(
        IngestionBlock.kind == block.kind, IngestionBlock.pattern == pattern, IngestionBlock.id != block.id,
    ).first()
    if pattern and not clash:
        block.pattern = pattern
    block.note = request.form.get("reason", "").strip() or block.note
    db.session.commit()
    return redirect(url_for("admin.blocklist_page", _anchor=f"rule-ingestion-{block.id}"))


@admin.route("/blocklist/ingestion/<int:block_id>/active", methods=["POST"])
@role_required(ROLE_ADMIN)
def set_ingestion_block_active(block_id):
    block = IngestionBlock.query.get_or_404(block_id)
    block.is_active = bool((request.get_json(silent=True) or {}).get("active"))
    db.session.commit()
    return jsonify({"id": block.id, "active": block.is_active})


@admin.route("/blocklist/scrape/<int:entry_id>/update", methods=["POST"])
@role_required(ROLE_ADMIN)
def update_scrape_block(entry_id):
    entry = ScrapeBlocklist.query.get_or_404(entry_id)
    entry.reason = request.form.get("reason", "").strip() or entry.reason
    db.session.commit()
    return redirect(url_for("admin.blocklist_page", _anchor=f"rule-scrape-{entry.id}"))


@admin.route("/blocklist/<table>/<int:rule_id>/delete", methods=["POST"])
@role_required(ROLE_ADMIN)
def delete_block_rule(table, rule_id):
    model = {"ingestion": IngestionBlock, "scrape": ScrapeBlocklist}.get(table)
    if model is None:
        abort(404)
    db.session.delete(model.query.get_or_404(rule_id))
    db.session.commit()
    return redirect(url_for("admin.blocklist_page"))
