"""Admin › AI: LLM routing, each provider's keys, models and spending caps,
and the prompt templates. Same rules as blueprints/admin/llm_settings.py and
config_crud.py."""
import logging
import time
from datetime import datetime

from flask import g

from aggregator import config_store, db
from aggregator.models import PromptTemplate

from . import _config, page
from . import schema as s
from .schema import FieldError

logger = logging.getLogger(__name__)

PROVIDER_LABELS = {"ollama": "Ollama", "lmstudio": "LM Studio", "anthropic": "Anthropic", "gemini": "Gemini",
                   "groq": "Groq", "openrouter": "OpenRouter"}
AUTO_DEEP_ANALYSIS_HELP = "Write a deep analysis for each article as it comes in. Uses the main model a lot."


def _llm():
    from news_fetcher import llm_client
    return llm_client


def _label(key):
    return PROVIDER_LABELS.get(key, key)


def _auto_deep_key():
    from news_fetcher.fetch_and_store_articles import AUTO_ARTICLE_DEEP_ANALYSIS_SETTING_KEY
    return AUTO_ARTICLE_DEEP_ANALYSIS_SETTING_KEY


@page("llm", "LLM providers", "ai", "llm", detail=lambda: _label(_llm().provider_for_tier(_llm().TIER_QUALITY)))
def llm_page(_=None):
    from aggregator.blueprints.admin._shared import _load_json_setting
    from aggregator.blueprints.admin.llm_settings import _provider_rows
    llm = _llm()
    tiers = []
    for tier, name in ((llm.TIER_QUALITY, "Main"), (llm.TIER_FAST, "Fast")):
        ok = llm.is_configured(tier)
        tiers.append(s.fact(f"{name} model", f"{_label(llm.provider_for_tier(tier))} · {llm.model_for_tier(tier) or '—'}"
                            if ok else "Not set up", None if ok else "bad"))
    tiers.append(s.fact("Embeddings", f"{_label(llm.EMBEDDING_PROVIDER)} · {llm.embedding_model_name()}"))
    providers = []
    for row in _provider_rows(config_store.field_states()):
        providers.append(s.item(row["key"], row["name"], subtitle=", ".join(row["roles"]) or None,
                                badge=None if row["configured"] else s.badge("Not set up"),
                                page=f"llm/{row['key']}"))
    fast_choices = [("main", "Same as main")] + [(k, _label(k)) for k in llm.CHAT_PROVIDERS]
    fast = llm.LLM_FAST_PROVIDER if llm.LLM_FAST_PROVIDER and llm.LLM_FAST_PROVIDER != llm.LLM_PROVIDER else "main"
    return s.page("llm", "LLM providers", [
        s.section("now", facts=tiers, actions=[s.action("test_main", "Test the main model"),
                                               s.action("test_fast", "Test the fast model")]),
        s.section("routing", "Routing", fields=[
            s.choice("LLM_PROVIDER", "Main model", llm.LLM_PROVIDER, [(k, _label(k)) for k in llm.CHAT_PROVIDERS],
                     help="Summaries, deep reports, headlines."),
            s.choice("LLM_FAST_PROVIDER", "Fast model", fast, fast_choices,
                     help="Topic labels, story grouping checks, bias scoring."),
            s.choice("EMBEDDING_PROVIDER", "Embeddings", llm.EMBEDDING_PROVIDER,
                     [(k, _label(k)) for k in llm.EMBEDDING_PROVIDERS], help="What story grouping matches on."),
        ], save="auto", footer="The scheduler picks a change up within a minute."),
        s.section("auto", fields=[s.toggle("auto_deep_analysis", "Auto deep analysis",
                                           bool(_load_json_setting(_auto_deep_key())), help=AUTO_DEEP_ANALYSIS_HELP)],
                  save="auto"),
        s.section("providers", "Providers", items=providers),
    ])


@llm_page.save
def save_llm(_, values, section):
    if "auto_deep_analysis" in values:
        from aggregator.blueprints.admin._shared import _save_json_setting
        _save_json_setting(_auto_deep_key(), s.get_bool(values, "auto_deep_analysis"))
        return None
    llm = _llm()
    pending = {}
    for name, kind in (("LLM_PROVIDER", "provider"), ("LLM_FAST_PROVIDER", "provider"),
                       ("EMBEDDING_PROVIDER", "embedding_provider")):
        if name in values:
            value = s.get_str(values, name, 40).lower()
            if name == "LLM_FAST_PROVIDER" and value == llm.LLM_PROVIDER:
                value = "main"
            error = config_store.validate(name, value, kind)
            if error:
                raise FieldError(error, name)
            pending[name] = value
    for name, value in pending.items():
        config_store.save(name, value, g.user.username)
    db.session.commit()
    config_store.reload_now()
    return None


def _test(tier):
    llm = _llm()
    provider = _label(llm.provider_for_tier(tier))
    if not llm.is_configured(tier):
        raise FieldError(f"{provider} isn't set up for this tier.")
    started = time.monotonic()
    try:
        reply = llm.generate_text("Reply with the single word OK.", timeout=30, tier=tier)
    except Exception as e:  # any provider error is the answer here
        logger.warning("[LLM test] %s (%s) failed: %s", provider, tier, e)
        reply = None
    ms = int((time.monotonic() - started) * 1000)
    if not reply:
        raise FieldError(f"{provider} gave no reply after {ms} ms.")
    return {"message": f"{provider} ({llm.model_for_tier(tier)}) replied in {ms} ms."}


