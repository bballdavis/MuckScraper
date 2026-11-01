import logging
from collections import OrderedDict
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, jsonify
from flask_login import current_user
from aggregator import appearance_prefs, breaking, feed_prefs, headline_views, reading_prefs, reads, story_analysis
from aggregator.models import FeedEntry
from aggregator.permissions import ROLE_SCRAPER, public_or_login, public_read_enabled, role_required
from aggregator.models import Article, Story, Topic, RawArticlePayload, Edition, EditionStory
from aggregator.opinion import opinion_for_story, published_edition_opinion_cards, split_by_leaning
from aggregator.story_view import apply_aggregator_filter, annotate_edition_story_flags
from aggregator.blueprints.admin._shared import story_bias_totals
from news_fetcher.llm_client import check_llm_status as check_ollama_status, TIER_QUALITY

logger = logging.getLogger(__name__)

public = Blueprint("public", __name__)


# Headlines, story and article pages, and the two status endpoints the sidebar
# polls, are open to signed-out visitors only when PUBLIC_READ_ACCESS is on
# (aggregator/permissions.py); otherwise they need an account. Signed-out
# visitors never get an article's scraped full text (other publishers' work,
# under sequential IDs), and no action buttons are shown without the role
# that can use them.


@public.route("/")
def index():
    # Land on Headlines.
    if current_user.is_authenticated:
        # The start page the reader chose on their profile (feed_prefs.py).
        start = feed_prefs.load(current_user)["start_page"]
        if start == "all":
            return redirect(url_for("admin.list_articles"))
        if start == "grouped":
            return redirect(url_for("admin.multi_article_stories"))
        return redirect(url_for("public.headlines_feed"))
    if public_read_enabled():
        return redirect(url_for("public.headlines_feed"))
    return redirect(url_for("auth.login", next=url_for("public.headlines_feed")))


