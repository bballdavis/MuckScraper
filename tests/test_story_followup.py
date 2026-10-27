"""Tests for NewsAPI's live topics and the story follow-up searches
(news_fetcher/story_followup.py), and the per-kind cap in the source API
budget that keeps the follow-ups from eating the polls' share.

Keyword picking runs without a database. The rest runs against the scratch
Postgres database named by API_TEST_DATABASE_URL, like
tests/test_api_budget.py, and is skipped without it.
"""

import dataclasses
import json
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

from news_fetcher import api_budget, story_followup

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


class _Response:
    def __init__(self, body=None):
        self.status_code = 200
        self._body = body if body is not None else {"articles": []}

    def json(self):
        return self._body


class KeywordTests(unittest.TestCase):
    def test_names_first_then_longest_words(self):
        self.assertEqual(story_followup.keywords("Senate passes Ukraine aid after Schumer's deal with House"),
                         ["Ukraine", "Schumer", "House", "Senate"])

    def test_query_needs_two_words(self):
        self.assertEqual(story_followup.search_query("Hurricane Milton makes landfall in Florida"),
                         "Milton AND Florida AND Hurricane AND landfall")
        self.assertIsNone(story_followup.search_query("Breaking news"))

    def test_budget_split(self):
        newsapi = api_budget.PROVIDERS["newsapi"]
        with mock.patch.object(api_budget, "_setting", side_effect=lambda name, default="": default):
            self.assertEqual(api_budget.kind_cap(newsapi, "followup"), 20)
            self.assertIsNone(api_budget.kind_cap(newsapi, "poll"))
            # The seeded five fetches a poll still fit the polls' share.
            self.assertEqual(api_budget.stretch(newsapi, requests_per_poll=5), 1.0)


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the follow-up tests")
class FollowupDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {"DATABASE_URL": TEST_DB, "SECRET_KEY": "test-secret"}
        with mock.patch.dict(os.environ, env):
            from aggregator import create_app, db
            cls.app = create_app()
            # Builds the app on import, so it needs the same environment.
            import news_fetcher.fetch_and_store_articles  # noqa: F401
        cls.app.config.update(TESTING=True)
        with cls.app.app_context():
            db.drop_all()
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        from news_fetcher import llm_client, saved_settings
        saved_settings.set_source(None)
        llm_client._sync_config()

    def setUp(self):
        from aggregator import db
        api_budget._rejected.clear()
        self.settings = {"NEWS_API_KEY": "news-key"}
        patcher = mock.patch.object(api_budget, "_setting",
                                    side_effect=lambda name, default="": self.settings.get(name, default))
        patcher.start()
        self.addCleanup(patcher.stop)
        fast = {k: dataclasses.replace(p, min_interval_seconds=0) for k, p in api_budget.PROVIDERS.items()}
        patcher = mock.patch.dict(api_budget.PROVIDERS, fast)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = self.app.app_context()
        self.ctx.push()
        for table in ("articles", "stories", "outlets", "source_api_calls", "raw_article_payloads",
                      "app_settings", "scheduled_fetches"):
            db.session.execute(db.text(f"DELETE FROM {table}"))
        db.session.commit()
        self.now = datetime.utcnow()

    def tearDown(self):
        from aggregator import db
        db.session.remove()
        self.ctx.pop()

    def _story(self, title, ages_in_hours, outlets=2):
        """A story with one article per age (hours before now), spread
        over `outlets` outlets."""
        from aggregator import db
        from aggregator.models import Article, Outlet, Story
        story = Story(title=title)
        db.session.add(story)
        db.session.flush()
        for i, hours in enumerate(ages_in_hours):
            outlet = Outlet.query.filter_by(name=f"Outlet {i % outlets}").first()
            if outlet is None:
                outlet = Outlet(name=f"Outlet {i % outlets}")
                db.session.add(outlet)
                db.session.flush()
            db.session.add(Article(title=f"{title} {i}", url=f"https://example.com/{story.id}/{i}",
                                   outlet_id=outlet.id, story_id=story.id,
                                   date=self.now - timedelta(hours=hours)))
        db.session.commit()
        return story

    def test_only_stories_still_running_after_a_day(self):
        long_running = self._story("Senate passes Ukraine aid package", [30, 10, 2], outlets=3)
        self._story("Short Lived Earthquake Report", [5, 1])            # under 24 hours
        self._story("Finished Trial Verdict Appeal", [60, 30])          # nothing in the last day
        picked = [story.id for story, _ in story_followup.candidates(self.now)]
        self.assertEqual(picked, [long_running.id])

    def test_search_keeps_a_payload_and_waits_a_day(self):
        from aggregator.models import AppSetting, RawArticlePayload, SourceApiCall
        story = self._story("Senate passes Ukraine aid package", [30, 2])
        body = {"articles": [{"title": "Earlier", "url": "https://other.example/early", "source": {"name": "Other"}}]}
        with mock.patch.object(api_budget.requests, "get", return_value=_Response(body)) as get:
            metrics = story_followup.search_followups(self.now)
        self.assertEqual(metrics["searched"], 1)
        self.assertEqual(metrics["articles"], 1)
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["q"], "Ukraine AND package AND Senate AND passes")
        self.assertIn("/everything", get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs["headers"], {"X-Api-Key": "news-key"})
        payload = RawArticlePayload.query.one()
        self.assertEqual(payload.topic_name, story_followup.TOPIC_NAME)
        self.assertIsNone(payload.ingested_at)
        self.assertEqual(SourceApiCall.query.filter_by(provider="newsapi", kind="followup").count(), 1)
        self.assertIn(str(story.id), json.loads(AppSetting.query.get(story_followup.SEARCHED_KEY).value))
        # Searched already: not again for a day.
        with mock.patch.object(api_budget.requests, "get") as get:
            self.assertEqual(story_followup.search_followups(self.now + timedelta(hours=2))["searched"], 0)
        get.assert_not_called()

    def test_followups_stop_at_their_share_of_the_limit(self):
        from aggregator import db
        from aggregator.models import SourceApiCall
        self._story("Senate passes Ukraine aid package", [30, 2])
        db.session.add_all([SourceApiCall(provider="newsapi", kind="followup", http_status=200,
                                          called_at=self.now - timedelta(hours=1)) for _ in range(20)])
        db.session.commit()
        with mock.patch.object(api_budget.requests, "get") as get:
            metrics = story_followup.search_followups(self.now)
        get.assert_not_called()
        self.assertEqual(metrics["status"], "unavailable")
        # Polls still get their requests.
        self.assertIsNotNone(api_budget.take_slot("newsapi", "poll"))

    def test_sources_fetch_uses_live_headlines(self):
        from aggregator import db
        from aggregator.models import ScheduledFetch
        from news_fetcher.fetch_and_store_articles import providers
        db.session.add(ScheduledFetch(label="World News", mode="top", newsapi_sources="bbc-news,reuters",
                                      gnews_query="world", is_active=True, sort_order=1))
        db.session.commit()
        with mock.patch.object(api_budget.requests, "get", return_value=_Response()) as get:
            providers._poll("newsapi", providers.scheduled_fetches())
        self.assertIn("/top-headlines", get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs["params"], {"sources": "bbc-news,reuters", "pageSize": 100})