@llm_page.action("test_main")
def test_main(_, values):
    return _test(_llm().TIER_QUALITY)


@llm_page.action("test_fast")
def test_fast(_, values):
    return _test(_llm().TIER_FAST)


def _money(value):
    return "—" if value is None else f"${value:,.2f}"


@page("llm/*", "Provider", "ai", "llm", listed=False)
def provider_page(arg):
    from aggregator.blueprints.admin.llm_settings import _provider_rows
    if arg not in config_store.PROVIDER_FIELDS:
        raise LookupError(arg)
    states = config_store.field_states()
    row = next(r for r in _provider_rows(states) if r["key"] == arg)
    spend_names = {name for name, _, _ in config_store.SPEND_FIELDS.get(arg, [])}
    spec = [f for f in config_store.PROVIDER_FIELDS[arg] if f[0] not in spend_names]
    spend_spec = [f for f in config_store.PROVIDER_FIELDS[arg] if f[0] in spend_names]
    facts = [s.fact("Serves", ", ".join(row["roles"]) or "Nothing"),
             s.fact("Set up", "Yes" if row["configured"] else "No", None if row["configured"] else "warn")]
    facts += [s.fact(label, value) for label, value in row["facts"]]
    sections = [s.section("about", facts=facts, footer=row.get("about"))]
    sections.append(s.section("settings", "Settings", fields=_config.fields(spec, states),
                              footer=f"Saved here, these override {row['env']} from the environment. "
                                     "Clear one to go back to it."))
    if spend_spec:
        spend = row.get("spend") or {}
        spend_facts = []
        if spend:
            spend_facts = [
                s.fact("Spent today", _money(spend.get("today")), "bad" if spend.get("blocked") else None),
                s.fact("Spent this month", _money(spend.get("month"))),
                s.fact("Calls today", spend.get("calls_today")),
            ]
        sections.append(s.section("spend", "Spending", facts=spend_facts, fields=_config.fields(spend_spec, states),
                                  footer="Calls stop for the day or month once a cap is reached. Blank is no cap. "
                                         "Blank prices use the known price for the model."))
    return s.page(f"llm/{arg}", row["name"], sections)


@provider_page.save
def save_provider(arg, values, section):
    spec = config_store.PROVIDER_FIELDS.get(arg)
    if spec is None:
        raise LookupError(arg)
    _config.save(spec, values)
    return f"{_label(arg)} settings saved."


# --- Prompts ---------------------------------------------------------------------------

@page("prompts", "Prompts", "ai", "prompt", detail=lambda: str(PromptTemplate.query.count()))
def prompts(_=None):
    rows = PromptTemplate.query.order_by(PromptTemplate.key.asc()).all()
    return s.page("prompts", "Prompts", [
        s.section("prompts", items=[
            s.item(p.key, p.key.replace("_", " ").capitalize(), subtitle=p.description,
                   badge=s.badge("Edited") if p.updated_at else None, page=f"prompts/{p.key}") for p in rows
        ], empty="No prompts yet. They're added when the scheduler first starts."),
    ])


def _prompt(arg):
    prompt = PromptTemplate.query.filter_by(key=arg).first() if arg else None
    if prompt is None:
        raise LookupError(arg)
    return prompt


@page("prompts/*", "Prompt", "ai", "prompt", listed=False)
def prompt_page(arg):
    from news_fetcher.prompt_registry import KNOWN_VARS
    p = _prompt(arg)
    names = sorted(KNOWN_VARS.get(p.key, set()))
    sections = [
        s.section("text", footer=("Placeholders: " + ", ".join("{" + n + "}" for n in names)) if names else None,
                  fields=[s.multiline("current_text", "Prompt", p.current_text)]),
    ]
    if p.updated_at:
        sections.append(s.section("reset", footer=f"Edited {p.updated_at:%b %-d, %Y}.", actions=[s.action(
            "reset", "Go back to the default", role="destructive", confirm="Replace your edits with the default prompt?")]))
    return s.page(f"prompts/{p.key}", p.key.replace("_", " ").capitalize(), sections,
                  notice=p.description)


@prompt_page.save
def save_prompt(arg, values, section):
    from news_fetcher.prompt_registry import invalidate_cache, validate_prompt_text
    p = _prompt(arg)
    text = values.get("current_text")
    if not isinstance(text, str):
        raise FieldError("Must be text.", "current_text")
    error, warning = validate_prompt_text(p.key, text)
    if error:
        raise FieldError(error, "current_text")
    p.current_text = text
    p.updated_at = datetime.utcnow()
    db.session.flush()
    invalidate_cache(p.key)
    return f"Saved. {warning}" if warning else "Saved."


@prompt_page.action("reset")
def reset_prompt(arg, values):
    from news_fetcher.prompt_registry import invalidate_cache
    p = _prompt(arg)
    p.current_text = p.default_text
    p.updated_at = None
    db.session.flush()
    invalidate_cache(p.key)
    return {"message": "Back to the default."}
