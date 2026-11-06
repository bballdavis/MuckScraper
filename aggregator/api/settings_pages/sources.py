"""Admin › Sources: sections, feeds, NewsAPI/GNews searches, source API keys,
the blocklist and scrape health. Same rules as blueprints/admin/
sources_settings.py, config_crud.py and source_keys.py."""
from datetime import datetime, timedelta

from sqlalchemy import func

from aggregator import config_store, db, feed_order, source_priority
from aggregator.blocklist import EFFECT_NO_SCRAPE, EFFECT_SKIP, blocklist_rows
from aggregator.models import FeedSection, IngestionBlock, RssFeed, ScheduledFetch, ScrapeBlocklist, Topic
from aggregator.sections import forget_section_labels
from aggregator.sources import ROLE_HINTS, ROLES, set_feed_sections

from . import _config, page
from . import schema as s
from .schema import FieldError

HEALTH = {"good": ("OK", "ok"), "warning": ("Shaky", "warn"), "bad": ("Failing", "bad"), "unknown": (None, None)}


def _topics():
    return Topic.query.order_by(Topic.sort_order.asc().nullslast(), Topic.name.asc()).all()


def _section_choices():
    return [(t.id, t.display_name or t.name) for t in _topics()]


def _ids(values, key, model, label):
    """Every id in values[key], each a row of `model`."""
    raw = s.get_list(values, key)
    try:
        ids = [int(v) for v in raw]
    except ValueError:
        raise FieldError(f"{label} must be ids.", key)
    found = {row.id for row in model.query.filter(model.id.in_(ids))} if ids else set()
    if any(i not in found for i in ids):
        raise FieldError(f"Some of those {label.lower()} are gone. Reload and try again.", key)
    return ids


def _reorder(values, model, start=0):
    raw = values.get("ids")
    if not isinstance(raw, list):
        raise FieldError("Send ids in their new order.")
    try:
        ids = [int(i) for i in raw]
    except (TypeError, ValueError):
        raise FieldError("ids must be whole numbers.")
    rows = {r.id: r for r in model.query.all()}
    if sorted(ids) != sorted(rows):
        raise FieldError("Someone changed this list. Reload and try again.")
    for position, row_id in enumerate(ids, start=start):
        rows[row_id].sort_order = position


# --- Sections --------------------------------------------------------------------------

@page("sections", "Sections", "sources", "sections",
      detail=lambda: str(Topic.query.filter_by(is_active=True).count()))
def sections(_=None):
    from aggregator.blueprints.admin.sources_settings import _section_coverage
    topics = _topics()
    coverage = _section_coverage(topics, datetime.utcnow())
    rows = []
    for t in topics:
        c = coverage[t.id]
        rows.append(s.item(t.id, t.display_name or t.name,
                           subtitle=f"{c['sources']} sources · {c['articles']} articles · {c['stories']} stories today",
                           badge=s.badge("Thin", "warn") if c["thin"] else None,
                           page=f"sections/{t.id}", active=t.is_active))
    return s.page("sections", "Sections", [
        s.section("sections", items=rows, reorder=True, empty="No sections yet.",
                  actions=[s.action("add", "Add a section", fields=[
                      s.text("name", "Name", required=True, help="Its key, used by the classifier. Can't change later."),
                      s.text("display_name", "Shown as", help="The menu's label, if different."),
                      s.text("description", "Description"),
                  ])],
                  footer="The menu, the app and the classifier all use this list, in this order. "
                         "A switched-off section is hidden and gets no new articles. Thin: few sources or "
                         "articles in the last day."),
    ])


@sections.action("add")
def add_section(_, values):
    name = s.get_str(values, "name", 60, required=True, label="Name")
    if Topic.query.filter(func.lower(Topic.name) == name.lower()).first():
        raise FieldError("There's already a section by that name.", "name")
    top = db.session.query(func.max(Topic.sort_order)).scalar()
    topic = Topic(name=name, display_name=s.get_str(values, "display_name", 80) or None,
                  description=s.get_str(values, "description", 300) or None,
                  sort_order=(top if top is not None else -1) + 1, is_active=True)
    db.session.add(topic)
    db.session.flush()
    forget_section_labels()
    return {"message": f"Added {name}."}


@sections.action("reorder")
def reorder_sections(_, values):
    _reorder(values, Topic)
    return None


