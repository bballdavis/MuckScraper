"""Admin › General: users, the API (pairing, keys, devices, limits) and
sign-in. The same rules as the web pages (blueprints/admin/users.py,
api_access.py, sign_in.py), so either place can be used."""
from datetime import datetime, timedelta

from flask import g
from sqlalchemy import func

from aggregator import appearance_prefs, bookmarks, db, feed_prefs, oidc, oidc_settings, pinned_sources, \
    reading_prefs, secret_box, source_priority
from aggregator.accounts import email_error, other_active_admins, password_error, username_error
from aggregator.models import (API_SCOPES, ApiDevice, ApiPairingCode, Bookmark, BookmarkSnapshot, OIDCIdentity,
                               ROLE_ADMIN, ROLE_LABELS, ROLES, User)

from .. import settings as api_settings
from ..auth import CODE_LENGTH, PAIRING_TTL, create_api_key, create_pairing_code
from . import ADMIN_SCOPE, page
from . import schema as s
from .schema import FieldError


def _active_users():
    return User.query.filter_by(is_active=True).order_by(func.lower(User.username)).all()


def _user_choices():
    return [(u.id, u.username, u.role_label) for u in _active_users()]


def _active_user(values, key="user_id"):
    raw = values.get(key)
    user = User.query.get(int(raw)) if isinstance(raw, (int, str)) and str(raw).isdigit() else None
    if user is None or not user.is_active:
        raise FieldError("Pick an active user.", key)
    return user


def _size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


# --- Users -----------------------------------------------------------------------------

@page("users", "Users", "general", "users", detail=lambda: str(User.query.count()))
def users(_=None):
    rows = []
    for user in User.query.order_by(func.lower(User.username)).all():
        tag = s.badge("You") if user.id == g.user.id else (None if user.is_active else s.badge("Off", "warn"))
        rows.append(s.item(user.id, user.username, subtitle=user.email, detail=user.role_label,
                           badge=tag, page=f"users/{user.id}"))
    return s.page("users", "Users", [
        s.section("users", items=rows, actions=[s.action("add", "Add a user", fields=[
            s.text("username", "Username", required=True),
            s.email("email", "Email", required=True),
            s.choice("role", "Role", oidc_settings.default_role(), ROLE_LABELS.items()),
            s.password("password", "Password", help="At least 8 characters. They can change it on their profile."),
        ])], footer="Readers read and save; Scrapers can also fetch and summarize; Admins change settings."),
    ])


@users.action("add")
def add_user(_, values):
    username = s.get_str(values, "username", 80)
    address = s.get_str(values, "email", 254)
    role = values.get("role")
    password = values.get("password") or ""
    for key, error in (("username", username_error(username)), ("email", email_error(address)),
                       ("role", None if role in ROLES else "Choose a role."), ("password", password_error(password))):
        if error:
            raise FieldError(error, key)
    user = User(username=username, email=address)
    user.set_role(role)
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    return {"message": f"Added {username} as {user.role_label}.", "open": f"users/{user.id}"}


def _user(arg):
    user = User.query.get(int(arg)) if arg and arg.isdigit() else None
    if user is None:
        raise LookupError(arg)
    return user


def _quota_note(config):
    default = config["bookmark_quota_mb"]
    return f"Blank uses the server's limit ({f'{default} MB' if default else 'unlimited'}); 0 is unlimited."


