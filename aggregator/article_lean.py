"""An article's own lean rating: the AI's, with a one-line reason, and a
reader's override from the Rate lean dialog.

Each rating is an ArticleLeanRating row. The one applied last is the
article's bias_score, with bias_source "ai" or "reader"; until then the
article carries a copy of its outlet's score (bias_source None) and outlet
re-syncs may replace it. The dialog asks for the AI's rating first and shows
it without applying it; only Save applies a lean. An AI rating that was shown
but not saved is reused the next time the dialog opens.
"""
from aggregator import db
from aggregator.models import ArticleLeanRating

SOURCE_AI = "ai"
SOURCE_READER = "reader"
NOTE_MAX = 500

LABELS = {1: "Left", 2: "Lean Left", 3: "Center", 4: "Lean Right", 5: "Right"}


class RatingUnavailable(Exception):
    """The model couldn't rate the article (offline, or no usable answer)."""


def latest(article_id, source):
    return (ArticleLeanRating.query.filter_by(article_id=article_id, source=source)
            .order_by(ArticleLeanRating.created_at.desc(), ArticleLeanRating.id.desc()).first())


def ask_ai(article):
    """The AI's rating for the dialog: the last one if there is one, else a
    fresh one from the model, stored but not applied."""
    row = latest(article.id, SOURCE_AI)
    if row is not None:
        return row
    from news_fetcher.outlet_bias_llm import get_article_lean_from_llm
    score, reason = get_article_lean_from_llm(article.title, article.content)
    if score is None:
        raise RatingUnavailable()
    row = ArticleLeanRating(article_id=article.id, source=SOURCE_AI, score=score, reason=reason)
    db.session.add(row)
    db.session.commit()
    return row


def apply_ai(article, score, reason=None):
    """Store and apply a fresh AI rating (the API's rate-bias job and the
    Articles list's Rate bias, which have no dialog to confirm in)."""
    _apply(article, ArticleLeanRating(article_id=article.id, source=SOURCE_AI, score=score, reason=reason))


def save_reader(article, user, score, note=None):
    """Apply the lean a reader saved in the dialog. Saving the AI's own pick
    with no note applies that AI rating rather than adding a reader one."""
    note = (note or "").strip()[:NOTE_MAX] or None
    ai = latest(article.id, SOURCE_AI)
    if ai is not None and ai.score == score and not note:
        _apply(article, ai)
        return ai
    row = ArticleLeanRating(article_id=article.id, source=SOURCE_READER, score=score, reason=note,
                            user_id=user.id if user else None)
    _apply(article, row)
    return row


def _apply(article, row):
    row.applied = True
    db.session.add(row)
    article.bias_score = row.score
    article.bias_source = row.source
    db.session.commit()


def own_rating(article):
    """The article's own rating for the dialog to start from, or None while
    it carries its outlet's: {score, source, note}."""
    if not article.bias_source or article.bias_score is None:
        return None
    row = latest(article.id, article.bias_source)
    note = row.reason if row is not None and row.source == SOURCE_READER else None
    return {"score": int(article.bias_score), "source": article.bias_source, "note": note}


def as_json(row):
    return {"score": row.score, "label": LABELS.get(row.score), "reason": row.reason, "source": row.source}
