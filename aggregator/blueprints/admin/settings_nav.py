"""The Settings section list (templates/settings_base.html).

Grouped as in the settings mock: General, Sources, AI
and Pipeline; the Sources group was cut to sections, sources and one
blocklist on 2026-10-27, each link with a count where one helps. A page names its own
entry by key; everything else is built here so the order and labels live in
one place.
"""
from flask import url_for

from aggregator import db
from aggregator.models import User, Topic, RssFeed, PromptTemplate, ScheduledFetch, IngestionBlock, ScrapeBlocklist

from . import admin


def _source_count():
    """Feeds (once per URL) plus NewsAPI/GNews searches."""
    return db.session.query(RssFeed.url).distinct().count() + ScheduledFetch.query.count()


def _rule_count():
    return IngestionBlock.query.count() + ScrapeBlocklist.query.filter_by(is_permanent=True).count()


# (group, [(key, label, endpoint, count, is_new)]). endpoint is a name or a
# (name, url args) pair; count is a model (its row count), a function, or None.
SETTINGS_SECTIONS = [
    ("General", [
        ("overview", "Overview", "admin.tools_page", None, False),
        ("users", "Users", "admin.users_page", User, False),
        ("api", "API", "admin.api_page", None, True),
        ("signin", "Sign-in", "admin.sign_in_page", None, True),
    ]),
    ("Sources", [
        ("topics", "Sections", "admin.topics_page", lambda: Topic.query.filter_by(is_active=True).count(), False),
        ("rss", "Sources", "admin.rss_feeds_page", _source_count, False),
        ("sourcekeys", "Source API keys", "admin.source_keys_page", None, True),
        ("blocklist", "Blocklist", "admin.blocklist_page", _rule_count, False),
        ("scrapehealth", "Scrape health", "admin.scrape_blocklist", None, False),
    ]),
    ("AI", [
        ("llm", "LLM providers", "admin.llm_providers_page", None, True),
        ("prompts", "Prompts", "admin.prompts_page", PromptTemplate, False),
    ]),
    ("Pipeline", [
        ("tasks", "Tasks", "admin.tasks_page", None, True),
        ("schedule", "Run schedule", "admin.pipeline_schedule_page", None, False),
    ]),
]


@admin.app_template_global()
def settings_sections(active=None):
    """The section list for the Settings nav, with `active` marked current."""
    groups = []
    for name, items in SETTINGS_SECTIONS:
        links = []
        for key, label, endpoint, count, is_new in items:
            endpoint, args = endpoint if isinstance(endpoint, tuple) else (endpoint, {})
            if count is None:
                number = None
            elif hasattr(count, "query"):
                number = count.query.count()
            else:
                number = count()
            links.append({
                "key": key,
                "label": label,
                "href": url_for(endpoint, **args),
                "count": number,
                "is_new": is_new,
                "current": key == active,
            })
        groups.append({"name": name, "items": links})
    return groups