@page("users/*", "User", "general", "users", listed=False)
def user_page(arg):
    user = _user(arg)
    config = api_settings.load(fresh=True)
    count = Bookmark.query.filter_by(user_id=user.id).count()
    used = db.session.query(func.coalesce(func.sum(BookmarkSnapshot.size_bytes), 0)).join(Bookmark) \
        .filter(Bookmark.user_id == user.id).scalar() or 0
    devices = ApiDevice.query.filter_by(user_id=user.id).all()
    is_self = user.id == g.user.id
    sections = [
        s.section("who", facts=[
            s.fact("Username", user.username),
            s.fact("Signs in with", "Password" if user.password_hash else "Sign-in provider"),
            s.fact("Devices and keys", sum(1 for d in devices if d.is_live)),
        ]),
        s.section("account", "Account", fields=[
            s.email("email", "Email", user.email),
            s.choice("role", "Role", user.role, ROLE_LABELS.items()),
            s.toggle("is_active", "Can sign in", user.is_active),
        ], footer="You can't change your own role or turn your own account off." if is_self else None),
        s.section("password", "Set a new password", fields=[
            s.password("password", "New password", help="At least 8 characters."),
        ], footer="Let them know it, and suggest they change it on their profile."),
        s.section("storage", "Saved stories", facts=[
            s.fact("Saved", count), s.fact("Storage used", _size(int(used))),
        ], fields=[
            s.number("quota_mb", "Storage limit", config["bookmark_quota_overrides"].get(str(user.id)), unit="MB",
                     min=0, max=api_settings.MAX_QUOTA_MB, placeholder="Server's limit"),
        ], footer=_quota_note(config)),
    ]
    if not is_self:
        sections.append(s.section("remove", actions=[s.action(
            "delete", f"Delete {user.username}", role="destructive",
            confirm=f"Delete {user.username}? Their saved stories, NewsBox and settings go too.")]))
    return s.page(f"users/{user.id}", user.username, sections)


@user_page.save
def save_user(arg, values, section):
    user = _user(arg)
    if section == "password":
        password = values.get("password") or ""
        error = password_error(password)
        if error:
            raise FieldError(error, "password")
        user.set_password(password)
        return f"Set a new password for {user.username}."
    if section == "storage":
        config = api_settings.load(fresh=True)
        overrides = dict(config["bookmark_quota_overrides"])
        raw = values.get("quota_mb")
        if raw is None or raw == "":
            overrides.pop(str(user.id), None)
        else:
            overrides[str(user.id)] = s.get_int(values, "quota_mb", 0, api_settings.MAX_QUOTA_MB, "Storage limit")
        config["bookmark_quota_overrides"] = overrides
        api_settings.save(config)
        return "Storage limit saved."
    address = s.get_str(values, "email", 254) if "email" in values else user.email
    role = values.get("role", user.role)
    active = s.get_bool(values, "is_active") if "is_active" in values else user.is_active
    error = email_error(address, exclude_id=user.id)
    if error:
        raise FieldError(error, "email")
    if role not in ROLES:
        raise FieldError("Choose a role.", "role")
    if user.id == g.user.id and (role != user.role or not active):
        raise FieldError("You can't change your own role or turn your own account off.")
    removes_admin = user.role == ROLE_ADMIN and user.is_active and (role != ROLE_ADMIN or not active)
    if removes_admin and other_active_admins(user.id) == 0:
        raise FieldError("There must always be at least one active admin.")
    user.email = address
    user.set_role(role)
    user.is_active = active
    return f"Saved {user.username}."


@user_page.action("delete")
def delete_user(arg, values):
    user = _user(arg)
    if user.id == g.user.id:
        raise FieldError("You can't delete your own account.")
    if user.role == ROLE_ADMIN and user.is_active and other_active_admins(user.id) == 0:
        raise FieldError("There must always be at least one active admin.")
    for prefs in (feed_prefs, appearance_prefs, reading_prefs, source_priority, pinned_sources):
        prefs.forget(user.id)
    name = user.username
    db.session.delete(user)
    return {"message": f"Deleted {name}.", "close": True}


# --- API -------------------------------------------------------------------------------

LIMIT_LABELS = {
    "per_device_per_minute": "Requests a minute, per device or key",
    "per_ip_per_minute": "Requests a minute, per IP address",
    "pair_per_ip_per_hour": "Pairing tries an hour, per IP address",
    "summaries_per_user_per_day": "Stories sent to NewsBox a day, per user",
    "schedule_runs_per_user_per_day": "Scheduled runs started by hand a day, per user",
    "bookmark_snapshots_per_user_per_day": "Saved story snapshots a day, per user",
}
KEY_EXPIRY = {"30": ("30 days", timedelta(days=30)), "90": ("90 days", timedelta(days=90)),
              "365": ("A year", timedelta(days=365)), "never": ("Never", None)}


