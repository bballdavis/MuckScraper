"""Settings › LLM providers: which backend serves each tier, each
provider's keys and models, and which pipeline tasks run on which tier.

Routing, keys, models and Ollama hosts can be saved here
(aggregator/config_store.py); a saved value overrides the environment
variable of the same name and a cleared one falls back to it. Keys are
encrypted before they reach the database and never come back to the
browser. The page also has the auto deep-analysis switch and a one-line
test call per tier.
"""
import logging
import time

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from aggregator import db, config_store, secret_box
from aggregator.permissions import ROLE_ADMIN, role_required

from . import admin
from ._shared import _load_json_setting

logger = logging.getLogger(__name__)

PROVIDER_LABELS = {
    "ollama": "Ollama",
    "lmstudio": "LM Studio",
    "anthropic": "Anthropic",
    "gemini": "Gemini",
    "groq": "Groq",
    "openrouter": "OpenRouter",
}

# Which tier each kind of pipeline work uses. Mirrors the generate_text()
# call sites: the high-volume label/yes-no calls are fast, anything a reader
# sees as prose is main (see the TIER_* comments in llm_client.py).
# What each role chip does, shown as its tooltip.
ROLE_HINTS = {
    "main": "Main model: summaries, deep reports, headlines",
    "fast": "Fast model: topic labels, story grouping checks, bias scoring",
    "embeddings": "Embeddings: the vectors story grouping matches on",
}

TASK_ROUTES = [
    ("Article and story summaries", "quality"),
    ("Deep reports and article analysis", "quality"),
    ("Edition headlines", "quality"),
    ("Opinion stance and roundups", "quality"),
    ("Topic classification", "fast"),
    ("Story grouping checks", "fast"),
    ("Outlet bias scoring", "fast"),
    ("Linking opinion to stories", "fast"),
]


def _mask(value):
    """Enough of a key to tell two apart, never enough to use."""
    if not value:
        return ""
    return f"…{value[-4:]}" if len(value) > 8 else "set"


def _add_spend(row, llm):
    """Split a paid provider's spending fields out of its form fields, and
    add what it has spent (news_fetcher/llm_spend.py)."""
    from news_fetcher import llm_spend
    spend_names = {name for name, _, _ in config_store.SPEND_FIELDS.get(row["key"], [])}
    row["spend_fields"] = {f["name"].split("_", 1)[1]: f for f in row["fields"] if f["name"] in spend_names}
    row["fields"] = [f for f in row["fields"] if f["name"] not in spend_names]
    if not spend_names:
        return
    model = llm.model_for_tier(llm.TIER_QUALITY) if llm.provider_for_tier(llm.TIER_QUALITY) == row["key"] else None
    if not model:
        model = {"anthropic": llm.ANTHROPIC_MODEL, "gemini": llm.GEMINI_MODEL}.get(row["key"]) or \
            (llm._openai_compatible_config(row["key"]) or {}).get("model") or ""
    row["spend_model"] = model
    row["spend_price"] = llm_spend.price(row["key"], model)
    row["spend_reports_cost"] = row["key"] == "openrouter"
    try:
        row["spend"] = llm_spend.summary(row["key"])
    except Exception:
        row["spend"] = None


