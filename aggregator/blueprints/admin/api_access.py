"""Settings › API: the switch, content policy and limits for the API
(aggregator/api/, its own container), paired devices and API keys, and
one-time pairing codes with a QR code for the MuckScroller app. Lives in the
web app, never in the API process."""
from datetime import datetime, timedelta

from flask import flash, make_response, redirect, render_template, request, session, url_for
from flask_login import current_user
from markupsafe import Markup
from sqlalchemy import func

from aggregator import bookmarks, db
from aggregator.api import settings as api_settings
from aggregator.api.auth import CODE_LENGTH, PAIRING_TTL, create_api_key, create_pairing_code
from aggregator.models import API_SCOPES, ApiDevice, ApiPairingCode, Bookmark, BookmarkSnapshot, User
from aggregator.permissions import ROLE_ADMIN, role_required

from . import admin

_NEW_CODE_KEY = "api_new_code"
# How long a new API key lasts, by the form's choice; None never expires.
KEY_EXPIRY = {"30": timedelta(days=30), "90": timedelta(days=90), "365": timedelta(days=365), "never": None}
LIMIT_LABELS = {
    "per_device_per_minute": "Requests per minute, per device or key",
    "per_ip_per_minute": "Requests per minute, per IP address",
    "pair_per_ip_per_hour": "Pairing attempts per hour, per IP address",
    "summaries_per_user_per_day": "Stories sent to NewsBox per day, per user",
    "schedule_runs_per_user_per_day": "Scheduled runs started by hand per day, per user",
    "bookmark_snapshots_per_user_per_day": "Bookmark snapshots per day, per user",
}


def _qr_svg(payload):
    import segno
    return Markup(segno.make(payload, error="m").svg_inline(scale=5, dark="#111", light="#fff"))


def _formatted_code(code):
    half = CODE_LENGTH // 2
    return f"{code[:half]}-{code[half:]}"


@admin.route("/settings/api")
@role_required(ROLE_ADMIN)
def api_page():
    return _render()


@admin.route("/settings/mobile-api")
@role_required(ROLE_ADMIN)
def old_mobile_api_page():
    """This page was Settings › Mobile API before API keys; old links land here."""
    return redirect(url_for("admin.api_page"), code=301)