def _device_item(d):
    kind = "Key" if d.kind == "key" else "Device"
    if d.id == g.device.id:
        tag = s.badge("This phone")
    elif not d.is_live:
        tag = s.badge("Signed out" if d.revoked_at else "Expired", "warn")
    else:
        tag = s.badge("Admin") if ADMIN_SCOPE in d.scope_list else None
    return s.item(d.id, d.name, subtitle=f"{kind} · {d.user.username}", badge=tag, page=f"devices/{d.id}")


@page("api", "API", "general", "api")
def api_page(_=None):
    config = api_settings.load(fresh=True)
    devices = (ApiDevice.query.join(User).order_by(ApiDevice.revoked_at.isnot(None),
                                                   ApiDevice.last_used_at.desc().nullslast()).limit(100).all())
    live = [d for d in devices if d.is_live]
    pending = ApiPairingCode.query.filter(ApiPairingCode.used_at.is_(None),
                                          ApiPairingCode.expires_at > datetime.utcnow()).count()
    users = _user_choices()
    me = str(g.user.id)
    return s.page("api", "API", [
        s.section("status", facts=[
            s.fact("API", "On" if config["enabled"] else "Off", "ok" if config["enabled"] else "bad"),
            s.fact("Devices and keys signed in", len(live)),
            s.fact("Pairing codes waiting", pending),
            s.fact("Saving page captures", "Ready" if bookmarks.capture_available() else "Not running",
                   None if bookmarks.capture_available() else "warn"),
        ]),
        s.section("pair", "Pair a phone", actions=[s.action("pair", "Create a pairing code", fields=[
            s.choice("user_id", "For", me, users),
            s.toggle("summarize", "Can summarize", True, help="Send stories to NewsBox and run scheduled summaries."),
            s.toggle("admin", "Admin settings", False,
                     help="Change the server's settings from the app. Only for an admin."),
        ])], footer=f"A code works once, for {int(PAIRING_TTL.total_seconds() // 60)} minutes. "
                    "Scan its QR code with the phone's camera or the app's Server page."),
        s.section("settings", "Settings", fields=[
            s.toggle("full_text", "Article text", config["full_text"],
                     help="Send the scraped article text. Off sends summaries and the publisher's link only."),
            s.toggle("api_keys", "API keys", config["api_keys"],
                     help="Let scripts and other apps in with a key. Off also stops keys already made."),
            s.url("public_url", "Public address", config["public_url"], placeholder="https://api.example.com",
                  help="Where phones reach the API, put into pairing codes."),
        ], footer="The API applies changes within 15 seconds. Turning it off is on the web only, "
                  "since this phone would lose its way back in."),
        s.section("limits", "Limits", fields=[
            s.number(name, label, config[name], min=1, max=100000) for name, label in LIMIT_LABELS.items()
        ]),
        s.section("storage", "Saved stories", fields=[
            s.number("bookmark_quota_mb", "Storage limit per user", config["bookmark_quota_mb"], unit="MB",
                     min=1, max=api_settings.MAX_QUOTA_MB, placeholder="Unlimited"),
        ], footer="Blank is unlimited. Set a different limit for one person on their page in Users."),
        s.section("keys", "API keys", actions=[s.action("key", "Create an API key", fields=[
            s.text("name", "Name", required=True, help="So you can tell it apart later."),
            s.choice("user_id", "Acts as", me, users),
            s.choice("expires", "Expires", "365", [(k, label) for k, (label, _) in KEY_EXPIRY.items()]),
            s.toggle("summarize", "Can summarize", False),
        ])], footer=None if config["api_keys"] else "Turn API keys on above first."),
        s.section("devices", "Devices and keys", items=[_device_item(d) for d in devices],
                  empty="Nothing paired yet.",
                  actions=[s.action("revoke_all", "Sign out everything", role="destructive",
                                    confirm="Sign out every device and key, this phone too? "
                                            "You'll need a new pairing code to get back in.")] if live else None),
    ])


