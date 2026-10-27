"""Settings › Sign-in: optional OIDC sign-in (aggregator/oidc.py), set here
instead of OIDC_* environment variables. Settings are stored by
aggregator/oidc_settings.py; the client secret is encrypted like the LLM and
source API keys and never comes back to the browser. Also lists the accounts
bound to a provider identity and binds or unbinds one, which the
link-oidc-identity command does from the shell."""
from flask import flash, redirect, render_template, request, session, url_for
from flask_login import current_user
from sqlalchemy import func

from aggregator import db, oidc, oidc_settings, secret_box
from aggregator.models import OIDCIdentity, ROLE_ADMIN, ROLE_SCRAPER, User
from aggregator.permissions import role_required

from . import admin
from .api_access import _site_url


def _https_request():
    # The web app has no ProxyFix; a TLS proxy shows up in X-Forwarded-Proto.
    return _site_url().startswith("https://")


@admin.route("/settings/sign-in")
@role_required(ROLE_ADMIN)
def sign_in_page():
    values = oidc_settings.load()
    runtime = oidc.current_config(fresh=True)
    bindings = (OIDCIdentity.query.join(User).order_by(func.lower(User.username)).all())
    return render_template(
        "settings_sign_in.html",
        values=values,
        live=bool(runtime),
        secret=oidc_settings.secret_state(),
        key_source=secret_box.key_source(),
        callback_url=_site_url() + url_for("auth.oidc_callback"),
        admin_groups=oidc_settings.role_groups(values["role_mapping"], ROLE_ADMIN),
        scraper_groups=oidc_settings.role_groups(values["role_mapping"], ROLE_SCRAPER),
        lifetime_minutes=values["session_lifetime_seconds"] // 60,
        oidc_session=session.get("auth_method") == "oidc",
        https=_https_request(),
        bindings=bindings,
        users=User.query.filter_by(is_active=True).order_by(func.lower(User.username)).all(),
    )


@admin.route("/settings/sign-in", methods=["POST"])
@role_required(ROLE_ADMIN)
def save_sign_in():
    form = request.form
    try:
        minutes = int(form.get("session_minutes", "").strip())
    except ValueError:
        minutes = 0
    values = {
        "enabled": form.get("enabled") == "on",
        "issuer_url": form.get("issuer_url", "").strip(),
        "client_id": form.get("client_id", "").strip(),
        "redirect_uri": form.get("redirect_uri", "").strip(),
        "provider_name": form.get("provider_name", "").strip(),
        "scopes": " ".join(form.get("scopes", "").split()),
        "groups_claim": form.get("groups_claim", "").strip(),
        "allowed_groups": oidc_settings.parse_groups(form.get("allowed_groups")),
        "role_mapping": oidc_settings.mapping_from(oidc_settings.parse_groups(form.get("admin_groups")),
                                                   oidc_settings.parse_groups(form.get("scraper_groups"))),
        "create_users": form.get("create_users") == "on",
        # The form asks the opposite question ("OIDC only"), so it is off by default.
        "allow_password_login": form.get("oidc_only") != "on",
        "auto_login": form.get("auto_login") == "on",
        "session_lifetime_seconds": minutes * 60,
    }
    secret = form.get(oidc_settings.SECRET_KEY, "").strip()
    clear_secret = bool(form.get(f"clear_{oidc_settings.SECRET_KEY}"))
    saved = oidc_settings.secret_state()
    has_secret = bool(secret) or (not clear_secret and saved["source"] == "saved" and not saved.get("unreadable"))

    problems = oidc_settings.errors(values, has_secret)
    if secret:
        problem = oidc_settings.secret_error(secret)
        if problem:
            problems.append(problem)
    if values["enabled"] and not _https_request():
        # Turned on, session cookies become Secure; over plain HTTP that
        # would sign everyone out, this admin included.
        problems.append("Open Settings over HTTPS to turn OIDC sign-in on: it makes sign-in cookies HTTPS-only.")
    if values["enabled"] and not values["allow_password_login"] and session.get("auth_method") != "oidc":
        problems.append("Sign in with the provider as an admin before turning password sign-in off, "
                        "so you know it works and don't lock yourself out.")
    for problem in problems:
        flash(problem, "error")
    if problems:
        return redirect(url_for("admin.sign_in_page"))

    if clear_secret:
        oidc_settings.save_secret("", current_user.username)
    elif secret:
        oidc_settings.save_secret(secret, current_user.username)
    oidc_settings.save(values, current_user.username)
    db.session.commit()
    flash("Sign-in settings saved. Other web workers pick them up within 15 seconds.", "success")
    return redirect(url_for("admin.sign_in_page"))


@admin.route("/settings/sign-in/link", methods=["POST"])
@role_required(ROLE_ADMIN)
def link_sign_in_identity():
    config = oidc.current_config(fresh=True)
    user = User.query.filter_by(id=request.form.get("user_id", type=int), is_active=True).first()
    subject = request.form.get("subject", "").strip()
    if not config:
        flash("Turn OIDC sign-in on before linking accounts.", "error")
    elif not user:
        flash("Pick an active account.", "error")
    else:
        try:
            result = oidc.bind(user, subject, config)
        except oidc.OIDCRefused:
            db.session.rollback()
            flash("That subject is invalid or already linked to another account.", "error")
        else:
            db.session.commit()
            flash(f"{user.username} was already linked to that identity." if result == "exists"
                  else f"{user.username} can now sign in with {config['PROVIDER_NAME']}.", "success")
    return redirect(url_for("admin.sign_in_page") + "#linked")


@admin.route("/settings/sign-in/unlink/<identity_key>", methods=["POST"])
@role_required(ROLE_ADMIN)
def unlink_sign_in_identity(identity_key):
    binding = OIDCIdentity.query.get(identity_key)
    if binding:
        username = binding.user.username
        db.session.delete(binding)
        db.session.commit()
        flash(f"Unlinked {username}. A provider session using that identity ends on its next request.", "success")
    return redirect(url_for("admin.sign_in_page") + "#linked")
