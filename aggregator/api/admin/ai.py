"""Admin › AI: LLM routing, each provider's keys, models and spending caps,
the auto deep-analysis switch, a test call per tier, and the prompt
templates. The same data and rules as blueprints/admin/llm_settings.py and
config_crud.py."""
import logging
import time
from datetime import datetime

from flask import g, jsonify

from aggregator import config_store, db, secret_box
from aggregator.models import PromptTemplate

from ..errors import ApiError
from . import config_fields, reply, route
from . import values as v
from .values import Invalid

logger = logging.getLogger(__name__)

PROVIDER_LABELS = {"ollama": "Ollama", "lmstudio": "LM Studio", "anthropic": "Anthropic", "gemini": "Gemini",
                   "groq": "Groq", "openrouter": "OpenRouter"}
ROLE_HINTS = {
    "main": "Summaries, deep reports, headlines.",
    "fast": "Topic labels, story grouping checks, bias scoring.",
    "embeddings": "The vectors story grouping matches on.",
}
AUTO_DEEP_ANALYSIS_HELP = "Write a deep analysis for each article as it comes in. Uses the main model a lot."
# The API container's gunicorn gives a request 30 seconds.
TEST_TIMEOUT = 20


def provider_label(key):
    return PROVIDER_LABELS.get(key, key)


def _llm():
    from news_fetcher import llm_client
    llm_client._sync_config()
    return llm_client


def _auto_deep_key():
    from news_fetcher.fetch_and_store_articles import AUTO_ARTICLE_DEEP_ANALYSIS_SETTING_KEY
    return AUTO_ARTICLE_DEEP_ANALYSIS_SETTING_KEY


def _choices(keys):
    return [{"value": k, "label": provider_label(k)} for k in keys]


def _provider(row, states):
    key = row["key"]
    spend_names = {name for name, _, _ in config_store.SPEND_FIELDS.get(key, [])}
    spec = [f for f in config_store.PROVIDER_FIELDS[key] if f[0] not in spend_names]
    spend_spec = [f for f in config_store.PROVIDER_FIELDS[key] if f[0] in spend_names]
    out = {
        "key": key,
        "name": row["name"],
        "roles": row["roles"],
        "configured": row["configured"],
        "toggles": row["toggles"],
        "facts": [{"label": label, "value": value} for label, value in row["facts"]],
        "about": row.get("about"),
        "env": row["env"],
        "fields": config_fields.fields(spec, states),
        "spend": None,
    }
    if spend_spec:
        price = row.get("spend_price")
        out["spend"] = {
            "fields": config_fields.fields(spend_spec, states),
            "model": row.get("spend_model") or None,
            "price": {"input": price[0], "output": price[1]} if price else None,
            "reports_cost": bool(row.get("spend_reports_cost")),
            "summary": row.get("spend"),
        }
    return out


def _llm_page():
    from aggregator.blueprints.admin._shared import _load_json_setting
    from aggregator.blueprints.admin.llm_settings import TASK_ROUTES, _provider_rows
    llm = _llm()
    tiers = {}
    for tier, name in ((llm.TIER_QUALITY, "main"), (llm.TIER_FAST, "fast")):
        provider = llm.provider_for_tier(tier)
        tiers[name] = {"provider": provider, "label": provider_label(provider),
                       "model": llm.model_for_tier(tier) or None, "configured": bool(llm.is_configured(tier))}
    states = config_store.field_states()
    fast = llm.LLM_FAST_PROVIDER if llm.LLM_FAST_PROVIDER and llm.LLM_FAST_PROVIDER != llm.LLM_PROVIDER else "main"
    return {
        "tiers": tiers,
        "embedding": {"provider": llm.EMBEDDING_PROVIDER, "label": provider_label(llm.EMBEDDING_PROVIDER),
                      "model": llm.embedding_model_name()},
        "routing": {
            "main": llm.LLM_PROVIDER,
            "fast": fast,
            "embeddings": llm.EMBEDDING_PROVIDER,
            "sources": {name: states.get(name, {}).get("source", "unset")
                        for name in config_store.ROUTING_FIELDS + config_store.EMBEDDING_FIELDS},
        },
        "chat_providers": _choices(llm.CHAT_PROVIDERS),
        "embedding_providers": _choices(llm.EMBEDDING_PROVIDERS),
        "role_hints": ROLE_HINTS,
        "providers": [_provider(row, states) for row in _provider_rows(states)],
        "task_routes": [{"task": task, "tier": "main" if tier == "quality" else "fast"} for task, tier in TASK_ROUTES],
        "auto_deep_analysis": bool(_load_json_setting(_auto_deep_key())),
        "auto_deep_analysis_help": AUTO_DEEP_ANALYSIS_HELP,
        "key_source": secret_box.key_source(),
    }


