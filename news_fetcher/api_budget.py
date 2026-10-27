# news_fetcher/api_budget.py
"""Daily request budgets and polling schedules for the news source APIs
(Settings › Source API keys): the New York Times, NewsAPI and GNews.

Each provider's key allows so many requests a day. Every request is a row in
source_api_calls, and one is made only while fewer than the provider's daily
limit fall in the last 24 hours. A rolling window stays under the limit
whenever the provider starts its day, and the ledger is shared by the
scheduler and the web container and survives restarts. Requests are spaced
`min_interval_seconds` apart, and a 429 pauses that provider for
PAUSE_AFTER_429.

Each provider also has a poll: one round of requests the scheduler makes on
its own schedule (NYT: one Times Wire request; NewsAPI and GNews: one request
per active scheduled fetch). The schedule is a list of (start hour, minutes
between polls) in Eastern time, more often in the morning and evening news
peaks, editable on the settings page. When the schedule as set would spend
more than the provider's poll share of the limit, polls are stretched to
fit, and past `slowdown` percent of the limit they come half as often again.

Settings, each read on every request so an edit applies without a restart:
<PREFIX>_API_KEY, <PREFIX>_API_DAILY_LIMIT, <PREFIX>_API_SCHEDULE
("0:20, 6:3, 10:6" style) and <PREFIX>_API_SLOWDOWN (a percent).
"""

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)
PAUSE_AFTER_429 = timedelta(minutes=15)
TIMEZONE = "America/New_York"
MIN_POLL_MINUTES = 1
MAX_POLL_MINUTES = 24 * 60
MAX_SCHEDULE_BANDS = 8
DEFAULT_SLOWDOWN = 75


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    prefix: str             # settings are <prefix>_API_KEY and so on
    default_limit: int
    max_limit: int          # the most the key allows; the limit can't go higher
    min_interval_seconds: float
    poll_kind: str          # the ledger kind a scheduled poll records
    # Share of the limit the scheduled polls may plan to use; the rest is
    # left for the provider's other kinds of request and for manual fetches.
    poll_share: float
    default_schedule: tuple
    # Calls left in the window that each kind leaves for the others.
    reserves: dict = field(default_factory=dict)
    lock_id: int = 0

    @property
    def key_setting(self):
        return f"{self.prefix}_API_KEY"

    @property
    def limit_setting(self):
        return f"{self.prefix}_API_DAILY_LIMIT"

    @property
    def schedule_setting(self):
        return f"{self.prefix}_API_SCHEDULE"

    @property
    def slowdown_setting(self):
        return f"{self.prefix}_API_SLOWDOWN"


PROVIDERS = {
    # 500 requests a day and 5 a minute, shared by every NYT API. The wire
    # stops with 25 left, so single lookups (an admin rescrape) still work;
    # lead-paragraph searches stop with 150 left, so the wire polls keep going.
    "nyt": Provider(
        key="nyt", name="New York Times", prefix="NYT", default_limit=500, max_limit=500,
        min_interval_seconds=12.5, poll_kind="wire", poll_share=0.7,
        default_schedule=((0, 20), (6, 3), (10, 6), (16, 3), (23, 20)),
        reserves={"lookup": 0, "wire": 25, "search": 150}, lock_id=4960513,
    ),
    # The free Developer plan allows 100 requests a day; paid plans more.
    "newsapi": Provider(
        key="newsapi", name="NewsAPI", prefix="NEWS", default_limit=100, max_limit=100000,
        min_interval_seconds=1, poll_kind="poll", poll_share=0.9,
        default_schedule=((0, 240), (6, 60), (10, 120), (16, 60), (23, 240)),
        reserves={"poll": 0, "manual": 0}, lock_id=4960514,
    ),
    # The free plan allows 100 requests a day, one a second, ten articles each.
    "gnews": Provider(
        key="gnews", name="GNews", prefix="GNEWS", default_limit=100, max_limit=100000,
        min_interval_seconds=1.2, poll_kind="poll", poll_share=0.9,
        default_schedule=((0, 240), (6, 60), (10, 120), (16, 60), (23, 240)),
        reserves={"poll": 0, "manual": 0}, lock_id=4960515,
    ),
}

