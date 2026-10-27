"""Optional OpenID Connect login using the existing account and permission model.

Only trusted server configuration selects the issuer and role policy: an
admin sets it on Settings › Sign-in (aggregator/oidc_settings.py), not the
environment. Identity is an exact issuer/subject binding, never a forwarded
header. A matching email or username links an identity to an existing
account only when the admin picks a matching claim, and only once: the
first sign-in stores the issuer/subject binding, which is used from then on.
"""
import hashlib
import secrets
import time
from datetime import datetime
from urllib.parse import urlsplit

import click
from flask import abort, current_app, redirect, request, session, url_for
from flask_login import current_user, login_user, logout_user
from sqlalchemy import func

from aggregator import db, oidc_settings
from aggregator.accounts import email_error, other_active_admins
from aggregator.models import OIDCIdentity, ROLE_ADMIN, ROLE_READER, ROLES, User


class OIDCRefused(ValueError):
    """A fixed refusal class; exception details must never reach logs or pages."""


def current_config(fresh=False):
    """The saved configuration (upper-case keys), or None when OIDC is off."""
    return oidc_settings.runtime(fresh=fresh)


def remote(config):
    """The Authlib client for `config`, rebuilt when the provider settings
    change so a save takes effect without a restart."""
    app = current_app._get_current_object()
    fingerprint = hashlib.sha256("\0".join(
        config[name] for name in ("ISSUER_URL", "CLIENT_ID", "CLIENT_SECRET", "SCOPES")).encode()).hexdigest()
    if app.extensions.get("muckscraper_oidc_fingerprint") != fingerprint:
        from authlib.integrations.flask_client import OAuth

        oauth = OAuth(app)
        app.extensions["muckscraper_oidc"] = oauth.register(
            "muckscraper", client_id=config["CLIENT_ID"], client_secret=config["CLIENT_SECRET"],
            server_metadata_url=config["ISSUER_URL"].rstrip("/") + "/.well-known/openid-configuration",
            client_kwargs={"scope": config["SCOPES"], "code_challenge_method": "S256", "timeout": 15},
            claims_options={"iss": {"essential": True, "value": config["ISSUER_URL"]}},
        )
        app.extensions["muckscraper_oidc_fingerprint"] = fingerprint
    return app.extensions["muckscraper_oidc"]


_COOKIE_FLAGS = dict(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                     SESSION_COOKIE_SAMESITE="Lax", REMEMBER_COOKIE_SECURE=True,
                     REMEMBER_COOKIE_HTTPONLY=True, REMEMBER_COOKIE_SAMESITE="Lax")


def identity_key(issuer, subject):
    if not isinstance(subject, str) or not 1 <= len(subject) <= 255 or "\0" in subject:
        raise OIDCRefused()
    return hashlib.sha256((issuer + "\0" + subject).encode()).hexdigest()


def safe_next(value):
    # Accept local absolute paths only, not schemes, authority URLs or backslashes.
    # The fallback is the index, which sends the reader to their chosen start page.
    if (not isinstance(value, str) or not value.startswith("/") or value.startswith("//")
            or "\\" in value or any(ord(c) < 32 for c in value)):
        return url_for("public.index")
    parsed = urlsplit(value)
    return value if not parsed.scheme and not parsed.netloc else url_for("public.index")


def display_name(claims):
    """Presentation only: never use these non-unique claims for account lookup."""
    for field in ("preferred_username", "name", "email"):
        value = claims.get(field)
        if isinstance(value, str) and value.strip() and len(value.strip()) <= 254:
            if not any(ord(char) < 32 or ord(char) == 127 for char in value):
                return value.strip()
    return "Account"


def match_existing(claims, config):
    """The one active account whose email or username equals the configured
    matching claim (ignoring case), or None. Refuses when the value fits
    more than one account, a disabled one, or one already bound to another
    identity at this issuer."""
    claim = config.get("MATCH_CLAIM")
    value = claims.get(claim) if claim else None
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip().lower()
    found = User.query.filter(db.or_(func.lower(User.email) == value, func.lower(User.username) == value)).all()
    if not found:
        return None
    if len(found) > 1 or not found[0].is_active:
        raise OIDCRefused()
    if OIDCIdentity.query.filter_by(user_id=found[0].id, issuer=config["ISSUER_URL"]).first():
        raise OIDCRefused()
    return found[0]


