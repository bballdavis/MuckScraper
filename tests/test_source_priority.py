"""Priority outlets (aggregator/source_priority.py): the saved list, the
Trending boost, and the Swipe deck order.

The pure checks need no database. The end-to-end tests run against the
scratch Postgres database named by API_TEST_DATABASE_URL, like
tests/test_story_rank.py, and are skipped without it. Its tables are dropped
and recreated.
"""
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")
NOW = datetime(2026, 10, 30, 12, 0)


class CleanTests(unittest.TestCase):
    def test_comma_or_newline_string_is_split_and_trimmed(self):
        from aggregator.source_priority import clean
        self.assertEqual(clean(" The Texas Tribune ,\nNPR\n"), ["The Texas Tribune", "NPR"])

    def test_repeats_ignore_case_and_blanks_are_dropped(self):
        from aggregator.source_priority import clean
        self.assertEqual(clean(["NPR", "npr", "", "  ", "Reuters"]), ["NPR", "Reuters"])

    def test_non_lists_and_non_strings_give_nothing(self):
        from aggregator.source_priority import clean
        self.assertEqual(clean(None), [])
        self.assertEqual(clean({"a": 1}), [])
        self.assertEqual(clean([1, None, "NPR"]), ["NPR"])


class ScoreBoostTests(unittest.TestCase):
    def test_boost_counts_like_extra_outlets_before_decay(self):
        from aggregator.source_priority import PRIORITY_BONUS
        from aggregator.story_rank import HALF_LIFE_HOURS, score_signals
        row = SimpleNamespace(story_id=1, outlets=1, left=0, center=0, right=0, recent=0, newest=NOW)
        boosted = score_signals(row, NOW, PRIORITY_BONUS)
        three_outlets = score_signals(SimpleNamespace(story_id=1, outlets=3, left=0, center=0, right=0,
                                                      recent=0, newest=NOW), NOW)
        self.assertAlmostEqual(boosted, three_outlets)
        # The boost decays with the story like any other part of the score.
        later = NOW + timedelta(hours=HALF_LIFE_HOURS)
        self.assertAlmostEqual(score_signals(row, later, PRIORITY_BONUS), boosted / 2)


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the priority-outlet tests")
class PriorityEndToEndTests(unittest.TestCase):
    def setUp(self):
        from aggregator import create_app, db
        env = {"DATABASE_URL": TEST_DB, "SECRET_KEY": "test-secret"}
        with mock.patch.dict(os.environ, env):
            self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

    def tearDown(self):
        from aggregator import db
        db.session.remove()
        self.ctx.pop()

    def _story(self, title, outlet_names, hours_old=1, copy_of=None):
        """A story with one article per outlet name (no outlet row when the
        name starts with "src:"), fetched `hours_old` ago."""
        from aggregator import db
        from aggregator.models import Article, Outlet, Story
        now = datetime.utcnow()
        story = Story(title=title, created_at=now - timedelta(hours=hours_old))
        db.session.add(story)
        db.session.flush()
        for n, name in enumerate(outlet_names):
            when = now - timedelta(hours=hours_old)
            article = Article(title=f"{title} {n}", url=f"https://{n}.example/{story.id}",
                              content="Reported text. " * 30, date=when, fetched_at=when, story=story)
            if name.startswith("src:"):
                article.source = name[4:]
            else:
                article.outlet = Outlet.query.filter_by(name=name).first() or Outlet(name=name, bias_score=3.0)
            db.session.add(article)
        db.session.commit()
        return story

    def test_default_list_has_the_texas_tribune_until_saved(self):
        from aggregator import source_priority
        self.assertEqual(source_priority.names(), ["The Texas Tribune"])
        source_priority.save([])
        self.assertEqual(source_priority.names(), [])
        source_priority.save("NPR, the texas tribune,")
        self.assertEqual(source_priority.names(), ["NPR", "the texas tribune"])

    def test_boosted_story_ids_match_outlet_names_and_skip_copies(self):
        from aggregator import db, source_priority
        from aggregator.models import Article
        tribune = self._story("Lege adjourns", ["The Texas Tribune"])
        wire = self._story("Port strike", ["Reuters", "AP"])
        raw = self._story("Raw tip", ["src:the texas tribune"])
        # A copy of the Reuters article that carries the Tribune's name doesn't count.
        copy = Article(title="copy", url="https://copy.example/x", content="x" * 600,
                       story=wire, copy_of_id=wire.articles[0].id, fetched_at=datetime.utcnow(),
                       date=datetime.utcnow(), source="The Texas Tribune")
        db.session.add(copy)
        db.session.commit()
        source_priority.save(["the TEXAS tribune"])
        self.assertEqual(source_priority.boosted_story_ids(), {tribune.id, raw.id})
        self.assertEqual(source_priority.boosted_story_ids([wire.id]), set())

    def test_trending_lists_a_boosted_single_source_story(self):
        from aggregator import source_priority
        from aggregator.headline_views import build
        tribune = self._story("Texas water board votes", ["The Texas Tribune"])
        many = self._story("Fed holds rates", ["Reuters", "AP", "NPR"])
        lone = self._story("Local school board recall", ["Fox News"])

        source_priority.save([])
        before = {e.story_id for e in build()["trending"]}
        self.assertEqual(before, {many.id})
        self.assertNotIn(tribune.id, before)

        source_priority.save(["The Texas Tribune"])
        entries = build()["trending"]
        self.assertEqual({e.story_id for e in entries}, {many.id, tribune.id})
        self.assertNotIn(lone.id, {e.story_id for e in entries})
        # Three outlets (plus their recent momentum) still outscore one boosted outlet.
        self.assertEqual([e.story_id for e in entries], [many.id, tribune.id])

    def test_swipe_deck_puts_priority_stories_first(self):
        from aggregator import source_priority
        from aggregator.api.skip_routes import _deck
        tribune = self._story("Texas water board votes", ["The Texas Tribune"])
        many = self._story("Fed holds rates", ["Reuters", "AP", "NPR", "Fox News"])
        source_priority.save([])
        self.assertEqual([s.id for s, _ in _deck(0)][0], many.id)
        source_priority.save(["The Texas Tribune"])
        self.assertEqual([s.id for s, _ in _deck(0)][0], tribune.id)

    def test_priority_story_survives_the_deck_limit(self):
        from aggregator import source_priority
        from aggregator.api import skip_routes
        tribune = self._story("Texas water board votes", ["The Texas Tribune"], hours_old=30)
        for n in range(3):
            self._story(f"Busy story {n}", ["Reuters", "AP"])
        source_priority.save(["The Texas Tribune"])
        with mock.patch.object(skip_routes, "DECK_MORE_LIMIT", 2):
            ids = [s.id for s, _ in skip_routes._deck(0)]
        self.assertIn(tribune.id, ids)


if __name__ == "__main__":
    unittest.main()