def _topic(arg):
    topic = Topic.query.get(int(arg)) if arg and arg.isdigit() else None
    if topic is None:
        raise LookupError(arg)
    return topic


@page("sections/*", "Section", "sources", "sections", listed=False)
def section_page(arg):
    t = _topic(arg)
    return s.page(f"sections/{t.id}", t.display_name or t.name, [
        s.section("state", fields=[s.toggle("active", "Shown", t.is_active)], save="auto"),
        s.section("details", facts=[s.fact("Name", t.name)], fields=[
            s.text("display_name", "Shown as", t.display_name, placeholder=t.name),
            s.text("description", "Description", t.description),
        ]),
    ])


@section_page.save
def save_section(arg, values, section):
    t = _topic(arg)
    if "active" in values:
        t.is_active = s.get_bool(values, "active")
    if "display_name" in values:
        t.display_name = s.get_str(values, "display_name", 80) or None
    if "description" in values:
        t.description = s.get_str(values, "description", 300) or None
    forget_section_labels()
    return None


# --- Feeds -----------------------------------------------------------------------------

def _feed_fields(group=None):
    group = group or {}
    feed = group.get("feed")
    return [
        s.url("url", "Feed URL", group.get("url"), placeholder="https://example.com/rss", required=True)
        if feed is None else None,
        s.text("label", "Outlet name", group.get("label"), help="Leave blank to use the name its articles carry."),
        s.url("fallback_url", "Fallback URL", group.get("fallback_url"), help="Read when the feed fails."),
        s.multichoice("roles", "Used for", group.get("roles") or ["general"],
                      [(k, v, ROLE_HINTS.get(k)) for k, v in ROLES.items()]),
        s.multichoice("sections", "Sections", [t.id for t in group.get("sections") or []], _section_choices(),
                      help="Articles go straight to these. None: the classifier decides."),
    ]


@page("sources", "Sources", "sources", "sources",
      detail=lambda: str(db.session.query(RssFeed.url).distinct().count() + ScheduledFetch.query.count()))
def sources_page(_=None):
    from aggregator.blueprints.admin.sources_settings import _feed_groups
    order = feed_order.current()
    feeds = _feed_groups(datetime.utcnow(), order)
    searches = ScheduledFetch.query.count()
    rows = []
    for f in feeds:
        label, tone = HEALTH.get(f["health"], (None, None))
        roles = ", ".join(ROLES.get(r, r) for r in f["roles"])
        rows.append(s.item(f["id"], f["name"], subtitle=f"{roles} · {f['headlines']} headlines today",
                           badge=s.badge(label, tone) if f["enabled"] else None,
                           page=f"feeds/{f['id']}", active=f["enabled"]))
    return s.page("sources", "Sources", [
        s.section("order", fields=[
            s.choice("order", "Read feeds", order, [(k, label, hint) for k, label, hint in feed_order.CHOICES]),
        ], save="auto", footer="The order each run reads the feeds in, so the best are in if a run is cut short."),
        s.section("priority", "Boosted for everyone", fields=[
            s.multiline("names", "Outlets", "\n".join(source_priority.names()), help="One a line."),
        ], footer="Their stories show in Trending even alone, and first in Latest and the Swipe deck. "
                  "Each reader can change the list for themselves."),
        s.section("searches", facts=[s.fact("NewsAPI and GNews searches", searches)],
                  items=[s.item("searches", "Searches", detail=str(searches), page="searches")]),
        s.section("feeds", f"Feeds ({len(feeds)})", items=rows, empty="No feeds yet.",
                  actions=[s.action("add", "Add a feed", fields=[f for f in _feed_fields() if f])]),
    ])


@sources_page.save
def save_sources(_, values, section):
    if section == "order" or "order" in values:
        if not feed_order.save(s.get_choice(values, "order", [k for k, _, _ in feed_order.CHOICES])):
            raise FieldError("Pick one of the choices.", "order")
        return None
    if section == "priority":
        source_priority.save(s.get_str(values, "names", 20000))
        return "Saved."
    raise FieldError("Nothing to save here.")


def _roles(values):
    roles = s.get_list(values, "roles", ROLES) if "roles" in values else ["general"]
    return roles or ["general"]


def _http_url(values, key, required=False):
    value = s.get_str(values, key, 1000, required=required, label="The URL")
    if value and not value.startswith(("http://", "https://")):
        raise FieldError("Start the URL with https://.", key)
    return value or None