_lock = threading.Lock()
# Keys a provider refused (401/403): no more requests on them until a
# different key is saved.
_rejected = {}


class Unavailable(Exception):
    """The API can't answer right now (budget spent, rate limited, key refused)."""


def provider(key):
    return PROVIDERS[key] if isinstance(key, str) else key


def _setting(name, default=""):
    """A saved admin setting, else the env var, else `default`."""
    try:
        from news_fetcher import saved_settings
    except ImportError:
        return os.environ.get(name, default)
    return saved_settings.get(name, default)


def _utcnow():
    return datetime.utcnow()


# Settings ---------------------------------------------------------------

def api_key(p):
    return (_setting(provider(p).key_setting) or "").strip()


def enabled(p):
    p = provider(p)
    key = api_key(p)
    return bool(key) and key != _rejected.get(p.key)


def key_rejected(p):
    p = provider(p)
    key = api_key(p)
    return bool(key) and key == _rejected.get(p.key)


def daily_limit(p):
    p = provider(p)
    try:
        limit = int(_setting(p.limit_setting, str(p.default_limit)) or p.default_limit)
    except ValueError:
        limit = p.default_limit
    return max(0, min(limit, p.max_limit))


def slowdown_share(p):
    p = provider(p)
    try:
        percent = int(_setting(p.slowdown_setting, str(DEFAULT_SLOWDOWN)) or DEFAULT_SLOWDOWN)
    except ValueError:
        percent = DEFAULT_SLOWDOWN
    return max(1, min(percent, 100)) / 100


def parse_schedule(text):
    """[(start hour, minutes)] from "0:20, 6:3", sorted by hour, or None
    when it isn't a valid schedule. A schedule always starts at hour 0: the
    first band is moved there if it doesn't."""
    bands = {}
    for part in (text or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        hour, sep, minutes = part.partition(":")
        if not sep or not hour.strip().isdigit() or not minutes.strip().isdigit():
            return None
        hour, minutes = int(hour), int(minutes)
        if not 0 <= hour <= 23 or not MIN_POLL_MINUTES <= minutes <= MAX_POLL_MINUTES:
            return None
        bands[hour] = minutes
    if not bands or len(bands) > MAX_SCHEDULE_BANDS:
        return None
    ordered = sorted(bands.items())
    if ordered[0][0] != 0:
        ordered[0] = (0, ordered[0][1])
    return ordered


def format_schedule(bands):
    return ", ".join(f"{hour}:{minutes}" for hour, minutes in bands)


def schedule(p):
    """The provider's poll schedule: the saved one, else its default."""
    p = provider(p)
    return parse_schedule(_setting(p.schedule_setting, "")) or list(p.default_schedule)


def _local(now):
    return now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(TIMEZONE))


def scheduled_interval(p, now=None):
    """Minutes between polls at `now` (UTC) by the schedule alone."""
    hour = _local(now or _utcnow()).hour
    return next(minutes for start, minutes in reversed(schedule(p)) if hour >= start)


def planned_per_day(bands, requests_per_poll=1):
    """Requests a day the schedule makes, at `requests_per_poll` a poll."""
    total = 0.0
    for i, (start, minutes) in enumerate(bands):
        end = bands[i + 1][0] if i + 1 < len(bands) else 24
        total += (end - start) * 60 / minutes
    return round(total * requests_per_poll)


def stretch(p, requests_per_poll=1):
    """How much longer than scheduled polls wait so the day's planned polls
    fit the provider's poll share of the limit (1.0 when they already fit)."""
    p = provider(p)
    allowed = daily_limit(p) * p.poll_share
    planned = planned_per_day(schedule(p), requests_per_poll)
    if allowed <= 0:
        return None
    return max(1.0, planned / allowed)


