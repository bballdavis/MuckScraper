"""Articles that carry another article's text (2026-10-11).

news_fetcher/copy_detector.py sets Article.copy_of_id when an article in a
story is mostly the same text as another one from a different outlet: a
piece one outlet republished from another (the New York Post running Fox
News Digital's article), or the same AP copy run by two outlets. Such a pair
is one report, not two.

The aggregator keeps listing the copy, labelled. muckscraper.news and the
pipeline steps that count sources (edition selection, ranking, the story
summary and deep report prompts) use original_articles() instead, so one
report is not counted, or shown, twice.
"""


def is_copy_in_story(article):
    """True when the article copies another article that is still in the
    same story. A copy whose original was later moved to another story (or
    deleted) counts as an original again."""
    if not getattr(article, "copy_of_id", None):
        return False
    original = getattr(article, "copy_of", None)
    return original is not None and original.story_id == article.story_id


def original_articles(articles):
    """The articles that are not copies of another article in their story."""
    return [article for article in articles if not is_copy_in_story(article)]


def copy_label(article):
    """'Same report as Fox News' for a copy, None otherwise."""
    if not is_copy_in_story(article):
        return None
    original = article.copy_of
    name = (original.outlet.name if original.outlet else None) or original.source or "another outlet"
    return f"Same report as {name}"
