"""The API: JSON under /api/v1, for the MuckScroller app and any other client.

Served only by its own app (aggregator/api_app.py), which registers this
blueprint and nothing else, so the internet-facing process has no admin,
login or web pages to reach. Clients authenticate with bearer tokens: a phone
gets one through a one-time pairing code, and anything else through an API
key, both from Settings › API in the web app.
"""
from flask import Blueprint, request

from .errors import ApiError, GENERIC, error_response

API_VERSION = "1.0"

api = Blueprint("api", __name__)

# Answer even when the API is switched off: a status check, and the spec.
_ALWAYS_OPEN = {"api.health", "api.openapi_spec"}


@api.before_request
def _gate():
    from . import settings
    from .ratelimit import check_ip
    check_ip()
    if request.endpoint not in _ALWAYS_OPEN and not settings.load()["enabled"]:
        raise ApiError(503, "unavailable", "The API is turned off.")


@api.errorhandler(ApiError)
def _api_error(e):
    return error_response(e.status, e.code, e.message, e.headers)


from . import routes, me_routes, bookmark_routes, breaking_routes, openapi  # noqa: E402,F401