@route("/admin/llm")
def llm_page():
    """Which provider serves each tier, every provider's settings and spend, and the task routing."""
    return jsonify(_llm_page())


def _save_routing(pending):
    for name, (value, kind) in pending.items():
        error = config_store.validate(name, value, kind)
        if error:
            raise Invalid(error, name)
    for name, (value, _) in pending.items():
        config_store.save(name, value, g.user.username)
    db.session.commit()
    config_store.reload_now()


@route("/admin/llm/routing", methods=("PUT",))
def save_routing():
    """Any of {main, fast, embeddings}: a provider key; fast also takes "main" to follow the main one."""
    llm = _llm()
    values = v.body()
    unknown = set(values) - {"main", "fast", "embeddings"}
    if unknown:
        raise Invalid(f"Unknown setting: {sorted(unknown)[0][:40]}.")
    pending = {}
    if "main" in values:
        pending["LLM_PROVIDER"] = (v.get_str(values, "main", 40, required=True, label="Main model").lower(), "provider")
    if "fast" in values:
        fast = v.get_str(values, "fast", 40, required=True, label="Fast model").lower()
        main = pending.get("LLM_PROVIDER", (llm.LLM_PROVIDER,))[0]
        pending["LLM_FAST_PROVIDER"] = ("main" if fast == main else fast, "provider")
    if "embeddings" in values:
        pending["EMBEDDING_PROVIDER"] = (v.get_str(values, "embeddings", 40, required=True,
                                                   label="Embeddings").lower(), "embedding_provider")
    _save_routing(pending)
    return reply(_llm_page(), "Routing saved. The scheduler picks it up within a minute.")


@route("/admin/llm/roles", methods=("PUT",))
def assign_role():
    """{role: main|fast|embeddings, provider}: one role chip on a provider's card."""
    llm = _llm()
    values = v.body()
    role = v.get_choice(values, "role", ("main", "fast", "embeddings"), "Pick main, fast or embeddings.")
    provider = v.get_str(values, "provider", 40, required=True, label="Provider").lower()
    if role == "embeddings":
        pending = {"EMBEDDING_PROVIDER": (provider, "embedding_provider")}
    elif role == "fast":
        pending = {"LLM_FAST_PROVIDER": ("main" if provider == llm.LLM_PROVIDER else provider, "provider")}
    else:
        pending = {"LLM_PROVIDER": (provider, "provider")}
    _save_routing(pending)
    label = {"main": "main model", "fast": "fast model", "embeddings": "embeddings"}[role]
    return reply(_llm_page(), f"{provider_label(provider)} now serves the {label}. "
                              "The scheduler picks it up within a minute.")


@route("/admin/llm/providers/<key>", methods=("PATCH",))
def save_provider(key):
    """{values: {NAME: value}}: a provider's keys, models, hosts, caps and prices. A key sent as "" is removed."""
    spec = config_store.PROVIDER_FIELDS.get(key)
    if spec is None:
        raise ApiError(404, "not_found", "No such provider.")
    config_fields.save(spec, v.body().get("values"))
    return reply(_llm_page(), f"{provider_label(key)} settings saved.")


@route("/admin/llm/auto-deep-analysis", methods=("PUT",))
def save_auto_deep_analysis():
    """{enabled}"""
    from aggregator.blueprints.admin._shared import _save_json_setting
    enabled = v.get_bool(v.body(), "enabled")
    _save_json_setting(_auto_deep_key(), enabled)
    return reply(_llm_page(), "Auto deep analysis is on." if enabled else "Auto deep analysis is off.")