@sources_page.action("add")
def add_feed(_, values):
    url = _http_url(values, "url", required=True)
    label = s.get_str(values, "label", 200) or None
    fallback = _http_url(values, "fallback_url")
    sections_ = _ids(values, "sections", Topic, "Sections") if "sections" in values else []
    if RssFeed.query.filter_by(url=url).first():
        raise FieldError("That feed is already on the list.", "url")
    rows = [RssFeed(url=url, bucket=role, label=label, enabled=True, fallback_url=fallback) for role in _roles(values)]
    db.session.add_all(rows)
    set_feed_sections(url, sections_)
    db.session.flush()
    return {"message": "Feed added. The next run reads it.", "open": f"feeds/{rows[0].id}"}


def _feed_group(arg):
    from aggregator.blueprints.admin.sources_settings import _feed_groups
    feed = RssFeed.query.get(int(arg)) if arg and arg.isdigit() else None
    if feed is None:
        raise LookupError(arg)
    for group in _feed_groups(datetime.utcnow()):
        if group["url"] == feed.url:
            return group
    raise LookupError(arg)


@page("feeds/*", "Feed", "sources", "sources", listed=False)
def feed_page(arg):
    from aggregator.feed_health import health_note
    group = _feed_group(arg)
    label, tone = HEALTH.get(group["health"], (None, None))
    return s.page(f"feeds/{arg}", group["name"], [
        s.section("state", fields=[s.toggle("active", "On", group["enabled"])], save="auto"),
        s.section("health", "Last read", facts=[
            s.fact("Health", label or "Not read yet", tone),
            s.fact("Note", group["health_note"] or health_note(group["feed"])),
            s.fact("Read", s.when(group["last_read_at"])),
            s.fact("Headlines today", group["headlines"]),
            s.fact("Stored today", group["stored"]),
            s.fact("URL", group["url"]),
        ]),
        s.section("edit", "Feed", fields=[f for f in _feed_fields(group) if f]),
        s.section("remove", actions=[s.action("delete", "Delete this feed", role="destructive",
                                              confirm=f"Delete {group['name']}? Its articles stay.")]),
    ])


@feed_page.save
def save_feed(arg, values, section):
    group = _feed_group(arg)
    url = group["url"]
    rows = {r.bucket: r for r in RssFeed.query.filter_by(url=url)}
    if section == "state" or set(values) == {"active"}:
        active = s.get_bool(values, "active")
        for row in rows.values():
            row.enabled = active
        return None
    label = s.get_str(values, "label", 200) or None if "label" in values else group["label"]
    fallback = _http_url(values, "fallback_url") if "fallback_url" in values else group["fallback_url"]
    roles = _roles(values) if "roles" in values else group["roles"]
    enabled = group["enabled"]
    # A row whose role is dropped takes a new role where it can, this page's
    # own row first, so the page's address keeps working.
    keep = {role: row for role, row in rows.items() if role in roles}
    spare = sorted((r for r in rows.values() if r.bucket not in roles), key=lambda r: str(r.id) != arg)
    own = spare[0] if spare and str(spare[0].id) == arg else None
    if own is not None and all(role in keep for role in roles):
        # Every role kept already has its row: this page's row takes one over.
        other = keep[roles[0]]
        db.session.delete(other)
        db.session.flush()
        own.bucket = roles[0]
        keep[roles[0]] = own
        spare.remove(own)
    for role in roles:
        if role not in keep:
            if spare:
                row = spare.pop(0)
                row.bucket = role
            else:
                row = RssFeed(url=url, bucket=role)
                db.session.add(row)
            keep[role] = row
    for row in spare:
        db.session.delete(row)
    for row in keep.values():
        row.label, row.fallback_url, row.enabled = label, fallback, enabled
    if "sections" in values:
        set_feed_sections(url, _ids(values, "sections", Topic, "Sections"))
    return "Feed saved."


@feed_page.action("delete")
def delete_feed(arg, values):
    group = _feed_group(arg)
    RssFeed.query.filter_by(url=group["url"]).delete()
    FeedSection.query.filter_by(feed_url=group["url"]).delete()
    return {"message": f"Deleted {group['name']}.", "close": True}


# --- Searches (scheduled NewsAPI / GNews fetches) ---------------------------------------

MODES = (("top", "Top headlines", "A country and category, or NewsAPI sources"),
         ("query", "Search", "Search terms"))


