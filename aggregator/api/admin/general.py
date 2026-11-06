"""Admin › General: Overview (health and containers), Users, API and
Sign-in. The same data and rules as blueprints/admin/tools.py, users.py,
api_access.py and sign_in.py."""
import os
from datetime import datetime

import requests
from flask import g, jsonify
from sqlalchemy import func

from aggregator import appearance_prefs, bookmarks, db, feed_prefs, oidc, oidc_settings, pinned_sources, \
    reading_prefs, secret_box, source_priority
from aggregator.accounts import email_error, other_active_admins, password_error, username_error
from aggregator.models import (ADMIN_SCOPE, API_SCOPES, ApiDevice, ApiPairingCode, Bookmark, BookmarkSnapshot,
                               OIDCIdentity, ROLE_ADMIN, ROLE_LABELS, ROLES, User)

from .. import serializers as ser
from .. import settings as api_settings
from ..auth import CODE_LENGTH, PAIRING_TTL, create_api_key, create_pairing_code
from ..errors import ApiError
from . import reply, route
from . import values as v
from .values import Invalid

ROLE_DETAILS = {
    "reader": "Reads and saves stories",
    "scraper": "Also fetches and summarizes",
    "admin": "Also changes settings",
}


def environment():
    """What the API container can't do without the app's environment, for
    the Overview and the pages that need it."""
    return {
        "app_env": "LLM_PROVIDER" in os.environ,
        "same_key": secret_box.same_key_as_web(),
    }


def require_same_key():
    if not secret_box.same_key_as_web():
        raise Invalid("The API can't save keys yet: its container needs the same CONFIG_ENCRYPTION_KEY as the "
                      "web app, or the scheduler couldn't read them. Save this key on the web for now.")


def _active_users():
    return User.query.filter_by(is_active=True).order_by(func.lower(User.username)).all()


def _user_choice(u):
    return {"id": u.id, "username": u.username, "role": u.role, "is_admin": u.role == ROLE_ADMIN}


def _active_user(values, key="user_id"):
    raw = values.get(key)
    user = db.session.get(User, int(raw)) if isinstance(raw, (int, str)) and str(raw).isdigit() else None
    if user is None or not user.is_active:
        raise Invalid("Pick an active user.", key)
    return user


def _size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


# --- Summary (the Settings home's details) -----------------------------------------------

@route("/admin/summary")
def summary():
    """One line of detail for each admin page, for the Settings home."""
    from aggregator import scheduler_tasks
    from aggregator.models import IngestionBlock, PipelineSchedule, PromptTemplate, RssFeed, ScheduledFetch, \
        ScrapeBlocklist, Topic
    from news_fetcher import llm_client as llm
    from .ai import provider_label

    def safe(fn):
        try:
            return fn()
        except Exception:  # a count is a nicety; never fail the home screen for one
            db.session.rollback()
            return None

    return jsonify({
        "users": safe(lambda: User.query.count()),
        "devices": safe(lambda: sum(1 for d in ApiDevice.query.filter(ApiDevice.revoked_at.is_(None)) if d.is_live)),
        "sign_in": safe(lambda: "OIDC" if oidc_settings.load()["enabled"] else "Passwords"),
        "sections": safe(lambda: Topic.query.filter_by(is_active=True).count()),
        "sources": safe(lambda: db.session.query(RssFeed.url).distinct().count() + ScheduledFetch.query.count()),
        "rules": safe(lambda: IngestionBlock.query.count() + ScrapeBlocklist.query.filter_by(is_permanent=True).count()),
        "llm": safe(lambda: provider_label(llm.provider_for_tier(llm.TIER_QUALITY))),
        "prompts": safe(lambda: PromptTemplate.query.count()),
        "runs": safe(lambda: PipelineSchedule.query.filter_by(is_active=True).count()),
        "scheduler_alive": safe(lambda: bool(scheduler_tasks.state().get("alive"))),
        "environment": safe(environment),
    })


# --- Overview ------------------------------------------------------------------------------

