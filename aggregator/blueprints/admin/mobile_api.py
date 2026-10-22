"""Settings › Mobile API: the switch, content policy and limits for the API
(aggregator/api/, its own container), paired devices, and one-time pairing
codes with a QR code for the MuckScroller app. Lives in the web app, never
in the API process."""
from datetime import datetime

from flask import flash, redirect, render_template, request, session, url_for
from flask_login import current_user
from markupsafe import Markup
from sqlalchemy import func

from aggregator import db
from aggregator.api import settings as api_settings
from aggregator.api.auth import CODE_LENGTH, PAIRING_TTL, create_pairing_code
from aggregator.models import API_SCOPES, ApiDevice, ApiPairingCode, User
from aggregator.permissions import ROLE_ADMIN, role_required

from . import admin

_NEW_CODE_KEY = "mobile_api_new_code"
LIMIT_LABELS = {
    "per_device_per_minute": "Requests per minute, per device",
    "per_ip_per_minute": "Requests per minute, per IP address",
    "pair_per_ip_per_hour": "Pairing attempts per hour, per IP address",
}


def _qr_svg(payload):
    import segno
    return Markup(segno.make(payload, error="m").svg_inline(scale=5, dark="#111", light="#fff"))


def _formatted_code(code):
    half = CODE_LENGTH // 2
    return f"{code[:half]}-{code[half:]}"


@admin.route("/settings/mobile-api")
@role_required(ROLE_ADMIN)
def mobile_api_page():
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
        "settings_mobile_api.html",
        config=config,
        devices=devices,
        live_count=len(live),
        last_request=max((d.last_used_at for d in devices if d.last_used_at), default=None),
        users=User.query.filter_by(is_active=True).order_by(func.lower(User.username)).all(),
        pairing=pairing,
        pending_codes=pending,
        limit_labels=LIMIT_LABELS,
        pairing_minutes=int(PAIRING_TTL.total_seconds() // 60),
    )


@admin.route("/settings/mobile-api/settings", methods=["POST"])
@role_required(ROLE_ADMIN)
def save_mobile_api_settings():
    values = {
        "enabled": request.form.get("enabled") == "on",
        "full_text": request.form.get("full_text") == "on",
        "public_url": request.form.get("public_url", "").strip(),
    }
    if values["public_url"] and not values["public_url"].startswith(("https://", "http://")):
        flash("The public address must start with https://.", "error")
        return redirect(url_for("admin.mobile_api_page"))
    for name in LIMIT_LABELS:
        raw = request.form.get(name, "").strip()
        if not raw.isdigit() or not 1 <= int(raw) <= 100000:
            flash(f"{LIMIT_LABELS[name]}: enter a whole number from 1 to 100000.", "error")
            return redirect(url_for("admin.mobile_api_page"))
        values[name] = int(raw)
    api_settings.save(values)
    db.session.commit()
    flash("Mobile API settings saved. The API applies them within 15 seconds.", "success")
    return redirect(url_for("admin.mobile_api_page"))


@admin.route("/settings/mobile-api/pair", methods=["POST"])
@role_required(ROLE_ADMIN)
def create_mobile_pairing_code():
    user = User.query.get(request.form.get("user_id", type=int) or 0)
    if user is None or not user.is_active:
        flash("Pick an active user.", "error")
        return redirect(url_for("admin.mobile_api_page"))
    code, row = create_pairing_code(user, list(API_SCOPES), current_user.username)
    db.session.commit()
    session[_NEW_CODE_KEY] = {"code": code, "username": user.username, "expires_at": row.expires_at.isoformat()}
    return redirect(url_for("admin.mobile_api_page") + "#pair")


@admin.route("/settings/mobile-api/devices/<int:device_id>/revoke", methods=["POST"])
@role_required(ROLE_ADMIN)
def revoke_mobile_device(device_id):
    device = ApiDevice.query.get_or_404(device_id)
    if device.revoked_at is None:
        device.revoked_at = datetime.utcnow()
        db.session.commit()
        flash(f"Signed out {device.name} ({device.user.username}).", "success")
    return redirect(url_for("admin.mobile_api_page") + "#devices")


@admin.route("/settings/mobile-api/devices/revoke-all", methods=["POST"])
@role_required(ROLE_ADMIN)
def revoke_all_mobile_devices():
    count = ApiDevice.query.filter(ApiDevice.revoked_at.is_(None)).update(
        {ApiDevice.revoked_at: datetime.utcnow()}, synchronize_session=False)
    db.session.commit()
    flash(f"Signed out {count} device{'s' if count != 1 else ''}.", "success")
    return redirect(url_for("admin.mobile_api_page") + "#devices")