@public.route("/headlines")
@public_or_login
def headlines_feed():
    """
    Headlines: Trending, Latest or Controversial (?view=, headline_views.py),
    read live. Before that it showed the newest edition
    in its stored rank, which without a ranker was selection order.

    ?edition=<id> shows a published edition, its stories
    read live so articles and op-eds added since it was published show up.
    """
    if request.args.get("edition", type=int):
        return _edition_headlines()

    view = request.args.get("view")
    if view not in headline_views.VIEWS:
        view = headline_views.DEFAULT_VIEW
    per_page = 30 if view == "latest" else 10
    page = max(1, request.args.get("page", 1, type=int) or 1)
    read_story_ids = reads.read_ids(current_user.id, "story") if current_user.is_authenticated else set()

    now = datetime.utcnow()
    built = headline_views.build(now=now, user_id=current_user.id if current_user.is_authenticated else None)
    entries = [e for e in built[view] if e.story_id not in read_story_ids]
    hidden_read = len(built[view]) - len(entries)
    total_stories = len(entries)
    total_pages = max(1, (total_stories + per_page - 1) // per_page)
    page = min(page, total_pages)
    stories = headline_views.load_stories(entries[(page - 1) * per_page:page * per_page])
    for story in stories:
        apply_aggregator_filter(story)
        story_bias_totals(story)
        story.why_label = headline_views.short_why(view, story.rank_entry)

    groups = []
    if view == "latest":
        for story in stories:
            story.age_label = _age_label(min(story.rank_entry.signals.newest, now), now)
            label = headline_views.hour_group(story.rank_entry.signals.newest, now)
            if not groups or groups[-1][0] != label:
                groups.append((label, []))
            groups[-1][1].append(story)

    newest_edition = Edition.query.filter_by(published=True).order_by(Edition.created_at.desc()).first()
    return render_template(
        "headlines_feed.html",
        **_breaking_context(),
        view=view,
        views=[(key, *headline_views.LABELS[key],
                sum(1 for e in built[key] if e.story_id not in read_story_ids))
               for key in headline_views.VIEWS],
        stories=stories,
        latest_groups=groups,
        hidden_read=hidden_read,
        edition_label=None,
        edition=newest_edition,
        is_past_edition=False,
        opinion_count=len(published_edition_opinion_cards(newest_edition)) if newest_edition else 0,
        page=page,
        total_pages=total_pages,
        topics=Topic.query.filter_by(is_active=True).order_by(Topic.sort_order).all(),
        active_nav="headlines",
    )


def _edition_headlines():
    """One published edition's stories by rank, 10 per page, minus the ones
    the reader marked read (?edition=<id>)."""
    per_page = 10
    page = max(1, request.args.get("page", 1, type=int) or 1)
    read_story_ids = reads.read_ids(current_user.id, "story") if current_user.is_authenticated else set()
    edition = Edition.query.filter_by(id=request.args.get("edition", type=int), published=True).first_or_404()

    all_edition_stories = edition.edition_stories.order_by(EditionStory.rank).all()
    hidden_read = sum(1 for es in all_edition_stories if es.story_id in read_story_ids)
    all_edition_stories = [es for es in all_edition_stories if es.story_id not in read_story_ids]
    total_stories = len(all_edition_stories)
    total_pages = max(1, (total_stories + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    stories = annotate_edition_story_flags(all_edition_stories[start:start + per_page])
    for story in stories:
        apply_aggregator_filter(story)

    return render_template(
        "headlines_feed.html",
        view=None,
        views=[],
        stories=stories,
        latest_groups=[],
        hidden_read=hidden_read,
        edition_label=f"{edition.edition_type.title()} Edition — {edition.date.strftime('%B %d, %Y')}",
        edition=edition,
        # Any edition opened by id reads as one: the switcher is for live views.
        is_past_edition=True,
        opinion_count=len(published_edition_opinion_cards(edition)),
        page=page,
        total_pages=total_pages,
        topics=Topic.query.filter_by(is_active=True).order_by(Topic.sort_order).all(),
        active_nav="headlines",
    )


def _age_label(when, now):
    minutes = max(0, int((now - when).total_seconds() // 60))
    if minutes < 60:
        return f"{max(1, minutes)}m"
    return f"{minutes // 60}h"


def _breaking_context():
    """Template values for the Breaking News bar (_breaking_bar.html)."""
    now = datetime.utcnow()
    rows = []
    for entry in breaking.bar_entries(now=now):
        state = breaking.entry_state(entry, now)
        rows.append({"entry": entry, "state": state, "story": breaking.story_of(entry),
                     "age": _age_label(entry.published_at, now)})
    return {
        "breaking_rows": rows,
        "breaking_working": any(r["state"] in breaking.WORKING for r in rows),
        "actionable": breaking.ACTIONABLE,
        "working": breaking.WORKING,
    }


@public.route("/headlines/breaking")
@public_or_login
def breaking_bar():
    """The Breaking News bar alone, for Headlines to refresh in place. Empty
    when nothing is breaking."""
    context = _breaking_context()
    if not context["breaking_rows"]:
        return ""
    return render_template("_breaking_bar.html", **context)


@public.route("/headlines/breaking/<int:entry_id>/summarize", methods=["POST"])
@role_required(ROLE_SCRAPER)
def summarize_breaking(entry_id):
    """Scrape a breaking headline (if needed) and summarize its story, in the
    background (aggregator/breaking.py)."""
    entry = FeedEntry.query.get_or_404(entry_id)
    if breaking.entry_state(entry) in breaking.ACTIONABLE:
        breaking.request(entry)
    return jsonify({"state": breaking.entry_state(entry)}), 202


@public.route("/headlines/opinion")
@public_or_login
def edition_opinion():
    """The edition's Opinion page, like muckscraper.news's: the stories that
    got new columns in this edition, each with those columns and what they
    argue. ?edition=<id>, else the newest edition. Read live."""
    newest_edition = Edition.query.filter_by(published=True).order_by(Edition.created_at.desc()).first()
    edition = newest_edition
    edition_id = request.args.get("edition", type=int)
    if edition_id:
        edition = Edition.query.filter_by(id=edition_id, published=True).first_or_404()
    cards = []
    if edition:
        cards = [(story, split_by_leaning(pieces)) for story, pieces in published_edition_opinion_cards(edition)]
    return render_template(
        "edition_opinion.html",
        cards=cards,
        edition=edition,
        edition_label=(f"{edition.edition_type.title()} Edition — {edition.date.strftime('%B %d, %Y')}"
                       if edition else ""),
        is_past_edition=bool(edition and newest_edition and edition.id != newest_edition.id),
        topics=Topic.query.filter_by(is_active=True).order_by(Topic.sort_order).all(),
        active_nav="headlines",
    )


def editions_by_month(editions):
    """[(month label, [(day label, [editions, morning first])])], newest first."""
    months = OrderedDict()
    for edition in editions:
        days = months.setdefault(edition.date.strftime("%B %Y"), OrderedDict())
        days.setdefault(edition.date.strftime("%A, %B %d"), []).append(edition)
    order = {"morning": 0, "evening": 1}
    return [
        (month, [(day, sorted(eds, key=lambda e: order.get(e.edition_type, 2))) for day, eds in days.items()])
        for month, days in months.items()
    ]


@public.route("/editions")
@public_or_login
def editions_archive():
    """Every published edition, by month, each linking to its Headlines page."""
    from sqlalchemy import func
    editions = Edition.query.filter_by(published=True).order_by(
        Edition.date.desc(), Edition.created_at.desc()
    ).all()
    counts = dict(
        Edition.query.session.query(EditionStory.edition_id, func.count(EditionStory.id))
        .group_by(EditionStory.edition_id).all()
    )
    return render_template(
        "editions.html",
        months=editions_by_month(editions),
        story_counts=counts,
        latest_id=editions[0].id if editions else None,
        topics=Topic.query.filter_by(is_active=True).order_by(Topic.sort_order).all(),
        active_nav="headlines",
    )


@public.route("/story/<int:story_id>")
@public_or_login
def view_story(story_id):
    from sqlalchemy.orm import joinedload
    story = Story.query.options(
        joinedload(Story.articles).joinedload(Article.outlet)
    ).get_or_404(story_id)

    ollama_online = check_ollama_status(TIER_QUALITY)

    apply_aggregator_filter(story)
    story_bias_totals(story)

    return render_template(
        "story.html",
        story=story,
        analysis=story_analysis.layout(story.deep_report),
        analysis_layout=appearance_prefs.load(current_user)["analysis_layout"],
        opinion=opinion_for_story(story.id),
        ollama_online=ollama_online,
        read_item=reads.get(current_user.id, "story", story.id) if current_user.is_authenticated else None,
        auto_read_seconds=reading_prefs.load(current_user)["auto_read_seconds"],
        topics=Topic.query.filter_by(is_active=True).order_by(Topic.sort_order).all(),
    )


@public.route("/article/<int:article_id>")
@public_or_login
def view_article(article_id):
    article = Article.query.get_or_404(article_id)
    ollama_online = check_ollama_status(TIER_QUALITY)

    return render_template(
        "article.html",
        article=article,
        ollama_online=ollama_online,
        read_item=reads.get(current_user.id, "article", article.id) if current_user.is_authenticated else None,
        auto_read_seconds=reading_prefs.load(current_user)["auto_read_seconds"],
        topics=Topic.query.filter_by(is_active=True).order_by(Topic.sort_order).all(),
    )


@public.route("/ollama-status")
@public_or_login
def ollama_status():
    # Unauthenticated route -- never the host/role, which would otherwise leak
    # internal network details (e.g. a LAN IP) to anyone. llm_status_detail_public()
    # exists to enforce that stripping in one place rather than here.
    # The full detail is available to admins at admin.ollama_status_detail.
    from news_fetcher.llm_client import llm_status_detail_public
    return jsonify(llm_status_detail_public())

