"""Admin › Sources: sections, feeds, NewsAPI/GNews searches, source API keys,
the blocklist and scrape health. The same data and rules as blueprints/
admin/sources_settings.py, config_crud.py, source_keys.py and
scrape_blocklist.py."""
from datetime import datetime, timedelta

from flask import jsonify
from sqlalchemy import func

from aggregator import config_store, db, feed_order, source_priority
from aggregator.blocklist import CAUGHT_DAYS, EFFECT_NO_SCRAPE, EFFECT_SKIP, blocklist_rows
from aggregator.models import FeedSection, IngestionBlock, RssFeed, ScheduledFetch, ScrapeBlocklist, Topic
from aggregator.sections import forget_section_labels
from aggregator.sources import ROLE_HINTS, ROLES, set_feed_sections

from .. import serializers as ser
from ..errors import ApiError
from . import config_fields, reply, route
from . import values as v
from .values import Invalid


def _topics():
    return Topic.query.order_by(Topic.sort_order.asc().nullslast(), Topic.name.asc()).all()


def _section_refs():
    return [{"id": t.id, "name": t.display_name or t.name} for t in _topics()]


def _reorder(values, model, start=0):
    ids = v.get_ids(values, "ids")
    rows = {r.id: r for r in model.query.all()}
    if sorted(ids) != sorted(rows):
        raise Invalid("Someone changed this list. Pull to refresh and try again.")
    for position, row_id in enumerate(ids, start=start):
        rows[row_id].sort_order = position


def _gone(what):
    return ApiError(404, "not_found", f"That {what} is gone. It may have been removed on the web.")


# --- Sections ------------------------------------------------------------------------------

def _sections():
    from aggregator.blueprints.admin.sources_settings import THIN_ARTICLES_PER_DAY, THIN_SOURCES, _section_coverage
    topics = _topics()
    coverage = _section_coverage(topics, datetime.utcnow())
    return {
        "items": [{
            "id": t.id,
            "name": t.name,
            "display_name": t.display_name,
            "description": t.description,
            "is_active": bool(t.is_active),
            "coverage": coverage[t.id],
        } for t in topics],
        "thin_sources": THIN_SOURCES,
        "thin_articles": THIN_ARTICLES_PER_DAY,
    }


@route("/admin/sections")
def sections():
    """Every section in menu order, with the last day's coverage."""
    return jsonify(_sections())


@route("/admin/sections", methods=("POST",))
def add_section():
    """{name, display_name, description}"""
    values = v.body()
    name = v.get_str(values, "name", 60, required=True, label="The name")
    if Topic.query.filter(func.lower(Topic.name) == name.lower()).first():
        raise Invalid("There's already a section by that name.", "name")
    top = db.session.query(func.max(Topic.sort_order)).scalar()
    db.session.add(Topic(name=name, display_name=v.get_str(values, "display_name", 80) or None,
                         description=v.get_str(values, "description", 300) or None,
                         sort_order=(top if top is not None else -1) + 1, is_active=True))
    db.session.flush()
    forget_section_labels()
    return reply(_sections(), f"Added {name}.")


@route("/admin/sections/<int:topic_id>", methods=("PATCH",))
def update_section(topic_id):
    """Any of display_name, description, is_active."""
    topic = db.session.get(Topic, topic_id)
    if topic is None:
        raise _gone("section")
    values = v.body()
    if "is_active" in values:
        topic.is_active = v.get_bool(values, "is_active")
    if "display_name" in values:
        topic.display_name = v.get_str(values, "display_name", 80) or None
    if "description" in values:
        topic.description = v.get_str(values, "description", 300) or None
    db.session.flush()
    forget_section_labels()
    return reply(_sections())


@route("/admin/sections/order", methods=("PUT",))
def order_sections():
    """{ids}: every section id in its new order."""
    _reorder(v.body(), Topic)
    db.session.flush()
    forget_section_labels()
    return reply(_sections())


# --- Sources (feeds) -----------------------------------------------------------------------

