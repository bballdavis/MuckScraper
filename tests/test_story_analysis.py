"""The story page's analysis layouts (Philip, 2026-10-23): the report is
split into its labelled sections (aggregator/story_analysis.py) and shown as
Cards (default) or Sections, picked under Appearance on the profile page.

The parser tests need nothing; the page tests run against the scratch
Postgres database named by API_TEST_DATABASE_URL, like tests/test_reads.py,
and are skipped without it.
"""
import os
import re
import unittest
from datetime import datetime
from unittest import mock

from aggregator import story_analysis

POLITICS = """The story: Riots spread to a dozen cities after a police shooting.

How the left is covering it: The Guardian leads with the bystander video.

How the center is covering it: AP and Reuters focus on the arrests.

How the right is covering it: No right-leaning sources were found in the current coverage.

What's contested: Whether the officer's account holds up.

What's missing: Residents' own voices.

What's next: Whether a state of emergency is declared."""

SCIENCE = """The discovery or development: A new battery chemistry.
It lasts twice as long.

Why it matters: Cheaper storage.

What experts are saying: Cautious optimism.

What's next: Field trials next year."""


class ParseTests(unittest.TestCase):
    def test_politics_sections(self):
        a = story_analysis.layout(POLITICS)
        self.assertEqual([s["kind"] for s in a["sections"]],
                         ["lead", "left", "center", "right", "other", "other", "next"])
        self.assertEqual(a["lead"]["label"], "The story")
        self.assertEqual([s["kind"] for s in a["buckets"]], ["left", "center"])
        self.assertEqual([s["kind"] for s in a["empty"]], ["right"])
        self.assertEqual([s["icon"] for s in a["others"]], ["scale", "eye-off"])
        self.assertEqual(a["next"]["icon"], "circle-arrow-right")

    def test_other_story_types_have_no_buckets(self):
        a = story_analysis.layout(SCIENCE)
        self.assertEqual(a["lead"]["paragraphs"], ["A new battery chemistry.", "It lasts twice as long."])
        self.assertEqual(a["buckets"], [])
        self.assertEqual([s["icon"] for s in a["others"]], ["zap", "message-square"])

    def test_unlabelled_text_still_renders(self):
        a = story_analysis.layout("Just one plain paragraph, no labels at all.")
        self.assertEqual(len(a["sections"]), 1)
        self.assertIsNone(a["sections"][0]["label"])
        self.assertEqual(story_analysis.layout("  \n ")["sections"], [])


TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the story analysis page tests")
class AnalysisPageTests(unittest.TestCase):
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
            reader = User(username="reader", email="r@example.com", role="reader")
            reader.set_password("password123")
            left = Outlet(name="Lefty", bias_score=1)
            center = Outlet(name="Middle", bias_score=3)
            story = Story(title="Riots", summary="What happened.", deep_report=POLITICS)
            db.session.add_all([reader, left, center, story])
            db.session.flush()
            now = datetime.utcnow().replace(microsecond=0)
            for n, outlet in enumerate([left, left, center]):
                db.session.add(Article(title=f"A{n}", url=f"https://x.example/{n}", outlet=outlet,
                                       date=now, scrape_status="success", story_id=story.id))
            db.session.commit()
            cls.ids = {"reader": reader.id, "story": story.id}

    def _client(self):
        client = self.web.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.ids["reader"])
            session["_fresh"] = True
        return client

    def _page(self, client):
        with mock.patch("aggregator.blueprints.public.check_ollama_status", return_value=True):
            return client.get(f"/story/{self.ids['story']}").get_data(as_text=True)

    def test_cards_by_default_then_sections_once_chosen(self):
        client = self._client()
        page = self._page(client)
        self.assertIn('class="sa sa-cards"', page)
        self.assertIn("2 sources", page)
        self.assertIn('class="sa-empty sa-right"', page)
        self.assertIn('aria-label="Left 2, center 1, right 0"', page)

        profile = client.get("/auth/profile").get_data(as_text=True)
        self.assertIn("Analysis layout", profile)
        client.post("/auth/profile", data={"action": "appearance", "analysis_layout": "sections"})
        page = self._page(client)
        self.assertIn('class="sa sa-sections"', page)
        self.assertEqual(len(re.findall(r'<section class="sa-sec', page)), 7)

        client.post("/auth/profile", data={"action": "appearance", "analysis_layout": "nonsense"})
        self.assertIn('class="sa sa-cards"', self._page(client))


if __name__ == "__main__":
    unittest.main()
