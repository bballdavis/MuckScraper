"""The shapes a settings page is made of, as JSON for the app.

A page is a list of sections. A section can hold read-only facts, fields the
reader edits, a list of items (each opening a page of its own, with an
optional on/off switch), and action buttons. The app draws every page with
the same native controls, so a setting added here shows up on the phone
without an app release.

Field types: toggle, choice, multichoice, text, url, email, number, decimal,
secret, password, multiline. Choice values are always strings. A secret's value is
never sent: the field says whether one is saved and its last four
characters, and the app sends a new value, "" to remove it, or nothing to
keep it.

A section's `save` says how its fields are saved: "auto" sends each change on
its own (switches and pickers), "form" waits for its Save button.
"""
from .. import serializers as ser


class FieldError(Exception):
    """A value the server won't take: 422 with the message, naming the field."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


def _clean(d):
    return {k: v for k, v in d.items() if v is not None}


def choices(pairs):
    """[(value, label) or (value, label, detail)] as choice objects."""
    out = []
    for pair in pairs:
        value, label, *rest = pair
        out.append(_clean({"value": str(value), "label": label, "detail": rest[0] if rest else None}))
    return out


def field(key, label, type, value=None, *, placeholder=None, help=None, options=None, min=None, max=None,
          unit=None, required=None, secret=None):
    return _clean({
        "key": key, "label": label, "type": type, "value": value, "placeholder": placeholder or None,
        "help": help or None, "choices": choices(options) if options is not None else None,
        "min": min, "max": max, "unit": unit, "required": required, "secret": secret,
    })


def toggle(key, label, value, **kw):
    return field(key, label, "toggle", bool(value), **kw)


def choice(key, label, value, options, **kw):
    return field(key, label, "choice", None if value is None else str(value), options=options, **kw)


def multichoice(key, label, values, options, **kw):
    return field(key, label, "multichoice", [str(v) for v in values], options=options, **kw)


def text(key, label, value="", **kw):
    return field(key, label, "text", value or "", **kw)


def url(key, label, value="", **kw):
    return field(key, label, "url", value or "", **kw)


def email(key, label, value="", **kw):
    return field(key, label, "email", value or "", **kw)


def number(key, label, value, **kw):
    return field(key, label, "number", value, **kw)


def decimal(key, label, value="", **kw):
    """A number with a decimal point, kept as text (dollar amounts)."""
    return field(key, label, "decimal", value or "", **kw)


def multiline(key, label, value="", **kw):
    return field(key, label, "multiline", value or "", **kw)


def password(key, label, **kw):
    return field(key, label, "password", "", **kw)


def secret(key, label, saved=False, hint=None, note=None, **kw):
    """A write-only key. `note` says where a value comes from when none is
    saved here (an environment variable, say)."""
    return field(key, label, "secret", None, secret=_clean({"saved": bool(saved), "hint": hint or None,
                                                             "note": note or None}), **kw)


def fact(label, value, tone=None):
    """A read-only row. tone: ok, warn, bad, or None."""
    return _clean({"label": label, "value": "—" if value in (None, "") else str(value), "tone": tone})


def badge(text_, tone=None):
    return _clean({"text": text_, "tone": tone}) if text_ else None


def item(id, title, *, subtitle=None, detail=None, badge=None, page=None, active=None, remove=None,
         remove_label=None):
    """A list row. `page` is the page it opens; `active` (True/False) gives it
    a switch, which saves {"active": value} to that page. `remove` names the
    page action a swipe runs with {"id": id}; `remove_label` is its button
    ("Remove" when not given)."""
    return _clean({"id": str(id), "title": title, "subtitle": subtitle or None, "detail": detail,
                   "badge": badge, "page": page, "active": active, "remove": remove,
                   "remove_label": remove_label if remove else None})


def action(key, label, *, role=None, confirm=None, fields=None):
    """A button. `fields` are asked for first, in a sheet; `role` is
    "destructive" for one that removes something; `confirm` asks first."""
    return _clean({"key": key, "label": label, "role": role, "confirm": confirm, "fields": fields})


def section(key, title=None, *, footer=None, facts=None, fields=None, save=None, items=None, empty=None,
            reorder=None, actions=None):
    if fields and save is None:
        save = "form"
    return _clean({
        "key": key, "title": title, "footer": footer, "facts": facts or None, "fields": fields or None,
        "save": save if fields else None, "items": items, "empty": empty if items is not None else None,
        "reorder": reorder or None, "actions": actions or None,
    })


def page(key, title, sections, *, notice=None, tone=None):
    return _clean({"key": key, "title": title, "sections": [s for s in sections if s],
                   "notice": _clean({"text": notice, "tone": tone}) if notice else None})


def when(value):
    """A UTC datetime as ISO 8601, for facts the app formats itself."""
    return ser.iso(value)


# Reading values sent by the app ---------------------------------------------------------

def get_bool(values, key):
    value = values.get(key)
    if not isinstance(value, bool):
        raise FieldError("Must be on or off.", key)
    return value


def get_str(values, key, max_len=300, required=False, label=None):
    value = values.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise FieldError("Must be text.", key)
    value = value.strip()
    if len(value) > max_len:
        raise FieldError(f"{label or 'This'} can be at most {max_len} characters.", key)
    if required and not value:
        raise FieldError(f"{label or 'This'} can't be empty.", key)
    return value


def get_int(values, key, low, high, label=None):
    value = values.get(key)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        prefix = f"{label}: enter" if label else "Enter"
        raise FieldError(f"{prefix} a whole number from {low} to {high}.", key)
    return value


def get_choice(values, key, allowed):
    value = values.get(key)
    if value is not None and not isinstance(value, str):
        value = str(value)
    if value not in {str(a) for a in allowed}:
        raise FieldError("Pick one of the choices.", key)
    return value


def get_list(values, key, allowed=None):
    value = values.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise FieldError("Must be a list.", key)
    if allowed is not None:
        allowed = {str(a) for a in allowed}
        if any(v not in allowed for v in value):
            raise FieldError("Pick from the choices.", key)
    return value