def _feed(group):
    return {
        "id": group["id"],
        "url": group["url"],
        "name": group["name"],
        "label": group["label"],
        "fallback_url": group["fallback_url"],
        "roles": group["roles"],
        "sections": [{"id": t.id, "name": t.display_name or t.name} for t in group["sections"]],
        "enabled": group["enabled"],
        "health": group["health"],
        "health_note": group["health_note"],
        "last_read_at": ser.iso(group["last_read_at"]),
        "headlines": group["headlines"],
        "stored": group["stored"],
    }


def _sources():
    from aggregator.blueprints.admin.sources_settings import _feed_groups
    order = feed_order.current()
    feeds = _feed_groups(datetime.utcnow(), order)
    counts = {}
    for f in feeds:
        counts[f["health"]] = counts.get(f["health"], 0) + 1
    return {
        "feeds": [_feed(f) for f in feeds],
        "health_counts": counts,
        "scan_order": order,
        "scan_orders": v.choices(feed_order.CHOICES),
        "priority": source_priority.names(),
        "roles": v.choices((k, label, ROLE_HINTS.get(k)) for k, label in ROLES.items()),
        "sections": _section_refs(),
        "search_count": ScheduledFetch.query.count(),
    }


@route("/admin/sources")
def sources():
    """Every feed once per URL, in the order runs read them, with health and counts."""
    return jsonify(_sources())


def _http_url(values, key, required=False):
    value = v.get_str(values, key, 1000, required=required, label="The URL")
    if value and not value.startswith(("http://", "https://")):
        raise Invalid("Start the URL with https://.", key)
    return value or None


def _roles(values):
    roles = v.get_list(values, "roles", ROLES) if "roles" in values else []
    return roles or ["general"]


def _section_ids(values):
    ids = v.get_ids(values, "sections", "sections")
    found = {t.id for t in Topic.query.filter(Topic.id.in_(ids))} if ids else set()
    if any(i not in found for i in ids):
        raise Invalid("Some of those sections are gone. Pull to refresh and try again.", "sections")
    return ids


@route("/admin/feeds", methods=("POST",))
def add_feed():
    """{url, label, fallback_url, roles, sections}"""
    values = v.body()
    url = _http_url(values, "url", required=True)
    label = v.get_str(values, "label", 200) or None
    fallback = _http_url(values, "fallback_url")
    sections_ = _section_ids(values) if "sections" in values else []
    if RssFeed.query.filter_by(url=url).first():
        raise Invalid("That feed is already on the list.", "url")
    db.session.add_all([RssFeed(url=url, bucket=role, label=label, enabled=True, fallback_url=fallback)
                        for role in _roles(values)])
    set_feed_sections(url, sections_)
    db.session.flush()
    return reply(_sources(), "Feed added. The next run reads it.")


def _feed_rows(feed_id):
    feed = db.session.get(RssFeed, feed_id)
    if feed is None:
        raise _gone("feed")
    return feed.url, {r.bucket: r for r in RssFeed.query.filter_by(url=feed.url)}


@route("/admin/feeds/<int:feed_id>", methods=("PATCH",))
def update_feed(feed_id):
    """Any of label, fallback_url, roles, sections, enabled. Edits every row for the feed's URL."""
    url, rows = _feed_rows(feed_id)
    values = v.body()
    first = next(iter(rows.values()))
    label = (v.get_str(values, "label", 200) or None) if "label" in values else first.label
    fallback = _http_url(values, "fallback_url") if "fallback_url" in values else first.fallback_url
    enabled = v.get_bool(values, "enabled") if "enabled" in values else any(r.enabled for r in rows.values())
    roles = _roles(values) if "roles" in values else list(rows)
    for role, row in rows.items():
        if role not in roles:
            db.session.delete(row)
    for role in roles:
        row = rows.get(role)
        if row is None:
            row = RssFeed(url=url, bucket=role)
            db.session.add(row)
        row.label, row.fallback_url, row.enabled = label, fallback, enabled
    if "sections" in values:
        set_feed_sections(url, _section_ids(values))
    db.session.flush()
    return reply(_sources(), None if set(values) == {"enabled"} else "Feed saved.")


