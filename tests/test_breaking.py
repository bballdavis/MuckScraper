"""Tests for Breaking News (aggregator/breaking.py, news_fetcher/breaking_feed.py
and /api/v1/breaking).

The detection checks need no database. The end-to-end tests run against the
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

QUAKE = ("Magnitude 6.8 earthquake strikes off coast of Japan",
         "Japan earthquake: magnitude 6.8 quake strikes off the coast",
         "Strong earthquake strikes off Japan's coast, magnitude 6.8")


def _api_app(database_url):
    with mock.patch.dict(os.environ, {"DATABASE_URL": database_url, "API_PROXY_HOPS": "0"}):
        from aggregator.api_app import create_api_app
        return create_api_app()


class DetectionTests(unittest.TestCase):
    def test_markers(self):
        from news_fetcher.breaking_feed import is_marked
        for title in ("BREAKING: Plane lands on river", "Live: votes are counted", "Developing - storm nears coast",
                      "Election night live updates", "Just in: rates cut"):
            self.assertTrue(is_marked(title), title)
        for title in ("Breaking the cycle of debt", "Live music returns to downtown", "Developing nations meet"):
            self.assertFalse(is_marked(title), title)

    def test_outlets_wording_one_event_differently_cluster(self):
        from news_fetcher.breaking_feed import cluster
        now = datetime(2026, 10, 22, 12, 0)
        entries = [SimpleNamespace(id=i, title=t, published_at=now + timedelta(minutes=i))
                   for i, t in enumerate(QUAKE)]
        entries.append(SimpleNamespace(id=9, title="City council approves new bike lanes downtown",
                                       published_at=now))
        # Same words, but long after the event began: a separate one.
        entries.append(SimpleNamespace(id=10, title=QUAKE[0], published_at=now + timedelta(hours=5)))
        events = cluster(entries)
        self.assertEqual(sorted(len(e) for e in events), [1, 1, 3])
        quake = next(e for e in events if len(e) == 3)
        self.assertEqual(quake[0].id, 0)


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the Breaking News end-to-end tests")
class BreakingEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from aggregator import db
        from aggregator.models import Story, User
        cls.app = _api_app(TEST_DB)
        with cls.app.app_context():
            db.drop_all()
            db.create_all()
            user = User(username="reader", email="r@example.com", role="reader")
            user.set_password("password123")
            # Already a story before the "budget" headlines came in.
            db.session.add_all([user, Story(title="Senate passes budget bill after long debate",
                                            created_at=datetime.utcnow() - timedelta(hours=6))])
            db.session.commit()
            cls.user_id = user.id

    def setUp(self):
        from aggregator import db
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        for table in ("api_rate_counters", "newsbox_items", "jobs", "feed_entries"):
            db.session.execute(db.text(f"DELETE FROM {table}"))
        db.session.commit()

    def tearDown(self):
        from aggregator import db
        db.session.remove()
        self.ctx.pop()

    def _token(self, scopes=("read", "summarize")):
        from aggregator import db
        from aggregator.api.auth import create_pairing_code
        from aggregator.models import User
        code, _ = create_pairing_code(User.query.get(self.user_id), list(scopes), "test")
        db.session.commit()
        return self.client.post("/api/v1/devices/pair", json={"code": code}).get_json()["token"]

    def _seed_headlines(self, now):
        from aggregator import breaking
        minutes = lambda m: now - timedelta(minutes=m)  # noqa: E731
        articles = [
            {"title": QUAKE[0], "url": "https://apnews.example/quake?utm=x", "source_name": "AP News", "published_at": minutes(30)},
            {"title": QUAKE[1], "url": "https://reuters.example/quake", "source_name": "Reuters", "published_at": minutes(25)},
            {"title": QUAKE[2], "url": "https://cnn.example/quake", "source_name": "CNN", "published_at": minutes(20)},
            {"title": "BREAKING: Plane lands safely on the Hudson river", "url": "https://wsj.example/plane",
             "source_name": "WSJ", "published_at": minutes(10)},
            {"title": "City council approves new bike lanes downtown", "url": "https://local.example/bikes",
             "source_name": "Local", "published_at": minutes(5)},
            {"title": "Senate passes budget bill after debate", "url": "https://a.example/budget", "source_name": "A",
             "published_at": minutes(15)},
            {"title": "Budget bill passes the Senate after debate", "url": "https://b.example/budget", "source_name": "B",
             "published_at": minutes(14)},
            {"title": "Senate budget bill passes after long debate", "url": "https://c.example/budget", "source_name": "C",
             "published_at": minutes(13)},
        ]
        self.assertEqual(breaking.store_headlines(articles, "https://feeds.example/rss", now), 8)
        # The same URLs again (a query string differs) add nothing.
        self.assertEqual(breaking.store_headlines(articles[:2], "https://feeds.example/rss", now), 0)

    def test_poller_flags_new_events_only(self):
        from aggregator import breaking
        from news_fetcher.breaking_feed import mark_breaking
        now = datetime.utcnow()
        self._seed_headlines(now)
        self.assertEqual(mark_breaking(now), 2)
        titles = [e.title for e in breaking.bar_entries(now=now)]
        # Newest first; the quake shows once, as the outlet that had it first.
        self.assertEqual(titles, ["BREAKING: Plane lands safely on the Hudson river", QUAKE[0]])
        quake = breaking.bar_entries(now=now)[1]
        self.assertEqual(quake.outlet_count, 3)
        # Outside the window, nothing is breaking.
        self.assertEqual(breaking.bar_entries(now=now + breaking.WINDOW + timedelta(minutes=31)), [])

    def test_send_to_newsbox_scrapes_then_summarizes(self):
        from aggregator import breaking, db, jobs
        from aggregator.models import Article, FeedEntry, Job, Outlet, Story
        from news_fetcher.breaking_feed import mark_breaking
        now = datetime.utcnow()
        self._seed_headlines(now)
        mark_breaking(now)
        token = self._token()
        auth = {"Authorization": f"Bearer {token}"}
        items = self.client.get("/api/v1/breaking", headers=auth).get_json()["items"]
        quake = items[1]
        self.assertEqual((quake["state"], quake["outlet"], quake["outlet_count"]), ("new", "AP News", 3))

        r = self.client.post(f"/api/v1/breaking/{quake['id']}/newsbox", headers=auth, json={})
        self.assertEqual(r.status_code, 202, r.get_json())
        item = r.get_json()
        self.assertIsNone(item["story"])
        self.assertEqual(item["headline"]["title"], QUAKE[0])
        self.assertEqual((item["origin"], item["state"], item["step"], item["progress"]),
                         ("breaking", "pending", "scrape", 0.1))
        self.assertEqual(Job.query.filter_by(kind=breaking.SCRAPE_JOB).count(), 1)
        self.assertEqual(self.client.get("/api/v1/breaking", headers=auth).get_json()["items"][1]["state"], "scraping")

        def fake_scrape(entry):
            outlet = Outlet(name="AP News")
            story = Story(title=entry.title, created_at=datetime.utcnow())
            article = Article(title=entry.title, url=breaking.normalize_url(entry.link), outlet=outlet, story=story)
            db.session.add_all([outlet, story, article])
            db.session.commit()
            return article.id, None

        with mock.patch("aggregator.breaking._scrape", side_effect=fake_scrape):
            self.assertTrue(jobs.run_next_job())
        item = self.client.get("/api/v1/me/newsbox", headers=auth).get_json()["items"][0]
        self.assertEqual(item["story"]["headline"], QUAKE[0])
        self.assertIsNone(item["headline"])
        self.assertEqual((item["state"], item["step"]), ("pending", "summary"))
        self.assertEqual(FeedEntry.query.get(quake["id"]).scrape_state, "summarizing")

        with mock.patch("news_fetcher.summarizer.check_ollama_status", return_value=True), \
                mock.patch("news_fetcher.summarizer.summarize_story", return_value="The big picture: A quake."):
            self.assertTrue(jobs.run_next_job())
        item = self.client.get("/api/v1/me/newsbox", headers=auth).get_json()["items"][0]
        self.assertEqual((item["state"], item["step"], item["progress"]), ("ready", None, None))
        bar = self.client.get("/api/v1/breaking", headers=auth).get_json()["items"][1]
        self.assertEqual(bar["state"], "summarized")
        self.assertEqual(bar["story_id"], item["story"]["id"])

    def test_refused_scrape_fails_the_item(self):
        from aggregator import breaking, jobs
        from news_fetcher.breaking_feed import mark_breaking
        now = datetime.utcnow()
        self._seed_headlines(now)
        mark_breaking(now)
        auth = {"Authorization": f"Bearer {self._token()}"}
        entry = breaking.bar_entries(now=now)[0]
        self.client.post(f"/api/v1/breaking/{entry.id}/newsbox", headers=auth, json={})
        with mock.patch("aggregator.breaking._scrape", return_value=(None, breaking.SKIP_NOTES["opinion"])):
            jobs.run_next_job()
        item = self.client.get("/api/v1/me/newsbox", headers=auth).get_json()["items"][0]
        self.assertEqual(item["state"], "failed")
        self.assertIn("opinion", item["error"])
        bar = self.client.get("/api/v1/breaking", headers=auth).get_json()["items"][0]
        self.assertEqual((bar["state"], bar["error"]), ("failed", breaking.SKIP_NOTES["opinion"]))

    def test_sending_needs_the_summarize_scope(self):
        from aggregator import breaking
        from news_fetcher.breaking_feed import mark_breaking
        now = datetime.utcnow()
        self._seed_headlines(now)
        mark_breaking(now)
        auth = {"Authorization": f"Bearer {self._token(scopes=('read',))}"}
        entry = breaking.bar_entries(now=now)[0]
        self.assertEqual(self.client.post(f"/api/v1/breaking/{entry.id}/newsbox", headers=auth, json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/v1/breaking/999999/newsbox",
                                          headers={"Authorization": f"Bearer {self._token()}"}, json={}).status_code, 404)
