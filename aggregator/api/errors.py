"""JSON errors for the API: a short code and message, never a traceback,
hostname or model name."""
from flask import jsonify


class ApiError(Exception):
    def __init__(self, status, code, message, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def error_response(status, code, message, headers=None):
    response = jsonify({"error": {"code": code, "message": message}})
    response.status_code = status
    for name, value in (headers or {}).items():
        response.headers[name] = value
    return response


GENERIC = {
    400: ("bad_request", "The request was not valid."),
    401: ("unauthorized", "A valid device token is required."),
    403: ("forbidden", "This device can't do that."),
    404: ("not_found", "Not found."),
    405: ("method_not_allowed", "Method not allowed."),
    413: ("too_large", "Request body too large."),
    429: ("rate_limited", "Too many requests."),
    500: ("server_error", "Something went wrong."),
    503: ("unavailable", "The API is turned off."),
}
