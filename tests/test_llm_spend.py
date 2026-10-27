"""Tests for the LLM spending caps (news_fetcher/llm_spend.py) and how
llm_client applies them.

Pricing and periods run without a database. The ledger, cap and settings
page tests run against the scratch Postgres database named by
API_TEST_DATABASE_URL, like tests/test_api_budget.py, and are skipped
without it.
"""

import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

from news_fetcher import llm_client, llm_spend

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


class _Settings(unittest.TestCase):
    def setUp(self):
        self.settings = {}
        patcher = mock.patch.object(llm_spend, "_setting",
                                    side_effect=lambda name, default="": self.settings.get(name, default))
        patcher.start()
        self.addCleanup(patcher.stop)


class PricingTests(_Settings):
    def test_built_in_prices_match_the_longest_prefix(self):
        self.assertEqual(llm_spend.price("anthropic", "claude-opus-5-5"), (4.00, 20.00))
        self.assertEqual(llm_spend.price("anthropic", "claude-opus-5"), (5.00, 25.00))
        self.assertEqual(llm_spend.price("anthropic", "claude-haiku-5-5"), (0.10, 0.50))
        self.assertIsNone(llm_spend.price("gemini", "gemini-2.5-flash"))

    def test_saved_prices_win(self):
        self.settings.update(GEMINI_PRICE_INPUT="0.30", GEMINI_PRICE_OUTPUT="2.50")
        self.assertEqual(llm_spend.price("gemini", "gemini-2.5-flash"), (0.30, 2.50))
        self.assertAlmostEqual(llm_spend.cost("gemini", "x", 1_000_000, 100_000), 0.55)

    def test_reported_cost_wins_and_no_price_is_none(self):
        self.assertEqual(llm_spend.cost("openrouter", "any/model", 10, 10, reported=0.0123), 0.0123)
        self.assertIsNone(llm_spend.cost("groq", "llama", 10, 10))

    def test_caps_default_to_unlimited(self):
        self.assertEqual(llm_spend.caps("anthropic"), (None, None))
        self.settings.update(ANTHROPIC_SPEND_DAILY_USD="2.5", ANTHROPIC_SPEND_MONTHLY_USD="nonsense")
        self.assertEqual(llm_spend.caps("anthropic"), (2.5, None))

    def test_periods_follow_eastern_time(self):
        # 03:00 UTC on 2026-10-01 is still 30 September in New York.
        day, month = llm_spend.period_starts(datetime(2026, 10, 1, 3, 0))
        self.assertEqual(day, datetime(2026, 9, 30, 4, 0))
        self.assertEqual(month, datetime(2026, 9, 1, 4, 0))

    def test_local_providers_are_never_capped(self):
        self.assertIsNone(llm_spend.blocked("ollama"))
        self.assertIsNone(llm_spend.blocked("lmstudio"))

    def test_no_caps_needs_no_database(self):
        with mock.patch.object(llm_spend, "spent") as spent:
            self.assertTrue(llm_spend.allowed("anthropic"))
        spent.assert_not_called()


