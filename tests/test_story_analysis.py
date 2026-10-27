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


class ApiSectionsTests(unittest.TestCase):
    """The mobile API sends the same sections, so the iOS app lays out
    left / center / right without a parser of its own."""

    def test_api_sections(self):
        from aggregator.api.serializers import analysis_sections
        sections = analysis_sections(POLITICS)
        self.assertEqual([s["kind"] for s in sections],
                         ["lead", "left", "center", "right", "other", "other", "next"])
        self.assertEqual(sections[1], {"label": "How the left is covering it", "kind": "left",
                                       "paragraphs": ["The Guardian leads with the bystander video."],
                                       "empty": False})
        self.assertTrue(sections[3]["empty"])
        self.assertEqual(analysis_sections(None), [])


class DeepReportTypeTests(unittest.TestCase):
    """Which prompt writes the analysis (news_fetcher.summarizer.deep_report_type):
    general stories with rated sources get the left/center/right breakdown
    (Philip, 2026-10-25), the specialist types keep their own."""

    def _story(self, title, topics, scores):
        from types import SimpleNamespace as NS
        arts = [NS(title=title, bias_score=None, outlet=NS(bias_score=s)) for s in scores]
        return NS(title=title, headline=None, topics=[NS(name=t) for t in topics], articles=arts)

    def test_general_story_with_rated_sources_gets_lcr(self):
        from news_fetcher.summarizer import deep_report_type
        story = self._story("Two soldiers killed at West Bank checkpoint", ["International News"], [2, 3, None])
        self.assertEqual(deep_report_type(story), "politics")

    def test_general_story_without_rated_sources_stays_general(self):
        from news_fetcher.summarizer import deep_report_type
        story = self._story("Two soldiers killed at West Bank checkpoint", ["International News"], [None, None])
        self.assertEqual(deep_report_type(story), "default")

    def test_specialist_types_keep_their_format(self):
        from news_fetcher.summarizer import deep_report_type
        self.assertEqual(deep_report_type(self._story("New battery chemistry", ["Sci/Tech"], [3])), "science")
        self.assertEqual(deep_report_type(self._story("Cup final result", ["Sports"], [3])), "sports")


class RegenerateTests(unittest.TestCase):
    """A generated summary or analysis can be run again only once sources
    have arrived since it was written (Philip, 2026-10-25)."""

    def _render(self, story, new_sources):
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "..", "aggregator", "templates")))
        env.filters["short_datetime"] = lambda d: ""
        return env.from_string(
            "{% from 'ai_actions.html' import story_analysis_action with context %}"
            "{{ story_analysis_action(s, true, new_sources=n) }}").render(s=story, n=new_sources)

    def test_regenerate_only_with_new_sources(self):
        from types import SimpleNamespace as NS
        story = NS(id=5, deep_report="The story: x", articles=[1, 2, 3], can_analyze=True)
        self.assertIn("act-done", self._render(story, 0))
        html = self._render(story, 2)
        self.assertIn("Regenerate multi-source analysis", html)
        self.assertIn("2 new sources since it was written", html)
        self.assertIn("startAiTask('story_deep_report', 5, this)", html)

    def test_new_sources_since(self):
        from aggregator.models import Article, Story
        then = datetime(2026, 10, 25, 8, 0)
        story = Story(title="t")
        story.articles = [Article(title="a", url="u1", fetched_at=datetime(2026, 10, 25, 7, 0)),
                          Article(title="b", url="u2", fetched_at=datetime(2026, 10, 25, 9, 0))]
        self.assertEqual(story.new_sources_since(then), 1)
        self.assertEqual(story.new_sources_since(None), 0)
        story.deep_report = "The story: y"
        self.assertIsNotNone(story.deep_report_generated_at)
        self.assertEqual(story.new_sources_for_analysis, 0)


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
