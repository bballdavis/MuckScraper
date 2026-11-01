"""Pinned sources (aggregator/pinned_sources.py): the per-reader pin list, the
/api/v1 endpoints that read and change it, and /stories?outlet=.

The end-to-end tests run against the scratch Postgres database named by
API_TEST_DATABASE_URL (with the vector and pg_trgm extensions) and are
skipped without it. Its tables are dropped and recreated.
"""
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the pinned-source tests")
class PinnedSourcesApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from aggregator import db
        with mock.patch.dict(os.environ, {"DATABASE_URL": TEST_DB, "API_PROXY_HOPS": "0"}):
            from aggregator.api_app import create_api_app
            cls.app = create_api_app()
        with cls.app.app_context():
            db.drop_all()
            db.create_all()
            cls._seed()

    @classmethod
    def _seed(cls):
        from aggregator import db
        from aggregator.models import Article, Outlet, Story, User
        now = datetime.utcnow().replace(microsecond=0)
        reader = User(username="reader", email="r@example.com", role="reader")
        reader.set_password("password123")
        other = User(username="other", email="o@example.com", role="reader")
        other.set_password("password123")
        tribune = Outlet(name="The Texas Tribune", bias_score=3.0, logo_url="https://tribune.example/logo.png")
        wire = Outlet(name="Wire Service", bias_score=3.0)
        quiet = Outlet(name="Quiet Gazette", bias_score=3.0)
        db.session.add_all([reader, other, tribune, wire, quiet])
        db.session.flush()

        def story(title, hours):
            row = Story(title=title, created_at=now - timedelta(hours=hours))
            db.session.add(row)
            db.session.flush()
            return row

        def article(row, outlet, hours, **kw):
            fetched = now - timedelta(hours=hours)
            a = Article(title=f"{row.title} / {outlet.name}", url=f"https://{outlet.id}.example/{row.id}",
                        outlet=outlet, story=row, date=fetched, fetched_at=fetched, **kw)
            db.session.add(a)
            db.session.flush()
            return a

        lone = story("Water board votes", 3)
        article(lone, tribune, 3)
        shared = story("Budget passes", 5)
        article(shared, wire, 5)
        article(shared, tribune, 2)
        # The wire's own story, with a copy of its text that carries the Tribune's name.
        copied = story("Port strike", 4)
        original = article(copied, wire, 4)
        article(copied, tribune, 4, copy_of_id=original.id)
        old = story("Last month", 24 * 40)
        article(old, tribune, 24 * 40)
        elsewhere = story("Rate decision", 1)
        article(elsewhere, wire, 1)
        article(story("Quiet day", 6), quiet, 6)
        db.session.commit()
        cls.ids = {"reader": reader.id, "other": other.id, "tribune": tribune.id, "wire": wire.id,
                   "quiet": quiet.id, "lone": lone.id, "shared": shared.id, "copied": copied.id,
                   "old": old.id}

    def setUp(self):
        from aggregator import db, pinned_sources
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.session.execute(db.text("DELETE FROM api_rate_counters"))
        for user_id in (self.ids["reader"], self.ids["other"]):
            pinned_sources.forget(user_id)
        db.session.commit()
        self.token = self._pair(self.ids["reader"])
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        from aggregator import db
        db.session.remove()
        self.ctx.pop()

    def _pair(self, user_id):
        from aggregator import db
        from aggregator.api.auth import create_pairing_code
        from aggregator.models import User
        code, _ = create_pairing_code(User.query.get(user_id), ["read"], "test")
        db.session.commit()
        r = self.client.post("/api/v1/devices/pair", json={"code": code, "device_name": "Test phone"})
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()["token"]

    def _get(self, path, headers=None):
        return self.client.get(path, headers=headers or self.headers)

    def _pin(self, outlet_id, pinned=True, headers=None):
        return self.client.put("/api/v1/me/pinned-sources", json={"outlet_id": outlet_id, "pinned": pinned},
                               headers=headers or self.headers)

    def test_nothing_is_pinned_until_the_reader_pins_something(self):
        self.assertEqual(self._get("/api/v1/me/pinned-sources").get_json(), {"items": []})

    def test_pin_and_unpin_round_trip_in_pin_order(self):
        r = self._pin(self.ids["wire"])
        self.assertEqual([o["name"] for o in r.get_json()["items"]], ["Wire Service"])
        r = self._pin(self.ids["tribune"])
        items = r.get_json()["items"]
        self.assertEqual([o["name"] for o in items], ["Wire Service", "The Texas Tribune"])
        self.assertEqual(items[1]["logo_url"], "https://tribune.example/logo.png")
        # Pinning again keeps its place.
        self.assertEqual([o["name"] for o in self._pin(self.ids["wire"]).get_json()["items"]],
                         ["Wire Service", "The Texas Tribune"])
        r = self._pin(self.ids["wire"], False)
        self.assertEqual([o["name"] for o in r.get_json()["items"]], ["The Texas Tribune"])
        self.assertEqual([o["name"] for o in self._get("/api/v1/me/pinned-sources").get_json()["items"]],
                         ["The Texas Tribune"])

    def test_pins_belong_to_the_reader(self):
        self._pin(self.ids["tribune"])
        other = {"Authorization": f"Bearer {self._pair(self.ids['other'])}"}
        self.assertEqual(self._get("/api/v1/me/pinned-sources", other).get_json(), {"items": []})

    def test_bad_pins_are_refused(self):
        self.assertEqual(self._pin(999999).status_code, 404)
        for body in ({}, {"outlet_id": self.ids["wire"]}, {"outlet_id": "1", "pinned": True},
                     {"outlet_id": True, "pinned": True}, {"outlet_id": 1, "pinned": "yes"},
                     {"outlet_id": 1, "pinned": True, "extra": 1}):
            r = self.client.put("/api/v1/me/pinned-sources", json=body, headers=self.headers)
            self.assertEqual(r.status_code, 400, body)
        self.assertEqual(self._get("/api/v1/me/pinned-sources").get_json(), {"items": []})

    def test_the_list_has_a_limit(self):
        from aggregator import db, pinned_sources
        from aggregator.models import Outlet
        extra = [Outlet(name=f"Extra {n}", bias_score=3.0) for n in range(pinned_sources.MAX_PINNED)]
        db.session.add_all(extra)
        db.session.commit()
        for outlet in extra:
            self.assertEqual(self._pin(outlet.id).status_code, 200)
        r = self._pin(self.ids["tribune"])
        self.assertEqual(r.status_code, 400)
        self.assertIn("up to", r.get_json()["error"]["message"])
        self._pin(extra[0].id, False)
        self.assertEqual(self._pin(self.ids["tribune"]).status_code, 200)
        db.session.query(Outlet).filter(Outlet.name.like("Extra %")).delete(synchronize_session=False)
        db.session.commit()

    def test_a_deleted_outlet_drops_out_of_the_list(self):
        from aggregator import db
        from aggregator.models import Outlet
        gone = Outlet(name="Gone Daily", bias_score=3.0)
        db.session.add(gone)
        db.session.commit()
        self._pin(gone.id)
        self._pin(self.ids["tribune"])
        db.session.delete(gone)
        db.session.commit()
        names = [o["name"] for o in self._get("/api/v1/me/pinned-sources").get_json()["items"]]
        self.assertEqual(names, ["The Texas Tribune"])

    def test_outlets_lists_recent_ones_with_the_pinned_flag(self):
        self._pin(self.ids["tribune"])
        items = self._get("/api/v1/outlets").get_json()["items"]
        flags = {o["name"]: o["pinned"] for o in items}
        self.assertEqual(flags, {"The Texas Tribune": True, "Wire Service": False, "Quiet Gazette": False})
        found = self._get("/api/v1/outlets?q=tribune").get_json()["items"]
        self.assertEqual([o["name"] for o in found], ["The Texas Tribune"])
        # LIKE wildcards in the search are plain characters.
        self.assertEqual(self._get("/api/v1/outlets?q=%25").get_json()["items"], [])
        self.assertEqual(self._get("/api/v1/outlets?limit=0").status_code, 400)

    def test_stories_for_an_outlet_include_single_source_ones(self):
        r = self._get(f"/api/v1/stories?outlet={self.ids['tribune']}&range=7d")
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        # Newest of the outlet's own articles first; the copy of the wire's
        # article and the month-old story are left out.
        self.assertEqual([c["id"] for c in body["items"]], [self.ids["shared"], self.ids["lone"]])
        self.assertEqual(body["outlet"]["name"], "The Texas Tribune")
        self.assertFalse(body["outlet"]["pinned"])
        self._pin(self.ids["tribune"])
        again = self._get(f"/api/v1/stories?outlet={self.ids['tribune']}&range=7d").get_json()
        self.assertTrue(again["outlet"]["pinned"])

    def test_single_false_keeps_only_stories_other_outlets_reported(self):
        r = self._get(f"/api/v1/stories?outlet={self.ids['tribune']}&range=7d&single=false")
        self.assertEqual([c["id"] for c in r.get_json()["items"]], [self.ids["shared"]])

    def test_an_outlets_range_covers_its_own_articles(self):
        r = self._get(f"/api/v1/stories?outlet={self.ids['tribune']}&range=all")
        ids = [c["id"] for c in r.get_json()["items"]]
        self.assertEqual(ids, [self.ids["shared"], self.ids["lone"], self.ids["old"]])

    def test_unknown_outlet_is_a_404_and_stories_without_one_have_no_outlet_key(self):
        self.assertEqual(self._get("/api/v1/stories?outlet=999999").status_code, 404)
        self.assertEqual(self._get("/api/v1/stories?outlet=abc").status_code, 400)
        self.assertNotIn("outlet", self._get("/api/v1/stories?range=7d").get_json())


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the pinned-source tests")
class WebPinnedSourcesTests(unittest.TestCase):
    """The sidebar's Sources group, a source's own page, and the pin buttons."""

    def setUp(self):
        from aggregator import create_app, db
        from aggregator.models import Article, Outlet, Story, User
        with mock.patch.dict(os.environ, {"DATABASE_URL": TEST_DB, "SECRET_KEY": "test-secret"}):
            self.app = create_app()
        self.app.config.update(WTF_CSRF_ENABLED=False)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        now = datetime.utcnow()
        user = User(username="reader", email="r@example.com", role="reader")
        user.set_password("password123")
        self.tribune = Outlet(name="The Texas Tribune", bias_score=3.0, logo_url="https://tribune.example/logo.png")
        self.plain = Outlet(name="Plain Gazette", bias_score=3.0)
        self.wire = Outlet(name="Wire Service", bias_score=3.0)
        db.session.add_all([user, self.tribune, self.plain, self.wire])
        db.session.flush()
        self.lone = Story(title="Water board votes", created_at=now)
        self.other = Story(title="Rate decision", created_at=now)
        self.plain_story = Story(title="Quiet day", created_at=now)
        db.session.add_all([self.lone, self.other, self.plain_story])
        db.session.flush()
        for story, outlet in ((self.lone, self.tribune), (self.other, self.wire), (self.plain_story, self.plain)):
            db.session.add(Article(title=f"{story.title} text", url=f"https://{outlet.id}.example/{story.id}",
                                   outlet=outlet, story=story, date=now, fetched_at=now))
        db.session.commit()
        self.user_id = user.id
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user.id)

    def tearDown(self):
        from aggregator import db
        db.session.remove()
        self.ctx.pop()

    def _page(self, outlet=None):
        from flask import url_for
        with self.app.test_request_context():
            url = url_for("admin.list_articles", outlet=outlet.id if outlet else None, range="all")
        return self.client.get(url)

    def test_sidebar_has_no_sources_group_until_something_is_pinned(self):
        self.assertNotIn(b'class="nav-link nav-source', self._page().data)

    def test_pinning_from_the_profile_by_name_adds_a_sidebar_row(self):
        from aggregator import pinned_sources
        page = self.client.get("/auth/profile")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Pinned sources", page.data)
        r = self.client.post("/auth/pin-source", data={"name": "the texas tribune", "pinned": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual([o.name for o in pinned_sources.outlets(self.user_id)], ["The Texas Tribune"])
        html = self._page().data.decode()
        self.assertIn(">Sources<", html)
        self.assertIn('class="nav-link nav-source', html)
        self.assertIn("https://tribune.example/logo.png", html)
        self.assertIn(f"outlet={self.tribune.id}", html)

    def test_an_unknown_name_pins_nothing(self):
        from aggregator import pinned_sources
        self.client.post("/auth/pin-source", data={"name": "Nowhere Times", "pinned": "1"})
        self.assertEqual(pinned_sources.ids(self.user_id), [])

    def test_the_source_page_lists_only_its_stories_and_shows_the_logo(self):
        page = self._page(self.tribune)
        self.assertEqual(page.status_code, 200)
        html = page.data.decode()
        self.assertIn("Water board votes", html)
        self.assertNotIn("Rate decision", html)
        self.assertNotIn("Quiet day", html)
        self.assertIn('class="outlet-logo"', html)
        self.assertIn("Pin to menu", html)

    def test_a_source_without_a_logo_shows_its_name_as_a_masthead(self):
        html = self._page(self.plain).data.decode()
        self.assertIn("outlet-mast no-logo", html)
        self.assertNotIn('class="outlet-logo"', html)
        self.assertIn("Plain Gazette", html)

    def test_the_pin_button_pins_and_then_unpins_and_goes_back(self):
        from aggregator import pinned_sources
        r = self.client.post("/auth/pin-source", data={"outlet_id": self.tribune.id, "pinned": "1",
                                                       "next": f"/admin/articles?outlet={self.tribune.id}"})
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"outlet={self.tribune.id}", r.headers["Location"])
        self.assertIn(b"Unpin from menu", self._page(self.tribune).data)
        self.client.post("/auth/pin-source", data={"outlet_id": self.tribune.id, "pinned": "0"})
        self.assertEqual(pinned_sources.ids(self.user_id), [])

    def test_a_missing_outlet_page_is_empty_not_everything(self):
        from flask import url_for
        with self.app.test_request_context():
            url = url_for("admin.list_articles", outlet=999999, range="all")
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b"Water board votes", page.data)
        self.assertNotIn(b"Quiet day", page.data)

    def test_deleting_a_user_drops_their_pins(self):
        from aggregator import db, pinned_sources
        from aggregator.models import AppSetting
        pinned_sources.set_pinned(self.user_id, self.tribune.id, True)
        pinned_sources.forget(self.user_id)
        db.session.commit()
        self.assertIsNone(AppSetting.query.filter_by(key=f"{pinned_sources.USER_KEY_PREFIX}{self.user_id}").first())


if __name__ == "__main__":
    unittest.main()