def poll_interval(p, now=None, requests_per_poll=1, used=None):
    """Minutes between polls at `now`: the schedule's, stretched to fit the
    budget, and doubled past the slowdown share of the limit."""
    p = provider(p)
    now = now or _utcnow()
    factor = stretch(p, requests_per_poll)
    if factor is None:
        return None
    minutes = scheduled_interval(p, now) * factor
    if used is None:
        used = window_usage(p, now)[0]
    if used >= slowdown_share(p) * daily_limit(p):
        minutes *= 2
    return minutes


# The ledger -------------------------------------------------------------
# Its own connection, so taking a slot never commits the caller's session.

def _ledger():
    from aggregator.models import SourceApiCall, db
    return db, SourceApiCall.__table__


def window_usage(p, now=None):
    """(calls in the last 24 hours, newest call time, newest 429 time)."""
    import sqlalchemy as sa
    p = provider(p)
    db, calls = _ledger()
    now = now or _utcnow()
    mine = calls.c.provider == p.key
    with db.engine.connect() as conn:
        used, last = conn.execute(
            sa.select(sa.func.count(), sa.func.max(calls.c.called_at)).where(mine, calls.c.called_at > now - WINDOW)
        ).one()
        last_429 = conn.execute(
            sa.select(sa.func.max(calls.c.called_at)).where(mine, calls.c.http_status == 429)
        ).scalar()
    return used, last, last_429


def take_slot(p, kind):
    """Wait out the spacing and record a call. Returns its ledger id, or
    None when this kind's share of the window is spent or the provider said
    429 recently."""
    import sqlalchemy as sa
    p = provider(p)
    db, calls = _ledger()
    limit = daily_limit(p)
    mine = calls.c.provider == p.key
    with _lock, db.engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            # Serializes the web and scheduler containers until this commits.
            conn.execute(sa.text(f"SELECT pg_advisory_xact_lock({int(p.lock_id)})"))
        now = _utcnow()
        used, last = conn.execute(
            sa.select(sa.func.count(), sa.func.max(calls.c.called_at)).where(mine, calls.c.called_at > now - WINDOW)
        ).one()
        last_429 = conn.execute(
            sa.select(sa.func.max(calls.c.called_at)).where(mine, calls.c.http_status == 429)
        ).scalar()
        if used >= limit - p.reserves.get(kind, 0):
            return None
        if last_429 and now - last_429 < PAUSE_AFTER_429:
            return None
        if last:
            wait = (last - now).total_seconds() + p.min_interval_seconds
            if wait > 0:
                time.sleep(wait)
        return conn.execute(
            calls.insert().values(provider=p.key, called_at=_utcnow(), kind=kind).returning(calls.c.id)
        ).scalar()


def record_result(call_id, http_status, items=None):
    db, calls = _ledger()
    with db.engine.begin() as conn:
        conn.execute(calls.update().where(calls.c.id == call_id).values(http_status=http_status, items=items))


