"""The article page's top controls (Philip, 2026-10-22): the Source
button, the round summary button that goes once a summary exists, and
the lean rating, which now returns to the page and shows its result.

Runs against the scratch Postgres database named by API_TEST_DATABASE_URL,
like tests/test_reads.py; skipped without it. Its tables are dropped and
recreated.
"""
import os
import unittest
from datetime import datetime
from unittest import mock

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the article page tests")
class ArticlePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {"DATABASE_URL": TEST_DB, "API_PROXY_HOPS": "0", "SECRET_KEY": "test-secret"}
        with mock.patch.dict(os.environ, env):
            from aggregator import create_app
            cls.web = create_app()
        cls.web.config.update(WTF_CSRF_ENABLED=False, TESTING=True)
        from aggregator import db
        from aggregator.models import Article, Outlet, User
        with cls.web.app_context():
            db.drop_all()
            db.create_all()
            editor = User(username="editor", email="e@example.com", role="scraper")
            editor.set_password("password123")
            outlet = Outlet(name="Daily", bias_score=3)
            db.session.add_all([editor, outlet])
            db.session.flush()
            now = datetime.utcnow().replace(microsecond=0)
            plain = Article(title="No summary yet", url="https://daily.example/a", outlet=outlet,
                            date=now, scrape_status="success", bias_score=3)
            summarized = Article(title="Summarized", url="https://daily.example/b", outlet=outlet,
                                 date=now, scrape_status="success", bias_score=3, summary="A summary.")
            db.session.add_all([plain, summarized])
            db.session.commit()
            cls.ids = {"editor": editor.id, "plain": plain.id, "summarized": summarized.id}

    def setUp(self):
        self.client = self.web.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.ids["editor"])
            session["_fresh"] = True

    def _page(self, article_id):
        with mock.patch("aggregator.blueprints.public.check_ollama_status", return_value=True):
            return self.client.get(f"/article/{article_id}").get_data(as_text=True)

    def test_original_link_at_the_top_and_no_bottom_summarize(self):
        page = self._page(self.ids["plain"])
        self.assertIn('href="https://daily.example/a" target="_blank" rel="noopener" class="pill"', page)
        self.assertNotIn(">Summarize</button>", page)
        self.assertNotIn("Original source:", page)

    def test_summary_button_until_there_is_a_summary(self):
        self.assertIn("Generate article summary", self._page(self.ids["plain"]))
        self.assertNotIn("Generate article summary", self._page(self.ids["summarized"]))

    def test_rate_lean_button_opens_the_dialog(self):
        page = self._page(self.ids["plain"])
        self.assertIn(f'onclick="openLeanModal({self.ids["plain"]})"', page)
        self.assertIn("js/lean_modal.js", page)

    def _lean(self, article_id):
        from aggregator.models import Article
        with self.web.app_context():
            a = Article.query.get(article_id)
            return a.bias_score, a.bias_source

    def test_ai_rating_is_shown_not_applied_and_asked_once(self):
        article_id = self.ids["summarized"]
        with mock.patch("news_fetcher.outlet_bias_llm.get_article_lean_from_llm",
                        return_value=(2, "Quotes only the governor's allies.")) as ask:
            first = self.client.post(f"/admin/article/{article_id}/lean/ai").get_json()
            again = self.client.post(f"/admin/article/{article_id}/lean/ai").get_json()
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(first["ai"], {"score": 2, "label": "Lean Left", "source": "ai",
                                       "reason": "Quotes only the governor's allies."})
        self.assertEqual(again["ai"], first["ai"])
        self.assertIsNone(first["own"])
        self.assertEqual(self._lean(article_id), (3, None))

        # Saving the AI's pick with no note applies the AI's rating.
        r = self.client.post(f"/admin/article/{article_id}/lean", json={"score": 2, "note": ""})
        self.assertEqual(r.get_json(), {"score": 2, "label": "Lean Left"})
        self.assertEqual(self._lean(article_id), (2, "ai"))

    def test_reader_override_with_note(self):
        article_id = self.ids["plain"]
        with mock.patch("news_fetcher.outlet_bias_llm.get_article_lean_from_llm", return_value=(None, None)):
            r = self.client.post(f"/admin/article/{article_id}/lean/ai").get_json()
        self.assertIsNone(r["ai"])
        self.assertIn("couldn't rate", r["error"])

        self.assertEqual(self.client.post(f"/admin/article/{article_id}/lean", json={"score": 9}).status_code, 400)
        r = self.client.post(f"/admin/article/{article_id}/lean",
                             json={"score": 5, "note": "Only one side quoted.", "report": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._lean(article_id), (5, "reader"))
        page = self._page(article_id)
        self.assertIn("Rated this article Right.", page)
        self.assertIn('bias-tag bias-5', page)

        with mock.patch("news_fetcher.outlet_bias_llm.get_article_lean_from_llm", return_value=(None, None)):
            r = self.client.post(f"/admin/article/{article_id}/lean/ai").get_json()
        self.assertEqual(r["own"], {"score": 5, "source": "reader", "note": "Only one side quoted."})

        # An outlet re-rank leaves the article's own rating alone.
        from aggregator.models import Article
        with self.web.app_context():
            outlet_id = Article.query.get(article_id).outlet_id
        with mock.patch("news_fetcher.outlet_bias_llm.get_outlet_bias_from_llm", return_value=1):
            self.client.post(f"/admin/rerank-outlet/{outlet_id}")
        self.assertEqual(self._lean(article_id), (5, "reader"))

if __name__ == "__main__":
    unittest.main()
