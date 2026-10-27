# muckscraperHeadlinesGoogleNEW/news_fetcher/topic_classifier.py
# news_fetcher/topic_classifier.py

import os
import time
import logging
from langfuse import Langfuse
from langfuse.decorators import observe, langfuse_context

from news_fetcher import llm_client
from news_fetcher.prompt_registry import render_prompt

logger = logging.getLogger(__name__)

langfuse = Langfuse(
    public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
    secret_key=os.environ.get("LANGFUSE_SECRET_KEY", ""),
    host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
)

# Default taxonomy, used only if the DB is briefly unreachable (see
# _active_topic_names below) — keeps a transient DB hiccup from crashing
# classification of every remaining article in a batch.
_FALLBACK_TOPIC_NAMES = [
    "US Politics", "US News", "International News",
    "Sci/Tech", "Sports", "Buss/Fin", "Other",
]
_TOPIC_NAME_CACHE_TTL_SECONDS = 60
_topic_name_cache = {"names": None, "descriptions": {}, "expires_at": 0.0}


def _load_active_topics():
    """
    Active sections from the DB, in admin-configured order, with the
    description each one gives the classifier (Settings › Sections). Cached
    for _TOPIC_NAME_CACHE_TTL_SECONDS so a batch classification run (one
    call per article) doesn't re-query on every article.
    """
    now = time.monotonic()
    if _topic_name_cache["names"] is not None and now < _topic_name_cache["expires_at"]:
        return _topic_name_cache["names"], _topic_name_cache["descriptions"]

    from aggregator import db
    from aggregator.models import Topic
    try:
        rows = Topic.query.filter_by(is_active=True).order_by(Topic.sort_order).all()
    except Exception as e:
        db.session.rollback()
        logger.error(f"  [Classifier] Failed to load active topics from DB, using default taxonomy: {e}")
        return _FALLBACK_TOPIC_NAMES, {}
    _topic_name_cache["names"] = [t.name for t in rows]
    _topic_name_cache["descriptions"] = {t.name: (t.description or "").strip() for t in rows}
    _topic_name_cache["expires_at"] = now + _TOPIC_NAME_CACHE_TTL_SECONDS
    return _topic_name_cache["names"], _topic_name_cache["descriptions"]


def _active_topic_names():
    return _load_active_topics()[0]


def categories_list(names, descriptions):
    """The prompt's category lines: "- Sports: Games, leagues, ..." (just the
    name when a section has no description). Other is left out; it is the
    prompt's own fallback."""
    lines = []
    for name in names:
        if name == "Other":
            continue
        description = descriptions.get(name)
        lines.append(f"- {name}: {description}" if description else f"- {name}")
    return "\n".join(lines)


def match_topics(result, valid_topics):
    """The section names in the model's answer, in order. A line counts by
    its name, the part before any colon, so a model that echoes a
    description ("US Politics: ... toward another country") doesn't also
    pick up names inside it ("other")."""
    by_lower = {t.lower(): t for t in valid_topics}
    matched = []
    for line in result.splitlines():
        name = line.split(":", 1)[0].strip(" \t-*\"'.").lower()
        if not name:
            continue
        found = by_lower.get(name)
        if found is None:
            found = next((t for t in valid_topics if t.lower() in name), None)
        if found and found not in matched:
            matched.append(found)
    return matched


@observe()
def classify_article(title, content_snippet=""):
    """
    Ask Ollama to classify an article into one or more topics.
    Returns a list of topic label strings.
    Falls back to ["Other"] if Ollama is unavailable or classification fails.
    """
    if not llm_client.is_configured(llm_client.TIER_FAST):
        return ["Other"]

    valid_topics, descriptions = _load_active_topics()
    if not valid_topics:
        return ["Other"]

    # Use title + first 200 chars of content for classification
    text = title
    if content_snippet:
        clean = content_snippet[:200].strip()
        if clean:
            text += f"\n{clean}"

    prompt = render_prompt("topic_classifier", text=text,
                           categories_list=categories_list(valid_topics, descriptions))
    if prompt is None:
        return ["Other"]

    langfuse_context.update_current_observation(
        input=prompt,
        metadata={
            "provider": llm_client.provider_for_tier(llm_client.TIER_FAST),
            "model": llm_client.model_for_tier(llm_client.TIER_FAST),
        }
    )
    result = llm_client.generate_text(prompt, timeout=30, tier=llm_client.TIER_FAST)
    if result is None:
        logger.info("  [Classifier] No response, using Other")
        return ["Other"]

    langfuse_context.update_current_observation(output=result)

    matched = match_topics(result, valid_topics)

    if matched:
        # Remove "Other" if any real categories were found
        matched = [t for t in matched if t != "Other"]
        if matched:
            logger.info(f"  [Classifier] Tagged as: {', '.join(matched)}")
            return matched

    logger.info(f"  [Classifier] No match found, using Other")
    return ["Other"]
