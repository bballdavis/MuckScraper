# muckscraperHeadlinesGoogleNEW/news_fetcher/outlet_bias_llm.py
# news_fetcher/outlet_bias_llm.py

import os
import re
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

BIAS_LABELS = {
    1: "Left",
    2: "Lean Left",
    3: "Center",
    4: "Lean Right",
    5: "Right"
}


@observe()
def _ask_ollama(prompt):
    """Send a prompt to the configured LLM provider and return the raw response string or None."""
    langfuse_context.update_current_observation(
        input=prompt,
        metadata={
            "provider": llm_client.provider_for_tier(llm_client.TIER_FAST),
            "model": llm_client.model_for_tier(llm_client.TIER_FAST),
        }
    )
    result = llm_client.generate_text(prompt, timeout=30, tier=llm_client.TIER_FAST)
    langfuse_context.update_current_observation(output=result)
    return result


# How much of the article the lean rating reads. The first 600 characters
# (until 2026-10-25) is the lede, the most neutral part of almost any news
# story, and with only that to go on the model rated nearly everything Center.
ARTICLE_TEXT_CHARS = 4000

_SCORE_LINE = re.compile(r"^\W*(?:lean|rating|score)?\W*([1-5])(?![\d.]*\d)", re.IGNORECASE)
_WHY_LINE = re.compile(r"^\W*why\W*:\s*(.+)$", re.IGNORECASE)


def _parse_bias_score(raw, label):
    """Parse a 1-5 rating from the model's reply.

    Takes a bare number ("3") or the article prompt's "LEAN: 3" line, and
    shrugs off what models wrap around it ("**3**", "Rating: 3.", "3/5").
    "unknown" or anything without a 1-5 at the start of a line is None.
    """
    if raw is None:
        return None
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line or _WHY_LINE.match(line):
            continue
        if "unknown" in line.lower():
            logger.info(f"  The model could not determine bias for: {label}")
            return None
        match = _SCORE_LINE.match(line)
        if match:
            return int(match.group(1))
    logger.info(f"  Could not parse the model's bias reply for '{label}': {raw!r}")
    return None


def _parse_bias_reason(raw):
    """The article prompt's one-sentence WHY line, or None."""
    for line in (raw or "").splitlines():
        match = _WHY_LINE.match(line.strip())
        if match:
            return match.group(1).strip() or None
    return None


@observe()
def get_outlet_bias_from_llm(outlet_name):
    """
    Ask Ollama to rate the political bias of a news outlet by name.
    Returns an integer 1-5 or None if it can't determine.
    """
    prompt = render_prompt("outlet_bias.by_name", outlet_name=outlet_name)
    if prompt is None:
        return None

    langfuse_context.update_current_trace(metadata={"outlet_name": outlet_name}, tags=[f"outlet:{outlet_name}"])
    langfuse_context.update_current_observation(
        input=prompt,
        metadata={"provider": llm_client.provider_for_tier(llm_client.TIER_FAST)}
    )
    raw = _ask_ollama(prompt)
    langfuse_context.update_current_observation(
        output=raw
    )
    logger.info(f"  Ollama rated outlet '{outlet_name}': {raw}")
    return _parse_bias_score(raw, outlet_name)


@observe()
def get_article_lean_from_llm(title, content=None):
    """
    Ask the fast model to rate the political lean of one article from its
    title and text. Returns (score 1-5 or None, one-sentence reason or None).
    """
    article_text = f"Title: {title}"
    if content:
        snippet = " ".join(content.split())[:ARTICLE_TEXT_CHARS]
        article_text += f"\n\nText: {snippet}"

    prompt = render_prompt("outlet_bias.by_article", article_text=article_text)
    if prompt is None:
        return None, None

    langfuse_context.update_current_observation(
        input=prompt,
        metadata={"provider": llm_client.provider_for_tier(llm_client.TIER_FAST)}
    )
    raw = _ask_ollama(prompt)
    langfuse_context.update_current_observation(
        output=raw
    )
    logger.info(f"  The model rated article '{title[:60]}...': {raw}")
    score = _parse_bias_score(raw, title)
    return score, (_parse_bias_reason(raw) if score is not None else None)


def get_article_bias_from_llm(title, content=None):
    """The 1-5 score from get_article_lean_from_llm(), or None."""
    return get_article_lean_from_llm(title, content)[0]