def _overview():
    from aggregator.blueprints.admin.config_crud import pipeline_schedule_restart_needed
    from aggregator.blueprints.admin.tools import (_container_rows, _job_queue_counts, _list_containers,
                                                   _next_scheduled_run, _running_operations)
    ops = _running_operations()
    fetching = [op for op in ops if op["kind"] == "fetch"]
    containers = _list_containers()
    rows = _container_rows(containers)
    next_run = _next_scheduled_run()
    return {
        "pipeline": {
            "running": bool(fetching),
            "label": fetching[0]["label"] if fetching else None,
            "next_run": {"at": next_run[0].isoformat(), "full": next_run[1]} if next_run else None,
        },
        "containers": {
            "reachable": bool(containers),
            "running": sum(1 for c in containers if c.get("status") == "running"),
            "items": [{
                "service": c.get("service") or c.get("name"),
                "name": c.get("name"),
                "image": c.get("image_short"),
                "level": c["level"],
                "state": c["state"],
                "since": c.get("since"),
                "restart_count": c.get("restart_count") or 0,
                "oom_killed": bool(c.get("oom_killed")),
                "failing_checks": c.get("health_failing_streak") or 0,
                "note": (c.get("error") or c.get("health_output")) if c["level"] != "ok" else None,
            } for c in rows],
        },
        "job_queue": _job_queue_counts(),
        "also_running": [op["label"] for op in ops if op["kind"] == "other"],
        "schedule_not_applied": pipeline_schedule_restart_needed(),
        "environment": environment(),
    }


@route("/admin/overview")
def overview():
    """Pipeline state, containers, the job queue and anything else running."""
    return jsonify(_overview())


def _restart(path, blocking, force, timeout, label):
    from aggregator.blueprints.admin import DOCKER_RESTART_PROXY_URL
    from aggregator.blueprints.admin.tools import _describe_blocking
    if blocking and not force:
        raise ApiError(409, "busy", f"Can't restart {label} right now. Still running: {_describe_blocking(blocking)}")
    try:
        response = requests.post(f"{DOCKER_RESTART_PROXY_URL}{path}", timeout=timeout)
        response.raise_for_status()
    except requests.RequestException:
        raise Invalid(f"Couldn't restart {label}: the restart service didn't answer.")


@route("/admin/containers/<name>/restart", methods=("POST",))
def restart_container(name):
    """{force}: restart one container. 409 when something it would cut off is running."""
    from aggregator.blueprints.admin.tools import _blocking_operations_for
    force = v.body().get("force") is True
    _restart(f"/containers/{name}/restart", _blocking_operations_for(name), force, 35, name)
    return reply(_overview(), f"Restarted {name}.")


@route("/admin/containers/restart-all", methods=("POST",))
def restart_all_containers():
    """{force}: restart every container, this API included."""
    from aggregator.blueprints.admin import DOCKER_RESTART_PROXY_URL
    from aggregator.blueprints.admin.tools import _describe_blocking, _running_operations
    force = v.body().get("force") is True
    blocking = _running_operations()
    if blocking and not force:
        raise ApiError(409, "busy", f"Can't restart everything right now. Still running: {_describe_blocking(blocking)}")
    try:
        requests.post(f"{DOCKER_RESTART_PROXY_URL}/restart-all", timeout=120)
    except requests.RequestException:
        pass  # expected once this container itself is restarted mid-call
    return reply(_overview(), "Restarting everything. The app reconnects in a minute.")


# --- Users ---------------------------------------------------------------------------------

def _user_row(u, devices=None):
    return {
        "id": u.id,
        "username": u.username,
        "email": u.email,
        "role": u.role,
        "role_label": u.role_label,
        "is_active": bool(u.is_active),
        "is_self": u.id == g.user.id,
        "password_sign_in": bool(u.password_hash),
        "created_at": ser.iso(u.created_at),
        "last_login_at": ser.iso(u.last_login_at),
        "devices": devices if devices is not None else sum(1 for d in u.api_devices if d.is_live),
    }


def _role_choices():
    return v.choices((r, ROLE_LABELS[r], ROLE_DETAILS.get(r)) for r in ROLES)


def _users():
    users = User.query.order_by(func.lower(User.username)).all()
    return {"items": [_user_row(u) for u in users], "roles": _role_choices(),
            "default_role": oidc_settings.default_role()}