@route("/admin/feeds/<int:feed_id>", methods=("DELETE",))
def delete_feed(feed_id):
    """Delete a feed (every role for its URL). Its articles stay."""
    url, rows = _feed_rows(feed_id)
    RssFeed.query.filter_by(url=url).delete()
    FeedSection.query.filter_by(feed_url=url).delete()
    db.session.flush()
    return reply(_sources(), "Feed deleted.")


@route("/admin/sources/scan-order", methods=("PUT",))
def save_scan_order():
    """{order}: the order each run reads the feeds in."""
    order = v.get_choice(v.body(), "order", [k for k, _, _ in feed_order.CHOICES])
    feed_order.save(order)
    return reply(_sources())


@route("/admin/sources/priority", methods=("PUT",))
def save_priority():
    """{names}: outlets boosted for everyone."""
    names = v.get_list(v.body(), "names")
    if len(names) > 200 or any(len(n) > 120 for n in names):
        raise Invalid("Up to 200 outlets, each at most 120 characters.", "names")
    source_priority.save("\n".join(n.strip() for n in names if n.strip()))
    return reply(_sources(), "Saved. Readers who changed their own list keep their changes.")


# --- Searches (scheduled NewsAPI / GNews fetches) --------------------------------------------

MODES = (("top", "Top headlines", "A country and category, or NewsAPI sources"),
         ("query", "Search", "Search terms"))


def _search(f):
    return {
        "id": f.id,
        "label": f.label,
        "description": f.description,
        "mode": f.mode,
        "newsapi_query": f.newsapi_query,
        "newsapi_country": f.newsapi_country,
        "newsapi_category": f.newsapi_category,
        "newsapi_sources": f.newsapi_sources,
        "gnews_query": f.gnews_query,
        "gnews_category": f.gnews_category,
        "topic_id": f.topic_id,
        "known_outlets_only": bool(f.known_outlets_only),
        "is_active": bool(f.is_active),
    }


def _searches():
    rows = ScheduledFetch.query.order_by(ScheduledFetch.sort_order.asc(), ScheduledFetch.id.asc()).all()
    return {"items": [_search(f) for f in rows], "modes": v.choices(MODES), "sections": _section_refs()}


@route("/admin/searches")
def searches():
    """The NewsAPI and GNews searches each run makes, in order."""
    return jsonify(_searches())


def _search_values(values, current=None):
    """The ScheduledFetch columns from `values`, blanking the unused half of
    the mode pair as the web form does."""
    def text(name, limit):
        if name not in values and current is not None:
            return getattr(current, name)
        return v.get_str(values, name, limit) or None

    mode = values.get("mode", current.mode if current else "query")
    if mode not in {m for m, _, _ in MODES}:
        raise Invalid("Pick a kind.", "mode")
    out = {
        "mode": mode,
        "description": text("description", 300),
        "gnews_query": text("gnews_query", 500),
        "gnews_category": text("gnews_category", 32),
        "newsapi_country": text("newsapi_country", 8) if mode == "top" else None,
        "newsapi_category": text("newsapi_category", 32) if mode == "top" else None,
        "newsapi_sources": text("newsapi_sources", 500) if mode == "top" else None,
        "newsapi_query": text("newsapi_query", 500) if mode == "query" else None,
        "known_outlets_only": v.get_bool(values, "known_outlets_only") if "known_outlets_only" in values
        else bool(current and current.known_outlets_only),
    }
    if "topic_id" in values:
        raw = values.get("topic_id")
        topic_id = int(raw) if isinstance(raw, (int, str)) and not isinstance(raw, bool) and str(raw).isdigit() else None
        out["topic_id"] = topic_id if topic_id and db.session.get(Topic, topic_id) else None
    elif current is not None:
        out["topic_id"] = current.topic_id
    return out


@route("/admin/searches", methods=("POST",))
def add_search():
    """{label, description, mode, newsapi_*, gnews_*, topic_id, known_outlets_only}"""
    values = v.body()
    label = v.get_str(values, "label", 120, required=True, label="The name")
    if ScheduledFetch.query.filter_by(label=label).first():
        raise Invalid("There's already a search with that name.", "label")
    top = db.session.query(func.max(ScheduledFetch.sort_order)).scalar() or 0
    db.session.add(ScheduledFetch(label=label, sort_order=top + 1, is_active=True, **_search_values(values)))
    db.session.flush()
    return reply(_searches(), f"Added {label}.")