def request(p, kind, url, params=None, headers=None, items_of=lambda body: None, key=None, timeout=20):
    """One budgeted GET. Returns the JSON body, or None on a 400 (a
    parameter the provider refuses). `items_of(body)` counts what it
    returned. Raises Unavailable when the budget is spent, on a 429, or when
    the key is refused."""
    p = provider(p)
    key = key if key is not None else api_key(p)
    call_id = take_slot(p, kind)
    if call_id is None:
        raise Unavailable(f"no {kind} requests left in the last 24 hours' budget of {daily_limit(p)}")
    # Keys can ride in the query string, and requests puts the full URL in
    # its error messages, so errors are re-raised without it.
    try:
        response = requests.get(url, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        record_result(call_id, None)
        raise RuntimeError(f"{type(exc).__name__} from {urlparse(url).path}") from None
    status = response.status_code
    if status == 429:
        record_result(call_id, status)
        raise Unavailable("rate limited (429)")
    if status in (401, 403):
        record_result(call_id, status)
        _rejected[p.key] = key
        raise Unavailable(f"key rejected (HTTP {status})")
    if status == 400:
        record_result(call_id, status)
        return None
    if status >= 400:
        record_result(call_id, status)
        raise RuntimeError(f"HTTP {status} from {urlparse(url).path}")
    body = response.json()
    record_result(call_id, status, items_of(body))
    return body


def last_poll(p):
    import sqlalchemy as sa
    p = provider(p)
    db, calls = _ledger()
    with db.engine.connect() as conn:
        return conn.execute(
            sa.select(sa.func.max(calls.c.called_at)).where(calls.c.provider == p.key, calls.c.kind == p.poll_kind)
        ).scalar()


def poll_due(p, now=None, requests_per_poll=1):
    """Whether the provider's next scheduled poll is due at `now`."""
    p = provider(p)
    if not enabled(p) or requests_per_poll <= 0:
        return False
    now = now or _utcnow()
    interval = poll_interval(p, now, requests_per_poll)
    if interval is None:
        return False
    last = last_poll(p)
    # A few seconds' slack, so a poll that took a while doesn't skip a beat.
    return not last or now - last >= timedelta(minutes=interval, seconds=-30)


def usage_summary(p, now=None, requests_per_poll=1):
    """For Settings › Source API keys: the last 24 hours by kind and by
    hour, and what the schedule plans."""
    import sqlalchemy as sa
    p = provider(p)
    db, calls = _ledger()
    now = now or _utcnow()
    since = now - WINDOW
    mine = calls.c.provider == p.key
    with db.engine.connect() as conn:
        rows = conn.execute(
            sa.select(calls.c.kind, sa.func.count(), sa.func.coalesce(sa.func.sum(calls.c["items"]), 0))
            .where(mine, calls.c.called_at > since).group_by(calls.c.kind)
        ).all()
        times = conn.execute(
            sa.select(calls.c.called_at, calls.c.http_status).where(mine, calls.c.called_at > since)
        ).all()
        last_429 = conn.execute(sa.select(sa.func.max(calls.c.called_at)).where(mine, calls.c.http_status == 429)).scalar()
    kinds = {kind: {"requests": n, "items": int(items)} for kind, n, items in rows}
    used = sum(k["requests"] for k in kinds.values())
    # 24 hourly buckets, oldest first, ending with the current hour.
    hours = [0] * 24
    errors = 0
    for called_at, status in times:
        age = int((now - called_at).total_seconds() // 3600)
        if 0 <= age < 24:
            hours[23 - age] += 1
        if status and status >= 400:
            errors += 1
    limit = daily_limit(p)
    bands = schedule(p)
    factor = stretch(p, requests_per_poll)
    interval = poll_interval(p, now, requests_per_poll, used=used) if factor else None
    last = last_poll(p)
    paused_until = last_429 + PAUSE_AFTER_429 if last_429 and now - last_429 < PAUSE_AFTER_429 else None
    poll = kinds.get(p.poll_kind, {"requests": 0, "items": 0})
    return {
        "limit": limit,
        "used": used,
        "left": max(0, limit - used),
        "share": min(1.0, used / limit) if limit else 1.0,
        "kinds": kinds,
        "hours": hours,
        "hour_max": max(hours) or 1,
        "errors": errors,
        "items_per_poll_request": round(poll["items"] / poll["requests"]) if poll["requests"] else None,
        "schedule": bands,
        "planned": planned_per_day(bands, requests_per_poll),
        "poll_budget": round(limit * p.poll_share),
        "stretch": factor,
        "interval": interval,
        "scheduled_interval": scheduled_interval(p, now),
        "slowed": used >= slowdown_share(p) * limit,
        "last_poll": last,
        "next_poll": (last + timedelta(minutes=interval)) if last and interval else None,
        "paused_until": paused_until,
        "requests_per_poll": requests_per_poll,
    }