def _render(new_key=None):
    """The page; `new_key` is a key just created, whose token is shown this
    once (rendered straight from the POST, never kept in the session)."""
    config = api_settings.load(fresh=True)
    devices = (ApiDevice.query.join(User).order_by(ApiDevice.revoked_at.isnot(None), ApiDevice.last_used_at.desc().nullslast())
               .all())
    live = [d for d in devices if d.is_live]
    new_code = session.pop(_NEW_CODE_KEY, None)
    pairing = None
    if new_code:
        server = config["public_url"].rstrip("/")
        payload = f"muckscroller://pair?code={new_code['code']}" + (f"&server={server}" if server else "")
        pairing = {**new_code, "display": _formatted_code(new_code["code"]), "qr": _qr_svg(payload), "server": server,
                   "expires_at": datetime.fromisoformat(new_code["expires_at"])}
    pending = ApiPairingCode.query.filter(ApiPairingCode.used_at.is_(None),
                                          ApiPairingCode.expires_at > datetime.utcnow()).count()
    return render_template(
        "settings_api.html",
        config=config,
        devices=devices,
        live_count=len(live),
        last_request=max((d.last_used_at for d in devices if d.last_used_at), default=None),
        users=User.query.filter_by(is_active=True).order_by(func.lower(User.username)).all(),
        pairing=pairing,
        pending_codes=pending,
        limit_labels=LIMIT_LABELS,
        pairing_minutes=int(PAIRING_TTL.total_seconds() // 60),
        storage=_storage_rows(config),
        capture_live=bookmarks.capture_available(),
        new_key=new_key,
        key_expiry=KEY_EXPIRY,
    )


def _size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


def _storage_rows(config):
    """Per active user: bookmarks kept, storage used and their own limit."""
    counts = dict(db.session.query(Bookmark.user_id, func.count(Bookmark.id)).group_by(Bookmark.user_id).all())
    used = dict(db.session.query(Bookmark.user_id, func.coalesce(func.sum(BookmarkSnapshot.size_bytes), 0))
                .join(BookmarkSnapshot).group_by(Bookmark.user_id).all())
    overrides = config["bookmark_quota_overrides"]
    return [{"user": u, "count": counts.get(u.id, 0), "used": _size(int(used.get(u.id, 0))),
             "override": overrides.get(str(u.id))}
            for u in User.query.filter_by(is_active=True).order_by(func.lower(User.username)).all()]


@admin.route("/settings/api/bookmarks", methods=["POST"])
@role_required(ROLE_ADMIN)
def save_bookmark_storage():
    raw = request.form.get("bookmark_quota_mb", "").strip()
    if raw and (not raw.isdigit() or not 1 <= int(raw) <= api_settings.MAX_QUOTA_MB):
        flash("Limit per user: leave it blank or enter whole megabytes from 1.", "error")
        return redirect(url_for("admin.api_page") + "#bookmarks")
    overrides = {}
    for user in User.query.all():
        value = request.form.get(f"quota_{user.id}", "").strip()
        if not value:
            continue
        if not value.isdigit() or int(value) > api_settings.MAX_QUOTA_MB:
            flash(f"{user.username}: leave it blank, 0 for unlimited, or whole megabytes.", "error")
            return redirect(url_for("admin.api_page") + "#bookmarks")
        overrides[str(user.id)] = int(value)
    # save() merges, so an emptied limit has to be stored as None explicitly.
    config = api_settings.load(fresh=True)
    config.update(bookmark_quota_mb=int(raw) if raw else None, bookmark_quota_overrides=overrides)
    api_settings.save(config)
    db.session.commit()
    flash("Bookmark storage limits saved.", "success")
    return redirect(url_for("admin.api_page") + "#bookmarks")


@admin.route("/settings/api/settings", methods=["POST"])
@role_required(ROLE_ADMIN)
def save_api_settings():
    values = {
        "enabled": request.form.get("enabled") == "on",
        "full_text": request.form.get("full_text") == "on",
        "api_keys": request.form.get("api_keys") == "on",
        "public_url": request.form.get("public_url", "").strip(),
    }
    if values["public_url"] and not values["public_url"].startswith(("https://", "http://")):
        flash("The public address must start with https://.", "error")
        return redirect(url_for("admin.api_page"))
    for name in LIMIT_LABELS:
        raw = request.form.get(name, "").strip()
        if not raw.isdigit() or not 1 <= int(raw) <= 100000:
            flash(f"{LIMIT_LABELS[name]}: enter a whole number from 1 to 100000.", "error")
            return redirect(url_for("admin.api_page"))
        values[name] = int(raw)
    api_settings.save(values)
    db.session.commit()
    flash("API settings saved. The API applies them within 15 seconds.", "success")
    return redirect(url_for("admin.api_page"))


@admin.route("/settings/api/pair", methods=["POST"])
@role_required(ROLE_ADMIN)
def create_api_pairing_code():
    user = User.query.get(request.form.get("user_id", type=int) or 0)
    if user is None or not user.is_active:
        flash("Pick an active user.", "error")
        return redirect(url_for("admin.api_page"))
    code, row = create_pairing_code(user, list(API_SCOPES), current_user.username)
    db.session.commit()
    session[_NEW_CODE_KEY] = {"code": code, "username": user.username, "expires_at": row.expires_at.isoformat()}
    return redirect(url_for("admin.api_page") + "#pair")


@admin.route("/settings/api/keys", methods=["POST"])
@role_required(ROLE_ADMIN)
def create_api_key_route():
    if not api_settings.load(fresh=True)["api_keys"]:
        flash("API keys are off. Turn them on under Settings below first.", "error")
        return redirect(url_for("admin.api_page") + "#keys")
    user = User.query.get(request.form.get("user_id", type=int) or 0)
    name = request.form.get("name", "").strip()
    expiry = request.form.get("expires", "365")
    if user is None or not user.is_active:
        flash("Pick an active user.", "error")
        return redirect(url_for("admin.api_page") + "#keys")
    if not 1 <= len(name) <= 80:
        flash("Give the key a name of 1 to 80 characters, so you can tell it apart later.", "error")
        return redirect(url_for("admin.api_page") + "#keys")
    if expiry not in KEY_EXPIRY:
        flash("Pick when the key expires.", "error")
        return redirect(url_for("admin.api_page") + "#keys")
    scopes = ["read"] + (["summarize"] if request.form.get("summarize") == "on" else [])
    token, key = create_api_key(user, name, scopes, KEY_EXPIRY[expiry])
    db.session.commit()
    response = make_response(_render(new_key={"token": token, "name": key.name, "username": user.username,
                                              "scopes": key.scope_list, "expires_at": key.expires_at}))
    response.headers["Cache-Control"] = "no-store"
    return response


@admin.route("/settings/api/devices/<int:device_id>/revoke", methods=["POST"])
@role_required(ROLE_ADMIN)
def revoke_api_device(device_id):
    device = ApiDevice.query.get_or_404(device_id)
    if device.revoked_at is None:
        device.revoked_at = datetime.utcnow()
        db.session.commit()
        flash(f"Signed out {device.name} ({device.user.username}).", "success")
    return redirect(url_for("admin.api_page") + "#devices")


@admin.route("/settings/api/devices/revoke-all", methods=["POST"])
@role_required(ROLE_ADMIN)
def revoke_all_api_devices():
    count = ApiDevice.query.filter(ApiDevice.revoked_at.is_(None)).update(
        {ApiDevice.revoked_at: datetime.utcnow()}, synchronize_session=False)
    db.session.commit()
    flash(f"Signed out {count} device{'s' if count != 1 else ''} and key{'s' if count != 1 else ''}." if count else "Nothing was signed in.", "success")
    return redirect(url_for("admin.api_page") + "#devices")