def _search_fields(f=None):
    def v(name):
        return getattr(f, name, None) if f else None
    return [
        s.text("label", "Name", v("label"), required=True, help="Keys this search's run numbers; keep it unique."),
        s.text("description", "Description", v("description")),
        s.choice("mode", "NewsAPI kind", v("mode") or "query", MODES),
        s.text("newsapi_query", "NewsAPI search terms", v("newsapi_query"), help="Search kind only."),
        s.text("newsapi_country", "NewsAPI country", v("newsapi_country"), placeholder="us",
               help="Top headlines only. Two letters."),
        s.text("newsapi_category", "NewsAPI category", v("newsapi_category"), placeholder="business"),
        s.text("newsapi_sources", "NewsAPI sources", v("newsapi_sources"), placeholder="bbc-news,reuters",
               help="Replaces the country and category."),
        s.text("gnews_query", "GNews search terms", v("gnews_query")),
        s.text("gnews_category", "GNews category", v("gnews_category"), placeholder="nation"),
        s.choice("topic_id", "Section", v("topic_id") or "", [("", "General (the classifier decides)")]
                 + _section_choices()),
        s.toggle("known_outlets_only", "Known outlets only", v("known_outlets_only") or False,
                 help="Skip articles from outlets we have no rating for."),
    ]


def _search_values(values, current=None):
    """The ScheduledFetch columns from `values`, blanking the unused half of
    the mode pair as the web form does."""
    def text_(name, limit):
        if name not in values and current is not None:
            return getattr(current, name)
        return s.get_str(values, name, limit) or None

    mode = values.get("mode", current.mode if current else "query")
    if mode not in {m for m, _, _ in MODES}:
        raise FieldError("Pick a kind.", "mode")
    out = {
        "mode": mode,
        "description": text_("description", 300),
        "gnews_query": text_("gnews_query", 500),
        "gnews_category": text_("gnews_category", 32),
        "newsapi_country": text_("newsapi_country", 8) if mode == "top" else None,
        "newsapi_category": text_("newsapi_category", 32) if mode == "top" else None,
        "newsapi_sources": text_("newsapi_sources", 500) if mode == "top" else None,
        "newsapi_query": text_("newsapi_query", 500) if mode == "query" else None,
        "known_outlets_only": s.get_bool(values, "known_outlets_only") if "known_outlets_only" in values
        else bool(current and current.known_outlets_only),
    }
    if "topic_id" in values:
        raw = values.get("topic_id")
        topic_id = int(raw) if isinstance(raw, (int, str)) and str(raw).isdigit() else None
        out["topic_id"] = topic_id if topic_id and db.session.get(Topic, topic_id) else None
    elif current is not None:
        out["topic_id"] = current.topic_id
    return out


@page("searches", "Searches", "sources", "search", listed=False)
def searches_page(_=None):
    rows = ScheduledFetch.query.order_by(ScheduledFetch.sort_order.asc(), ScheduledFetch.id.asc()).all()
    return s.page("searches", "Searches", [
        s.section("searches", items=[
            s.item(f.id, f.label, subtitle="Top headlines" if f.mode == "top" else (f.newsapi_query or f.gnews_query),
                   page=f"searches/{f.id}", active=f.is_active) for f in rows
        ], reorder=True, empty="No searches yet.", actions=[s.action("add", "Add a search", fields=_search_fields())],
            footer="What each pipeline run asks NewsAPI and GNews for, in this order. Each one is a request "
                   "against the daily budgets on Source API keys."),
    ])


@searches_page.action("add")
def add_search(_, values):
    label = s.get_str(values, "label", 120, required=True, label="Name")
    if ScheduledFetch.query.filter_by(label=label).first():
        raise FieldError("There's already a search with that name.", "label")
    top = db.session.query(func.max(ScheduledFetch.sort_order)).scalar() or 0
    fetch = ScheduledFetch(label=label, sort_order=top + 1, is_active=True, **_search_values(values))
    db.session.add(fetch)
    db.session.flush()
    return {"message": f"Added {label}.", "open": f"searches/{fetch.id}"}


@searches_page.action("reorder")
def reorder_searches(_, values):
    _reorder(values, ScheduledFetch, start=1)
    return None


def _search(arg):
    fetch = ScheduledFetch.query.get(int(arg)) if arg and arg.isdigit() else None
    if fetch is None:
        raise LookupError(arg)
    return fetch