def resolve_user(claims, config):
    if claims.get("iss") != config["ISSUER_URL"] or claims.get("email_verified") is not True:
        raise OIDCRefused()
    key = identity_key(config["ISSUER_URL"], claims.get("sub"))
    groups = claims.get(config["GROUPS_CLAIM"], [])
    if not isinstance(groups, list) or any(not isinstance(g, str) for g in groups):
        raise OIDCRefused()
    if config["ALLOWED_GROUPS"] and not set(groups).intersection(config["ALLOWED_GROUPS"]):
        raise OIDCRefused()
    # Reader for a config dict built without the setting.
    default = config.get("DEFAULT_ROLE", ROLE_READER)
    binding = OIDCIdentity.query.filter_by(identity_key=key).first()
    if binding:
        if (binding.issuer != config["ISSUER_URL"] or binding.subject != claims["sub"]
                or not binding.user.is_active):
            raise OIDCRefused()
        user = binding.user
    else:
        user = match_existing(claims, config)
    if user is None:
        if not config["CREATE_USERS"]:
            raise OIDCRefused()
        username = "oidc_" + key[:59]
        if User.query.filter_by(username=username).first():
            raise OIDCRefused()
        user = User(username=username, role=default, is_admin=False, is_active=True)
    email = claims.get("email")
    if not isinstance(email, str) or email_error(email, exclude_id=user.id):
        raise OIDCRefused()
    if config["ROLE_MAPPING"]:
        role = max((config["ROLE_MAPPING"].get(g, default) for g in groups),
                   key=ROLES.index, default=default)
        if user.role == ROLE_ADMIN and role != ROLE_ADMIN:
            # Serialize simultaneous OIDC demotions on databases supporting row
            # locks; do not let two administrators each count the other first.
            User.query.filter(User.role == ROLE_ADMIN, User.is_active.is_(True)).order_by(
                User.id).with_for_update().all()
            if other_active_admins(user.id) == 0:
                raise OIDCRefused()
        user.set_role(role)
    user.email = email
    user.last_login_at = datetime.utcnow()
    db.session.add(user)
    if not binding:
        db.session.add(OIDCIdentity(user=user, identity_key=key,
                                    issuer=config["ISSUER_URL"], subject=claims["sub"]))
    db.session.commit()
    return user, key


def bind(user, subject, config):
    """Bind `subject` at the configured issuer to `user`. Returns "created"
    or "exists"; raises OIDCRefused for an invalid subject or one already
    bound elsewhere. Never replaces a binding. Caller commits."""
    key = identity_key(config["ISSUER_URL"], subject)
    binding = OIDCIdentity.query.filter_by(identity_key=key).first()
    if binding:
        if binding.user_id != user.id or binding.issuer != config["ISSUER_URL"] or binding.subject != subject:
            raise OIDCRefused()
        return "exists"
    db.session.add(OIDCIdentity(user=user, identity_key=key, issuer=config["ISSUER_URL"], subject=subject))
    return "created"


