"""Tests for the shared source API budget (news_fetcher/api_budget.py), the
NewsAPI and GNews polls on it (fetch_and_store_articles/providers.py) and
Settings › Source API keys.

The schedule arithmetic runs without a database. The ledger, poll, ingest
and page tests run against the scratch Postgres database named by
API_TEST_DATABASE_URL, like tests/test_nyt_api.py, and are skipped without
it.
"""

import dataclasses
import json
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

from news_fetcher import api_budget

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


class _Response:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {"articles": []}

    def json(self):
        return self._body


class _Settings(unittest.TestCase):
    def setUp(self):
        api_budget._rejected.clear()
        self.settings = {}
        patcher = mock.patch.object(api_budget, "_setting",
                                    side_effect=lambda name, default="": self.settings.get(name, default))
        patcher.start()
        self.addCleanup(patcher.stop)


class ScheduleTests(_Settings):
    def test_parse_sorts_and_starts_at_midnight(self):
        self.assertEqual(api_budget.parse_schedule("16:3, 6:3; 10:6"), [(0, 3), (10, 6), (16, 3)])
        self.assertEqual(api_budget.parse_schedule("0:240"), [(0, 240)])

    def test_parse_rejects_nonsense(self):
        for text in ("", "6", "6:0", "24:5", "a:b", "6:3, 7:5000", ", ".join(f"{h}:5" for h in range(9))):
            self.assertIsNone(api_budget.parse_schedule(text), text)

    def test_planned_per_day_counts_each_band(self):
        self.assertEqual(api_budget.planned_per_day([(0, 60)]), 24)
        self.assertEqual(api_budget.planned_per_day([(0, 60), (12, 30)], requests_per_poll=5), (12 + 24) * 5)

    def test_saved_schedule_wins_and_bad_one_falls_back(self):
        nyt = api_budget.PROVIDERS["nyt"]
        self.assertEqual(api_budget.schedule(nyt), list(nyt.default_schedule))
        self.settings["NYT_API_SCHEDULE"] = "0:30, 12:10"
        self.assertEqual(api_budget.schedule(nyt), [(0, 30), (12, 10)])
        self.settings["NYT_API_SCHEDULE"] = "nonsense"
        self.assertEqual(api_budget.schedule(nyt), list(nyt.default_schedule))

    def test_scheduled_interval_uses_eastern_time(self):
        # 12:00 UTC is 8am in New York on 2026-10-27 (EDT).
        self.assertEqual(api_budget.scheduled_interval("newsapi", datetime(2026, 10, 27, 12, 0)), 60)
        self.assertEqual(api_budget.scheduled_interval("newsapi", datetime(2026, 10, 27, 7, 0)), 240)

    def test_default_schedules_fit_their_budgets(self):
        self.assertEqual(api_budget.stretch("nyt"), 1.0)
        # Five scheduled fetches a poll, as seeded.
        self.assertEqual(api_budget.stretch("newsapi", requests_per_poll=5), 1.0)
        self.assertEqual(api_budget.stretch("gnews", requests_per_poll=5), 1.0)

    def test_a_schedule_over_budget_stretches_to_fit(self):
        self.settings["GNEWS_API_SCHEDULE"] = "0:10"  # 144 polls a day
        self.settings["GNEWS_API_DAILY_LIMIT"] = "100"
        factor = api_budget.stretch("gnews", requests_per_poll=1)
        self.assertAlmostEqual(factor, 144 / 90)
        at = datetime(2026, 10, 27, 12, 0)
        self.assertAlmostEqual(api_budget.poll_interval("gnews", at, 1, used=0), 10 * factor)

    def test_past_the_slowdown_share_polls_come_half_as_often(self):
        self.settings["NEWS_API_SLOWDOWN"] = "50"
        at = datetime(2026, 10, 27, 12, 0)
        self.assertEqual(api_budget.poll_interval("newsapi", at, 1, used=49), 60)
        self.assertEqual(api_budget.poll_interval("newsapi", at, 1, used=50), 120)

    def test_limits(self):
        self.settings["NYT_API_DAILY_LIMIT"] = "5000"
        self.assertEqual(api_budget.daily_limit("nyt"), 500)
        self.settings["NEWS_API_DAILY_LIMIT"] = "5000"  # a paid plan
        self.assertEqual(api_budget.daily_limit("newsapi"), 5000)
        self.assertEqual(api_budget.daily_limit("gnews"), 100)

    def test_no_key_never_polls(self):
        self.assertFalse(api_budget.poll_due("gnews", requests_per_poll=5))


