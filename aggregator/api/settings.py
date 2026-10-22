"""Mobile API settings, set on Settings › Mobile API in the web app and read
by the API process. One JSON AppSetting, reread at most every RELOAD_SECONDS
so a change (turning the API off, a new limit) applies without a restart."""
import json
import time

from aggregator.models import AppSetting

KEY = "mobile_api_v1"
RELOAD_SECONDS = 15

DEFAULTS = {
    # Off answers every endpoint but /health with 503.
    "enabled": True,
    # Whether /articles/{id} includes the scraped article text. Off by
    # default, like the web app for signed-out visitors: summaries and the
    # publisher's link only.
    "full_text": False,
    # The address the phone uses, put into the pairing QR code.
    "public_url": "",
    "per_device_per_minute": 120,
    "per_ip_per_minute": 300,
    "pair_per_ip_per_hour": 10,
}
LIMIT_FIELDS = ("per_device_per_minute", "per_ip_per_minute", "pair_per_ip_per_hour")

_cache = {"at": 0.0, "value": None}


def clean(raw):
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    for name in ("enabled", "full_text"):
        if isinstance(raw.get(name), bool):
            out[name] = raw[name]
    if isinstance(raw.get("public_url"), str):
        out["public_url"] = raw["public_url"].strip()[:300]
    for name in LIMIT_FIELDS:
        value = raw.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 100000:
            out[name] = value
    return out


def load(fresh=False):
    now = time.monotonic()
    if not fresh and _cache["value"] is not None and now - _cache["at"] < RELOAD_SECONDS:
        return _cache["value"]
    row = AppSetting.query.get(KEY)
    try:
        value = clean(json.loads(row.value)) if row and row.value else dict(DEFAULTS)
    except ValueError:
        value = dict(DEFAULTS)
    _cache.update(at=now, value=value)
    return value


def save(values):
    """Store `values` (merged over the current settings). Caller commits."""
    from aggregator import db
    merged = clean({**load(fresh=True), **values})
    row = AppSetting.query.get(KEY)
    if row is None:
        row = AppSetting(key=KEY)
        db.session.add(row)
    row.value = json.dumps(merged)
    _cache.update(at=0.0, value=None)
    return merged
