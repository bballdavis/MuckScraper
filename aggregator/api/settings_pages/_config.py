"""Settings saved through aggregator/config_store.py (LLM providers and
source API keys) as page fields, and saved back the way the web form does
(blueprints/admin/llm_settings.save_fields_form): a key is write-only, and a
setting still following its environment variable or default is left alone
unless it's changed."""
from flask import g

from aggregator import config_store, db

from . import schema as s
from .schema import FieldError

# config_store kinds that are whole numbers on the phone.
_NUMBERS = {"int": (1, 100000, None), "percent": (1, 100, "%")}


def fields(spec, states, help=None):
    """Page fields for [(name, label, kind)]. The saved value is the value;
    the environment's or the default is the placeholder."""
    out = []
    for name, label, kind in spec:
        st = states.get(name, {})
        tip = (help or {}).get(name)
        if kind == "secret":
            env = st.get("env_hint")
            note = f"Using {name} from the environment (…{env})" if env and env != "set" else (
                f"Using {name} from the environment" if env else None)
            out.append(s.secret(name, label, st.get("source") == "saved", st.get("hint"), note=note, help=tip))
            continue
        fallback = st.get("env") or st.get("default") or ""
        saved = st.get("saved") or ""
        if kind in _NUMBERS:
            low, high, unit = _NUMBERS[kind]
            value = int(saved) if saved.isdigit() else None
            out.append(s.number(name, label, value, min=low, max=high, unit=unit,
                                placeholder=fallback or None, help=tip))
        elif kind == "money":
            out.append(s.decimal(name, label, saved, unit="USD", placeholder=fallback or "No cap", help=tip))
        elif kind == "url":
            out.append(s.url(name, label, saved, placeholder=fallback, help=tip))
        else:
            out.append(s.text(name, label, saved, placeholder=fallback, help=tip))
    return out


def save(spec, values):
    """Validate every sent field, then save them all, or raise FieldError."""
    states = config_store.field_states()
    pending = {}
    for name, _, kind in spec:
        if name not in values:
            continue
        raw = values[name]
        if raw is None:
            value = ""
        elif isinstance(raw, bool):
            raise FieldError("Must be text.", name)
        else:
            value = str(raw).strip()
        st = states.get(name, {})
        if kind != "secret" and st.get("source") != "saved" and value and value in (st.get("env"), st.get("default")):
            continue
        error = config_store.validate(name, value, kind)
        if error:
            raise FieldError(error, name)
        pending[name] = value
    for name, value in pending.items():
        config_store.save(name, value, g.user.username)
    db.session.commit()
    config_store.reload_now()
    return bool(pending)