@page("searches/*", "Search", "sources", "search", listed=False)
def search_page(arg):
    f = _search(arg)
    return s.page(f"searches/{f.id}", f.label, [
        s.section("state", fields=[s.toggle("active", "On", f.is_active)], save="auto"),
        s.section("edit", fields=_search_fields(f)),
        s.section("remove", actions=[s.action("delete", "Delete this search", role="destructive",
                                              confirm=f"Delete {f.label}?")]),
    ])


@search_page.save
def save_search(arg, values, section):
    f = _search(arg)
    if set(values) == {"active"}:
        f.is_active = s.get_bool(values, "active")
        return None
    if "label" in values:
        label = s.get_str(values, "label", 120, required=True, label="Name")
        if ScheduledFetch.query.filter(ScheduledFetch.label == label, ScheduledFetch.id != f.id).first():
            raise FieldError("Another search has that name.", "label")
        f.label = label
    for key, value in _search_values(values, f).items():
        setattr(f, key, value)
    return "Search saved."


@search_page.action("delete")
def delete_search(arg, values):
    f = _search(arg)
    db.session.delete(f)
    return {"message": f"Deleted {f.label}.", "close": True}


# --- Source API keys -------------------------------------------------------------------

def _source_status(key):
    from aggregator.blueprints.admin import source_keys as sk
    states = config_store.field_states()
    spec = config_store.SOURCE_FIELDS[key]
    configured = states[spec[0][0]]["source"] != "unset"
    usage = sk._usage(key, sk._requests_per_poll(key)) if configured else None
    chip, status = sk._status(key, configured, usage)
    tone = {"chip-safe": "ok", "chip-danger": "bad", "chip-broad": "warn"}.get(chip)
    return status, tone, usage, states, spec


@page("source-keys", "Source API keys", "sources", "key")
def source_keys_page(_=None):
    from aggregator.blueprints.admin.source_keys import SOURCES
    from news_fetcher import api_budget
    rows = []
    for key in SOURCES:
        status, tone, usage, _, _ = _source_status(key)
        used = f"{usage['used']} of {usage['limit']} today" if usage else None
        rows.append(s.item(key, api_budget.PROVIDERS[key].name, subtitle=used, badge=s.badge(status, tone),
                           page=f"source-keys/{key}"))
    return s.page("source-keys", "Source API keys", [
        s.section("sources", items=rows, footer="News APIs ingestion can use, each with its key, daily request "
                                                "budget and poll schedule. Keys are encrypted and never sent back."),
    ])


@page("source-keys/*", "Source API", "sources", "key", listed=False)
def source_key_page(arg):
    from aggregator.blueprints.admin.source_keys import SOURCES
    from news_fetcher import api_budget
    if arg not in SOURCES:
        raise LookupError(arg)
    about, signup, kind_labels, poll_about = SOURCES[arg]
    p = api_budget.PROVIDERS[arg]
    status, tone, usage, states, spec = _source_status(arg)
    facts = [s.fact("Status", status, tone)]
    if usage:
        facts += [
            s.fact("Used today", f"{usage['used']} of {usage['limit']} requests"),
            *[s.fact(label, usage["kinds"].get(kind, {}).get("requests", 0)) for kind, label in kind_labels.items()],
            s.fact("Last poll", usage.get("last_poll_clock")),
            s.fact("Next poll", usage.get("next_poll_clock")),
        ]
        if usage.get("errors"):
            facts.append(s.fact("Errors", usage["errors"], "warn"))
    help_ = {p.schedule_setting: "Bands of start hour (Eastern) and minutes between polls, like 0:30, 6:10, 22:30.",
             p.slowdown_setting: "Polls space out once this share of the day's budget is used."}
    return s.page(f"source-keys/{arg}", p.name, [
        s.section("status", facts=facts, footer=about),
        s.section("settings", "Settings", fields=_config.fields(spec, states, help_),
                  footer=f"{poll_about} Get a key at {signup}."),
    ])


@source_key_page.save
def save_source_key(arg, values, section):
    from news_fetcher import api_budget
    spec = config_store.SOURCE_FIELDS.get(arg)
    if spec is None:
        raise LookupError(arg)
    schedule = api_budget.PROVIDERS[arg].schedule_setting
    if isinstance(values.get(schedule), str) and values[schedule].strip():
        parsed = api_budget.parse_schedule(values[schedule])
        if parsed:
            values = {**values, schedule: api_budget.format_schedule(parsed)}
    _config.save(spec, values)
    return "Saved. The scheduler uses it from its next check."


