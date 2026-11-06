"""Settings for the app: the reader's own account, and for an admin the
server's settings, as pages the app draws (schema.py).

    GET   /settings                          the page list, by group
    GET   /settings/<page>                   one page
    PATCH /settings/<page>                   save fields {"section", "values"}
    POST  /settings/<page>/actions/<name>    run a button {"values"}

A page key is a name ("users") or a name and an id for one row's page
("users/12"). Writes answer with the page as it now stands, so the app
redraws from what was saved.

Admin pages need a device paired with the "admin" scope, which an admin
ticks when making the pairing code on Settings › API, and the user must
still be an admin on every request. A phone paired without it sees only its
own account, as before. Restarting containers and anything else that reaches
past the database stays on the web.
"""
from flask import g, jsonify, request

from aggregator import db
from aggregator.models import ADMIN_SCOPE, ROLE_ADMIN

from .. import api
from ..auth import require_token
from ..errors import ApiError
from .schema import FieldError

# (key, title, admin only)
GROUPS = (
    ("account", "Your account", False),
    ("general", "General", True),
    ("sources", "Sources", True),
    ("ai", "AI", True),
    ("pipeline", "Pipeline", True),
)
_ADMIN_GROUPS = {key for key, _, admin in GROUPS if admin}


class PageSpec:
    def __init__(self, key, title, group, icon, build, listed, detail):
        self.key, self.title, self.group, self.icon = key, title, group, icon
        self.build, self.listed, self.detail = build, listed, detail
        self.saver = None
        self.actions = {}

    @property
    def admin(self):
        return self.group in _ADMIN_GROUPS

    def save(self, fn):
        """saver(arg, values, section) -> a message or None."""
        self.saver = fn
        return fn

    def action(self, name):
        """fn(arg, values) -> None or {"message", "secret", "close", "page"}."""
        def decorator(fn):
            self.actions[name] = fn
            return fn
        return decorator


PAGES = {}


def page(key, title, group, icon, listed=True, detail=None):
    """Register a page builder, build(arg) -> schema.page(...). A key ending
    in "/*" is a row's page: arg is the id after the slash."""
    def decorator(build):
        spec = PageSpec(key, title, group, icon, build, listed, detail)
        PAGES[key] = spec
        return spec
    return decorator


def can_admin():
    return ADMIN_SCOPE in g.device.scope_list and g.user.role == ROLE_ADMIN


def _resolve(key):
    spec = PAGES.get(key)
    arg = None
    if spec is None or key.endswith("/*"):
        base, _, arg = key.partition("/")
        spec = PAGES.get(f"{base}/*") if arg and arg != "*" else None
    if spec is None:
        raise ApiError(404, "not_found", "No such settings page.")
    if spec.admin and not can_admin():
        raise ApiError(403, "forbidden", "Admin settings need a device paired with admin access.")
    return spec, arg


def _build(spec, arg):
    try:
        return spec.build(arg)
    except LookupError:
        raise ApiError(404, "not_found", "That's gone. It may have been removed on the web.")


def _invalid(e):
    db.session.rollback()
    body = {"error": {"code": "invalid", "message": e.message}}
    if e.field:
        body["error"]["field"] = e.field
    return jsonify(body), 422


def _body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", "Expected a JSON object.")
    values = body.get("values", {})
    if not isinstance(values, dict):
        raise ApiError(400, "bad_request", "values must be an object.")
    section = body.get("section")
    if section is not None and not isinstance(section, str):
        raise ApiError(400, "bad_request", "section must be text.")
    return values, section


@api.route("/settings")
@require_token()
def settings_index():
    admin = can_admin()
    groups = []
    for key, title, admin_only in GROUPS:
        if admin_only and not admin:
            continue
        pages = []
        for spec in PAGES.values():
            if spec.group != key or not spec.listed:
                continue
            row = {"key": spec.key, "title": spec.title, "icon": spec.icon}
            if spec.detail:
                try:
                    row["detail"] = spec.detail()
                except Exception:  # a count is a nicety; never fail the list for one
                    db.session.rollback()
            pages.append(row)
        groups.append({"key": key, "title": title, "pages": pages})
    return jsonify({
        "groups": groups,
        "admin": admin,
        # An admin on a phone paired without admin access: the app says how to get it.
        "admin_available": g.user.role == ROLE_ADMIN and not admin,
    })


@api.route("/settings/<path:key>")
@require_token()
def settings_page(key):
    spec, arg = _resolve(key)
    return jsonify(_build(spec, arg))


@api.route("/settings/<path:key>", methods=["PATCH"])
@require_token()
def save_settings_page(key):
    spec, arg = _resolve(key)
    if spec.saver is None:
        raise ApiError(405, "method_not_allowed", "Nothing on this page can be changed.")
    values, section = _body()
    _build(spec, arg)  # 404 before saving to a row that's gone
    try:
        message = spec.saver(arg, values, section)
        db.session.commit()
    except FieldError as e:
        return _invalid(e)
    try:
        out = {"page": _build(spec, arg)}
    except ApiError:
        out = {"close": True}  # the save moved or removed what this page showed
    if message:
        out["message"] = message
    return jsonify(out)


@api.route("/settings/<path:key>/actions/<name>", methods=["POST"])
@require_token()
def run_settings_action(key, name):
    spec, arg = _resolve(key)
    fn = spec.actions.get(name)
    if fn is None:
        raise ApiError(404, "not_found", "No such action.")
    values, _ = _body()
    try:
        result = fn(arg, values) or {}
        db.session.commit()
    except FieldError as e:
        return _invalid(e)
    out = {k: result[k] for k in ("message", "secret", "close") if result.get(k)}
    if not result.get("close"):
        target = result.get("page")
        if target:
            out["page"] = _build(*_resolve(target))
        else:
            out["page"] = _build(spec, arg)
    if "open" in result:
        out["open"] = result["open"]
    return jsonify(out)


from . import account, general, sources, ai, pipeline  # noqa: E402,F401