def _provider_rows(states):
    from news_fetcher import llm_client as llm

    quality, fast = llm.LLM_PROVIDER, llm.LLM_FAST_PROVIDER
    embedding = llm.EMBEDDING_PROVIDER

    def uses(name):
        roles = []
        if name == quality:
            roles.append("Main")
        if name == fast:
            roles.append("Fast")
        if name == embedding:
            roles.append("Embeddings")
        return roles

    def role_toggles(name):
        """The Main / Fast / Embeddings chips on a provider's header: which
        it holds, and whether it has the config to take the others."""
        toggles = [
            {"key": "main", "label": "Main", "active": name == quality, "ready": llm.provider_configured(name)},
            {"key": "fast", "label": "Fast", "active": name == fast, "ready": llm.provider_configured(name)},
        ]
        if name in llm.EMBEDDING_PROVIDERS:
            toggles.append({"key": "embeddings", "label": "Embeddings", "active": name == embedding,
                            "ready": llm.embedding_configured(name)})
        return toggles

    rows = []

    hosts = [h for h in (llm.OLLAMA_HOST, llm.OLLAMA_FALLBACK_HOST) if h]
    rows.append({
        "key": "ollama",
        "name": "Ollama",
        "roles": uses("ollama"),
        "configured": bool(llm.OLLAMA_HOST and llm.OLLAMA_MODEL),
        "facts": [
            ("Primary host", llm.OLLAMA_HOST or "Not set"),
            ("Fallback host", llm.OLLAMA_FALLBACK_HOST or "None"),
            ("Main model", llm.OLLAMA_MODEL or "Not set"),
            ("Fast model", llm.OLLAMA_FAST_MODEL or "Not set"),
            ("Embedding model", llm.EMBEDDING_MODEL),
        ],
        "env": "OLLAMA_HOST, OLLAMA_FALLBACK_HOST, OLLAMA_MODEL, OLLAMA_FAST_MODEL",
        "live": bool(hosts),
    })
    rows.append({
        "key": "anthropic",
        "name": "Anthropic",
        "is_new": True,
        "roles": uses("anthropic"),
        "configured": bool(llm.ANTHROPIC_API_KEY and llm.ANTHROPIC_MODEL),
        "facts": [
            ("API key", _mask(llm.ANTHROPIC_API_KEY) or "Not set"),
            ("Main model", llm.ANTHROPIC_MODEL or "Not set"),
            ("Fast model", llm.ANTHROPIC_FAST_MODEL or "Not set"),
            ("Max tokens", str(llm.ANTHROPIC_MAX_TOKENS)),
        ],
        "env": "ANTHROPIC_API_KEY, ANTHROPIC_MODEL, ANTHROPIC_FAST_MODEL",
    })
    rows.append({
        "key": "gemini",
        "name": "Gemini",
        "roles": uses("gemini"),
        "configured": bool(llm.GEMINI_API_KEY),
        "facts": [
            ("API key", _mask(llm.GEMINI_API_KEY) or "Not set"),
            ("Main model", llm.GEMINI_MODEL),
            ("Fast model", llm.GEMINI_FAST_MODEL),
            ("Embedding model", llm.GEMINI_EMBEDDING_MODEL),
        ],
        "env": "GEMINI_API_KEY, GEMINI_MODEL, GEMINI_FAST_MODEL",
    })
    rows.append({
        "key": "lmstudio",
        "name": "LM Studio",
        "is_new": True,
        "roles": uses("lmstudio"),
        "configured": bool(llm.LMSTUDIO_HOST and (llm.LMSTUDIO_MODEL or llm.LMSTUDIO_EMBEDDING_MODEL)),
        "facts": [],
        "env": "LMSTUDIO_HOST, LMSTUDIO_MODEL, LMSTUDIO_EMBEDDING_MODEL",
        "about": "A local LM Studio server (Developer › Start server). It can take the embeddings while another provider writes the text. The embedding model must return 768 numbers, e.g. google/embedding-gemma-300m or text-embedding-nomic-embed-text-v1.5.",
    })
    for key in llm.OPENAI_COMPATIBLE_PROVIDERS:
        if key == "lmstudio":
            continue
        config = llm._openai_compatible_config(key)
        rows.append({
            "key": key,
            "name": config["label"],
            "roles": uses(key),
            "configured": bool(config["api_key"] and config["model"]),
            "facts": [
                ("API key", _mask(config["api_key"]) or "Not set"),
                ("Main model", config["model"] or "Not set"),
                ("Fast model", config["fast_model"] or "Not set"),
                ("Endpoint", config["base_url"]),
            ],
            "env": f"{key.upper()}_API_KEY, {key.upper()}_MODEL, {key.upper()}_FAST_MODEL",
        })

    for row in rows:
        row["toggles"] = role_toggles(row["key"])
        row["fields"] = [
            {"name": name, "label": label, "kind": kind, **states[name]}
            for name, label, kind in config_store.PROVIDER_FIELDS[row["key"]]
        ]
        row["saved_count"] = sum(1 for f in row["fields"] if f["source"] == "saved")
        _add_spend(row, llm)
        # The facts the form doesn't edit (embedding model, endpoint, ...).
        edited = {f["label"] for f in row["fields"]}
        row["facts"] = [(label, value) for label, value in row["facts"] if label not in edited]

    # Providers in use first (main, then fast, then embeddings), the rest after.
    def order(row):
        r = row["roles"]
        return (0 if "Main" in r else 1 if "Fast" in r else 2 if r else 3)
    rows.sort(key=order)
    return rows


@admin.route("/settings/llm")
@role_required(ROLE_ADMIN)
def llm_providers_page():
    from news_fetcher import llm_client as llm
    from news_fetcher.fetch_and_store_articles import AUTO_ARTICLE_DEEP_ANALYSIS_SETTING_KEY

    tiers = {
        "quality": {
            "provider": llm.provider_for_tier(llm.TIER_QUALITY),
            "model": llm.model_for_tier(llm.TIER_QUALITY),
            "configured": llm.is_configured(llm.TIER_QUALITY),
        },
        "fast": {
            "provider": llm.provider_for_tier(llm.TIER_FAST),
            "model": llm.model_for_tier(llm.TIER_FAST),
            "configured": llm.is_configured(llm.TIER_FAST),
        },
    }
    routes = [
        {"task": task, "tier": tier, **tiers[tier]}
        for task, tier in TASK_ROUTES
    ]
    states = config_store.field_states()
    return render_template(
        "settings_llm.html",
        providers=_provider_rows(states),
        routing_states={name: states[name] for name in config_store.ROUTING_FIELDS + config_store.EMBEDDING_FIELDS},
        chat_providers=llm.CHAT_PROVIDERS,
        embedding_providers=llm.EMBEDDING_PROVIDERS,
        embedding_model=llm.embedding_model_name(),
        key_source=secret_box.key_source(),
        provider_labels=PROVIDER_LABELS,
        role_hints=ROLE_HINTS,
        tiers=tiers,
        routes=routes,
        embedding_provider=llm.EMBEDDING_PROVIDER,
        auto_article_deep_analysis_enabled=bool(_load_json_setting(AUTO_ARTICLE_DEEP_ANALYSIS_SETTING_KEY)),
    )


