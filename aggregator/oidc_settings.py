"""OIDC sign-in settings, set on Settings › Sign-in (blueprints/admin/sign_in.py)
instead of environment variables.

Everything but the client secret is one JSON AppSetting, like the API's
settings (aggregator/api/settings.py). The client secret is a ConfigSetting
row encrypted with aggregator/secret_box.py, as the LLM and source API keys
are: write-only, never sent back to the browser (only its last four
characters), never logged, and with no environment fallback.

aggregator/oidc.py reads runtime() at most every RELOAD_SECONDS, so a change
reaches every web worker within that time without a restart; save() resets
this worker's copy at once.
"""
import json
import logging
import re
import time
from datetime import datetime
from urllib.parse import urlsplit

from aggregator import db, secret_box
from aggregator.models import ROLES, AppSetting, ConfigSetting

logger = logging.getLogger(__name__)

KEY = "oidc_v1"
SECRET_KEY = "OIDC_CLIENT_SECRET"
RELOAD_SECONDS = 15

DEFAULTS = {
    # Off: password sign-in only, and any OIDC session ends on its next request.
    "enabled": False,
    "issuer_url": "",
    "client_id": "",
    "redirect_uri": "",
    "provider_name": "OpenID Connect",
    "scopes": "openid profile email groups",
    "groups_claim": "groups",
    # Empty accepts any verified identity from the issuer.
    "allowed_groups": [],
    # group -> role. Empty leaves roles to Settings › Users; non-empty is
    # applied at every OIDC sign-in, highest role wins, unmapped is Reader.
    "role_mapping": {},
    "create_users": False,
    "allow_password_login": True,
    "auto_login": False,
    "session_lifetime_seconds": 3600,
}
MIN_LIFETIME, MAX_LIFETIME = 60, 86400

_GROUP_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_CLAIM_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,100}$")
_cache = {"at": 0.0, "value": None}


def https_url_error(value, label):
    """An error message unless `value` is an HTTPS URL with a host and no
    credentials, query or fragment; None when it is."""
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        return f"{label} must be an HTTPS address without a username, query or fragment."
    return None


def _group_list(raw):
    return [g for g in raw if isinstance(g, str) and _GROUP_RE.match(g)] if isinstance(raw, list) else []


def clean(raw):
    """Stored settings with every value checked; anything malformed is the default."""
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS, allowed_groups=[], role_mapping={})
    for name in ("enabled", "create_users", "allow_password_login", "auto_login"):
        if isinstance(raw.get(name), bool):
            out[name] = raw[name]
    for name in ("issuer_url", "client_id", "redirect_uri", "provider_name", "scopes", "groups_claim"):
        if isinstance(raw.get(name), str) and raw[name].strip():
            out[name] = raw[name].strip()[:500]
    out["allowed_groups"] = _group_list(raw.get("allowed_groups"))
    mapping = raw.get("role_mapping")
    if isinstance(mapping, dict):
        out["role_mapping"] = {g: r for g, r in mapping.items()
                               if isinstance(g, str) and _GROUP_RE.match(g) and r in ROLES}
    lifetime = raw.get("session_lifetime_seconds")
    if isinstance(lifetime, int) and not isinstance(lifetime, bool) and MIN_LIFETIME <= lifetime <= MAX_LIFETIME:
        out["session_lifetime_seconds"] = lifetime
    return out


def load():
    """The saved settings (no secret), read fresh."""
    row = AppSetting.query.get(KEY)
    try:
        return clean(json.loads(row.value)) if row and row.value else clean({})
    except ValueError:
        return clean({})


def errors(values, has_secret):
    """What stops `values` from being saved, as messages. Turned off, only
    malformed values count; turned on, everything sign-in needs is required."""
    problems = []
    for name, label in (("issuer_url", "Issuer URL"), ("redirect_uri", "Callback URL")):
        if values.get(name):
            problem = https_url_error(values[name], label)
            if problem:
                problems.append(problem)
    if "openid" not in values.get("scopes", "").split():
        problems.append("Scopes must include openid.")
    if not _CLAIM_RE.match(values.get("groups_claim", "")):
        problems.append("The groups claim is a claim name: letters, digits and . _ : / - only.")
    if not 1 <= len(values.get("provider_name", "")) <= 100:
        problems.append("The button label needs 1 to 100 characters.")
    lifetime = values.get("session_lifetime_seconds")
    if not isinstance(lifetime, int) or not MIN_LIFETIME <= lifetime <= MAX_LIFETIME:
        problems.append("Session length must be between 1 minute and 24 hours.")
    if values.get("enabled"):
        for name, label in (("issuer_url", "Issuer URL"), ("client_id", "Client ID"), ("redirect_uri", "Callback URL")):
            if not values.get(name):
                problems.append(f"{label} is required to turn OIDC sign-in on.")
        if not has_secret:
            problems.append("A client secret is required to turn OIDC sign-in on.")
    return problems


