"""Tests for Headlines' Trending / Latest / Controversial views
(aggregator/headline_views.py and public.headlines_feed).

Runs against the scratch Postgres database named by API_TEST_DATABASE_URL,
like tests/test_api.py; skipped without it. Its tables are dropped and
recreated.
"""
import os
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

import numpy as np

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


def _vector(seed):
    rng = np.random.default_rng(seed)
    return rng.normal(size=768).tolist()


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the headline view tests")
class HeadlineViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from aggregator import create_app, db
        env = {"DATABASE_URL": TEST_DB, "SECRET_KEY": "test-secret"}
        with mock.patch.dict(os.environ, env):
            cls.app = create_app()
        cls.app.config.update(WTF_CSRF_ENABLED=False, TESTING=True)
        with cls.app.app_context():
            db.drop_all()
            db.create_all()
            cls.ids = cls._seed()

    @classmethod
    def _seed(cls):
        from aggregator import db
        from aggregator.models import Article, Edition, EditionStory, OpinionPiece, Outlet, Story, User
        now = datetime.utcnow()
        outlets = {}

        def outlet(name, bias):
            return outlets.setdefault(name, Outlet(name=name, bias_score=bias))

        def story(title, sources, hours_old, vectors=None):
            s = Story(title=title, headline=title, created_at=now - timedelta(hours=hours_old))
            db.session.add(s)
            for n, (name, bias) in enumerate(sources):
                when = now - timedelta(hours=hours_old)
                db.session.add(Article(
                    title=f"{title} ({name})", url=f"https://{name}.example/{abs(hash(title))}/{n}",
                    outlet=outlet(name, bias), story=s, content="Reported text. " * 50, date=when, fetched_at=when,
                    embedding=vectors[n] if vectors else None))
            return s

        reader = User(username="reader", email="r@example.com", role="reader")
        reader.set_password("password123")
        db.session.add(reader)
        shared = _vector(1)
        single = story("Mayor accused of steering contracts", [("dailywire", 4.6)], 0.2)
        broad = story("Senate passes stopgap funding bill", [("ap", 3), ("npr", 2), ("fox", 4.5), ("wsj", 3.7),
                                                            ("nyt", 1.8)], 2)
        small = story("Wildfire forces evacuations", [("ap", 3), ("latimes", 2), ("reuters", 3.2)], 1)
        # Both sides, written up alike (same vector) vs. very differently.
        alike = story("Hurricane makes landfall in Florida", [("npr", 2), ("nyt", 1.8), ("fox", 4.5), ("wsj", 3.7)],
                      3, vectors=[shared, shared, shared, shared])
        split = story("Guard deployment expands to two cities", [("npr", 2), ("nyt", 1.8), ("fox", 4.5),
                                                                 ("wsj", 3.7)],
                      3, vectors=[shared, shared, _vector(2), _vector(2)])
        old = story("Last week's budget fight", [("ap", 3), ("npr", 2), ("fox", 4.5)], 30)
        db.session.flush()
        db.session.add(OpinionPiece(title="The Guard does not belong here", url="https://op.example/1",
                                    story_id=split.id, bias_score=2))
        edition = Edition(date=date.today(), edition_type="morning", created_at=now, published=True)
        db.session.add(edition)
        db.session.flush()
        db.session.add(EditionStory(edition_id=edition.id, story_id=single.id, rank=1))
        db.session.commit()
        return {"reader": reader.id, "single": single.id, "broad": broad.id, "small": small.id,
                "alike": alike.id, "split": split.id, "old": old.id, "edition": edition.id}

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        from aggregator import db
        db.session.remove()
        self.ctx.pop()

    def _ids(self, view, **kwargs):
        from aggregator import headline_views
        return [e.story_id for e in headline_views.build(**kwargs)[view]]

    def test_trending_needs_three_outlets_and_ranks_breadth_first(self):
        ids = self._ids("trending")
        self.assertEqual(ids[0], self.ids["broad"])
        self.assertIn(self.ids["small"], ids)
        self.assertNotIn(self.ids["single"], ids)
        self.assertNotIn(self.ids["old"], ids)

    def test_latest_is_newest_first_and_includes_single_source(self):
        ids = self._ids("latest")
        self.assertEqual(ids[:3], [self.ids["single"], self.ids["small"], self.ids["broad"]])
        self.assertNotIn(self.ids["old"], ids)

    def test_controversial_needs_both_sides_and_ranks_the_framing_gap(self):
        ids = self._ids("controversial")
        self.assertEqual(ids[0], self.ids["split"])
        self.assertIn(self.ids["alike"], ids)
        for missing in ("single", "small"):
            self.assertNotIn(self.ids[missing], ids)

    def test_hidden_ids_drop_out(self):
        self.assertNotIn(self.ids["broad"], self._ids("trending", hidden_ids={self.ids["broad"]}))

    def _client(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.ids["reader"])
            session["_fresh"] = True
        return client

    def test_page_defaults_to_trending_with_the_switcher(self):
        page = self._client().get("/headlines").get_data(as_text=True)
        self.assertIn('class="view-switch"', page)
        self.assertIn('view-seg on" href="/headlines?view=trending"', page)
        self.assertIn("Senate passes stopgap funding bill", page)
        self.assertNotIn("Mayor accused of steering contracts", page)

    def test_latest_and_controversial_pages(self):
        client = self._client()
        latest = client.get("/headlines?view=latest").get_data(as_text=True)
        self.assertIn("single source", latest)
        self.assertIn("Last hour", latest)
        controversial = client.get("/headlines?view=controversial").get_data(as_text=True)
        self.assertIn("Framing gap: high · 1 op-ed", controversial)

    def test_an_edition_by_id_has_no_switcher(self):
        page = self._client().get(f"/headlines?edition={self.ids['edition']}").get_data(as_text=True)
        self.assertNotIn('class="view-switch"', page)
        self.assertIn("Morning Edition", page)
        self.assertIn("Mayor accused of steering contracts", page)
