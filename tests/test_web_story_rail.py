"""The story page's round rail buttons: under the read
toggle, editors get Generate executive summary and Generate multi-source
analysis while each is still to be generated; each goes once it exists.

Runs against the scratch Postgres database named by API_TEST_DATABASE_URL,
like tests/test_reads.py; skipped without it. Its tables are dropped and
recreated.
"""
import os
import re
import unittest
from datetime import datetime, timedelta
from unittest import mock

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the story rail tests")
class StoryRailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {"DATABASE_URL": TEST_DB, "API_PROXY_HOPS": "0", "SECRET_KEY": "test-secret"}
        with mock.patch.dict(os.environ, env):
            from aggregator import create_app
            cls.web = create_app()
        cls.web.config.update(WTF_CSRF_ENABLED=False, TESTING=True)
        from aggregator import db
        from aggregator.models import Article, Outlet, Story, User
        with cls.web.app_context():
            db.drop_all()
            db.create_all()
            editor = User(username="editor", email="e@example.com", role="scraper")
            editor.set_password("password123")
            reader = User(username="reader", email="r@example.com", role="reader")
            reader.set_password("password123")
            outlet = Outlet(name="Daily", bias_score=3)
            # A second outlet, so the multi-article stories have two sources.
            other = Outlet(name="Weekly", bias_score=3)
            now = datetime.utcnow().replace(microsecond=0)
            bare = Story(title="Bare story")
            done = Story(title="Done story", summary="What happened.", deep_report="Left: one view.")
            single = Story(title="Single source")
            grew = Story(title="Grew since", summary="What happened.", deep_report="Left: one view.")
            db.session.add_all([editor, reader, outlet, other, bare, done, single, grew])
            db.session.flush()
            # Fetched before the summary and analysis were written...
            for n, story in enumerate([bare, bare, done, done, single, grew, grew]):
                db.session.add(Article(title=f"A{n}", url=f"https://daily.example/{n}", outlet=(outlet, other)[n % 2],
                                       date=now, fetched_at=now - timedelta(hours=1),
                                       scrape_status="success", story_id=story.id))
            # ...and one that arrived after them.
            db.session.add(Article(title="Late", url="https://daily.example/late", outlet=outlet,
                                   date=now, fetched_at=now + timedelta(hours=1),
                                   scrape_status="success", story_id=grew.id))
            db.session.commit()
            cls.ids = {"editor": editor.id, "reader": reader.id,
                       "bare": bare.id, "done": done.id, "single": single.id, "grew": grew.id}

    def _rail(self, story_id, user="editor"):
        client = self.web.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.ids[user])
            session["_fresh"] = True
        with mock.patch("aggregator.blueprints.public.check_ollama_status", return_value=True):
            page = client.get(f"/story/{story_id}").get_data(as_text=True)
        return re.search(r'<div class="rail-stack".*?<a class="float-back"', page, re.S).group(0)

    def test_both_buttons_until_generated(self):
        rail = self._rail(self.ids["bare"])
        self.assertIn('aria-label="Generate executive summary"', rail)
        self.assertIn('aria-label="Generate multi-source analysis"', rail)
        self.assertIn("rail-btn", rail)
        self.assertIn("read-toggle", rail)

    def test_gone_once_generated(self):
        rail = self._rail(self.ids["done"])
        self.assertNotIn("executive summary", rail)
        self.assertNotIn("multi-source analysis", rail)
        self.assertIn("read-toggle", rail)

    def test_regenerate_once_sources_arrive(self):
        rail = self._rail(self.ids["grew"])
        self.assertIn('aria-label="Regenerate executive summary"', rail)
        self.assertIn('aria-label="Regenerate multi-source analysis"', rail)
        self.assertIn("1 new source since it was written", rail)

    def test_no_analysis_button_with_one_source(self):
        rail = self._rail(self.ids["single"])
        self.assertIn("Generate executive summary", rail)
        self.assertNotIn("multi-source analysis", rail)

    def test_readers_only_get_the_read_toggle(self):
        rail = self._rail(self.ids["bare"], user="reader")
        self.assertNotIn("executive summary", rail)
        self.assertIn("read-toggle", rail)

    # While a summary or analysis is written, ai_tasks.js puts a skeleton in
    # that section's [data-ai-slot]. Editors get the
    # script and a slot per section; readers can't generate, so no script.
    def _page(self, path, user="editor"):
        client = self.web.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.ids[user])
            session["_fresh"] = True
        with mock.patch("aggregator.blueprints.public.check_ollama_status", return_value=True):
            return client.get(path).get_data(as_text=True)

    def test_story_has_a_slot_per_section(self):
        page = self._page(f"/story/{self.ids['bare']}")
        self.assertIn('data-ai-slot="story_summary"', page)
        self.assertIn('data-ai-slot="story_deep_report"', page)
        self.assertIn("js/ai_tasks.js", page)
        self.assertNotIn("let activeAiTaskPoll", page)

    def test_readers_get_no_task_script(self):
        page = self._page(f"/story/{self.ids['bare']}", user="reader")
        self.assertNotIn("js/ai_tasks.js", page)


if __name__ == "__main__":
    unittest.main()