def save(values, username):
    """Store `values` (cleaned). Caller commits."""
    row = AppSetting.query.get(KEY)
    if row is None:
        row = AppSetting(key=KEY)
        db.session.add(row)
    row.value = json.dumps(clean(values))
    logger.info("[oidc] Sign-in settings saved by %s", username)
    reset()


def secret_state():
    """For the page: whether a client secret is saved, its last four
    characters and whether it still decrypts. Never the secret."""
    row = ConfigSetting.query.get(SECRET_KEY)
    if row is None or not row.secret_value:
        return {"name": SECRET_KEY, "label": "Client secret", "kind": "secret", "source": "unset", "env_hint": ""}
    return {"name": SECRET_KEY, "label": "Client secret", "kind": "secret", "source": "saved", "env_hint": "",
            "hint": row.hint or "", "unreadable": secret_box.decrypt(row.secret_value) is None,
            "updated_at": row.updated_at, "updated_by": row.updated_by}


def secret_error(value):
    if len(value) > 1000 or any(c.isspace() for c in value):
        return "That doesn't look like a client secret."
    return None


def save_secret(value, username):
    """Encrypt and store the client secret; a blank value removes it. Caller commits."""
    row = ConfigSetting.query.get(SECRET_KEY)
    if not value:
        if row:
            db.session.delete(row)
        reset()
        return
    if row is None:
        row = ConfigSetting(key=SECRET_KEY)
        db.session.add(row)
    row.value = None
    row.secret_value = secret_box.encrypt(value)
    row.hint = value[-4:]
    row.updated_at = datetime.utcnow()
    row.updated_by = username
    reset()


def _secret():
    row = ConfigSetting.query.get(SECRET_KEY)
    return secret_box.decrypt(row.secret_value) if row and row.secret_value else None


def _build():
    """The runtime configuration, or None when OIDC is off, incomplete or
    the secret no longer decrypts (so a broken setup falls back to passwords)."""
    values = load()
    if not values["enabled"]:
        return None
    secret = _secret()
    if not secret or errors(values, has_secret=True):
        logger.warning("[oidc] Sign-in is on but its settings are incomplete or the client secret "
                       "can't be decrypted; OIDC sign-in is unavailable until Settings › Sign-in is fixed.")
        return None
    return {
        "ISSUER_URL": values["issuer_url"],
        "CLIENT_ID": values["client_id"],
        "CLIENT_SECRET": secret,
        "REDIRECT_URI": values["redirect_uri"],
        "SCOPES": values["scopes"],
        "PROVIDER_NAME": values["provider_name"],
        "GROUPS_CLAIM": values["groups_claim"],
        "ALLOWED_GROUPS": list(values["allowed_groups"]),
        "ROLE_MAPPING": dict(values["role_mapping"]),
        "CREATE_USERS": values["create_users"],
        "ALLOW_PASSWORD_LOGIN": values["allow_password_login"],
        "AUTO_LOGIN": values["auto_login"],
        "SESSION_LIFETIME_SECONDS": values["session_lifetime_seconds"],
    }


def runtime(fresh=False):
    """The configuration aggregator/oidc.py signs in with, or None when OIDC
    is off. Cached for RELOAD_SECONDS per process."""
    now = time.monotonic()
    if fresh or _cache["value"] is None or now - _cache["at"] >= RELOAD_SECONDS:
        try:
            value = _build()
        except Exception as error:
            # No table yet on an install that hasn't migrated: passwords only.
            db.session.rollback()
            logger.debug("[oidc] Settings unavailable (%s)", type(error).__name__)
            value = None
        _cache.update(at=now, value=value or False)
    return _cache["value"] or None


def reset():
    _cache.update(at=0.0, value=None)


def parse_groups(text):
    """Group names from a textarea or field: one per line or comma separated."""
    return list(dict.fromkeys(g.strip() for g in re.split(r"[,\n]", text or "") if g.strip()))


def role_groups(mapping, role):
    return [g for g, r in mapping.items() if r == role]


def mapping_from(admin_groups, scraper_groups):
    """group -> role from the page's two lists; a group in both is Admin."""
    mapping = {g: "scraper" for g in scraper_groups}
    mapping.update({g: "admin" for g in admin_groups})
    return mapping

