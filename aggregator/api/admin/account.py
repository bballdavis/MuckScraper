"""Your account (the web's Profile): who you are, email and password, and the
preferences the web keeps for you. Any paired device can open it.

Boosted and pinned sources keep their own routes (/me/boosted-sources and
/me/pinned-sources in routes.py); the account carries both lists so the
app's Account screen draws in one request.
"""
from flask import g, jsonify

from aggregator import appearance_prefs, feed_prefs, pinned_sources, reading_prefs, source_priority
from aggregator.accounts import email_error, password_error
from aggregator.models import ROLE_ADMIN

from .. import serializers as ser
from . import can_admin, reply, route
from . import values as v
from .values import Invalid

SCRAPE_LABELS = {"all": "All", "success": "Scraped", "fallback": "Fallback", "blocked": "Blocked",
                 "failed": "Failed", "skipped": "Skipped", "pending": "Waiting"}


def preferences(user):
    feed = feed_prefs.load(user)
    return {
        "start_page": feed["start_page"],
        "range": feed["range"],
        "scrape_status": feed["scrape_status"],
        "hide_single": feed["hide_single"],
        "analysis_layout": appearance_prefs.load(user)["analysis_layout"],
        "auto_read_seconds": reading_prefs.load(user)["auto_read_seconds"],
    }


def _admin_settings():
    """"on" when this device opens the admin pages, "available" for an admin
    on a device paired without them, None for everyone else."""
    if can_admin():
        return "on"
    return "available" if g.user.role == ROLE_ADMIN else None


def _account():
    user = g.user
    return {
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "role_label": user.role_label,
        "created_at": ser.iso(user.created_at),
        "last_login_at": ser.iso(user.last_login_at),
        # No password: the sign-in provider looks after email and password.
        "password_sign_in": bool(user.password_hash),
        "admin_settings": _admin_settings(),
        "preferences": preferences(user),
        "choices": {
            "start_page": v.choices(feed_prefs.START_PAGES.items()),
            "range": v.choices(feed_prefs.RANGES.items()),
            "scrape_status": v.choices((s, SCRAPE_LABELS.get(s, s.capitalize())) for s in feed_prefs.SCRAPE_STATUSES),
            "analysis_layout": v.choices(appearance_prefs.ANALYSIS_LAYOUTS.items()),
            "auto_read_seconds": v.choices(reading_prefs.AUTO_READ_DELAYS.items()),
        },
        "boosted": source_priority.effective_names(user.id),
        "pinned": [ser.outlet(o) for o in pinned_sources.outlets(user.id)],
        "pinned_max": pinned_sources.MAX_PINNED,
    }


@route("/me/account", admin=False)
def account():
    """The signed-in account, its web preferences and its sources."""
    return jsonify(_account())


def _check_password(values):
    if not g.user.password_hash:
        raise Invalid("Your sign-in provider looks after your email and password.")
    if not g.user.check_password(values.get("current_password") or ""):
        raise Invalid("Your current password was not correct.", "current_password")


@route("/me/account/email", methods=("PUT",), admin=False)
def change_email():
    """{email, current_password}"""
    values = v.body()
    _check_password(values)
    address = v.get_str(values, "email", 254, required=True, label="Email")
    error = email_error(address, exclude_id=g.user.id)
    if error:
        raise Invalid(error, "email")
    g.user.email = address
    return reply(_account(), "Email address updated.")


@route("/me/account/password", methods=("PUT",), admin=False)
def change_password():
    """{current_password, new_password, confirm_password}"""
    values = v.body()
    _check_password(values)
    new = values.get("new_password") or ""
    error = password_error(new, values.get("confirm_password") or "")
    if error:
        raise Invalid(error, "new_password")
    g.user.set_password(new)
    return reply(_account(), "Password changed.")


@route("/me/account/preferences", methods=("PATCH",), admin=False)
def save_preferences():
    """Any of start_page, range, scrape_status, hide_single, analysis_layout, auto_read_seconds."""
    values = v.body()
    known = {"start_page", "range", "scrape_status", "hide_single", "analysis_layout", "auto_read_seconds"}
    unknown = set(values) - known
    if unknown:
        raise Invalid(f"Unknown setting: {sorted(unknown)[0][:40]}.")
    user = g.user
    feed = feed_prefs.load(user)
    if "start_page" in values:
        feed["start_page"] = v.get_choice(values, "start_page", feed_prefs.START_PAGES)
    if "range" in values:
        feed["range"] = v.get_choice(values, "range", feed_prefs.RANGES)
    if "scrape_status" in values:
        feed["scrape_status"] = v.get_choice(values, "scrape_status", feed_prefs.SCRAPE_STATUSES)
    if "hide_single" in values:
        feed["hide_single"] = v.get_bool(values, "hide_single")
    layout = v.get_choice(values, "analysis_layout", appearance_prefs.ANALYSIS_LAYOUTS) \
        if "analysis_layout" in values else None
    seconds = int(v.get_choice(values, "auto_read_seconds", reading_prefs.AUTO_READ_DELAYS)) \
        if "auto_read_seconds" in values else None
    if known & set(values) - {"analysis_layout", "auto_read_seconds"}:
        feed_prefs.save(user, feed)
    if layout is not None:
        appearance_prefs.save(user, {"analysis_layout": layout})
    if seconds is not None:
        reading_prefs.save(user, {**reading_prefs.load(user), "auto_read_seconds": seconds})
    return reply(_account())
