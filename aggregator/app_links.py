"""Universal links for the MuckScroller iOS app: the apple-app-site-association
file that lets iOS open this site's /story/<id> links in the app.

Off unless IOS_APP_IDS is set to the app's "<TEAM ID>.<bundle id>" (several
may be given, comma-separated), e.g. ABCDE12345.news.muckscraper.MuckScroller.
The app also needs the Associated Domains entitlement for this host.
"""
import os

from flask import abort, jsonify

LINK_PATHS = ["/story/*"]


def app_ids():
    return [a.strip() for a in os.environ.get("IOS_APP_IDS", "").split(",") if a.strip()]


def association_file():
    ids = app_ids()
    if not ids:
        abort(404)
    response = jsonify({
        "applinks": {
            "details": [{"appIDs": ids, "components": [{"/": path} for path in LINK_PATHS]}],
        },
    })
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


def register(app):
    """Serve the file at both paths iOS checks."""
    for path in ("/.well-known/apple-app-site-association", "/apple-app-site-association"):
        app.add_url_rule(path, f"apple_app_site_association{path.count('/')}", association_file)
