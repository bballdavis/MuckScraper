"""The Settings section list (templates/settings_base.html).

Grouped as in the settings mock (Philip, 2026-10-22): General, Sources,
Blocklists, AI and Pipeline, each link with a count where one helps. A page names its own
entry by key; everything else is built here so the order and labels live in
one place.
"""
from flask import url_for

from aggregator.models import User, Topic, RssFeed, PromptTemplate, ScheduledFetch, IngestionBlock

from . import admin

# (group, [(key, label, endpoint, count, is_new)]). endpoint is a name or a
# (name, url args) pair; count is a model (its row count), a function, or None.
SETTINGS_SECTIONS = [
    ("General", [
        ("overview", "Overview", "admin.tools_page", None, False),
        ("users", "Users", "admin.users_page", User, False),
        ("mobileapi", "Mobile API", "admin.mobile_api_page", None, True),
    ]),
    ("Sources", [
        ("topics", "Topics", "admin.topics_page", Topic, False),
        ("rss", "RSS feeds", "admin.rss_feeds_page", RssFeed, False),
        ("fetches", "Scheduled fetches", "admin.scheduled_fetches_page", ScheduledFetch, False),
        ("sourcekeys", "Source API keys", "admin.source_keys_page", None, True),
    ]),
    ("Blocklists", [
        ("blocksources", "Sources", ("admin.ingestion_blocks_page", {"slug": "sources"}),
         lambda: IngestionBlock.query.filter_by(kind="source").count(), False),
        ("blockkeywords", "Title keywords", ("admin.ingestion_blocks_page", {"slug": "keywords"}),
         lambda: IngestionBlock.query.filter_by(kind="title_keyword").count(), False),
        ("scrapeblock", "Scrape", "admin.scrape_blocklist", None, False),
    ]),
    ("AI", [
        ("llm", "LLM providers", "admin.llm_providers_page", None, True),
        ("prompts", "Prompts", "admin.prompts_page", PromptTemplate, False),
    ]),
    ("Pipeline", [
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