def init_app(app):
    plain_cookies = {name: app.config.get(name) for name in _COOKIE_FLAGS}

    @app.context_processor
    def account_presentation():
        if not current_user.is_authenticated:
            return {"account_display_name": "Account"}
        if session.get("auth_method") == "oidc":
            return {"account_display_name": session.get("oidc_display_name") or current_user.email or "Account"}
        return {"account_display_name": current_user.username}

    @app.before_request
    def oidc_cookie_policy():
        # Turned on, OIDC needs HTTPS (its callback is HTTPS), so cookies are
        # Secure and SameSite=Lax; turned off they are back to the app's own.
        app.config.update(_COOKIE_FLAGS if current_config() else plain_cookies)

    @app.before_request
    def expire_oidc_session():
        if session.get("auth_method") == "oidc":
            config = current_config()
            key = session.get("oidc_identity")
            if (not config or session.get("oidc_expires_at", 0) <= time.time()
                    or not current_user.is_authenticated or not OIDCIdentity.query.filter_by(
                        identity_key=key, user_id=current_user.id, issuer=config["ISSUER_URL"]).first()):
                session.clear()
                logout_user()

    @app.after_request
    def private_auth_responses(response):
        if request.endpoint == "auth.login" or (request.endpoint and request.endpoint.startswith("auth.oidc_")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "same-origin" if request.endpoint == "auth.login" else "no-referrer"
        return response

    @app.cli.command("link-oidc-identity")
    @click.option("--username", required=True)
    @click.option("--subject", required=True)
    @click.option("--apply", "apply_binding", is_flag=True)
    def link_identity(username, subject, apply_binding):
        """Bind a verified provider subject to an existing account; dry-run by default.
        Settings › Sign-in does the same from the browser."""
        config = current_config(fresh=True)
        if not config:
            raise click.ClickException("OIDC is not enabled")
        try:
            identity_key(config["ISSUER_URL"], subject)
        except OIDCRefused:
            raise click.ClickException("Invalid subject") from None
        user = User.query.filter_by(username=username, is_active=True).first()
        if not user:
            raise click.ClickException("Active account not found")
        try:
            result = bind(user, subject, config)
        except OIDCRefused:
            raise click.ClickException("Identity already bound; replacement refused") from None
        if result == "exists":
            click.echo("Binding already exists; no change.")
        elif apply_binding:
            db.session.commit()
            click.echo("Binding created.")
        else:
            db.session.rollback()
            click.echo("Dry-run: binding can be created; no change.")

    @app.cli.command("disable-oidc")
    def disable_oidc():
        """Recovery: turn OIDC sign-in off so password sign-in works again."""
        values = oidc_settings.load()
        values["enabled"] = False
        oidc_settings.save(values, "cli")
        db.session.commit()
        click.echo("OIDC sign-in is off; password sign-in is available.")


def start():
    config = current_config()
    if not config:
        abort(404)
    client = remote(config)
    # Only one attempt is supported. Discard its old Authlib state too, or
    # abandoned redirects eventually overflow the signed session cookie.
    prefix = f"_state_{client.name}_"
    for key in list(session):
        if key.startswith(prefix):
            client.framework.clear_state_data(session, key[len(prefix):])
    state = secrets.token_urlsafe(32)
    # A failed or cancelled attempt must leave a usable manual login page.
    session["oidc_auto_login_paused"] = True
    session["oidc_pending"] = {"state": state, "next": safe_next(request.args.get("next"))}
    try:
        return client.authorize_redirect(
            config["REDIRECT_URI"], state=state)
    except Exception as error:
        current_app.logger.warning("OIDC start refused (%s)", type(error).__name__)
        abort(503)


def callback():
    config = current_config()
    if not config:
        abort(404)
    pending = session.get("oidc_pending", {})
    state = request.args.get("state", "")
    if not state or not state.isascii() or not secrets.compare_digest(state, pending.get("state", "")):
        abort(403)
    session.pop("oidc_pending", None)
    try:
        token = remote(config).authorize_access_token()
        if not token.get("id_token") or not token.get("userinfo"):
            raise OIDCRefused()
        user, key = resolve_user(token["userinfo"], config)
    except Exception as error:
        db.session.rollback()
        current_app.logger.warning("OIDC sign-in refused (%s)", type(error).__name__)
        abort(403)
    session.clear()
    logout_user()  # Also clears a stale local remember-me cookie.
    login_user(user, remember=False)
    session.update(auth_method="oidc", oidc_identity=key,
                   oidc_display_name=display_name(token["userinfo"]),
                   oidc_expires_at=time.time() + config["SESSION_LIFETIME_SECONDS"])
    return redirect(safe_next(pending.get("next")))