# --- Blocklist -------------------------------------------------------------------------

EFFECTS = ((EFFECT_SKIP, "Don't collect", "Skip matching articles"),
           (EFFECT_NO_SCRAPE, "Don't scrape", "Collect them, but never fetch the page"))
MATCHES = (("url", "Address contains"), ("title", "Title contains"))
ORIGINS = {"seeded": "Built in", "hand": "Added by hand", "auto": "Automatic"}


@page("blocklist", "Blocklist", "sources", "blocklist",
      detail=lambda: str(IngestionBlock.query.count() + ScrapeBlocklist.query.filter_by(is_permanent=True).count()))
def blocklist(_=None):
    rows = blocklist_rows()
    items = []
    for r in rows:
        what = "Don't scrape" if r["effect"] == EFFECT_NO_SCRAPE else (
            "Title" if r["match"] == "title" else "Address")
        items.append(s.item(f"{r['table']}-{r['id']}", r["pattern"],
                            subtitle=f"{what} · {r['reason']}" if r["reason"] else what,
                            detail=f"{r['caught']} caught" if r["caught"] else None,
                            badge=None if r["active"] else s.badge("Off"),
                            page=f"rules/{r['table']}-{r['id']}",
                            active=r["active"] if r["table"] == "ingestion" else None))
    return s.page("blocklist", "Blocklist", [
        s.section("rules", items=items, empty="No rules.", actions=[s.action("add", "Add a rule", fields=[
            s.choice("effect", "Effect", EFFECT_SKIP, EFFECTS),
            s.choice("match", "Match", "url", MATCHES, help="Don't scrape always matches the site's domain."),
            s.text("pattern", "Pattern", required=True, placeholder="example.com"),
            s.text("reason", "Why", required=True),
        ])], footer="Caught counts the last 7 days."),
    ])


@blocklist.action("add")
def add_rule(_, values):
    effect = s.get_choice(values, "effect", [e for e, _, _ in EFFECTS])
    pattern = s.get_str(values, "pattern", 300, required=True, label="Pattern").lower()
    reason = s.get_str(values, "reason", 300, required=True, label="Why")
    if effect == EFFECT_NO_SCRAPE:
        domain = pattern.split("//")[-1].split("/")[0].removeprefix("www.")
        entry = ScrapeBlocklist.query.filter_by(domain=domain).first()
        if entry is None:
            db.session.add(ScrapeBlocklist(domain=domain, reason=reason, is_permanent=True))
        else:
            entry.reason, entry.is_permanent, entry.expires_at = reason, True, None
    else:
        match = s.get_choice(values, "match", [m for m, _ in MATCHES])
        kind = "source" if match == "url" else "title_keyword"
        if IngestionBlock.query.filter_by(kind=kind, pattern=pattern).first():
            raise FieldError("That rule is already on the list.", "pattern")
        db.session.add(IngestionBlock(kind=kind, pattern=pattern, note=reason, is_active=True))
    return {"message": f"Blocked {pattern}."}


def _rule(arg):
    table, _, rule_id = (arg or "").partition("-")
    model = {"ingestion": IngestionBlock, "scrape": ScrapeBlocklist}.get(table)
    row = model.query.get(int(rule_id)) if model and rule_id.isdigit() else None
    if row is None:
        raise LookupError(arg)
    return table, row


@page("rules/*", "Rule", "sources", "blocklist", listed=False)
def rule_page(arg):
    table, row = _rule(arg)
    info = next((r for r in blocklist_rows() if r["table"] == table and r["id"] == row.id), None) or {}
    facts = [s.fact("Effect", "Don't scrape" if table == "scrape" else "Don't collect"),
             s.fact("Where it came from", ORIGINS.get(info.get("origin"), "—")),
             s.fact("Caught, last 7 days", info.get("caught", 0))]
    if table == "scrape" and not row.is_permanent:
        facts += [s.fact("Failed scrapes", f"{row.hit_count} of {info.get('hit_threshold')} to block"),
                  s.fact("Until", s.when(row.expires_at) if row.expires_at else "—")]
    sections = [s.section("about", facts=facts)]
    if table == "ingestion":
        sections.insert(0, s.section("state", fields=[s.toggle("active", "On", row.is_active)], save="auto"))
        sections.append(s.section("edit", fields=[
            s.text("pattern", "Pattern", row.pattern),
            s.text("reason", "Why", row.note),
        ]))
    else:
        sections.append(s.section("edit", facts=[s.fact("Domain", row.domain)],
                                  fields=[s.text("reason", "Why", row.reason)]))
    sections.append(s.section("remove", actions=[s.action("delete", "Delete this rule", role="destructive",
                                                          confirm="Delete this rule?")]))
    title = row.pattern if table == "ingestion" else row.domain
    return s.page(f"rules/{arg}", title, sections)


