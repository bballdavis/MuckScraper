"""Per-user reading choices, set under Reading on the profile page.

For now just auto mark-as-read: once a story or article
page has been in front of the reader for this many seconds (counting only
while the tab is visible), static/js/reads.js marks it read and shows a toast
with Undo. 0 turns it off. Stored like appearance_prefs, as one JSON
AppSetting per user (`reading_v1:<user id>`), so it needs no migration.
Signed-out visitors get the defaults, though they never see a read toggle.
"""
import json
import logging

from aggregator import db
from aggregator.models import AppSetting

logger = logging.getLogger(__name__)

KEY_PREFIX = "reading_v1:"

# Seconds -> the label on the profile page's segmented control.
AUTO_READ_DELAYS = {
    0: "Off",
    30: "30s",
    45: "45s",
    60: "1 min",
    120: "2 min",
}

DEFAULTS = {
    "auto_read_seconds": 45,
}


def _key(user_id):
    return f"{KEY_PREFIX}{user_id}"


def clean(raw):
    """`raw` as a full, valid set of choices; bad values fall back one by one."""
    raw = raw or {}
    prefs = dict(DEFAULTS)
    try:
        seconds = int(raw.get("auto_read_seconds"))
    except (TypeError, ValueError):
        seconds = None
    if seconds in AUTO_READ_DELAYS:
        prefs["auto_read_seconds"] = seconds
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
        logger.warning("Unreadable reading settings for user %s; using the defaults", user.id)
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
