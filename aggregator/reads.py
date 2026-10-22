"""Per-user read state for stories and articles (models.ReadItem).

One store for the web app and /api/v1, so a story marked read on the phone
is gone from Headlines in the browser too. Marking is idempotent: marking
again moves the item to the top of Recently Read; unmarking something that
was never read does nothing. Callers commit.
"""
from datetime import datetime

from sqlalchemy.orm import joinedload

from aggregator import db
from aggregator.models import Article, ReadItem, Story

KINDS = ("story", "article")


def _column(kind):
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    return ReadItem.story_id if kind == "story" else ReadItem.article_id


def get(user_id, kind, item_id):
    return ReadItem.query.filter(ReadItem.user_id == user_id, _column(kind) == item_id).first()


def mark(user_id, kind, item_id, when=None):
    """Mark a story or article read. Returns the ReadItem, or None when the
    story or article doesn't exist."""
    target = (Story if kind == "story" else Article).query.get(item_id) if kind in KINDS else None
    if target is None:
        return None
    row = get(user_id, kind, item_id)
    if row is None:
        row = ReadItem(user_id=user_id, **{f"{kind}_id": item_id})
        db.session.add(row)
    row.read_at = when or datetime.utcnow()
    db.session.flush()
    return row


def unmark(user_id, kind, item_id):
    """Mark it unread again. Returns whether it had been read."""
    deleted = (ReadItem.query.filter(ReadItem.user_id == user_id, _column(kind) == item_id)
               .delete(synchronize_session=False))
    return bool(deleted)


def read_ids(user_id, kind, ids=None):
    """The ids of `kind` this user has read, optionally only among `ids`."""
    column = _column(kind)
    query = db.session.query(column).filter(ReadItem.user_id == user_id, column.isnot(None))
    if ids is not None:
        ids = list(ids)
        if not ids:
            return set()
        query = query.filter(column.in_(ids))
    return {row[0] for row in query.all()}


def recent(user_id, offset=0, limit=50, kind=None):
    """The user's read items, newest first, with their story or article (and
    the article's outlet) loaded."""
    query = ReadItem.query.filter_by(user_id=user_id).options(
        joinedload(ReadItem.story), joinedload(ReadItem.article).joinedload(Article.outlet))
    if kind:
        query = query.filter(_column(kind).isnot(None))
    return query.order_by(ReadItem.read_at.desc(), ReadItem.id.desc()).offset(offset).limit(limit).all()


def count(user_id):
    return ReadItem.query.filter_by(user_id=user_id).count()