@admin.route("/settings/llm/test", methods=["POST"])
@role_required(ROLE_ADMIN)
def llm_test():
    """One short generate call on a tier, so a key or model change can be
    checked without waiting for the next pipeline run."""
    from news_fetcher import llm_client as llm

    tier = llm.TIER_FAST if (request.get_json(silent=True) or {}).get("tier") == "fast" else llm.TIER_QUALITY
    provider = llm.provider_for_tier(tier)
    if not llm.is_configured(tier):
        return jsonify({"ok": False, "provider": provider, "message": "Not configured."})
    started = time.monotonic()
    try:
        reply = llm.generate_text("Reply with the single word OK.", timeout=30, tier=tier)
    except Exception as e:  # any provider error is the answer here
        logger.warning("[LLM test] %s (%s) failed: %s", provider, tier, e)
        reply = None
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if not reply:
        return jsonify({"ok": False, "provider": provider, "message": f"No reply after {elapsed_ms} ms."})
    return jsonify({
        "ok": True, "provider": provider, "model": llm.model_for_tier(tier),
        "message": f"Replied in {elapsed_ms} ms.",
    })


def _save_all(values):
    """Validate then save {name: value}; flash and return False on the first
    error, so a bad field never half-applies a form."""
    for name, (value, kind) in values.items():
        error = config_store.validate(name, value, kind)
        if error:
            flash(f"{name}: {error}", "error")
            return False
    for name, (value, _) in values.items():
        config_store.save(name, value, current_user.username)
    db.session.commit()
    config_store.reload_now()
    return True


@admin.route("/settings/llm/routing", methods=["POST"])
@role_required(ROLE_ADMIN)
def save_llm_routing():
    values = {
        name: (request.form.get(name, "").strip().lower(), "provider")
        for name in config_store.ROUTING_FIELDS
    }
    for name in config_store.EMBEDDING_FIELDS:
        values[name] = (request.form.get(name, "").strip().lower(), "embedding_provider")
    if _save_all(values):
        flash("Routing saved. The scheduler picks it up within a minute.", "success")
    return redirect(url_for("admin.llm_providers_page"))


@admin.route("/settings/llm/assign", methods=["POST"])
@role_required(ROLE_ADMIN)
def assign_llm_role():
    """One role chip on a provider's header: make that provider the main,
    fast or embeddings provider. Picking the main provider for Fast saves
    "main", so the fast tier follows the main one again."""
    from news_fetcher import llm_client as llm

    role = request.form.get("role", "")
    provider = request.form.get("provider", "").strip().lower()
    if role == "embeddings":
        values = {"EMBEDDING_PROVIDER": (provider, "embedding_provider")}
    elif role == "fast":
        values = {"LLM_FAST_PROVIDER": ("main" if provider == llm.LLM_PROVIDER else provider, "provider")}
    elif role == "main":
        values = {"LLM_PROVIDER": (provider, "provider")}
    else:
        flash("Unknown role.", "error")
        return redirect(url_for("admin.llm_providers_page"))
    if not provider:
        flash("Pick a provider.", "error")
    elif _save_all(values):
        label = {"main": "main model", "fast": "fast model", "embeddings": "embeddings"}[role]
        flash(f"{PROVIDER_LABELS.get(provider, provider)} now serves the {label}. The scheduler picks it up within a minute.", "success")
    return redirect(url_for("admin.llm_providers_page") + f"#provider-{provider}")


def save_fields_form(fields, values_in=None):
    """Save a form of config_field()s (templates/_config_fields.html).
    `values_in` gives a field's value when the form sends it some other way
    (Settings › Source API keys' schedule rows).
    Non-key fields are saved as typed (blank goes back to the environment);
    one that wasn't saved and still shows its environment or default value
    is left alone, so it keeps following the environment. A key field left
    blank keeps the saved key; its Remove checkbox deletes it. Returns True
    when everything saved."""
    states = config_store.field_states()
    values = {}
    for name, _, kind in fields:
        value = (values_in or {}).get(name, request.form.get(name, "")).strip()
        state = states.get(name, {})
        if kind != "secret" and state.get("source") != "saved" and value in (state.get("env"), state.get("default")):
            continue
        if kind == "secret":
            if request.form.get(f"clear_{name}"):
                value = ""
            elif not value:
                continue
        values[name] = (value, kind)
    return _save_all(values)


@admin.route("/settings/llm/provider/<key>", methods=["POST"])
@role_required(ROLE_ADMIN)
def save_llm_provider(key):
    fields = config_store.PROVIDER_FIELDS.get(key)
    if fields is None:
        flash("Unknown provider.", "error")
        return redirect(url_for("admin.llm_providers_page"))
    if save_fields_form(fields):
        flash(f"{PROVIDER_LABELS.get(key, key)} settings saved.", "success")
    return redirect(url_for("admin.llm_providers_page") + f"#provider-{key}")
