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


def _search(query, q):
    """Narrow a ReadItem query to items whose story headline or title, or
    article title or outlet name, contains `q` (case-insensitive)."""
    from aggregator.models import Outlet
    words = (q or "").split()
    if not words:
        return query
    query = (query.outerjoin(Story, ReadItem.story_id == Story.id)
             .outerjoin(Article, ReadItem.article_id == Article.id)
             .outerjoin(Outlet, Article.outlet_id == Outlet.id))
    for word in words:
        like = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.filter(db.or_(Story.headline.ilike(like), Story.title.ilike(like),
                                    Article.title.ilike(like), Outlet.name.ilike(like)))
    return query


def recent(user_id, offset=0, limit=50, kind=None, q=None):
    """The user's read items, newest first, with their story or article (and
    the article's outlet) loaded. `q` searches titles and outlet names."""
    query = _search(ReadItem.query.filter_by(user_id=user_id), q).options(
        joinedload(ReadItem.story), joinedload(ReadItem.article).joinedload(Article.outlet))
    if kind:
        query = query.filter(_column(kind).isnot(None))
    return query.order_by(ReadItem.read_at.desc(), ReadItem.id.desc()).offset(offset).limit(limit).all()


def count(user_id, q=None):
    return _search(ReadItem.query.filter_by(user_id=user_id), q).count()
