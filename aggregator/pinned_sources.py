"""Pinned sources: outlets a reader keeps in the menu, each with a page of its
own stories (Philip, 2026-11-01). The Texas Tribune is the first one he wants.

A pin is a per-reader choice, kept separate from the priority outlets in
aggregator/source_priority.py, which only change how stories rank. One
AppSetting per user (`pinned_sources_v1:<user id>`) holds a JSON list of
outlet ids in the order they were pinned. A user with no row has none.

A pinned outlet that has since been merged away or deleted is skipped when
the list is read, and dropped the next time the reader changes it.
"""
import json
from datetime import datetime, timedelta

from sqlalchemy import func

from aggregator import db
from aggregator.models import AppSetting, Article, Outlet

USER_KEY_PREFIX = "pinned_sources_v1:"
# The menu is a short list; a reader who wants more is using it as a feed.
MAX_PINNED = 12


def _key(user_id):
    return f"{USER_KEY_PREFIX}{user_id}"


def ids(user_id):
    """The pinned outlet ids, in pin order. Bad or missing data gives []."""
    row = AppSetting.query.filter_by(key=_key(user_id)).first()
    if row is None or not row.value:
        return []
    try:
        raw = json.loads(row.value)
    except ValueError:
        return []
    out = []
    for value in raw if isinstance(raw, list) else []:
        if isinstance(value, int) and not isinstance(value, bool) and value not in out:
            out.append(value)
    return out


def _store(user_id, outlet_ids):
    row = AppSetting.query.filter_by(key=_key(user_id)).first()
    text = json.dumps(outlet_ids)
    if row is None:
        db.session.add(AppSetting(key=_key(user_id), value=text))
    else:
        row.value = text
    db.session.commit()


def outlets(user_id):
    """The reader's pinned Outlet rows, in pin order."""
    wanted = ids(user_id)
    if not wanted:
        return []
    found = {o.id: o for o in Outlet.query.filter(Outlet.id.in_(wanted)).all()}
    return [found[i] for i in wanted if i in found]


def is_pinned(user_id, outlet_id):
    return outlet_id in ids(user_id)


def set_pinned(user_id, outlet_id, pinned):
    """Pin or unpin one outlet for this reader and return their pinned
    outlets. Pinning an outlet that is already pinned keeps its place; a
    full list refuses another pin (ValueError); an outlet that doesn't exist
    can't be pinned (LookupError)."""
    current = [o.id for o in outlets(user_id)]
    if pinned:
        if outlet_id not in current:
            if Outlet.query.get(outlet_id) is None:
                raise LookupError("No such outlet.")
            if len(current) >= MAX_PINNED:
                raise ValueError(f"You can pin up to {MAX_PINNED} sources.")
            current.append(outlet_id)
    else:
        current = [i for i in current if i != outlet_id]
    _store(user_id, current)
    return outlets(user_id)


def forget(user_id):
    """Drop a deleted reader's pins."""
    row = AppSetting.query.filter_by(key=_key(user_id)).first()
    if row is not None:
        db.session.delete(row)


def candidates(query=None, limit=30):
    """Outlets a reader could pin: those with articles in the last 30 days,
    most articles first, optionally narrowed to names containing `query`."""
    since = datetime.utcnow() - timedelta(days=30)
    rows = (db.session.query(Outlet)
            .join(Article, Article.outlet_id == Outlet.id)
            .filter(Article.fetched_at >= since))
    if query:
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = rows.filter(Outlet.name.ilike(like, escape="\\"))
    return (rows.group_by(Outlet.id)
            .order_by(func.count(Article.id).desc(), Outlet.name.asc())
            .limit(limit).all())
