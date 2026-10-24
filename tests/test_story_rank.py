"""Tests for the built-in headline ranker (aggregator/story_rank.py).

The scoring checks need no database. The end-to-end tests run against the
scratch Postgres database named by API_TEST_DATABASE_URL, like
tests/test_api.py, and are skipped without it. Its tables are dropped and
recreated.
"""
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")
NOW = datetime(2026, 10, 24, 12, 0)


def _row(outlets, left=0, right=0, recent=0, hours_old=0):
    return SimpleNamespace(story_id=1, outlets=outlets, left=left, center=0, right=right, recent=recent,
                           newest=NOW - timedelta(hours=hours_old))


class ScoreTests(unittest.TestCase):
    def test_breadth_beats_a_fresher_single_outlet(self):
        from aggregator.story_rank import score_signals
        single = score_signals(_row(1, right=1, recent=1), NOW)
        broad = score_signals(_row(8, left=3, right=2, hours_old=10), NOW)
        self.assertGreater(broad, single)

    def test_both_sides_and_momentum_add(self):
        from aggregator.story_rank import score_signals
        plain = score_signals(_row(4, left=4), NOW)
        self.assertGreater(score_signals(_row(4, left=2, right=2), NOW), plain)
        self.assertGreater(score_signals(_row(4, left=4, recent=3), NOW), plain)

    def test_score_halves_every_half_life(self):
        from aggregator.story_rank import HALF_LIFE_HOURS, score_signals
        fresh = score_signals(_row(6), NOW)
        self.assertAlmostEqual(score_signals(_row(6, hours_old=HALF_LIFE_HOURS), NOW), fresh / 2)

    def test_future_dates_and_old_stories_stay_positive(self):
        from aggregator.story_rank import score_signals
        self.assertEqual(score_signals(_row(3, hours_old=-5), NOW), score_signals(_row(3), NOW))
        self.assertGreater(score_signals(_row(1, hours_old=24 * 30), NOW), 0)


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the ranking end-to-end tests")
class RankEndToEndTests(unittest.TestCase):
    def setUp(self):
        from aggregator import create_app, db
        env = {"DATABASE_URL": TEST_DB, "SECRET_KEY": "test-secret"}
        with mock.patch.dict(os.environ, env):
            self.app = create_app()
            # These build their own app at import time.
            import news_fetcher.fetch_and_store_articles  # noqa: F401
            import news_fetcher.scheduler  # noqa: F401
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

    def tearDown(self):
        from aggregator import db
        db.session.remove()
        self.ctx.pop()

    def _story(self, title, outlets, hours_old=1, copies_of_first=0):
        """A story with one article per (name, bias) outlet, plus copies of
        its first article."""
        from aggregator import db
        from aggregator.models import Article, Outlet, Story
        now = datetime.utcnow()
        outlets_by_name = {o.name: o for o in Outlet.query.all()}
        story = Story(title=title, created_at=now - timedelta(hours=hours_old))
        db.session.add(story)
        articles = []
        for n, (name, bias) in enumerate(outlets):
            outlet = outlets_by_name.setdefault(name, Outlet(name=name, bias_score=bias))
            articles.append(Article(title=f"{title} ({name})", url=f"https://{name}.example/{title[:12]}{n}",
                                    outlet=outlet, story=story, content="Reported text. " * 30,
                                    date=now - timedelta(hours=hours_old), fetched_at=now - timedelta(hours=hours_old)))
        db.session.add_all(articles)
        db.session.flush()
        for n in range(copies_of_first):
            db.session.add(Article(title=f"{title} wire copy {n}", url=f"https://copy{n}.example/{story.id}",
                                   source=f"Copy Desk {n}", story=story, copy_of_id=articles[0].id,
                                   content="Reported text. " * 30, date=articles[0].date, fetched_at=articles[0].fetched_at))
        db.session.commit()
        return story

    def test_signals_count_outlets_sides_and_skip_copies(self):
        from aggregator.story_rank import _signal_rows
        story = self._story("Senate passes stopgap bill", [("npr", 2), ("reuters", 3), ("fox", 4.5), ("npr", 2)],
                            copies_of_first=3)
        (row,) = _signal_rows(story_ids=[story.id])
        self.assertEqual((row.outlets, row.left, row.center, row.right, row.recent), (3, 1, 1, 1, 3))

    def test_copy_of_an_article_elsewhere_counts(self):
        from aggregator import db
        from aggregator.models import Article
        from aggregator.story_rank import _signal_rows
        story = self._story("Port strike talks resume", [("ap", 3)], copies_of_first=1)
        other = self._story("Unrelated story", [("bbc", 3)])
        copy = Article.query.filter(Article.copy_of_id.isnot(None)).one()
        copy.copy_of_id = other.articles[0].id
        db.session.commit()
        self.assertEqual(_signal_rows(story_ids=[story.id])[0].outlets, 2)
        # An original that left every story (story_id NULL) no longer makes it a copy.
        other.articles[0].story_id = None
        db.session.commit()
        self.assertEqual(_signal_rows(story_ids=[story.id])[0].outlets, 2)

    def test_ranker_writes_scores_and_edition_leads_with_the_broad_story(self):
        from aggregator.models import EditionStory, Story
        from news_fetcher.fetch_and_store_articles.edition_publish import publish_edition
        from aggregator.story_rank import rank_recent_stories
        singles = [self._story(title, [(outlet, 4.6)], hours_old=0.5) for title, outlet in (
            ("Mayor accused of steering contracts", "dailywire"),
            ("Border agents report record month", "washtimes"),
            ("Campus speech code challenged in court", "foxnews"),
        )]
        # No right-leaning outlet: unranked, the edition's right-leaning
        # pass put the three singles ahead of it.
        broad = self._story("Wildfire forces evacuations across Sierra counties",
                            [("ap", 3), ("latimes", 2), ("npr", 2), ("reuters", 3.2), ("nyt", 2)], hours_old=3)

        result = rank_recent_stories()
        self.assertEqual(result["stories_scored"], 4)
        self.assertTrue(all(s.headline_score > 0 for s in Story.query.all()))

        publish_edition()
        top = EditionStory.query.order_by(EditionStory.rank).first()
        self.assertEqual(top.story_id, broad.id)
        self.assertEqual(len(singles), EditionStory.query.count() - 1)

    def test_scheduler_falls_back_to_the_built_in_ranker(self):
        with mock.patch.dict("sys.modules", {"news_fetcher.headline_ranker": None}), \
                mock.patch("aggregator.story_rank.rank_recent_stories", return_value={"status": "ok"}) as ranker:
            from news_fetcher.scheduler import run_optional_headline_ranking
            self.assertEqual(run_optional_headline_ranking(), {"status": "ok"})
        ranker.assert_called_once()
