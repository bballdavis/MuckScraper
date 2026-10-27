"""How much of an article MuckScraper has read, for clients that can't see
its text (the API leaves content_html out unless Settings › API allows full
text). An article is "full" when its page was read, "excerpt" when only the
publisher's feed teaser or a short bit is stored, and "none" when it is
headline only: a paywall, a blocked site (reuters.com) or a feed with no
description. A story with no article long enough to summarize can't get a
summary, so the app says it is headline only instead of offering Summarize
(Philip, 2026-10-27).
"""
import re

# news_fetcher/summarizer.py summarize_story skips a story when none of its
# articles has this much text.
STORY_SUMMARY_MIN_TEXT = 200
# Below this the stored text is a feed blurb, not the article: the same floor
# summarizer.ARTICLE_DEEP_ANALYSIS_MIN_CHARS uses.
FULL_TEXT_MIN = 500
EXCERPT_MAX = 600

_SCRIPT = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&nbsp;": " ", "&quot;": '"', "&#39;": "'"}


def plain_text(html):
    """The stored content as one line of text, as summarizer.strip_html reads it."""
    if not html:
        return ""
    text = _TAG.sub(" ", _SCRIPT.sub(" ", html))
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    return _SPACE.sub(" ", text).strip()


def text_kind(article):
    text = plain_text(article.content)
    if not text:
        return "none"
    if article.scrape_status == "success" and len(text) >= FULL_TEXT_MIN:
        return "full"
    return "excerpt"


def excerpt(article):
    """The publisher's teaser when that is all there is, else None. A teaser
    is what the feed itself publishes, so it is sent even when full text isn't."""
    if text_kind(article) != "excerpt":
        return None
    text = plain_text(article.content)
    if len(text) <= EXCERPT_MAX:
        return text
    return text[:EXCERPT_MAX].rsplit(" ", 1)[0] + "…"


def can_summarize(story):
    """Whether any of the story's articles has enough text for a summary."""
    return any(len(plain_text(a.content)) >= STORY_SUMMARY_MIN_TEXT for a in story.articles)