@rule_page.save
def save_rule(arg, values, section):
    table, row = _rule(arg)
    if table == "ingestion":
        if "active" in values:
            row.is_active = s.get_bool(values, "active")
        if "pattern" in values:
            pattern = s.get_str(values, "pattern", 300, required=True, label="Pattern").lower()
            clash = IngestionBlock.query.filter(IngestionBlock.kind == row.kind, IngestionBlock.pattern == pattern,
                                                IngestionBlock.id != row.id).first()
            if clash:
                raise FieldError("Another rule has that pattern.", "pattern")
            row.pattern = pattern
        if "reason" in values:
            row.note = s.get_str(values, "reason", 300) or row.note
    elif "reason" in values:
        row.reason = s.get_str(values, "reason", 300) or row.reason
    return None if set(values) == {"active"} else "Rule saved."


@rule_page.action("delete")
def delete_rule(arg, values):
    _, row = _rule(arg)
    db.session.delete(row)
    return {"message": "Rule deleted.", "close": True}


# --- Scrape health ---------------------------------------------------------------------

@page("scrape-health", "Scrape health", "sources", "health")
def scrape_health(_=None):
    from aggregator.blueprints.admin._shared import article_domain
    from aggregator.models import Article
    since = datetime.utcnow() - timedelta(hours=24)
    totals = dict(db.session.query(func.coalesce(Article.scrape_status, "pending"), func.count(Article.id))
                  .filter(Article.fetched_at >= since).group_by(func.coalesce(Article.scrape_status, "pending")))
    by_domain = {}
    for url, status in db.session.query(Article.url, Article.scrape_status).filter(Article.fetched_at >= since):
        domain = article_domain(url)
        if domain:
            counts = by_domain.setdefault(domain, {})
            key = (status or "pending").lower()
            counts[key] = counts.get(key, 0) + 1
    problems = sorted(
        ((d, c) for d, c in by_domain.items() if c.get("blocked", 0) + c.get("failed", 0) + c.get("fallback", 0)),
        key=lambda dc: (dc[1].get("blocked", 0) + dc[1].get("failed", 0) + dc[1].get("fallback", 0),
                        dc[1].get("blocked", 0)), reverse=True)[:15]
    total = sum(totals.values())
    ok = totals.get("success", 0)
    labels = (("success", "Scraped"), ("fallback", "Fallback text"), ("blocked", "Blocked"), ("failed", "Failed"),
              ("skipped", "Skipped"), ("pending", "Waiting"))
    tones = {"blocked": "bad", "failed": "bad", "fallback": "warn"}
    return s.page("scrape-health", "Scrape health", [
        s.section("totals", "Last 24 hours", facts=[s.fact("Articles", total)] + [
            s.fact(label, totals.get(key, 0), tones.get(key) if totals.get(key) else None) for key, label in labels
        ], footer=f"{round(100 * ok / total)}% scraped in full." if total else None),
        s.section("domains", "Sites with problems", items=[
            s.item(d, d, subtitle=", ".join(f"{c[k]} {k}" for k in ("blocked", "failed", "fallback") if c.get(k)),
                   detail=f"{c.get('success', 0)} ok") for d, c in problems
        ], empty="No problems in the last day.", actions=[s.action(
            "audit", "Audit stored scrapes", confirm="Re-audit every stored scrape? Articles that fail are "
                                                     "marked for repair.")]),
    ])


@scrape_health.action("audit")
def audit_scrapes(_, values):
    from aggregator.blueprints.admin._tasks import _start_bulk_task
    started, _status = _start_bulk_task("audit_scrapes")
    return {"message": "Audit queued." if started else "An audit is already running."}
