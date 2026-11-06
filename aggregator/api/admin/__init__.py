"""Settings for the app as plain JSON resources: the reader's own account
under /me/account, and for an admin the server's settings under /admin.

Each web settings page has its resource here, built from the same modules
the web page uses, with the same rules. The app draws its own screen for
each one (MuckScroller's Features/Settings), so a page reads like the web's
rather than like a form.

Conventions:
- A GET returns the page's data. A write returns {"message", "data"}, where
  data is the page as it now stands, so the app redraws from what was saved.
- A value the server won't take is a 422 {"error": {"code": "invalid",
  "message", "field"}}, naming the field when there is one.
- Admin routes need a device paired with the "admin" scope, for a user who
  is still an admin on every request. The scope is ticked when making the
  pairing code, or switched on for a paired device on Settings › API.
"""
import re
from functools import wraps

from flask import g, jsonify

from aggregator import db
from aggregator.models import ADMIN_SCOPE, ROLE_ADMIN

from .. import api
from ..auth import require_token
from ..errors import ApiError
from .values import Invalid

# (OpenAPI path, method, operation id, description) for every route here;
# openapi.py lists them, so the spec covers them without a hand-kept copy.
ROUTES = []


def can_admin():
    return ADMIN_SCOPE in g.device.scope_list and g.user.role == ROLE_ADMIN


def _openapi_path(rule):
    return re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", rule)


def route(rule, methods=("GET",), scope="read", admin=True):
    """api.route behind a device token; `admin` also needs can_admin()."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if admin and not can_admin():
                raise ApiError(403, "admin_scope",
                               "Admin settings need this device paired with admin access. Turn it on for this "
                               "device on the web's Settings › API, or pair again with Admin settings ticked.")
            return view(*args, **kwargs)
        module = view.__module__.rsplit(".", 1)[-1]
        endpoint = f"settings_{module}_{view.__name__}"
        for method in methods:
            ROUTES.append((_openapi_path(rule), method.lower(), endpoint, (view.__doc__ or "").strip()))
        return api.route(rule, methods=list(methods), endpoint=endpoint)(require_token(scope)(wrapped))
    return decorator


def reply(data, message=None):
    """A write's answer: the page as it now stands, and what to tell the reader."""
    db.session.commit()
    return jsonify({"message": message, "data": data})


@api.errorhandler(Invalid)
def _invalid(e):
    db.session.rollback()
    body = {"error": {"code": "invalid", "message": e.message}}
    if e.field:
        body["error"]["field"] = e.field
    return jsonify(body), 422


from . import account, general, sources, ai, pipeline  # noqa: E402,F401