@api_page.save
def save_api(_, values, section):
    config = api_settings.load(fresh=True)
    if section == "settings":
        address = s.get_str(values, "public_url", 300)
        if address and not address.startswith(("https://", "http://")):
            raise FieldError("The public address must start with https://.", "public_url")
        config.update(full_text=s.get_bool(values, "full_text") if "full_text" in values else config["full_text"],
                      api_keys=s.get_bool(values, "api_keys") if "api_keys" in values else config["api_keys"],
                      public_url=address if "public_url" in values else config["public_url"])
    elif section == "limits":
        for name, label in LIMIT_LABELS.items():
            if name in values:
                config[name] = s.get_int(values, name, 1, 100000, label)
    elif section == "storage":
        raw = values.get("bookmark_quota_mb")
        config["bookmark_quota_mb"] = None if raw in (None, "") else \
            s.get_int(values, "bookmark_quota_mb", 1, api_settings.MAX_QUOTA_MB, "Storage limit")
    else:
        raise FieldError("Nothing to save here.")
    api_settings.save(config)
    return "Saved. The API applies it within 15 seconds."


def _formatted_code(code):
    half = CODE_LENGTH // 2
    return f"{code[:half]}-{code[half:]}"


@api_page.action("pair")
def make_pairing_code(_, values):
    from aggregator.blueprints.admin.api_access import pairing_link
    user = _active_user(values)
    scopes = ["read"] + (["summarize"] if values.get("summarize", True) is True and "summarize" in API_SCOPES else [])
    if values.get("admin") is True:
        if user.role != ROLE_ADMIN:
            raise FieldError(f"{user.username} isn't an admin, so their phone can't have admin settings.", "admin")
        scopes.append(ADMIN_SCOPE)
    code, row = create_pairing_code(user, scopes, g.user.username)
    server = api_settings.load(fresh=True)["public_url"].rstrip("/")
    minutes = int(PAIRING_TTL.total_seconds() // 60)
    return {"secret": {
        "title": "Pairing code",
        "value": _formatted_code(code),
        "note": f"For {user.username}{', with admin settings' if ADMIN_SCOPE in scopes else ''}. "
                f"Works once, for {minutes} minutes.",
        "link": pairing_link(code, server, None),
    }}


@api_page.action("key")
def make_api_key(_, values):
    if not api_settings.load(fresh=True)["api_keys"]:
        raise FieldError("API keys are off. Turn them on under Settings first.")
    user = _active_user(values)
    name = s.get_str(values, "name", 80, required=True, label="Name")
    expiry = s.get_choice(values, "expires", KEY_EXPIRY)
    scopes = ["read"] + (["summarize"] if values.get("summarize") is True else [])
    token, key = create_api_key(user, name, scopes, KEY_EXPIRY[expiry][1])
    return {"secret": {
        "title": "API key",
        "value": token,
        "note": f"{key.name}, acting as {user.username}. Copy it now: it isn't shown again.",
    }}


@api_page.action("revoke_all")
def revoke_all(_, values):
    count = ApiDevice.query.filter(ApiDevice.revoked_at.is_(None)).update(
        {ApiDevice.revoked_at: datetime.utcnow()}, synchronize_session=False)
    return {"message": f"Signed out {count}.", "close": True}


def _device(arg):
    device = ApiDevice.query.get(int(arg)) if arg and arg.isdigit() else None
    if device is None:
        raise LookupError(arg)
    return device


@page("devices/*", "Device", "general", "api", listed=False)
def device_page(arg):
    d = _device(arg)
    scopes = {"read": "Read", "summarize": "Summarize", ADMIN_SCOPE: "Admin settings"}
    facts = [
        s.fact("Kind", "API key" if d.kind == "key" else "Paired device"),
        s.fact("User", d.user.username),
        s.fact("Can", ", ".join(scopes.get(x, x) for x in d.scope_list)),
        s.fact("Paired", s.when(d.created_at)),
        s.fact("Last used", s.when(d.last_used_at)),
        s.fact("Expires", s.when(d.expires_at) if d.expires_at else "Never"),
    ]
    if d.revoked_at:
        facts.append(s.fact("Signed out", s.when(d.revoked_at), "warn"))
    actions = None
    if d.revoked_at is None:
        this = d.id == g.device.id
        actions = [s.action("revoke", "Sign out this phone" if this else f"Sign out {d.name}", role="destructive",
                            confirm="Sign this phone out? You'll need a new pairing code to get back in." if this
                            else f"Sign out {d.name}? It stops working right away.")]
    return s.page(f"devices/{d.id}", d.name, [s.section("device", facts=facts, actions=actions)])


@device_page.action("revoke")
def revoke_device(arg, values):
    d = _device(arg)
    if d.revoked_at is None:
        d.revoked_at = datetime.utcnow()
    return {"message": f"Signed out {d.name}."}


# --- Sign-in ---------------------------------------------------------------------------

@page("signin", "Sign-in", "general", "signin",
      detail=lambda: "OIDC on" if oidc_settings.load()["enabled"] else "Passwords")
def sign_in(_=None):
    values = oidc_settings.load()
    secret = oidc_settings.secret_state()
    live = bool(oidc.current_config(fresh=True))
    bindings = OIDCIdentity.query.join(User).order_by(func.lower(User.username)).all()
    return s.page("signin", "Sign-in", [
        s.section("status", facts=[
            s.fact("OIDC sign-in", "On" if live else "Off", "ok" if live else None),
            s.fact("Password sign-in", "Allowed" if values["allow_password_login"] else "Off"),
            s.fact("Keys encrypted with", secret_box.key_source()),
        ], footer="Turning OIDC on, or password sign-in off, is done on the web: it checks the provider works "
                  "with your own sign-in first, so nobody is locked out."),
        s.section("provider", "Provider", fields=[
            s.text("provider_name", "Button label", values["provider_name"]),
            s.url("issuer_url", "Issuer URL", values["issuer_url"], placeholder="https://auth.example.com"),
            s.text("client_id", "Client ID", values["client_id"]),
            s.secret(oidc_settings.SECRET_KEY, "Client secret", secret["source"] == "saved", secret.get("hint")),
            s.url("redirect_uri", "Callback URL", values["redirect_uri"],
                  help="The web app's /oidc/callback address, as registered with the provider."),
            s.text("scopes", "Scopes", values["scopes"]),
        ]),
        s.section("access", "Who gets in", fields=[
            s.text("groups_claim", "Groups claim", values["groups_claim"]),
            s.multiline("allowed_groups", "Allowed groups", "\n".join(values["allowed_groups"]),
                        help="One a line. Empty lets any identity from the issuer in."),
            s.multiline("admin_groups", "Admin groups",
                        "\n".join(oidc_settings.role_groups(values["role_mapping"], ROLE_ADMIN))),
            s.multiline("scraper_groups", "Scraper groups",
                        "\n".join(oidc_settings.role_groups(values["role_mapping"], "scraper"))),
            s.choice("default_role", "New accounts start as", values["default_role"],
                     [(r, ROLE_LABELS[r]) for r in oidc_settings.DEFAULT_ROLES]),
            s.toggle("create_users", "Create accounts on first sign-in", values["create_users"]),
            s.text("match_claim", "Match existing accounts by", values["match_claim"], placeholder="Don't match",
                   help="A claim such as email or preferred_username, to link a new identity to the account "
                        "with that email or username."),
        ]),
        s.section("session", "Session", fields=[
            s.toggle("auto_login", "Go straight to the provider", values["auto_login"]),
            s.number("session_minutes", "Session length", values["session_lifetime_seconds"] // 60, unit="min",
                     min=1, max=oidc_settings.MAX_LIFETIME // 60),
        ]),
        s.section("linked", "Linked accounts", items=[
            s.item(b.identity_key, b.user.username, subtitle=b.subject, remove="unlink") for b in bindings
        ], empty="No accounts linked.", actions=[s.action("link", "Link an account", fields=[
            s.choice("user_id", "Account", None, _user_choices()),
            s.text("subject", "Provider subject", required=True, help="The identity's sub claim."),
        ])] if live else None),
    ])


@sign_in.save
def save_sign_in(_, values, section):
    current = oidc_settings.load()
    merged = dict(current)
    if section == "provider":
        for name, limit in (("provider_name", 100), ("issuer_url", 500), ("client_id", 500),
                            ("redirect_uri", 500), ("scopes", 500)):
            if name in values:
                merged[name] = s.get_str(values, name, limit)
        merged["scopes"] = " ".join(merged["scopes"].split())
    elif section == "access":
        for name in ("groups_claim", "match_claim"):
            if name in values:
                merged[name] = s.get_str(values, name, 100)
        if "allowed_groups" in values:
            merged["allowed_groups"] = oidc_settings.parse_groups(s.get_str(values, "allowed_groups", 20000))
        admins = oidc_settings.role_groups(current["role_mapping"], ROLE_ADMIN)
        scrapers = oidc_settings.role_groups(current["role_mapping"], "scraper")
        if "admin_groups" in values:
            admins = oidc_settings.parse_groups(s.get_str(values, "admin_groups", 20000))
        if "scraper_groups" in values:
            scrapers = oidc_settings.parse_groups(s.get_str(values, "scraper_groups", 20000))
        merged["role_mapping"] = oidc_settings.mapping_from(admins, scrapers)
        if "default_role" in values:
            merged["default_role"] = s.get_choice(values, "default_role", oidc_settings.DEFAULT_ROLES)
        if "create_users" in values:
            merged["create_users"] = s.get_bool(values, "create_users")
    elif section == "session":
        if "auto_login" in values:
            merged["auto_login"] = s.get_bool(values, "auto_login")
        if "session_minutes" in values:
            merged["session_lifetime_seconds"] = 60 * s.get_int(
                values, "session_minutes", 1, oidc_settings.MAX_LIFETIME // 60, "Session length")
    else:
        raise FieldError("Nothing to save here.")

    new_secret = values.get(oidc_settings.SECRET_KEY) if section == "provider" else None
    if new_secret is not None and not isinstance(new_secret, str):
        raise FieldError("Must be text.", oidc_settings.SECRET_KEY)
    new_secret = (new_secret or "").strip() if new_secret is not None else None
    saved = oidc_settings.secret_state()
    if new_secret is None:
        has_secret = saved["source"] == "saved" and not saved.get("unreadable")
    else:
        has_secret = bool(new_secret)
        if new_secret:
            problem = oidc_settings.secret_error(new_secret)
            if problem:
                raise FieldError(problem, oidc_settings.SECRET_KEY)
    problems = oidc_settings.errors(merged, has_secret)
    if problems:
        raise FieldError(problems[0])
    if new_secret is not None:
        oidc_settings.save_secret(new_secret, g.user.username)
    oidc_settings.save(merged, g.user.username)
    return "Sign-in settings saved. The web app picks them up within 15 seconds."


@sign_in.action("link")
def link_identity(_, values):
    config = oidc.current_config(fresh=True)
    if not config:
        raise FieldError("Turn OIDC sign-in on before linking accounts.")
    user = _active_user(values)
    subject = s.get_str(values, "subject", 255, required=True, label="Subject")
    try:
        result = oidc.bind(user, subject, config)
    except oidc.OIDCRefused:
        raise FieldError("That subject is invalid or already linked to another account.", "subject")
    return {"message": f"{user.username} was already linked to that identity." if result == "exists"
            else f"{user.username} can now sign in with {config['PROVIDER_NAME']}."}


@sign_in.action("unlink")
def unlink_identity(_, values):
    binding = OIDCIdentity.query.get(s.get_str(values, "id", 64, required=True))
    if binding is None:
        raise FieldError("That link is already gone.")
    name = binding.user.username
    db.session.delete(binding)
    return {"message": f"Unlinked {name}."}
