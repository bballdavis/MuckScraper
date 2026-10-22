"""Fixed-window rate limits in Postgres (api_rate_counters), so every
gunicorn worker shares one count without Redis. One upsert per check."""
import random
from datetime import datetime, timedelta

from flask import request
from sqlalchemy import text

from aggregator import db

from . import settings
from .errors import ApiError


def _hit(key, window_seconds):
    """Count one request against `key`; returns (count, seconds left in window)."""
    now = datetime.utcnow()
    epoch = int(now.timestamp())
    start = datetime.utcfromtimestamp(epoch - epoch % window_seconds)
    count = db.session.execute(
        text("INSERT INTO api_rate_counters (key, window_start, count) VALUES (:key, :start, 1) "
             "ON CONFLICT (key, window_start) DO UPDATE SET count = api_rate_counters.count + 1 "
             "RETURNING count"),
        {"key": key[:120], "start": start},
    ).scalar()
    # Now and then drop windows older than a day.
    if random.random() < 0.01:
        db.session.execute(text("DELETE FROM api_rate_counters WHERE window_start < :cutoff"),
                           {"cutoff": now - timedelta(days=1)})
    db.session.commit()
    return count, window_seconds - (epoch % window_seconds)


def _limit(key, limit, window_seconds):
    count, retry_after = _hit(key, window_seconds)
    if count > limit:
        raise ApiError(429, "rate_limited", "Too many requests.", {"Retry-After": str(retry_after)})


def check_ip():
    _limit(f"ip:{request.remote_addr}", settings.load()["per_ip_per_minute"], 60)


def check_device(device):
    _limit(f"device:{device.id}", settings.load()["per_device_per_minute"], 60)


def check_pairing():
    _limit(f"pair:{request.remote_addr}", settings.load()["pair_per_ip_per_hour"], 3600)


def check_summaries(user):
    _limit(f"summarize:{user.id}", settings.load()["summaries_per_user_per_day"], 86400)


def check_schedule_runs(user):
    _limit(f"schedule_run:{user.id}", settings.load()["schedule_runs_per_user_per_day"], 86400)
