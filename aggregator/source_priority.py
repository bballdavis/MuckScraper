"""Priority outlets (Philip, 2026-10-30): outlets whose stories Headlines and
the Swipe deck should surface even when no other outlet has picked them up.

The Texas Tribune is the first. Most of its stories are Texas-specific, so
they rarely reach the three outlets Trending asks for and sank in the Swipe
deck, which orders by number of sources.

Stored as one AppSetting: a JSON list of outlet names, as they appear on
outlets.name. Matching is case-insensitive, and an article with no outlet row
matches on its own source name. Copies of another article (aggregator/
article_copies.py) don't count: a copy that carried the outlet's name doesn't
mean the outlet reported the story.

Effects, both read live:
- Trending (aggregator/headline_views.py): a story with an article from a
  priority outlet is listed with one outlet, and its score gets PRIORITY_BONUS.
- Swipe deck (aggregator/api/skip_routes.py): those stories come first.
"""
import json
from datetime import datetime, timedelta

from sqlalchemy import func, or_

from aggregator import db
from aggregator.models import AppSetting, Article, Outlet

SETTING_KEY = "source_priority_v1"
# Added to a priority story's headline score: about what three outlets earn
# (one per outlet), so a boosted single-source story ranks with a 3-outlet one.
PRIORITY_BONUS = 2.0
# Before anyone saves the list. Saving replaces it, and saving nothing turns
# the boost off.
DEFAULT_NAMES = ["The Texas Tribune"]


def clean(raw):
    """Outlet names from a list or a comma/newline-separated string: trimmed,
    blanks and repeats (case-insensitive) dropped, first spelling kept."""
    if isinstance(raw, str):
        raw = raw.replace("\n", ",").split(",")
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for name in raw:
        if not isinstance(name, str):
            continue
        name = name.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def names():
    """The priority outlet names, in the order they were saved."""
    row = AppSetting.query.filter_by(key=SETTING_KEY).first()
    if row is None:
        return list(DEFAULT_NAMES)
    if not row.value:
        return []
    try:
        return clean(json.loads(row.value))
    except ValueError:
        return []


def save(raw):
    """Replace the list with `raw` (see clean()). Returns the saved names."""
    value = clean(raw)
    row = AppSetting.query.filter_by(key=SETTING_KEY).first()
    if row is None:
        db.session.add(AppSetting(key=SETTING_KEY, value=json.dumps(value)))
    else:
        row.value = json.dumps(value)
    db.session.commit()
    return value


def boosted_story_ids(story_ids=None, since=None):
    """{story_id} among `story_ids` (all stories when None) that have an
    original article from a priority outlet, fetched since `since` if given."""
    wanted = [name.lower() for name in names()]
    if not wanted:
        return set()
    query = (db.session.query(Article.story_id)
             .outerjoin(Outlet, Outlet.id == Article.outlet_id)
             .filter(Article.story_id.isnot(None), Article.copy_of_id.is_(None))
             .filter(or_(func.lower(Outlet.name).in_(wanted),
                         func.lower(func.trim(Article.source)).in_(wanted))))
    if since is not None:
        query = query.filter(Article.fetched_at >= since)
    if story_ids is not None:
        ids = list(story_ids)
        if not ids:
            return set()
        query = query.filter(Article.story_id.in_(ids))
    return {sid for (sid,) in query.distinct().all()}


def outlet_suggestions(limit=300):
    """Outlet names with articles in the last 7 days, most articles first,
    for the Sources page's suggestions."""
    since = datetime.utcnow() - timedelta(days=7)
    rows = (db.session.query(Outlet.name, func.count(Article.id))
            .join(Article, Article.outlet_id == Outlet.id)
            .filter(Article.fetched_at >= since)
            .group_by(Outlet.name)
            .order_by(func.count(Article.id).desc(), Outlet.name.asc())
            .limit(limit).all())
    return [name for name, _ in rows]
