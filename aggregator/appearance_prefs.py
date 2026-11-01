"""Per-user appearance choices, set under Appearance on the profile page.

For now just the story page's analysis layout: Cards
(the default) or Sections. Stored like feed_prefs, as one JSON AppSetting per
user (`appearance_v1:<user id>`), so it needs no migration. Signed-out
visitors get the defaults.
"""
import json
import logging

from aggregator import db
from aggregator.models import AppSetting

logger = logging.getLogger(__name__)

KEY_PREFIX = "appearance_v1:"

ANALYSIS_LAYOUTS = {
    "cards": "Cards",
    "sections": "Sections",
}

DEFAULTS = {
    "analysis_layout": "cards",
}


def _key(user_id):
    return f"{KEY_PREFIX}{user_id}"


def clean(raw):
    """`raw` as a full, valid set of choices; bad values fall back one by one."""
    raw = raw or {}
    prefs = dict(DEFAULTS)
    if raw.get("analysis_layout") in ANALYSIS_LAYOUTS:
        prefs["analysis_layout"] = raw["analysis_layout"]
    return prefs


def load(user):
    if user is None or not getattr(user, "is_authenticated", False):
        return dict(DEFAULTS)
    setting = AppSetting.query.filter_by(key=_key(user.id)).first()
    if not setting or not setting.value:
        return dict(DEFAULTS)
    try:
        return clean(json.loads(setting.value))
    except (TypeError, ValueError):
        logger.warning("Unreadable appearance settings for user %s; using the defaults", user.id)
        return dict(DEFAULTS)


def save(user, raw):
    prefs = clean(raw)
    setting = AppSetting.query.filter_by(key=_key(user.id)).first()
    if setting:
        setting.value = json.dumps(prefs)
    else:
        db.session.add(AppSetting(key=_key(user.id), value=json.dumps(prefs)))
    db.session.commit()
    return prefs


def forget(user_id):
    """Drop a deleted user's choices (the caller commits)."""
    AppSetting.query.filter_by(key=_key(user_id)).delete()
