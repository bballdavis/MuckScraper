"""Settings › Source API keys: the news APIs ingestion can use (the New York
Times, NewsAPI, GNews), each with its key, daily request budget and poll
schedule (news_fetcher/api_budget.py). Keys are saved like the LLM provider
keys (aggregator/config_store.py): encrypted, write-only, and overriding the
environment variable of the same name. Ingestion reads every setting through
news_fetcher/saved_settings.py, so a change applies without a restart."""
from flask import flash, redirect, render_template, request, url_for

from aggregator import config_store, secret_box
from aggregator.permissions import ROLE_ADMIN, role_required
from news_fetcher import api_budget

from . import admin
from .llm_settings import save_fields_form

# key: (what it adds, where to get a key, {ledger kind: label}, what one poll is)
SOURCES = {
    "nyt": (
        "NYT articles are paywalled, so the scraper only gets their RSS description. With a free key, "
        "MuckScraper reads everything NYT publishes from the Times Wire through the day, several hundred "
        "items a request, and ingests the news sections with their abstract and opening paragraph.",
        "https://developer.nytimes.com/get-started",
        {"wire": "Times Wire", "search": "Lead paragraphs", "lookup": "Single articles"},
        "One Times Wire request a poll. Lead paragraph searches (ten articles a request) come out of "
        "the same budget when a pipeline run ingests the wire, so the polls plan to use 70% of it. "
        "Enable the Times Wire and Article Search APIs for the key; it allows 500 requests a day.",
    ),
    "newsapi": (
        "Top headlines and keyword search for each scheduled fetch, up to 100 articles a request.",
        "https://newsapi.org/register",
        {"poll": "Scheduled polls", "manual": "Manual fetches"},
        "One request per active scheduled fetch a poll; pipeline runs store what the polls brought. "
        "The free Developer plan allows 100 requests a day.",
    ),
    "gnews": (
        "Top headlines and search for each scheduled fetch, 10 articles a request on the free plan.",
        "https://gnews.io/register",
        {"poll": "Scheduled polls", "manual": "Manual fetches"},
        "One request per active scheduled fetch a poll; pipeline runs store what the polls brought. "
        "The free plan allows 100 requests a day.",
    ),
}

HOUR_LABELS = ["12a"] + [f"{h}a" for h in range(1, 12)] + ["12p"] + [f"{h}p" for h in range(1, 12)]


def _requests_per_poll(key):
    if key == "nyt":
        return 1
    try:
        from news_fetcher.fetch_and_store_articles import scheduled_fetches
        return len(scheduled_fetches())
    except Exception:
        return 0


def _clock(dt):
    """A UTC time as Eastern clock time, like the schedule: "4:32pm"."""
    if dt is None:
        return None
    local = api_budget._local(dt)
    return local.strftime("%I:%M%p").lstrip("0").lower()


def _usage(key, per_poll):
    try:
        if key == "nyt":
            from news_fetcher import nyt_api
            usage = nyt_api.usage_summary()
        else:
            usage = api_budget.usage_summary(key, requests_per_poll=per_poll)
    except Exception:
        return None
    usage["last_poll_clock"] = _clock(usage["last_poll"])
    usage["next_poll_clock"] = _clock(usage["next_poll"])
    return usage


def _status(key, configured, usage):
    """(chip class, text) for the card's head."""
    if not configured:
        return "", "No key"
    if api_budget.key_rejected(key):
        return "chip-danger", "Key refused"
    if usage and usage["paused_until"]:
        return "chip-broad", f"Rate limited until {_clock(usage['paused_until'])}"
    if usage and usage["left"] <= 0:
        return "chip-broad", "Budget spent"
    if usage and usage["slowed"]:
        return "chip-broad", "Slowed down"
    return "chip-safe", "Active"


@admin.route("/settings/source-keys")
@role_required(ROLE_ADMIN)
def source_keys_page():
    states = config_store.field_states()
    sources = []
    for key, (about, signup, kind_labels, poll_about) in SOURCES.items():
        p = api_budget.PROVIDERS[key]
        fields = {
            kind: {"name": n, "label": label, "kind": kind, **states[n]}
            for n, label, kind in config_store.SOURCE_FIELDS[key]
        }
        configured = fields["secret"]["source"] != "unset"
        per_poll = _requests_per_poll(key)
        usage = _usage(key, per_poll) if configured else None
        chip, status = _status(key, configured, usage)
        sources.append({
            "key": key, "name": p.name, "about": about, "signup": signup, "poll_about": poll_about,
            "kind_labels": kind_labels, "fields": fields, "configured": configured,
            "status": status, "status_chip": chip, "usage": usage,
            "limit": api_budget.daily_limit(p), "max_limit": p.max_limit, "poll_share": p.poll_share,
            "schedule": api_budget.schedule(p), "default_schedule": list(p.default_schedule),
            "requests_per_poll": per_poll, "slowdown": round(api_budget.slowdown_share(p) * 100),
        })
    return render_template("settings_source_keys.html", sources=sources, key_source=secret_box.key_source(),
                           hour_labels=HOUR_LABELS, timezone=api_budget.TIMEZONE)


def _schedule_from_form():
    """The schedule rows (band_hour / band_minutes) as saved text, or the
    raw rows when they don't parse, so validation reports them."""
    bands = [
        f"{hour.strip()}:{minutes.strip()}"
        for hour, minutes in zip(request.form.getlist("band_hour"), request.form.getlist("band_minutes"))
        if hour.strip() or minutes.strip()
    ]
    text = ", ".join(bands)
    parsed = api_budget.parse_schedule(text)
    return api_budget.format_schedule(parsed) if parsed else text


@admin.route("/settings/source-keys/<key>", methods=["POST"])
@role_required(ROLE_ADMIN)
def save_source_keys(key):
    fields = config_store.SOURCE_FIELDS.get(key)
    if fields is None:
        flash("Unknown source.", "error")
        return redirect(url_for("admin.source_keys_page"))
    p = api_budget.PROVIDERS[key]
    values = {}
    if "band_hour" in request.form:
        values[p.schedule_setting] = _schedule_from_form()
    if request.form.get("reset_schedule"):
        values[p.schedule_setting] = ""
    if save_fields_form(fields, values):
        flash(f"{p.name} settings saved. The scheduler uses them from its next check.", "success")
    return redirect(url_for("admin.source_keys_page") + f"#source-{key}")