def _search_row(fetch_id):
    fetch = db.session.get(ScheduledFetch, fetch_id)
    if fetch is None:
        raise _gone("search")
    return fetch


@route("/admin/searches/<int:fetch_id>", methods=("PATCH",))
def update_search(fetch_id):
    """Any of the search's fields, and is_active."""
    f = _search_row(fetch_id)
    values = v.body()
    if "is_active" in values:
        f.is_active = v.get_bool(values, "is_active")
    if set(values) - {"is_active"}:
        if "label" in values:
            label = v.get_str(values, "label", 120, required=True, label="The name")
            # Labels key each run's numbers, so two searches can't share one.
            if ScheduledFetch.query.filter(ScheduledFetch.label == label, ScheduledFetch.id != f.id).first():
                raise Invalid("Another search has that name.", "label")
            f.label = label
        for key, value in _search_values(values, f).items():
            setattr(f, key, value)
    db.session.flush()
    return reply(_searches(), None if set(values) == {"is_active"} else "Search saved.")


@route("/admin/searches/<int:fetch_id>", methods=("DELETE",))
def delete_search(fetch_id):
    """Delete a search."""
    f = _search_row(fetch_id)
    label = f.label
    db.session.delete(f)
    db.session.flush()
    return reply(_searches(), f"Deleted {label}.")


@route("/admin/searches/order", methods=("PUT",))
def order_searches():
    """{ids}: every search id in its new run order."""
    _reorder(v.body(), ScheduledFetch, start=1)
    db.session.flush()
    return reply(_searches())


# --- Source API keys -----------------------------------------------------------------------

STATUS_TONES = {"chip-safe": "ok", "chip-danger": "bad", "chip-broad": "warn"}


def _source_keys():
    from aggregator.blueprints.admin import source_keys as sk
    from aggregator import secret_box
    from news_fetcher import api_budget
    states = config_store.field_states()
    items = []
    for key, (about, signup, kind_labels, poll_about) in sk.SOURCES.items():
        p = api_budget.PROVIDERS[key]
        spec = config_store.SOURCE_FIELDS[key]
        configured = states[p.key_setting]["source"] != "unset"
        per_poll = sk._requests_per_poll(key)
        usage = sk._usage(key, per_poll) if configured else None
        chip, status = sk._status(key, configured, usage)
        items.append({
            "key": key,
            "name": p.name,
            "about": about,
            "signup_url": signup,
            "poll_about": poll_about,
            "configured": configured,
            "status": {"text": status, "tone": STATUS_TONES.get(chip)},
            # The schedule is edited as bands, not as a text field.
            "fields": config_fields.fields([f for f in spec if f[2] != "schedule"], states, {
                p.slowdown_setting: "Polls space out once this share of the day's budget is used."}),
            "usage": {
                "used": usage["used"],
                "limit": usage["limit"],
                "left": usage["left"],
                "share": usage["share"],
                "errors": usage["errors"],
                "slowed": usage["slowed"],
                "kinds": [{"key": kind, "label": label, "requests": usage["kinds"].get(kind, {}).get("requests", 0)}
                          for kind, label in kind_labels.items()],
                "hours": usage["hours"],
                "last_poll": ser.iso(usage["last_poll"]),
                "next_poll": ser.iso(usage["next_poll"]),
                "paused_until": ser.iso(usage["paused_until"]),
                "planned": usage["planned"],
                "poll_budget": usage["poll_budget"],
            } if usage else None,
            "limit": api_budget.daily_limit(p),
            "max_limit": p.max_limit,
            "requests_per_poll": per_poll,
            "schedule": [{"hour": h, "minutes": m} for h, m in api_budget.schedule(p)],
            "default_schedule": [{"hour": h, "minutes": m} for h, m in p.default_schedule],
            "schedule_saved": states[p.schedule_setting]["source"] == "saved",
        })
    return {"items": items, "key_source": secret_box.key_source(), "timezone": api_budget.TIMEZONE,
            "max_bands": api_budget.MAX_SCHEDULE_BANDS}


