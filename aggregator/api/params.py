"""Query-string and body validation. Each endpoint names the parameters it
takes; anything else, or a value out of range, is a 400 rather than being
ignored."""
import base64
import binascii
import json

from flask import request

from .errors import ApiError

MAX_PAGE_SIZE = 50
MAX_QUERY_CHARS = 200


def bad(message):
    return ApiError(400, "bad_request", message)


def enum(*choices):
    def parse(value):
        if value not in choices:
            raise bad(f"Must be one of: {', '.join(choices)}.")
        return value
    return parse


def integer(low, high):
    def parse(value):
        if not str(value).isdigit() or not low <= int(value) <= high:
            raise bad(f"Must be a whole number from {low} to {high}.")
        return int(value)
    return parse


def text(max_chars=MAX_QUERY_CHARS):
    def parse(value):
        value = (value or "").strip()
        if not value or len(value) > max_chars:
            raise bad(f"Must be 1 to {max_chars} characters.")
        return value
    return parse


def boolean(value):
    if value in ("true", "1"):
        return True
    if value in ("false", "0"):
        return False
    raise bad("Must be true or false.")


def encode_cursor(offset):
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode().rstrip("=")


def cursor(value):
    try:
        padded = value + "=" * (-len(value) % 4)
        offset = json.loads(base64.urlsafe_b64decode(padded.encode()))["o"]
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise bad("Invalid cursor.")
    if not isinstance(offset, int) or not 0 <= offset <= 100000:
        raise bad("Invalid cursor.")
    return offset


def query_args(**spec):
    """{name: parsed value} for the query string, by `spec` ({name: parser}).
    Unknown or repeated parameters are rejected; missing ones are None."""
    out = {name: None for name in spec}
    for name in request.args:
        if name not in spec:
            raise bad(f"Unknown parameter: {name[:40]}.")
        values = request.args.getlist(name)
        if len(values) != 1:
            raise bad(f"{name} given more than once.")
        try:
            out[name] = spec[name](values[0])
        except ApiError as e:
            raise bad(f"{name}: {e.message}")
    return out


def json_body(**spec):
    """{name: parsed value} for a JSON object body, by `spec` ({name: parser
    taking the JSON value}). Unknown keys are rejected; missing ones are None."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise bad("Expected a JSON object.")
    out = {name: None for name in spec}
    for name, value in body.items():
        if name not in spec:
            raise bad(f"Unknown field: {str(name)[:40]}.")
        try:
            out[name] = spec[name](value)
        except ApiError as e:
            raise bad(f"{name}: {e.message}")
    return out