def _user(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        raise ApiError(404, "not_found", "That user is gone. They may have been deleted on the web.")
    return user


def _user_detail(user):
    config = api_settings.load(fresh=True)
    used = db.session.query(func.coalesce(func.sum(BookmarkSnapshot.size_bytes), 0)).join(Bookmark) \
        .filter(Bookmark.user_id == user.id).scalar() or 0
    devices = ApiDevice.query.filter_by(user_id=user.id).order_by(ApiDevice.last_used_at.desc().nullslast()).all()
    return {
        **_user_row(user, sum(1 for d in devices if d.is_live)),
        "roles": _role_choices(),
        "saved_stories": Bookmark.query.filter_by(user_id=user.id).count(),
        "storage_used": _size(int(used)),
        "quota_mb": config["bookmark_quota_overrides"].get(str(user.id)),
        "server_quota_mb": config["bookmark_quota_mb"],
        "max_quota_mb": api_settings.MAX_QUOTA_MB,
        "device_list": [_device(d) for d in devices],
    }


@route("/admin/users")
def users():
    """Every account, with roles to choose from."""
    return jsonify(_users())


@route("/admin/users", methods=("POST",))
def add_user():
    """{username, email, role, password}"""
    values = v.body()
    username = v.get_str(values, "username", 80)
    address = v.get_str(values, "email", 254)
    role = values.get("role")
    password = values.get("password") or ""
    for key, error in (("username", username_error(username)), ("email", email_error(address)),
                       ("role", None if role in ROLES else "Choose a role."), ("password", password_error(password))):
        if error:
            raise Invalid(error, key)
    user = User(username=username, email=address)
    user.set_role(role)
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    return reply(_users(), f"Added {username} as {user.role_label}.")


@route("/admin/users/<int:user_id>")
def user(user_id):
    """One account: its details, saved-story storage and devices."""
    return jsonify(_user_detail(_user(user_id)))


@route("/admin/users/<int:user_id>", methods=("PATCH",))
def update_user(user_id):
    """Any of email, role, is_active, quota_mb (null: the server's limit; 0: unlimited)."""
    user = _user(user_id)
    values = v.body()
    if "quota_mb" in values:
        config = api_settings.load(fresh=True)
        overrides = dict(config["bookmark_quota_overrides"])
        quota = v.get_optional_int(values, "quota_mb", 0, api_settings.MAX_QUOTA_MB, "Storage limit")
        if quota is None:
            overrides.pop(str(user.id), None)
        else:
            overrides[str(user.id)] = quota
        config["bookmark_quota_overrides"] = overrides
        api_settings.save(config)
    if values.keys() & {"email", "role", "is_active"}:
        address = v.get_str(values, "email", 254) if "email" in values else user.email
        role = values.get("role", user.role)
        active = v.get_bool(values, "is_active") if "is_active" in values else bool(user.is_active)
        error = email_error(address, exclude_id=user.id)
        if error:
            raise Invalid(error, "email")
        if role not in ROLES:
            raise Invalid("Choose a role.", "role")
        if user.id == g.user.id and (role != user.role or not active):
            raise Invalid("You can't change your own role or turn your own account off.",
                          "role" if role != user.role else "is_active")
        removes_admin = user.role == ROLE_ADMIN and user.is_active and (role != ROLE_ADMIN or not active)
        if removes_admin and other_active_admins(user.id) == 0:
            raise Invalid("There must always be at least one active admin.")
        user.email = address
        user.set_role(role)
        user.is_active = active
    return reply(_user_detail(user), f"Saved {user.username}.")


@route("/admin/users/<int:user_id>/password", methods=("PUT",))
def set_user_password(user_id):
    """{password}: set a new password for someone."""
    user = _user(user_id)
    password = v.body().get("password") or ""
    error = password_error(password)
    if error:
        raise Invalid(error, "password")
    user.set_password(password)
    return reply(_user_detail(user), f"Set a new password for {user.username}. Let them know it, and suggest "
                                     "they change it on their profile.")


@route("/admin/users/<int:user_id>", methods=("DELETE",))
def delete_user(user_id):
    """Delete an account, with its saved stories, NewsBox and settings."""
    user = _user(user_id)
    if user.id == g.user.id:
        raise Invalid("You can't delete your own account.")
    if user.role == ROLE_ADMIN and user.is_active and other_active_admins(user.id) == 0:
        raise Invalid("There must always be at least one active admin.")
    for prefs in (feed_prefs, appearance_prefs, reading_prefs, source_priority, pinned_sources):
        prefs.forget(user.id)
    name = user.username
    db.session.delete(user)
    db.session.flush()
    return reply(_users(), f"Deleted {name}.")


# --- API -----------------------------------------------------------------------------------

LIMIT_LABELS = {
    "per_device_per_minute": "Requests a minute, per device or key",
    "per_ip_per_minute": "Requests a minute, per IP address",
    "pair_per_ip_per_hour": "Pairing tries an hour, per IP address",
    "summaries_per_user_per_day": "Stories sent to NewsBox a day, per user",
    "schedule_runs_per_user_per_day": "Scheduled runs started by hand a day, per user",
    "bookmark_snapshots_per_user_per_day": "Saved story snapshots a day, per user",
}
KEY_EXPIRY = {"30": "30 days", "90": "90 days", "365": "A year", "never": "Never"}
SCOPE_LABELS = {"read": "Read", "summarize": "Summarize", ADMIN_SCOPE: "Admin settings"}


def _device(d):
    return {
        "id": d.id,
        "name": d.name,
        "kind": d.kind,
        "user_id": d.user_id,
        "username": d.user.username if d.user else None,
        "user_is_admin": bool(d.user and d.user.role == ROLE_ADMIN),
        "scopes": [SCOPE_LABELS.get(s, s) for s in d.scope_list],
        "admin": ADMIN_SCOPE in d.scope_list,
        "created_at": ser.iso(d.created_at),
        "last_used_at": ser.iso(d.last_used_at),
        "expires_at": ser.iso(d.expires_at),
        "revoked_at": ser.iso(d.revoked_at),
        "is_live": d.is_live,
        "is_this_device": d.id == g.device.id,
    }


def _api_page():
    from aggregator.blueprints.admin.api_access import _storage_rows
    config = api_settings.load(fresh=True)
    devices = (ApiDevice.query.join(User).order_by(ApiDevice.revoked_at.isnot(None),
                                                   ApiDevice.last_used_at.desc().nullslast()).limit(200).all())
    pending = ApiPairingCode.query.filter(ApiPairingCode.used_at.is_(None),
                                          ApiPairingCode.expires_at > datetime.utcnow()).count()
    return {
        "settings": {
            "enabled": config["enabled"],
            "full_text": config["full_text"],
            "api_keys": config["api_keys"],
            "public_url": config["public_url"],
            "limits": {name: config[name] for name in LIMIT_LABELS},
        },
        # A list, beside the limits object, so a client that converts key
        # case can still match each limit to its value.
        "limit_fields": [{"key": k, "label": label, "value": config[k]} for k, label in LIMIT_LABELS.items()],
        "live_count": sum(1 for d in devices if d.is_live),
        "pending_codes": pending,
        "last_request": ser.iso(max((d.last_used_at for d in devices if d.last_used_at), default=None)),
        "capture_ready": bookmarks.capture_available(),
        "devices": [_device(d) for d in devices],
        "users": [_user_choice(u) for u in _active_users()],
        "storage": {
            "default_mb": config["bookmark_quota_mb"],
            "max_mb": api_settings.MAX_QUOTA_MB,
            "rows": [{"user_id": r["user"].id, "username": r["user"].username, "count": r["count"],
                      "used": r["used"], "override_mb": r["override"]} for r in _storage_rows(config)],
        },
        "pairing_minutes": int(PAIRING_TTL.total_seconds() // 60),
        "key_expiry": v.choices(KEY_EXPIRY.items()),
    }


@route("/admin/api")
def api_page():
    """The API's switches, limits, devices, keys and saved-story storage."""
    return jsonify(_api_page())


@route("/admin/api/settings", methods=("PATCH",))
def save_api_settings():
    """Any of enabled, full_text, api_keys, public_url, limits {name: number}."""
    values = v.body()
    config = api_settings.load(fresh=True)
    for name in ("enabled", "full_text", "api_keys"):
        if name in values:
            config[name] = v.get_bool(values, name)
    if "public_url" in values:
        address = v.get_str(values, "public_url", 300)
        if address and not address.startswith(("https://", "http://")):
            raise Invalid("The public address must start with https://.", "public_url")
        config["public_url"] = address
    limits = values.get("limits")
    if limits is not None:
        if not isinstance(limits, dict):
            raise Invalid("Send limits as an object.", "limits")
        for name, label in LIMIT_LABELS.items():
            if name in limits:
                config[name] = v.get_int(limits, name, 1, 100000, label)
    api_settings.save(config)
    message = "Saved. The API applies it within 15 seconds."
    if values.get("enabled") is False:
        message = "The API turns off within 15 seconds. Turn it back on from the web's Settings › API."
    return reply(_api_page(), message)


@route("/admin/api/storage", methods=("PATCH",))
def save_storage():
    """{default_mb: number or null}: the saved-story limit per user. One person's is on their user page."""
    values = v.body()
    config = api_settings.load(fresh=True)
    config["bookmark_quota_mb"] = v.get_optional_int(values, "default_mb", 1, api_settings.MAX_QUOTA_MB,
                                                     "Storage limit")
    api_settings.save(config)
    return reply(_api_page(), "Storage limit saved.")


def _formatted_code(code):
    half = CODE_LENGTH // 2
    return f"{code[:half]}-{code[half:]}"


@route("/admin/api/pairing-codes", methods=("POST",))
def create_pairing():
    """{user_id, admin, site, server}: a one-time pairing code, its display form and the QR link."""
    from aggregator.blueprints.admin.api_access import pairing_link
    values = v.body()
    user = _active_user(values)
    scopes = list(API_SCOPES)
    if values.get("admin") is True:
        if user.role != ROLE_ADMIN:
            raise Invalid(f"{user.username} isn't an admin, so their phone can't have admin settings.", "admin")
        scopes.append(ADMIN_SCOPE)
    code, row = create_pairing_code(user, scopes, g.user.username)
    def address(key):
        value = values.get(key)
        value = value.strip().rstrip("/") if isinstance(value, str) else ""
        return value if value.startswith(("https://", "http://")) else ""

    # The phone making the code knows both addresses, so a server without a
    # public address set still gets a link another phone can pair from.
    server = api_settings.load(fresh=True)["public_url"].rstrip("/") or address("server")
    site = address("site")
    db.session.commit()
    return jsonify({
        "code": code,
        "display": _formatted_code(code),
        "link": pairing_link(code, server, site or None),
        "username": user.username,
        "admin": ADMIN_SCOPE in scopes,
        "expires_at": ser.iso(row.expires_at),
    }), 201


@route("/admin/api/keys", methods=("POST",))
def create_key():
    """{user_id, name, expires (30, 90, 365, never), summarize}: a new API key, shown once."""
    from datetime import timedelta
    values = v.body()
    if not api_settings.load(fresh=True)["api_keys"]:
        raise Invalid("API keys are off. Turn them on first.")
    user = _active_user(values)
    name = v.get_str(values, "name", 80, required=True, label="The name")
    expiry = v.get_choice(values, "expires", KEY_EXPIRY, "Pick when the key expires.")
    ttl = None if expiry == "never" else timedelta(days=int(expiry))
    scopes = ["read"] + (["summarize"] if values.get("summarize") is True else [])
    token, key = create_api_key(user, name, scopes, ttl)
    db.session.commit()
    return jsonify({"token": token, "name": key.name, "username": user.username,
                    "scopes": [SCOPE_LABELS.get(s, s) for s in key.scope_list],
                    "expires_at": ser.iso(key.expires_at)}), 201


def _device_row(device_id):
    device = db.session.get(ApiDevice, device_id)
    if device is None:
        raise ApiError(404, "not_found", "That device is gone.")
    return device


@route("/admin/api/devices/<int:device_id>/revoke", methods=("POST",))
def revoke_device(device_id):
    """Sign out one device or key."""
    device = _device_row(device_id)
    if device.revoked_at is None:
        device.revoked_at = datetime.utcnow()
    return reply(_api_page(), f"Signed out {device.name} ({device.user.username}).")


@route("/admin/api/devices/<int:device_id>/admin", methods=("PUT",))
def set_device_admin(device_id):
    """{admin}: give a paired admin's phone admin settings, or take them away."""
    device = _device_row(device_id)
    on = v.get_bool(v.body(), "admin")
    if device.kind == "key":
        raise Invalid("API keys can't have admin settings.", "admin")
    if on and (device.user is None or device.user.role != ROLE_ADMIN):
        raise Invalid(f"{device.user.username} isn't an admin, so their phone can't have admin settings.", "admin")
    if not on and device.id == g.device.id:
        raise Invalid("Turn admin settings off for this phone from another device or the web, so you don't lock "
                      "yourself out here.", "admin")
    scopes = [s for s in device.scope_list if s != ADMIN_SCOPE] + ([ADMIN_SCOPE] if on else [])
    device.scopes = " ".join(scopes)
    return reply(_api_page(), f"{device.name} {'can' if on else 'can no longer'} change the server's settings.")


@route("/admin/api/devices/revoke-all", methods=("POST",))
def revoke_all_devices():
    """Sign out every device and key, this one too."""
    count = ApiDevice.query.filter(ApiDevice.revoked_at.is_(None)).update(
        {ApiDevice.revoked_at: datetime.utcnow()}, synchronize_session=False)
    db.session.commit()
    return jsonify({"message": f"Signed out {count}.", "data": None})


# --- Sign-in -------------------------------------------------------------------------------

CALLBACK_PATH = "/auth/oidc/callback"


def _sign_in_page():
    values = oidc_settings.load()
    secret = oidc_settings.secret_state()
    bindings = OIDCIdentity.query.join(User).order_by(func.lower(User.username)).all()
    return {
        "settings": {
            "enabled": values["enabled"],
            "issuer_url": values["issuer_url"],
            "client_id": values["client_id"],
            "redirect_uri": values["redirect_uri"],
            "provider_name": values["provider_name"],
            "scopes": values["scopes"],
            "groups_claim": values["groups_claim"],
            "allowed_groups": values["allowed_groups"],
            "admin_groups": oidc_settings.role_groups(values["role_mapping"], ROLE_ADMIN),
            "scraper_groups": oidc_settings.role_groups(values["role_mapping"], "scraper"),
            "default_role": values["default_role"],
            "create_users": values["create_users"],
            "match_claim": values["match_claim"],
            "allow_password_login": values["allow_password_login"],
            "auto_login": values["auto_login"],
            "session_minutes": values["session_lifetime_seconds"] // 60,
        },
        "secret": {"saved": secret["source"] == "saved", "hint": secret.get("hint") or None,
                   "unreadable": bool(secret.get("unreadable"))},
        "live": bool(oidc.current_config(fresh=True)),
        "key_source": secret_box.key_source(),
        "callback_path": CALLBACK_PATH,
        "default_roles": v.choices((r, ROLE_LABELS[r]) for r in oidc_settings.DEFAULT_ROLES),
        "match_claims": list(oidc_settings.COMMON_MATCH_CLAIMS),
        "max_session_minutes": oidc_settings.MAX_LIFETIME // 60,
        # The web lets an admin turn password sign-in off only from a provider
        # session; from the app, only once their own account is linked to one.
        "you_are_linked": any(b.user_id == g.user.id for b in bindings),
        "linked": [{"key": b.identity_key, "user_id": b.user_id, "username": b.user.username, "subject": b.subject}
                   for b in bindings],
        "users": [_user_choice(u) for u in _active_users()],
    }


@route("/admin/sign-in")
def sign_in():
    """OIDC sign-in settings and the accounts linked to a provider identity."""
    return jsonify(_sign_in_page())


def _groups(values, key, current):
    if key not in values:
        return current
    raw = values[key]
    if isinstance(raw, list):
        raw = "\n".join(str(x) for x in raw)
    if not isinstance(raw, str) or len(raw) > 20000:
        raise Invalid("Send the groups as a list.", key)
    return oidc_settings.parse_groups(raw)


@route("/admin/sign-in", methods=("PATCH",))
def save_sign_in():
    """Any of the settings fields, and client_secret ("" removes it)."""
    values = v.body()
    current = oidc_settings.load()
    merged = dict(current)
    for name, limit in (("provider_name", 100), ("issuer_url", 500), ("client_id", 500), ("redirect_uri", 500),
                        ("scopes", 500), ("groups_claim", 100), ("match_claim", 100)):
        if name in values:
            merged[name] = v.get_str(values, name, limit)
    merged["scopes"] = " ".join(merged["scopes"].split())
    merged["allowed_groups"] = _groups(values, "allowed_groups", current["allowed_groups"])
    admins = _groups(values, "admin_groups", oidc_settings.role_groups(current["role_mapping"], ROLE_ADMIN))
    scrapers = _groups(values, "scraper_groups", oidc_settings.role_groups(current["role_mapping"], "scraper"))
    merged["role_mapping"] = oidc_settings.mapping_from(admins, scrapers)
    if "default_role" in values:
        merged["default_role"] = v.get_choice(values, "default_role", oidc_settings.DEFAULT_ROLES)
    for name in ("enabled", "create_users", "allow_password_login", "auto_login"):
        if name in values:
            merged[name] = v.get_bool(values, name)
    if "session_minutes" in values:
        merged["session_lifetime_seconds"] = 60 * v.get_int(values, "session_minutes", 1,
                                                            oidc_settings.MAX_LIFETIME // 60, "Session length")

    new_secret = values.get(oidc_settings.SECRET_KEY, values.get("client_secret"))
    if new_secret is not None:
        if not isinstance(new_secret, str):
            raise Invalid("Must be text.", "client_secret")
        new_secret = new_secret.strip()
        if new_secret:
            problem = oidc_settings.secret_error(new_secret)
            if problem:
                raise Invalid(problem, "client_secret")
            require_same_key()
    saved = oidc_settings.secret_state()
    has_secret = bool(new_secret) if new_secret is not None else (
        saved["source"] == "saved" and not saved.get("unreadable"))
    problems = oidc_settings.errors(merged, has_secret)
    if merged["enabled"] and not current["enabled"] and not merged["redirect_uri"].startswith("https://"):
        # The web refuses to turn sign-in on over plain HTTP, since its cookies
        # become HTTPS-only. From the app, the callback address stands for it.
        problems.append("Set the callback URL to the site's https:// address to turn OIDC sign-in on.")
    if merged["enabled"] and not merged["allow_password_login"] and current["allow_password_login"] \
            and not OIDCIdentity.query.filter_by(user_id=g.user.id).first():
        problems.append("Sign in with the provider on the web as an admin before turning password sign-in off, "
                        "so you know it works and don't lock yourself out.")
    if problems:
        raise Invalid(problems[0])
    if new_secret is not None:
        oidc_settings.save_secret(new_secret, g.user.username)
    oidc_settings.save(merged, g.user.username)
    return reply(_sign_in_page(), "Sign-in settings saved. The web app picks them up within 15 seconds.")


@route("/admin/sign-in/links", methods=("POST",))
def link_identity():
    """{user_id, subject}: let an account sign in with a provider identity."""
    values = v.body()
    config = oidc.current_config(fresh=True)
    if not config:
        raise Invalid("Turn OIDC sign-in on before linking accounts.")
    user = _active_user(values)
    subject = v.get_str(values, "subject", 255, required=True, label="The subject")
    try:
        result = oidc.bind(user, subject, config)
    except oidc.OIDCRefused:
        db.session.rollback()
        raise Invalid("That subject is invalid or already linked to another account.", "subject")
    return reply(_sign_in_page(), f"{user.username} was already linked to that identity." if result == "exists"
                 else f"{user.username} can now sign in with {config['PROVIDER_NAME']}.")


@route("/admin/sign-in/links/<key>", methods=("DELETE",))
def unlink_identity(key):
    """Unlink one account from its provider identity."""
    binding = db.session.get(OIDCIdentity, key)
    if binding is None:
        raise ApiError(404, "not_found", "That link is already gone.")
    name = binding.user.username
    db.session.delete(binding)
    db.session.flush()
    return reply(_sign_in_page(), f"Unlinked {name}. A provider session using that identity ends on its next request.")