@route("/admin/source-keys")
def source_keys():
    """The NYT, NewsAPI and GNews: each key, budget, poll schedule and the last day's use."""
    return jsonify(_source_keys())


@route("/admin/source-keys/<key>", methods=("PATCH",))
def save_source_key(key):
    """{values: {NAME: value}, schedule: [{hour, minutes}] or null for the default}"""
    from news_fetcher import api_budget
    spec = config_store.SOURCE_FIELDS.get(key)
    if spec is None:
        raise ApiError(404, "not_found", "No such source.")
    p = api_budget.PROVIDERS[key]
    body = v.body()
    values = dict(body.get("values") or {})
    if p.schedule_setting in values:
        raise Invalid("Send the poll schedule as bands.", p.schedule_setting)
    if "schedule" in body:
        bands = body["schedule"]
        if bands is None:
            values[p.schedule_setting] = ""
        else:
            if not isinstance(bands, list) or not all(isinstance(b, dict) for b in bands):
                raise Invalid("Send the schedule as a list of bands.", "schedule")
            text = ", ".join(f"{b.get('hour')}:{b.get('minutes')}" for b in bands)
            parsed = api_budget.parse_schedule(text)
            if not parsed:
                raise Invalid(f"Each band needs a start hour (0 to 23) and minutes between polls (1 to 1440), "
                              f"up to {api_budget.MAX_SCHEDULE_BANDS} bands.", "schedule")
            values[p.schedule_setting] = api_budget.format_schedule(parsed)
    config_fields.save(spec, values)
    return reply(_source_keys(), f"{p.name} settings saved. The scheduler uses them from its next check.")


# --- Blocklist -----------------------------------------------------------------------------

def _blocklist():
    rows = blocklist_rows()
    return {
        "items": [{
            "id": r["id"],
            "table": r["table"],
            "effect": r["effect"],
            "match": r["match"],
            "pattern": r["pattern"],
            "reason": r["reason"],
            "origin": r["origin"],
            "active": bool(r["active"]),
            "caught": r["caught"],
            "added_at": ser.iso(r.get("added_at")),
            "hits": r.get("hits"),
            "hit_threshold": r.get("hit_threshold"),
            "expires_at": ser.iso(r.get("expires_at")),
        } for r in rows],
        "caught_days": CAUGHT_DAYS,
    }


@route("/admin/blocklist")
def blocklist():
    """Every rule that keeps an article out or unscraped."""
    return jsonify(_blocklist())


@route("/admin/blocklist", methods=("POST",))
def add_rule():
    """{effect (skip or noscrape), match (url or title, for skip), pattern, reason}"""
    values = v.body()
    effect = v.get_choice(values, "effect", (EFFECT_SKIP, EFFECT_NO_SCRAPE))
    pattern = v.get_str(values, "pattern", 300, required=True, label="The pattern").lower()
    reason = v.get_str(values, "reason", 300, required=True, label="The reason")
    if effect == EFFECT_NO_SCRAPE:
        domain = pattern.split("//")[-1].split("/")[0].removeprefix("www.")
        entry = ScrapeBlocklist.query.filter_by(domain=domain).first()
        if entry is None:
            db.session.add(ScrapeBlocklist(domain=domain, reason=reason, is_permanent=True))
        else:
            # Taking an automatic block over by hand makes it permanent.
            entry.reason, entry.is_permanent, entry.expires_at = reason, True, None
    else:
        match = v.get_choice(values, "match", ("url", "title"))
        kind = "source" if match == "url" else "title_keyword"
        if IngestionBlock.query.filter_by(kind=kind, pattern=pattern).first():
            raise Invalid("That rule is already on the list.", "pattern")
        db.session.add(IngestionBlock(kind=kind, pattern=pattern, note=reason, is_active=True))
    db.session.flush()
    return reply(_blocklist(), f"Blocked {pattern}.")


def _rule(table, rule_id):
    model = {"ingestion": IngestionBlock, "scrape": ScrapeBlocklist}.get(table)
    row = db.session.get(model, rule_id) if model else None
    if row is None:
        raise _gone("rule")
    return row


