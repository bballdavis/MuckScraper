# news_fetcher/llm_spend.py
"""Spending caps for the paid LLM providers (Settings › LLM providers):
Anthropic, Gemini, Groq and OpenRouter.

Every call to one of them is a row in llm_usage with the tokens it used and
what it cost: OpenRouter reports the cost itself; for the others it is the
tokens times the provider's price per million tokens (the saved
<P>_PRICE_INPUT / <P>_PRICE_OUTPUT, else the built-in price for the model
when PRICES knows it). Before each call, llm_client asks allowed(): once
today's spend (Eastern time) reaches <P>_SPEND_DAILY_USD, or this calendar
month's reaches <P>_SPEND_MONTHLY_USD, the provider gets no more calls until
the day or month turns, and the call fails as if the provider were down.
Both caps are blank, so unlimited, by default. The check runs before a call,
so the last call before a cap can go over it by its own cost.

Ollama and LM Studio run on your own hardware and are never capped.
"""

import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

TIMEZONE = "America/New_York"
PROVIDERS = {"anthropic": "ANTHROPIC", "gemini": "GEMINI", "groq": "GROQ", "openrouter": "OPENROUTER"}

# Built-in prices, $ per million (input, output) tokens, by model id prefix;
# the longest matching prefix wins. Anthropic's first-party API rates as of
# 2026-10. Gemini, Groq and OpenRouter models aren't listed: their prices
# change too often to ship, so enter them on the settings page (OpenRouter
# needs none, it reports each call's cost).
PRICES = {
    "anthropic": {
        "claude-fable-5": (10.00, 50.00),
        "claude-opus-5-5": (4.00, 20.00),
        "claude-opus-5": (5.00, 25.00),
        "claude-opus-4": (5.00, 25.00),
        "claude-sonnet-5": (2.00, 10.00),
        "claude-sonnet-4": (3.00, 15.00),
        "claude-haiku-5-5": (0.10, 0.50),
        "claude-haiku-4-5": (1.00, 5.00),
    },
}

_LOG_EVERY_SECONDS = 600
_last_logged = {}


def _setting(name, default=""):
    try:
        from news_fetcher import saved_settings
    except ImportError:
        return os.environ.get(name, default)
    return saved_settings.get(name, default)


def _money(name):
    """A saved dollar amount, or None when blank or unreadable."""
    value = (_setting(name, "") or "").strip()
    if not value:
        return None
    try:
        amount = float(value)
    except ValueError:
        return None
    return amount if amount >= 0 else None


def caps(provider):
    """(daily cap, monthly cap) in dollars; None is unlimited."""
    prefix = PROVIDERS[provider]
    return _money(f"{prefix}_SPEND_DAILY_USD"), _money(f"{prefix}_SPEND_MONTHLY_USD")


def price(provider, model):
    """($ per million input tokens, $ per million output tokens) for
    `model`, or None when neither a saved price nor PRICES has one."""
    prefix = PROVIDERS[provider]
    saved_in, saved_out = _money(f"{prefix}_PRICE_INPUT"), _money(f"{prefix}_PRICE_OUTPUT")
    if saved_in is not None or saved_out is not None:
        return saved_in or 0.0, saved_out or 0.0
    table = PRICES.get(provider, {})
    match = max((p for p in table if (model or "").startswith(p)), key=len, default=None)
    return table[match] if match else None


def cost(provider, model, input_tokens, output_tokens, reported=None):
    """A call's cost in dollars: the provider's own figure when it gave one,
    else tokens times price. None when there's no price to go on."""
    if reported is not None:
        return float(reported)
    rates = price(provider, model)
    if rates is None:
        return None
    return ((input_tokens or 0) * rates[0] + (output_tokens or 0) * rates[1]) / 1_000_000


def _utcnow():
    return datetime.utcnow()


def period_starts(now=None):
    """(start of today, start of this month) in Eastern time, as naive UTC."""
    now = now or _utcnow()
    local = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(TIMEZONE))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    to_utc = lambda dt: dt.astimezone(timezone.utc).replace(tzinfo=None)  # noqa: E731
    return to_utc(day), to_utc(month)


