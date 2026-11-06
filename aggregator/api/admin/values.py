"""Reading what the app sends: a JSON object body, and its values checked
the way the web forms check theirs. A bad value raises Invalid, which the
API answers with a 422 naming the field."""
from flask import request

from aggregator.api.errors import ApiError


class Invalid(Exception):
    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError(400, "bad_request", "Expected a JSON object.")
    return data


def get_bool(values, key):
    value = values.get(key)
    if not isinstance(value, bool):
        raise Invalid("Must be on or off.", key)
    return value


def get_str(values, key, max_len=300, required=False, label=None):
    value = values.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise Invalid("Must be text.", key)
    value = value.strip()
    if len(value) > max_len:
        raise Invalid(f"{label or 'This'} can be at most {max_len} characters.", key)
    if required and not value:
        raise Invalid(f"{label or 'This'} can't be empty.", key)
    return value


def get_int(values, key, low, high, label=None):
    value = values.get(key)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        prefix = f"{label}: enter" if label else "Enter"
        raise Invalid(f"{prefix} a whole number from {low} to {high}.", key)
    return value


def get_optional_int(values, key, low, high, label=None):
    """A whole number, or None for a blank or null value."""
    if values.get(key) in (None, ""):
        return None
    return get_int(values, key, low, high, label)


def get_choice(values, key, allowed, message="Pick one of the choices."):
    value = values.get(key)
    if value is not None and not isinstance(value, str):
        value = str(value)
    if value not in {str(a) for a in allowed}:
        raise Invalid(message, key)
    return value


def get_list(values, key, allowed=None):
    value = values.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise Invalid("Must be a list.", key)
    if allowed is not None:
        allowed = {str(a) for a in allowed}
        if any(v not in allowed for v in value):
            raise Invalid("Pick from the choices.", key)
    return value


def get_ids(values, key, label="ids"):
    """A list of whole-number ids."""
    raw = values.get(key)
    if not isinstance(raw, list):
        raise Invalid(f"Send {label} as a list.", key)
    try:
        return [int(v) for v in raw if not isinstance(v, bool)]
    except (TypeError, ValueError):
        raise Invalid(f"{label.capitalize()} must be whole numbers.", key)


def choices(pairs):
    """[(value, label) or (value, label, detail)] as [{"value", "label", "detail"}]."""
    out = []
    for value, label, *rest in pairs:
        out.append({"value": str(value), "label": label, "detail": rest[0] if rest else None})
    return out
