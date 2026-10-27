from datetime import datetime
from urllib.parse import urlparse

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from aggregator import appearance_prefs, db, feed_prefs, reading_prefs
from aggregator.accounts import email_error, password_error
from aggregator.models import User

auth = Blueprint("auth", __name__)


@auth.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("public.headlines_feed"))

    oidc = current_app.config.get("OIDC")
    if (request.method == "GET" and oidc and oidc["AUTO_LOGIN"]
            and request.args.get("manual") != "1"
            and not session.get("oidc_auto_login_paused")):
        return redirect(url_for("auth.oidc_login", next=request.args.get("next", "")))
    allow_password = not oidc or oidc["ALLOW_PASSWORD_LOGIN"]
    if request.method == "POST" and not allow_password:
        abort(403)
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        remember = True if request.form.get("remember") else False

        user = User.query.filter_by(username=username).first()
        # A disabled account gets the same message as a wrong password, so the
        # form doesn't reveal which accounts exist.
        if user and user.is_active and user.check_password(password):
            login_user(user, remember=remember)
            user.last_login_at = datetime.utcnow()
            db.session.commit()
            from aggregator.oidc import safe_next
            return redirect(safe_next(request.args.get("next")))
        else:
            flash("Invalid username or password")

    return render_template("login.html", oidc_enabled=bool(oidc),
                           oidc_provider_name=oidc["PROVIDER_NAME"] if oidc else "",
                           allow_password_login=allow_password)


@auth.route("/oidc/login", methods=["GET"])
def oidc_login():
    from aggregator.oidc import start
    return start()


@auth.route("/oidc/callback", methods=["GET"])
def oidc_callback():
    from aggregator.oidc import callback
    return callback()


@auth.route("/logout", methods=["GET", "POST"])
def logout():
    if session.get("auth_method") == "oidc" and request.method != "POST":
        abort(405)
    session.clear()
    logout_user()
    session["oidc_auto_login_paused"] = True
    return redirect(url_for("public.index"))


@auth.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    """The signed-in user's own account: change email or password.

    Both changes ask for the current password, so a session left open on a
    shared machine can't be used to take the account over.
    """
    oidc_managed = session.get("auth_method") == "oidc"
    # Email and password belong to the identity provider during an OIDC
    # session; feed, appearance and reading preferences stay local.
    if (request.method == "POST" and oidc_managed
            and request.form.get("action") not in ("feed_defaults", "appearance", "reading")):
        abort(403)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "feed_defaults":
            # Preferences, not credentials: no password needed.
            # An unticked checkbox sends nothing, so read presence.
            feed_prefs.save(current_user, {**request.form.to_dict(), "hide_single": "hide_single" in request.form})
            flash("Feed defaults saved.", "success")
            return redirect(url_for("auth.profile"))
        if action == "appearance":
            appearance_prefs.save(current_user, request.form.to_dict())
            flash("Appearance saved.", "success")
            return redirect(url_for("auth.profile"))
        if action == "reading":
            reading_prefs.save(current_user, request.form.to_dict())
            flash("Reading settings saved.", "success")
            return redirect(url_for("auth.profile"))
        if not current_user.check_password(request.form.get("current_password", "")):
            flash("Your current password was not correct.", "error")
        elif action == "email":
            email = request.form.get("email", "").strip()
            error = email_error(email, exclude_id=current_user.id)
            if error:
                flash(error, "error")
            else:
                current_user.email = email
                db.session.commit()
                flash("Email address updated.", "success")
        elif action == "password":
            new_password = request.form.get("new_password", "")
            error = password_error(new_password, request.form.get("confirm_password", ""))
            if error:
                flash(error, "error")
            else:
                current_user.set_password(new_password)
                db.session.commit()
                flash("Password changed.", "success")
        return redirect(url_for("auth.profile"))

    return render_template(
        "profile.html",
        oidc_managed=oidc_managed,
        feed_defaults=feed_prefs.load(current_user),
        start_pages=feed_prefs.START_PAGES,
        ranges=feed_prefs.RANGES,
        scrape_statuses=feed_prefs.SCRAPE_STATUSES,
        appearance=appearance_prefs.load(current_user),
        analysis_layouts=appearance_prefs.ANALYSIS_LAYOUTS,
        reading=reading_prefs.load(current_user),
        auto_read_delays=reading_prefs.AUTO_READ_DELAYS,
    )


def _local_next(value, fallback):
    """`value` if it is a path on this site, else `fallback`."""
    if not value or not value.startswith("/") or value.startswith("//") or urlparse(value).netloc:
        return fallback
    return value


@auth.route("/feed-defaults", methods=["POST"])
@login_required
def save_feed_defaults():
    """"Save as my default" in the feed's Filters popover: store the filters
    the list is showing now and go back to it. Fields the form leaves out keep
    their saved values."""
    prefs = feed_prefs.load(current_user)
    for field in ("start_page", "range", "scrape_status", "hide_single"):
        if field in request.form:
            prefs[field] = request.form[field]
    feed_prefs.save(current_user, prefs)
    flash("Saved as your feed defaults.", "success")
    return redirect(_local_next(request.form.get("next"), url_for("admin.list_articles")))