class ConfigFieldTests(unittest.TestCase):
    def test_source_fields_and_validation(self):
        from aggregator import config_store
        names = [name for name, _, _ in config_store.SOURCE_FIELDS["gnews"]]
        self.assertEqual(names, ["GNEWS_API_KEY", "GNEWS_API_DAILY_LIMIT", "GNEWS_API_SLOWDOWN", "GNEWS_API_SCHEDULE"])
        self.assertEqual(config_store.DEFAULTS["NYT_API_SCHEDULE"], "0:20, 6:3, 10:6, 16:3, 23:20")
        self.assertIsNone(config_store.validate("GNEWS_API_SCHEDULE", "0:60, 6:30", "schedule"))
        self.assertIsNotNone(config_store.validate("GNEWS_API_SCHEDULE", "6:0", "schedule"))
        self.assertIsNone(config_store.validate("GNEWS_API_SLOWDOWN", "80", "percent"))
        self.assertIsNotNone(config_store.validate("GNEWS_API_SLOWDOWN", "180", "percent"))


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the budget ledger and poll tests")
class BudgetDatabaseTests(_Settings):
    @classmethod
    def setUpClass(cls):
        env = {"DATABASE_URL": TEST_DB, "SECRET_KEY": "test-secret"}
        with mock.patch.dict(os.environ, env):
            from aggregator import create_app, db
            cls.app = create_app()
            # Builds the app on import, so it needs the same environment.
            import news_fetcher.fetch_and_store_articles  # noqa: F401
        cls.app.config.update(WTF_CSRF_ENABLED=False, TESTING=True)
        with cls.app.app_context():
            db.drop_all()
            db.create_all()
            from aggregator.models import ScheduledFetch, User
            admin = User(username="admin", email="a@example.com", role="admin")
            admin.set_password("password123")
            db.session.add_all([
                admin,
                ScheduledFetch(label="US Politics", mode="query", newsapi_query="congress",
                               gnews_query="congress", is_active=True, sort_order=1),
                ScheduledFetch(label="Business", mode="top", newsapi_country="us", newsapi_category="business",
                               gnews_category="business", is_active=True, sort_order=2),
            ])
            db.session.commit()
            cls.admin_id = admin.id

    @classmethod
    def tearDownClass(cls):
        from news_fetcher import llm_client, saved_settings
        saved_settings.set_source(None)
        llm_client._sync_config()

    def setUp(self):
        super().setUp()
        from aggregator import db
        self.ctx = self.app.app_context()
        self.ctx.push()
        for table in ("source_api_calls", "raw_article_payloads", "config_settings"):
            db.session.execute(db.text(f"DELETE FROM {table}"))
        db.session.commit()
        self.settings.update(NEWS_API_KEY="news-key", GNEWS_API_KEY="gnews-key")
        fast = {k: dataclasses.replace(p, min_interval_seconds=0) for k, p in api_budget.PROVIDERS.items()}
        patcher = mock.patch.dict(api_budget.PROVIDERS, fast)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        from aggregator import db
        db.session.remove()
        self.ctx.pop()

    def _call(self, provider, kind="poll", minutes_ago=1, status=200):
        from aggregator import db
        from aggregator.models import SourceApiCall
        db.session.add(SourceApiCall(provider=provider, kind=kind, http_status=status,
                                     called_at=datetime.utcnow() - timedelta(minutes=minutes_ago)))
        db.session.commit()

    def test_each_provider_counts_only_its_own_calls(self):
        for _ in range(3):
            self._call("newsapi")
        self._call("nyt", kind="wire")
        self._call("gnews", minutes_ago=25 * 60)
        self.assertEqual(api_budget.window_usage("newsapi")[0], 3)
        self.assertEqual(api_budget.window_usage("gnews")[0], 0)
        self.settings["NEWS_API_DAILY_LIMIT"] = "3"
        self.assertIsNone(api_budget.take_slot("newsapi", "poll"))
        self.assertIsNotNone(api_budget.take_slot("gnews", "poll"))

    def test_a_429_pauses_only_that_provider(self):
        self._call("gnews", status=429)
        self.assertIsNone(api_budget.take_slot("gnews", "poll"))
        self.assertIsNotNone(api_budget.take_slot("newsapi", "poll"))

    def test_poll_keeps_payloads_one_request_per_fetch(self):
        from aggregator.models import RawArticlePayload, SourceApiCall
        from news_fetcher.fetch_and_store_articles import providers
        body = {"articles": [{"title": "A", "url": "https://example.com/a", "source": {"name": "Ex"}}]}
        with mock.patch.object(api_budget.requests, "get", return_value=_Response(body=body)) as get:
            polled = providers.poll_news_apis_if_due()
        self.assertEqual(set(polled), {"newsapi", "gnews"})
        self.assertEqual(get.call_count, 4)
        newsapi_call = next(c for c in get.call_args_list if "newsapi" in c.args[0])
        self.assertEqual(newsapi_call.kwargs["headers"], {"X-Api-Key": "news-key"})
        self.assertEqual(RawArticlePayload.query.filter(RawArticlePayload.ingested_at.is_(None)).count(), 4)
        self.assertEqual(SourceApiCall.query.filter_by(provider="gnews", kind="poll").count(), 2)
        # Just polled: nothing is due again.
        with mock.patch.object(api_budget.requests, "get") as get:
            self.assertEqual(providers.poll_news_apis_if_due(), {"skipped": "not due"})
        get.assert_not_called()

    def test_spent_budget_stops_a_poll(self):
        from aggregator.models import RawArticlePayload
        from news_fetcher.fetch_and_store_articles import providers
        self.settings["NEWS_API_DAILY_LIMIT"] = "1"
        with mock.patch.object(api_budget.requests, "get", return_value=_Response()) as get:
            metrics = providers._poll("newsapi", providers.scheduled_fetches())
        self.assertEqual(get.call_count, 1)
        self.assertEqual(metrics["status"], "unavailable")
        self.assertEqual(RawArticlePayload.query.count(), 1)

    def test_errors_never_carry_the_key(self):
        error = api_budget.requests.ConnectionError("Max retries exceeded with url: /x?apikey=gnews-key")
        with mock.patch.object(api_budget.requests, "get", side_effect=error), \
                self.assertRaises(RuntimeError) as raised:
            api_budget.request("gnews", "poll", "https://gnews.io/api/v4/search", params={"apikey": "gnews-key"})
        self.assertNotIn("gnews-key", str(raised.exception))

    def test_ingest_merges_polls_by_url_and_marks_them(self):
        from aggregator import db
        from aggregator.models import RawArticlePayload
        from news_fetcher.fetch_and_store_articles import providers
        article = lambda n: {"title": f"T{n}", "url": f"https://example.com/{n}", "source": {"name": "Ex"},  # noqa: E731
                             "publishedAt": "2026-10-27T08:00:00Z"}
        old = datetime.utcnow() - timedelta(hours=40)
        db.session.add_all([
            RawArticlePayload(source="newsapi", topic_name="Business", payload=json.dumps({"articles": [article(1), article(2)]})),
            RawArticlePayload(source="newsapi", topic_name="Business", payload=json.dumps({"articles": [article(2), article(3)]})),
            RawArticlePayload(source="gnews", topic_name="Business", payload=json.dumps({"articles": [article(4)]})),
            RawArticlePayload(source="gnews", topic_name="Old", payload=json.dumps({"articles": [article(5)]}), fetched_at=old),
        ])
        db.session.commit()
        stored = []
        with mock.patch.object(providers, "store_articles",
                               side_effect=lambda arts, topic, provider: stored.append((provider, topic, len(arts))) or
                               {"input_articles": len(arts), "stored": len(arts)}):
            topics = providers.ingest_payloads()
        self.assertEqual(sorted(stored), [("gnews", "Business", 1), ("newsapi", "Business", 3)])
        self.assertEqual(set(topics["Business"]["providers"]), {"newsapi", "gnews"})
        self.assertEqual(RawArticlePayload.query.filter(RawArticlePayload.ingested_at.is_(None)).count(), 1)
        with mock.patch.object(providers, "store_articles") as again:
            self.assertEqual(providers.ingest_payloads(), {})
        again.assert_not_called()

    def _client(self):
        from flask import g
        g.pop("_login_user", None)
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.admin_id)
        return client

    def test_page_shows_each_provider_and_saves_a_schedule(self):
        from aggregator import config_store
        from aggregator.models import ConfigSetting
        self._call("newsapi")
        with mock.patch.dict(os.environ, {"NEWS_API_KEY": "news-key-from-env"}):
            page = self._client().get("/admin/settings/source-keys").get_data(as_text=True)
            self.assertIn("New York Times", page)
            self.assertIn("GNews", page)
            self.assertIn('data-per-poll="2"', page)
            self.assertIn("of 100 requests", page)
            response = self._client().post("/admin/settings/source-keys/gnews", data={
                "band_hour": ["16", "0", "7"], "band_minutes": ["30", "240", "15"],
                "GNEWS_API_DAILY_LIMIT": "100", "GNEWS_API_SLOWDOWN": "75",
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ConfigSetting.query.get("GNEWS_API_SCHEDULE").value, "0:240, 7:15, 16:30")
        # Limit and slowdown left at their defaults stay unsaved.
        self.assertIsNone(ConfigSetting.query.get("GNEWS_API_DAILY_LIMIT"))
        self._client().post("/admin/settings/source-keys/gnews", data={
            "band_hour": ["0"], "band_minutes": ["0"]})
        self.assertEqual(ConfigSetting.query.get("GNEWS_API_SCHEDULE").value, "0:240, 7:15, 16:30")
        self._client().post("/admin/settings/source-keys/gnews", data={"reset_schedule": "1"})
        self.assertIsNone(ConfigSetting.query.get("GNEWS_API_SCHEDULE"))
        self.assertIn("GNEWS_API_SCHEDULE", config_store.ALL_NAMES)