@route("/admin/blocklist/<table>/<int:rule_id>", methods=("PATCH",))
def update_rule(table, rule_id):
    """Any of pattern, reason, active (pattern and active for collect rules only)."""
    row = _rule(table, rule_id)
    values = v.body()
    if table == "ingestion":
        if "active" in values:
            row.is_active = v.get_bool(values, "active")
        if "pattern" in values:
            pattern = v.get_str(values, "pattern", 300, required=True, label="The pattern").lower()
            clash = IngestionBlock.query.filter(IngestionBlock.kind == row.kind, IngestionBlock.pattern == pattern,
                                                IngestionBlock.id != row.id).first()
            if clash:
                raise Invalid("Another rule has that pattern.", "pattern")
            row.pattern = pattern
        if "reason" in values:
            row.note = v.get_str(values, "reason", 300) or row.note
    else:
        if "active" in values or "pattern" in values:
            raise Invalid("A don't-scrape rule is a domain: delete it and add another to change it.")
        if "reason" in values:
            row.reason = v.get_str(values, "reason", 300) or row.reason
    db.session.flush()
    return reply(_blocklist(), None if set(values) == {"active"} else "Rule saved.")


@route("/admin/blocklist/<table>/<int:rule_id>", methods=("DELETE",))
def delete_rule(table, rule_id):
    """Delete a rule. An automatic don't-scrape block comes back if the site keeps failing."""
    db.session.delete(_rule(table, rule_id))
    db.session.flush()
    return reply(_blocklist(), "Rule deleted.")


# --- Scrape health -------------------------------------------------------------------------

def _scrape_health():
    from aggregator.blueprints.admin._shared import _load_json_setting, article_domain
    from aggregator.blueprints.admin._tasks import _bulk_task_status_payload
    from aggregator.models import Article
    since = datetime.utcnow() - timedelta(hours=24)
    totals = {}
    by_domain = {}
    last_seen = {}
    for url, status, fetched in db.session.query(Article.url, Article.scrape_status, Article.fetched_at) \
            .filter(Article.fetched_at >= since):
        key = (status or "pending").lower()
        totals[key] = totals.get(key, 0) + 1
        domain = article_domain(url)
        if domain:
            counts = by_domain.setdefault(domain, {})
            counts[key] = counts.get(key, 0) + 1
            if fetched and fetched > last_seen.get(domain, datetime.min):
                last_seen[domain] = fetched
    problems = []
    for domain, c in by_domain.items():
        issues = c.get("blocked", 0) + c.get("failed", 0) + c.get("fallback", 0)
        if issues:
            problems.append({"domain": domain, "issues": issues, **{k: c.get(k, 0) for k in (
                "success", "fallback", "blocked", "failed", "skipped")}, "last_seen": ser.iso(last_seen.get(domain))})
    problems.sort(key=lambda r: (r["issues"], r["blocked"], r["failed"], r["fallback"]), reverse=True)

    cache = _load_json_setting("scrape_retry_cache_v1") or {}

    def retry_rows(group, name):
        rows = [{name: k, "status": p.get("status"), "reason": p.get("failure_reason"),
                 "failures": p.get("failure_count", 0), "until": p.get("defer_until")}
                for k, p in (cache.get(group) or {}).items() if isinstance(p, dict)]
        rows.sort(key=lambda r: (r["until"] or "", r["failures"]), reverse=True)
        return rows

    domains, urls = retry_rows("domains", "domain"), retry_rows("urls", "url")
    audit = _bulk_task_status_payload("audit_scrapes")
    return {
        "window_hours": 24,
        "totals": totals,
        "problem_domains": problems[:15],
        "retry_domains": domains[:10],
        "retry_urls": urls[:10],
        "retry_domain_count": len(domains),
        "retry_url_count": len(urls),
        "audit": {k: audit.get(k) for k in ("status", "started_at", "finished_at", "message")},
    }


@route("/admin/scrape-health")
def scrape_health():
    """The last day's scrape results, problem sites and the retry cache."""
    return jsonify(_scrape_health())
