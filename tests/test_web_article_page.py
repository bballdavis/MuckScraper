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

    def test_rate_lean_returns_to_the_article_and_shows_the_rating(self):
        article_id = self.ids["plain"]
        with mock.patch("news_fetcher.outlet_bias_llm.get_article_lean_from_llm",
                        return_value=(5, "Quotes only the governor's critics.")):
            r = self.client.post(f"/admin/rate-article/{article_id}",
                                 data={"next": f"/article/{article_id}", "report": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith(f"/article/{article_id}"))
        page = self._page(article_id)
        self.assertIn("Rated this article Right. Quotes only the governor&#39;s critics.", page)
        self.assertIn('bias-tag bias-5', page)


if __name__ == "__main__":
    unittest.main()