@route("/admin/llm/test", methods=("POST",))
def test_tier():
    """{tier: main|fast}: one short call, to check a key or model without waiting for a run."""
    llm = _llm()
    name = v.get_choice(v.body(), "tier", ("main", "fast"), "Pick main or fast.")
    tier = llm.TIER_FAST if name == "fast" else llm.TIER_QUALITY
    provider = llm.provider_for_tier(tier)
    model = llm.model_for_tier(tier) or None
    out = {"ok": False, "tier": name, "provider": provider, "label": provider_label(provider), "model": model,
           "ms": None}
    if not llm.is_configured(tier):
        return jsonify({**out, "message": f"{provider_label(provider)} isn't set up for this tier."})
    started = time.monotonic()
    try:
        answer = llm.generate_text("Reply with the single word OK.", timeout=TEST_TIMEOUT, tier=tier)
    except Exception as e:  # any provider error is the answer here
        logger.warning("[LLM test] %s (%s) failed: %s", provider, tier, e)
        answer = None
    ms = int((time.monotonic() - started) * 1000)
    if not answer:
        return jsonify({**out, "ms": ms, "message": f"No reply after {ms} ms."})
    return jsonify({**out, "ok": True, "ms": ms, "message": f"Replied in {ms} ms."})


@route("/admin/llm/health")
def llm_health():
    """Whether each provider the pipeline uses is answering."""
    detail = _llm().llm_status_detail()
    return jsonify({
        "online": bool(detail.get("online")),
        "providers": [{"provider": e.get("provider"), "label": provider_label(e.get("provider")),
                       "tier": "fast" if e.get("tier") == _llm().TIER_FAST else "main",
                       "online": bool(e.get("online")), "host": e.get("host"), "role": e.get("role")}
                      for e in detail.get("providers") or []],
    })


# --- Prompts ---------------------------------------------------------------------------

def _title(key):
    return key.replace(".", " · ").replace("_", " ").capitalize()


def _prompt_row(p):
    return {"key": p.key, "title": _title(p.key), "description": p.description, "edited": p.updated_at is not None,
            "updated_at": p.updated_at.isoformat() + "Z" if p.updated_at else None}


def _prompt_detail(p, warning=None):
    from news_fetcher.prompt_registry import KNOWN_VARS
    return {**_prompt_row(p), "text": p.current_text, "default_text": p.default_text,
            "placeholders": sorted(KNOWN_VARS.get(p.key, set())), "warning": warning}


def _prompt(key):
    prompt = PromptTemplate.query.filter_by(key=key).first()
    if prompt is None:
        raise ApiError(404, "not_found", "No such prompt.")
    return prompt


@route("/admin/prompts")
def prompts():
    """Every prompt template, and whether it's been edited."""
    rows = PromptTemplate.query.order_by(PromptTemplate.key.asc()).all()
    return jsonify({"items": [_prompt_row(p) for p in rows]})


@route("/admin/prompts/<key>")
def prompt(key):
    """One prompt: its text, its default and the placeholders it can use."""
    return jsonify(_prompt_detail(_prompt(key)))


@route("/admin/prompts/<key>", methods=("PUT",))
def save_prompt(key):
    """{text}"""
    from news_fetcher.prompt_registry import invalidate_cache, validate_prompt_text
    p = _prompt(key)
    text = v.body().get("text")
    if not isinstance(text, str):
        raise Invalid("Must be text.", "text")
    if not text.strip():
        raise Invalid("The prompt can't be empty.", "text")
    error, warning = validate_prompt_text(p.key, text)
    if error:
        raise Invalid(error, "text")
    p.current_text = text
    p.updated_at = datetime.utcnow()
    db.session.flush()
    invalidate_cache(p.key)
    return reply(_prompt_detail(p, warning), f"Saved. {warning}" if warning else "Saved.")


@route("/admin/prompts/<key>/reset", methods=("POST",))
def reset_prompt(key):
    """Put the default text back."""
    from news_fetcher.prompt_registry import invalidate_cache
    p = _prompt(key)
    p.current_text = p.default_text
    p.updated_at = None
    db.session.flush()
    invalidate_cache(p.key)
    return reply(_prompt_detail(p), "Back to the default.")
