"""Tests for per-user read state (aggregator/reads.py): the web app's
reading-glasses buttons and Recently Read, and /api/v1/me/read, which share
one store.

Runs against the scratch Postgres database named by API_TEST_DATABASE_URL,
like tests/test_api.py; skipped without it. Its tables are dropped and
recreated.
"""
import os
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


def _apps(database_url):
    env = {"DATABASE_URL": database_url, "API_PROXY_HOPS": "0", "SECRET_KEY": "test-secret"}
    with mock.patch.dict(os.environ, env):
        from aggregator import create_app
        from aggregator.api_app import create_api_app
        web = create_app()
        web.config.update(WTF_CSRF_ENABLED=False, TESTING=True)
        return web, create_api_app()


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the read-state tests")
class ReadStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from aggregator import db
        from aggregator.models import Article, Edition, EditionStory, Outlet, Story, User
        cls.web, cls.api = _apps(TEST_DB)
        with cls.web.app_context():
            db.drop_all()
            db.create_all()
            now = datetime.utcnow().replace(microsecond=0)
            reader = User(username="reader", email="r@example.com", role="reader")
            reader.set_password("password123")
            other = User(username="other", email="o@example.com", role="reader")
            other.set_password("password123")
            outlet = Outlet(name="Daily", bias_score=3)
            stories = [Story(title=f"Headline {n}", headline=f"Headline {n}", created_at=now - timedelta(hours=n))
                       for n in range(3)]
            db.session.add_all([reader, other, outlet, *stories])
            db.session.flush()
            articles = [Article(title=f"Article {n}", url=f"https://daily.example/{n}", outlet=outlet, story=s,
                                date=now - timedelta(hours=n), scrape_status="success")
                        for n, s in enumerate(stories)]
            edition = Edition(date=date.today(), edition_type="morning", created_at=now, published=True)
            db.session.add_all([*articles, edition])
            db.session.flush()
            for rank, s in enumerate(stories, 1):
                db.session.add(EditionStory(edition_id=edition.id, story_id=s.id, rank=rank))
            db.session.commit()
            cls.ids = {"reader": reader.id, "other": other.id, "stories": [s.id for s in stories],
                       "article": articles[0].id}

    def setUp(self):
        from aggregator import db
        with self.web.app_context():
            db.session.execute(db.text("DELETE FROM read_items"))
            db.session.execute(db.text("DELETE FROM api_rate_counters"))
            db.session.commit()
        self.client = self.web.test_client()
        self._login(self.client, self.ids["reader"])

    @staticmethod
    def _login(client, user_id):
        with client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def _token(self):
        from aggregator import db
        from aggregator.api.auth import create_pairing_code
        from aggregator.models import User
        with self.api.app_context():
            code, _ = create_pairing_code(User.query.get(self.ids["reader"]), ["read"], "test")
            db.session.commit()
        client = self.api.test_client()
        token = client.post("/api/v1/devices/pair", json={"code": code}).get_json()["token"]
        return client, {"Authorization": f"Bearer {token}"}

    def test_marking_read_hides_the_story_from_headlines(self):
        first, second, _ = self.ids["stories"]
        page = self.client.get("/headlines?view=latest").get_data(as_text=True)
        self.assertIn("Headline 0", page)
        self.assertIn('data-story-id="%d"' % first, page)

        r = self.client.post(f"/read/story/{first}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["is_read"])
        page = self.client.get("/headlines?view=latest").get_data(as_text=True)
        self.assertNotIn("Headline 0", page)
        self.assertIn("Headline 1", page)
        self.assertIn("1 story you've read is hidden", page)

        # Undo brings it back.
        r = self.client.delete(f"/read/story/{first}")
        self.assertFalse(r.get_json()["is_read"])
        self.assertIn("Headline 0", self.client.get("/headlines?view=latest").get_data(as_text=True))

    def test_read_state_is_per_user(self):
        first = self.ids["stories"][0]
        self.client.post(f"/read/story/{first}")
        other = self.web.test_client()
        self._login(other, self.ids["other"])
        self.assertIn("Headline 0", other.get("/headlines?view=latest").get_data(as_text=True))
        self.assertNotIn("Headline 0", other.get("/read").get_data(as_text=True))

    def test_recently_read_lists_newest_first_with_articles(self):
        first, second, _ = self.ids["stories"]
        self.client.post(f"/read/story/{first}")
        self.client.post(f"/read/article/{self.ids['article']}")
        self.client.post(f"/read/story/{second}")
        page = self.client.get("/read").get_data(as_text=True)
        self.assertLess(page.index("Headline 1"), page.index("Article 0"))
        self.assertLess(page.index("Article 0"), page.index("Headline 0"))
        self.assertIn("data-mark-unread", page)

    def test_recently_read_search(self):
        first, second, _ = self.ids["stories"]
        self.client.post(f"/read/story/{first}")
        self.client.post(f"/read/story/{second}")
        self.client.post(f"/read/article/{self.ids['article']}")
        page = self.client.get("/read?q=headline 1").get_data(as_text=True)
        self.assertIn("Headline 1", page)
        self.assertNotIn(">Headline 0<", page)
        self.assertNotIn("Article 0", page)
        # Outlet names match articles.
        page = self.client.get("/read?q=daily").get_data(as_text=True)
        self.assertIn("Article 0", page)
        self.assertNotIn(">Headline 1<", page)
        page = self.client.get("/read?q=100%25").get_data(as_text=True)
        self.assertIn("Nothing you've read matches", page)

    def test_story_and_article_pages_show_the_toggle(self):
        first = self.ids["stories"][0]
        page = self.client.get(f"/story/{first}").get_data(as_text=True)
        self.assertIn('class="read-toggle"', page)
        self.assertIn('aria-pressed="false"', page)
        self.client.post(f"/read/story/{first}")
        page = self.client.get(f"/story/{first}").get_data(as_text=True)
        self.assertIn('class="read-toggle is-read"', page)
        self.assertIn('aria-pressed="true"', page)
        page = self.client.get(f"/article/{self.ids['article']}").get_data(as_text=True)
        self.assertIn('data-kind="article"', page)

    def test_auto_read_delay_is_a_profile_setting(self):
        from aggregator import db
        with self.web.app_context():
            db.session.execute(db.text("DELETE FROM app_settings WHERE key LIKE 'reading_v1:%'"))
            db.session.commit()
        first = self.ids["stories"][0]
        # 45 seconds until the reader picks something else.
        self.assertIn('data-auto-read="45"', self.client.get(f"/story/{first}").get_data(as_text=True))
        profile = self.client.get("/auth/profile").get_data(as_text=True)
        self.assertIn("Auto mark as read", profile)
        self.assertIn('name="auto_read_seconds" value="45" checked', profile)

        self.client.post("/auth/profile", data={"action": "reading", "auto_read_seconds": "0"})
        self.assertIn('data-auto-read="0"', self.client.get(f"/story/{first}").get_data(as_text=True))
        self.client.post("/auth/profile", data={"action": "reading", "auto_read_seconds": "60"})
        self.assertIn('data-auto-read="60"',
                      self.client.get(f"/article/{self.ids['article']}").get_data(as_text=True))
        # Anything off the list falls back to the default.
        self.client.post("/auth/profile", data={"action": "reading", "auto_read_seconds": "7"})
        self.assertIn('data-auto-read="45"', self.client.get(f"/story/{first}").get_data(as_text=True))

        # Per user.
        self.client.post("/auth/profile", data={"action": "reading", "auto_read_seconds": "0"})
        other = self.web.test_client()
        self._login(other, self.ids["other"])
        self.assertIn('data-auto-read="45"', other.get(f"/story/{first}").get_data(as_text=True))

    def test_bad_targets(self):
        self.assertEqual(self.client.post("/read/story/999999").status_code, 404)
        self.assertEqual(self.client.post("/read/topic/1").status_code, 404)
        signed_out = self.web.test_client()
        self.assertNotEqual(signed_out.post(f"/read/story/{self.ids['stories'][0]}").status_code, 200)

    def test_app_and_web_share_read_state(self):
        first, second, _ = self.ids["stories"]
        api, auth = self._token()

        r = api.put(f"/api/v1/me/read/stories/{first}", headers=auth)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["kind"], "story")
        self.assertNotIn("Headline 0", self.client.get("/headlines?view=latest").get_data(as_text=True))

        cards = api.get("/api/v1/editions/latest", headers=auth).get_json()["stories"]
        self.assertEqual({c["id"]: c["is_read"] for c in cards}[first], True)
        self.assertEqual({c["id"]: c["is_read"] for c in cards}[second], False)
        self.assertTrue(api.get(f"/api/v1/stories/{first}", headers=auth).get_json()["is_read"])

        # Read on the web, seen by the app.
        self.client.post(f"/read/article/{self.ids['article']}")
        items = api.get("/api/v1/me/read", headers=auth).get_json()["items"]
        self.assertEqual([(i["kind"], i["id"]) for i in items], [("article", self.ids["article"]), ("story", first)])
        self.assertEqual(items[0]["article"]["title"], "Article 0")
        self.assertEqual(items[1]["story"]["headline"], "Headline 0")
        only = api.get("/api/v1/me/read?kind=story", headers=auth).get_json()["items"]
        self.assertEqual([i["id"] for i in only], [first])
        self.assertTrue(api.get(f"/api/v1/articles/{self.ids['article']}", headers=auth).get_json()["is_read"])

        self.assertEqual(api.delete(f"/api/v1/me/read/stories/{first}", headers=auth).status_code, 204)
        self.assertIn("Headline 0", self.client.get("/headlines?view=latest").get_data(as_text=True))
        self.assertEqual(api.put("/api/v1/me/read/stories/999999", headers=auth).status_code, 404)
        self.assertEqual(api.put(f"/api/v1/me/read/topics/{first}", headers=auth).status_code, 404)


if __name__ == "__main__":
    unittest.main()
