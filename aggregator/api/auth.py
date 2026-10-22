"""Device tokens and pairing codes.

A token is "msk_" plus 32 random bytes (url-safe base64). The database holds
only its SHA-256, compared with hmac.compare_digest. A pairing code is ten
characters from an unambiguous alphabet, also stored hashed, valid for
PAIRING_TTL and usable once.
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import g, request
from sqlalchemy import text

from aggregator import db
from aggregator.models import ApiDevice, ApiPairingCode, User

from .errors import ApiError

TOKEN_PREFIX = "msk_"
PAIRING_TTL = timedelta(minutes=10)
# Device tokens last a year unless revoked sooner; pairing again renews.
TOKEN_TTL = timedelta(days=365)
# No 0/O, 1/I/L: the code may be typed from a screen.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 10
# last_used_at/last_ip are written at most this often per device.
_TOUCH_EVERY = timedelta(minutes=1)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def new_token():
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def normalize_code(code):
    return "".join(c for c in (code or "").upper() if c.isalnum())


def create_pairing_code(user, scopes, created_by):
    """(plaintext code, ApiPairingCode). Caller commits."""
    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LENGTH))
    row = ApiPairingCode(
        user_id=user.id, code_hash=_hash(code), scopes=" ".join(scopes), created_by=created_by,
        expires_at=datetime.utcnow() + PAIRING_TTL,
    )
    db.session.add(row)
    return code, row


def redeem_pairing_code(code, device_name, ip):
    """Trade a code for (plaintext token, ApiDevice), or None. The UPDATE ...
    WHERE used_at IS NULL claims the code atomically, so two phones racing
    with the same code can't both win."""
    code = normalize_code(code)
    if len(code) != CODE_LENGTH:
        return None
    now = datetime.utcnow()
    row = db.session.execute(
        text("UPDATE api_pairing_codes SET used_at = :now "
             "WHERE code_hash = :hash AND used_at IS NULL AND expires_at > :now "
             "RETURNING id, user_id, scopes"),
        {"now": now, "hash": _hash(code)},
    ).first()
    if row is None:
        db.session.rollback()
        return None
    user = User.query.get(row.user_id)
    if user is None or not user.is_active:
        db.session.rollback()
        return None
    token = new_token()
    device = ApiDevice(
        user_id=user.id, name=(device_name or "Phone").strip()[:80] or "Phone", token_hash=_hash(token),
        scopes=row.scopes, created_at=now, expires_at=now + TOKEN_TTL, last_used_at=now, last_ip=ip,
    )
    db.session.add(device)
    db.session.flush()
    db.session.execute(text("UPDATE api_pairing_codes SET device_id = :d WHERE id = :id"),
                       {"d": device.id, "id": row.id})
    db.session.commit()
    return token, device


def device_for_token(token):
    """The live device for a bearer token, or None."""
    if not token or not token.startswith(TOKEN_PREFIX) or len(token) > 200:
        return None
    digest = _hash(token)
    device = ApiDevice.query.filter_by(token_hash=digest).first()
    if device is None or not hmac.compare_digest(device.token_hash, digest):
        return None
    if not device.is_live or device.user is None or not device.user.is_active:
        return None
    return device


def _bearer():
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def require_token(scope="read"):
    """Decorator: a live device token with `scope`. Sets g.device and g.user."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            device = device_for_token(_bearer())
            if device is None:
                raise ApiError(401, "unauthorized", "A valid device token is required.",
                               {"WWW-Authenticate": 'Bearer realm="muckscraper"'})
            if scope not in device.scope_list:
                raise ApiError(403, "forbidden", "This device can't do that.")
            g.device = device
            g.user = device.user
            from .ratelimit import check_device
            check_device(device)
            now = datetime.utcnow()
            if device.last_used_at is None or now - device.last_used_at > _TOUCH_EVERY or device.last_ip != request.remote_addr:
                device.last_used_at = now
                device.last_ip = (request.remote_addr or "")[:64]
                db.session.commit()
            return view(*args, **kwargs)
        return wrapped
    return decorator
