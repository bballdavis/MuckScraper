"""Priority outlets: outlets whose stories Headlines and the Swipe deck should
surface even when no other outlet has picked them up.

The Texas Tribune is the first. Most of its stories are Texas-specific, so
they rarely reach the three outlets Trending asks for, and the Swipe deck
(which orders by source count) sank them.

Two layers (Philip, 2026-10-30):
- The admin's list (Settings › Sources): one AppSetting, a JSON list of outlet
  names. Until it is saved, DEFAULT_NAMES. Every reader gets these.
- Each reader's own changes (the profile page, or the API): one AppSetting
  per user (`source_boost_v1:<user id>`), JSON {"add": [...], "remove": [...]}.
  "add" boosts an outlet for this reader, "remove" turns off one the admin
  list boosts for them. A user with no row has exactly the admin's list.

Names match outlets.name, or an article's source when it has no outlet row,
case-insensitively. Copies of another article (aggregator/article_copies.py)
don't count: a copy that carried the outlet's name doesn't mean the outlet
reported the story.

Effects, read live for the reader who asks:
- Trending (aggregator/headline_views.py): a story from a boosted outlet is
  listed with one outlet, and its score gets PRIORITY_BONUS.
- Latest: boosted stories come first, then newest first.
- Swipe deck (aggregator/api/skip_routes.py): those stories come first.
"""
import json
from datetime import datetime, timedelta

from sqlalchemy import func, or_

from aggregator import db
from aggregator.models import AppSetting, Article, Outlet

SETTING_KEY = "source_priority_v1"
USER_KEY_PREFIX = "source_boost_v1:"
# Added to a boosted story's headline score: about what three outlets earn
# (one per outlet), so a boosted single-source story ranks with a 3-outlet one.
PRIORITY_BONUS = 2.0
# Before the admin saves a list. Saving replaces it, and saving nothing turns
# the admin's boost off.
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


def _load_json(key):
    row = AppSetting.query.filter_by(key=key).first()
    if row is None:
        return None
    try:
        return json.loads(row.value) if row.value else None
    except ValueError:
        return None


def _store_json(key, value):
    row = AppSetting.query.filter_by(key=key).first()
    text = json.dumps(value)
    if row is None:
        db.session.add(AppSetting(key=key, value=text))
    else:
        row.value = text
    db.session.commit()


def names():
    """The admin's priority outlet names, in the order they were saved."""
    raw = _load_json(SETTING_KEY)
    if raw is None:
        return list(DEFAULT_NAMES)
    return clean(raw)


def save(raw):
    """Replace the admin's list with `raw` (see clean()). Returns the saved names."""
    value = clean(raw)
    _store_json(SETTING_KEY, value)
    return value


def user_changes(user_id):
    """{"add": [...], "remove": [...]}: this reader's changes to the admin list."""
    raw = _load_json(f"{USER_KEY_PREFIX}{user_id}") or {}
    if not isinstance(raw, dict):
        raw = {}
    return {"add": clean(raw.get("add")), "remove": clean(raw.get("remove"))}


def effective_names(user_id=None):
    """The outlet names boosted for this reader: the admin's list, less what
    they removed, plus what they added. Without a user, the admin's list."""
    base = names()
    if user_id is None:
        return base
    changes = user_changes(user_id)
    removed = {n.lower() for n in changes["remove"]}
    out = [n for n in base if n.lower() not in removed]
    seen = {n.lower() for n in out}
    for name in changes["add"]:
        if name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def set_boost(user_id, name, boosted):
    """Boost or stop boosting `name` for this reader. Returns their effective
    names. Stopping a name the admin list boosts records a removal; boosting
    one the admin list already boosts just drops any removal."""
    name = (name or "").strip()
    if not name:
        return effective_names(user_id)
    key = name.lower()
    changes = user_changes(user_id)
    add = [n for n in changes["add"] if n.lower() != key]
    remove = [n for n in changes["remove"] if n.lower() != key]
    in_admin_list = any(n.lower() == key for n in names())
    if boosted and not in_admin_list:
        add.append(name)
    if not boosted and in_admin_list:
        remove.append(name)
    _store_json(f"{USER_KEY_PREFIX}{user_id}", {"add": add, "remove": remove})
    return effective_names(user_id)


def forget(user_id):
    """Drop a deleted reader's changes."""
    row = AppSetting.query.filter_by(key=f"{USER_KEY_PREFIX}{user_id}").first()
    if row is not None:
        db.session.delete(row)


def boosted_story_ids(story_ids=None, since=None, user_id=None):
    """{story_id} among `story_ids` (all stories when None) that have an
    original article from an outlet boosted for `user_id` (the admin's list
    when None), fetched since `since` if given."""
    wanted = [name.lower() for name in effective_names(user_id)]
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
    for the suggestions on the admin and profile pages."""
    since = datetime.utcnow() - timedelta(days=7)
    rows = (db.session.query(Outlet.name, func.count(Article.id))
            .join(Article, Article.outlet_id == Outlet.id)
            .filter(Article.fetched_at >= since)
            .group_by(Outlet.name)
            .order_by(func.count(Article.id).desc(), Outlet.name.asc())
            .limit(limit).all())
    return [name for name, _ in rows]