class ClientTests(unittest.TestCase):
    def test_a_capped_provider_gets_no_call(self):
        with mock.patch.object(llm_client, "_sync_config"), \
                mock.patch.object(llm_client, "provider_for_tier", return_value="anthropic"), \
                mock.patch.object(llm_client, "model_for_tier", return_value="claude-sonnet-5-5"), \
                mock.patch.object(llm_spend, "allowed", return_value=False), \
                mock.patch.object(llm_client.requests, "post") as post:
            self.assertIsNone(llm_client.generate_text("Hello"))
        post.assert_not_called()

    def test_anthropic_call_records_its_tokens(self):
        body = {"content": [{"type": "text", "text": "Hi"}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "cache_read_input_tokens": 20, "output_tokens": 7}}
        response = mock.Mock(status_code=200, json=lambda: body)
        with mock.patch.object(llm_client, "ANTHROPIC_API_KEY", "k"), \
                mock.patch.object(llm_client.requests, "post", return_value=response), \
                mock.patch.object(llm_spend, "record") as record:
            self.assertEqual(llm_client._generate_text_anthropic("Hello", 5, "claude-sonnet-5-5"), "Hi")
        record.assert_called_once_with("anthropic", "claude-sonnet-5-5", "chat", 120, 7)

    def test_openrouter_asks_for_and_records_its_cost(self):
        body = {"choices": [{"message": {"content": "Hi"}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 5, "cost": 0.0004}}
        response = mock.Mock(status_code=200, json=lambda: body)
        config = {"label": "OpenRouter", "api_key": "k", "key_required": True, "model": "a/b",
                  "headers": {}, "base_url": "https://openrouter.ai/api/v1"}
        with mock.patch.object(llm_client, "_openai_compatible_config", return_value=config), \
                mock.patch.object(llm_client.requests, "post", return_value=response) as post, \
                mock.patch.object(llm_spend, "record") as record:
            llm_client._generate_text_openai_compatible("Hello", 5, "a/b", "openrouter")
        self.assertEqual(post.call_args.kwargs["json"]["usage"], {"include": True})
        record.assert_called_once_with("openrouter", "a/b", "chat", 50, 5, 0.0004)


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the spend ledger tests")
class SpendDatabaseTests(_Settings):
    @classmethod
    def setUpClass(cls):
        env = {"DATABASE_URL": TEST_DB, "SECRET_KEY": "test-secret"}
        with mock.patch.dict(os.environ, env):
            from aggregator import create_app, db
            cls.app = create_app()
        cls.app.config.update(WTF_CSRF_ENABLED=False, TESTING=True)
        with cls.app.app_context():
            db.drop_all()
            db.create_all()
            from aggregator.models import User
            admin = User(username="admin", email="a@example.com", role="admin")
            admin.set_password("password123")
            db.session.add(admin)
            db.session.commit()
            cls.admin_id = admin.id

    @classmethod
    def tearDownClass(cls):
        from news_fetcher import saved_settings
        saved_settings.set_source(None)
        llm_client._sync_config()

    def setUp(self):
        super().setUp()
        from aggregator import db
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.session.execute(db.text("DELETE FROM llm_usage"))
        db.session.execute(db.text("DELETE FROM config_settings"))
        db.session.commit()

    def tearDown(self):
        from aggregator import db
        db.session.remove()
        self.ctx.pop()

    def test_record_prices_each_call(self):
        from aggregator.models import LlmUsage
        llm_spend.record("anthropic", "claude-opus-5-5", "chat", 1_000_000, 100_000)
        llm_spend.record("groq", "llama", "chat", 10, 10)
        rows = {r.provider: r for r in LlmUsage.query.all()}
        self.assertAlmostEqual(float(rows["anthropic"].cost_usd), 6.0)
        self.assertTrue(rows["anthropic"].priced)
        self.assertIsNone(rows["groq"].cost_usd)
        self.assertFalse(rows["groq"].priced)

    def test_daily_cap_blocks_until_the_day_turns(self):
        from aggregator import db
        from aggregator.models import LlmUsage
        self.settings["ANTHROPIC_SPEND_DAILY_USD"] = "1"
        now = datetime.utcnow()
        llm_spend.record("anthropic", "claude-opus-5-5", "chat", 100_000, 10_000)  # $0.60
        self.assertTrue(llm_spend.allowed("anthropic"))
        llm_spend.record("anthropic", "claude-opus-5-5", "chat", 100_000, 10_000)  # $1.20
        self.assertEqual(llm_spend.blocked("anthropic"), "daily")
        self.assertFalse(llm_spend.allowed("anthropic"))
        # Yesterday's spend doesn't count today.
        LlmUsage.query.update({LlmUsage.called_at: now - timedelta(days=2)})
        db.session.commit()
        self.assertIsNone(llm_spend.blocked("anthropic", now))

    def test_monthly_cap(self):
        self.settings["GROQ_SPEND_MONTHLY_USD"] = "0.5"
        self.settings.update(GROQ_PRICE_INPUT="1", GROQ_PRICE_OUTPUT="1")
        llm_spend.record("groq", "llama", "chat", 300_000, 300_000)
        self.assertEqual(llm_spend.blocked("groq"), "monthly")
        summary = llm_spend.summary("groq")
        self.assertAlmostEqual(summary["month"], 0.6)
        self.assertEqual(summary["monthly_share"], 1.0)

    def test_settings_page_shows_and_saves_caps(self):
        from flask import g
        from aggregator.models import ConfigSetting
        llm_spend.record("anthropic", "claude-sonnet-5-5", "chat", 1000, 100)
        g.pop("_login_user", None)
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.admin_id)
        page = client.get("/admin/settings/llm").get_data(as_text=True)
        self.assertIn("Spending caps", page)
        self.assertIn("ANTHROPIC_SPEND_DAILY_USD", page)
        client.post("/admin/settings/llm/provider/anthropic",
                    data={"ANTHROPIC_SPEND_DAILY_USD": "5", "ANTHROPIC_SPEND_MONTHLY_USD": "",
                          "ANTHROPIC_PRICE_INPUT": "", "ANTHROPIC_PRICE_OUTPUT": ""})
        self.assertEqual(ConfigSetting.query.get("ANTHROPIC_SPEND_DAILY_USD").value, "5")
        self.assertIsNone(ConfigSetting.query.get("ANTHROPIC_SPEND_MONTHLY_USD"))
        client.post("/admin/settings/llm/provider/anthropic", data={"ANTHROPIC_SPEND_DAILY_USD": "lots"})
        self.assertEqual(ConfigSetting.query.get("ANTHROPIC_SPEND_DAILY_USD").value, "5")
