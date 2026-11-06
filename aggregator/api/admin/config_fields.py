"""Settings saved through aggregator/config_store.py (LLM providers and
source API keys) as JSON fields, saved back the way the web form does
(blueprints/admin/llm_settings.save_fields_form): a key is write-only, and a
setting still following its environment variable or default is left alone
unless it's changed."""
from flask import g

from aggregator import config_store, db

from .values import Invalid


def field(name, label, kind, state, help=None):
    """One setting: where its value comes from ("saved", "env" or "unset"),
    the saved value, and what it falls back to. A key is never sent: only
    whether one is saved, its last four characters and the environment's."""
    out = {"name": name, "label": label, "kind": kind, "source": state.get("source", "unset"), "help": help}
    if kind == "secret":
        env = state.get("env_hint") or ""
        out["secret"] = {
            "saved": state.get("source") == "saved",
            "hint": state.get("hint") or None,
            "unreadable": bool(state.get("unreadable")),
            "env": bool(env),
            "env_hint": env if env and env != "set" else None,
        }
    else:
        out["value"] = state.get("saved") or ""
        out["fallback"] = state.get("env") or state.get("default") or ""
    return out


def fields(spec, states=None, help=None):
    states = config_store.field_states() if states is None else states
    return [field(name, label, kind, states.get(name, {}), (help or {}).get(name)) for name, label, kind in spec]


def save(spec, values):
    """Check every sent value, then save them all, or raise Invalid naming
    the first bad one. A key sent as "" is removed; one not sent is kept.
    Returns whether anything changed."""
    from .general import require_same_key
    if not isinstance(values, dict):
        raise Invalid("Send the settings as an object.")
    known = {name for name, _, _ in spec}
    unknown = set(values) - known
    if unknown:
        raise Invalid(f"Unknown setting: {sorted(unknown)[0][:60]}.")
    states = config_store.field_states()
    pending = {}
    for name, _, kind in spec:
        if name not in values:
            continue
        raw = values[name]
        if raw is None:
            value = ""
        elif isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            raise Invalid("Must be text.", name)
        else:
            value = str(raw).strip()
        state = states.get(name, {})
        if kind != "secret" and state.get("source") != "saved" and value and value in (state.get("env"),
                                                                                       state.get("default")):
            continue
        error = config_store.validate(name, value, kind)
        if error:
            raise Invalid(error, name)
        if kind == "secret" and value:
            require_same_key()
        pending[name] = value
    for name, value in pending.items():
        config_store.save(name, value, g.user.username)
    db.session.commit()
    config_store.reload_now()
    return bool(pending)