def _ledger():
    from aggregator.models import LlmUsage, db
    return db, LlmUsage.__table__


def _can_use_db():
    try:
        from flask import has_app_context
    except ImportError:
        return False
    return has_app_context()


def spent(provider, now=None):
    """(dollars today, dollars this month, calls today, calls this month)."""
    import sqlalchemy as sa
    db, usage = _ledger()
    day, month = period_starts(now)
    mine = usage.c.provider == provider
    with db.engine.connect() as conn:
        today = conn.execute(
            sa.select(sa.func.coalesce(sa.func.sum(usage.c.cost_usd), 0), sa.func.count())
            .where(mine, usage.c.called_at >= day)
        ).one()
        this_month = conn.execute(
            sa.select(sa.func.coalesce(sa.func.sum(usage.c.cost_usd), 0), sa.func.count())
            .where(mine, usage.c.called_at >= month)
        ).one()
    return float(today[0]), float(this_month[0]), today[1], this_month[1]


def blocked(provider, now=None):
    """Why `provider` gets no more calls now ("daily" or "monthly"), or
    None. Unknown providers, no caps, or no database: never blocked."""
    if provider not in PROVIDERS:
        return None
    daily, monthly = caps(provider)
    if daily is None and monthly is None:
        return None
    if not _can_use_db():
        return None
    try:
        today, month, _, _ = spent(provider, now)
    except Exception:
        logger.exception("[llm_spend] Could not read %s spend; not capping this call", provider)
        return None
    if daily is not None and today >= daily:
        return "daily"
    if monthly is not None and month >= monthly:
        return "monthly"
    return None


def allowed(provider):
    """Whether a call to `provider` may go ahead. Logs a refusal at most
    every _LOG_EVERY_SECONDS per provider."""
    reason = blocked(provider)
    if reason is None:
        return True
    if time.monotonic() - _last_logged.get(provider, -_LOG_EVERY_SECONDS) >= _LOG_EVERY_SECONDS:
        _last_logged[provider] = time.monotonic()
        logger.warning("[llm_spend] %s reached its %s spending cap; skipping calls until it resets",
                       provider, reason)
    return False


def record(provider, model, kind, input_tokens=None, output_tokens=None, reported_cost=None):
    """Keep one call's usage. Never raises: a call that worked isn't failed
    for want of a ledger row."""
    if provider not in PROVIDERS or not _can_use_db():
        return
    try:
        db, usage = _ledger()
        amount = cost(provider, model, input_tokens, output_tokens, reported_cost)
        with db.engine.begin() as conn:
            conn.execute(usage.insert().values(
                provider=provider, model=(model or "")[:200], kind=kind, called_at=_utcnow(),
                input_tokens=input_tokens, output_tokens=output_tokens,
                cost_usd=amount, priced=amount is not None,
            ))
    except Exception:
        logger.exception("[llm_spend] Could not record a %s call", provider)


def summary(provider, now=None):
    """For Settings › LLM providers: spend against the caps, and whether
    calls can be priced."""
    import sqlalchemy as sa
    db, usage = _ledger()
    today, month, calls_today, calls_month = spent(provider, now)
    day_start, month_start = period_starts(now)
    with db.engine.connect() as conn:
        unpriced = conn.execute(
            sa.select(sa.func.count()).where(usage.c.provider == provider, usage.c.called_at >= month_start,
                                             usage.c.priced.is_(False))
        ).scalar()
        tokens = conn.execute(
            sa.select(sa.func.coalesce(sa.func.sum(usage.c.input_tokens), 0),
                      sa.func.coalesce(sa.func.sum(usage.c.output_tokens), 0))
            .where(usage.c.provider == provider, usage.c.called_at >= day_start)
        ).one()
    daily, monthly = caps(provider)
    return {
        "today": today, "month": month, "calls_today": calls_today, "calls_month": calls_month,
        "tokens_in_today": int(tokens[0]), "tokens_out_today": int(tokens[1]),
        "daily_cap": daily, "monthly_cap": monthly, "unpriced": unpriced,
        "blocked": blocked(provider, now),
        "daily_share": min(1.0, today / daily) if daily else None,
        "monthly_share": min(1.0, month / monthly) if monthly else None,
    }
