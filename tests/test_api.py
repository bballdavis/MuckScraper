"""Tests for the Mobile API (aggregator/api/, aggregator/api_app.py).

The route checks need no database. The end-to-end tests pair a device and
call every endpoint against a scratch Postgres database named by
API_TEST_DATABASE_URL (with the vector and pg_trgm extensions); they are
skipped without it. The database's tables are dropped and recreated.
"""
import os
import re
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


def _api_app(database_url="postgresql://unused/none"):
    with mock.patch.dict(os.environ, {"DATABASE_URL": database_url, "API_PROXY_HOPS": "0"}):
        from aggregator.api_app import create_api_app
        return create_api_app()


class RouteIsolationTests(unittest.TestCase):
    def test_only_api_routes_are_registered(self):
        rules = [rule.rule for rule in _api_app().url_map.iter_rules()]
        self.assertTrue(rules)
        for rule in rules:
            self.assertTrue(rule.startswith("/api/v1/"), rule)

    def test_every_route_is_in_the_openapi_spec(self):
        from aggregator.api.openapi import SPEC
        documented = {}
        for path, ops in SPEC["paths"].items():
            documented.setdefault(re.sub(r"\{\w+\}", "{}", path), set()).update(m.upper() for m in ops)
        for rule in _api_app().url_map.iter_rules():
            path = re.sub(r"<(?:\w+:)?\w+>", "{}", rule.rule[len("/api/v1"):])
            methods = rule.methods - {"HEAD", "OPTIONS"}
            self.assertIn(path, documented, rule.rule)
            self.assertTrue(methods <= documented[path], f"{rule.rule} {methods}")


class ParamTests(unittest.TestCase):
    def test_cursor_round_trip_and_rejects_junk(self):
        from aggregator.api.errors import ApiError
        from aggregator.api.params import cursor, encode_cursor
        self.assertEqual(cursor(encode_cursor(40)), 40)
        for junk in ("x", "eyJvIjogLTF9", "e30"):
            with self.assertRaises(ApiError):
                cursor(junk)


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the API end-to-end tests")
class ApiEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from aggregator import db
        cls.app = _api_app(TEST_DB)
        with cls.app.app_context():
            db.drop_all()
            db.create_all()
            cls._seed()

    @classmethod
    def _seed(cls):
        from aggregator import db
        from aggregator.models import (Article, Edition, EditionStory, OpinionPiece, Outlet, Story, Topic, User)
        now = datetime.utcnow().replace(microsecond=0)
        reader = User(username="reader", email="r@example.com", role="reader")
        reader.set_password("password123")
        off = User(username="off", email="o@example.com", role="reader", is_active=False)
        politics = Topic(name="US Politics", icon="🏛", sort_order=1)
        left = Outlet(name="Left Daily", bias_score=1.5)
        right = Outlet(name="Right Times", bias_score=4.5)
        story = Story(title="Budget vote", headline="Senate passes budget", summary="The big picture: The Senate passed it.\nWhy it matters: Money.",
                      deep_report="The story: long report.", created_at=now - timedelta(hours=3), topics=[politics])
        lone = Story(title="Lone story", created_at=now - timedelta(hours=2))
        db.session.add_all([reader, off, politics, left, right, story, lone])
        db.session.flush()
        a1 = Article(title="Budget passes", url="https://left.example/a1", outlet=left, story=story, date=now - timedelta(hours=2),
                     content="<p>Full scraped text</p><script>x()</script>", summary="Senate passed the budget",
                     image_url="https://left.example/a1.jpg", scrape_status="success", scrape_failure_reason="secret reason")
        a2 = Article(title="Budget passes, critics say", url="https://right.example/a2", outlet=right, story=story,
                     date=now - timedelta(hours=1), scrape_status="blocked")
        a3 = Article(title="Lone article", url="https://left.example/a3", outlet=left, story=lone, date=now - timedelta(hours=1))
        edition = Edition(date=date.today(), edition_type="morning", created_at=now - timedelta(minutes=30), published=True)
        db.session.add_all([a1, a2, a3, edition])
        db.session.flush()
        db.session.add(EditionStory(edition_id=edition.id, story_id=story.id, rank=1))
        db.session.add(OpinionPiece(title="Why the budget fails", url="https://right.example/op", outlet=right, bias_score=4.5,
                                    story_id=story.id, stance="Too much spending", date=now, link_checked_at=now))
        db.session.commit()
        cls.ids = {"reader": reader.id, "off": off.id, "story": story.id, "lone": lone.id, "a1": a1.id, "edition": edition.id}

    def setUp(self):
        from aggregator import db
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        # Every test pairs at least one device; start each with fresh counts.
        db.session.execute(db.text("DELETE FROM api_rate_counters"))
        db.session.commit()

    def tearDown(self):
        from aggregator import db
        db.session.remove()
        self.ctx.pop()

    def _code(self, user_id=None, expired=False):
        from aggregator import db
        from aggregator.api.auth import create_pairing_code
        from aggregator.models import User
        code, row = create_pairing_code(User.query.get(user_id or self.ids["reader"]), ["read"], "test")
        if expired:
            row.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        return code

    def _pair(self, name="Test phone"):
        r = self.client.post("/api/v1/devices/pair", json={"code": self._code(), "device_name": name})
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()["token"]

    def _get(self, path, token, **headers):
        return self.client.get(path, headers={"Authorization": f"Bearer {token}", **headers})

    def test_health_needs_no_token_and_says_nothing_else(self):
        r = self.client.get("/api/v1/health")
        self.assertEqual(r.get_json(), {"ok": True, "version": "1.0"})
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("max-age", r.headers["Strict-Transport-Security"])

    def test_pairing_code_works_once_and_token_is_hashed(self):
        from aggregator.models import ApiDevice
        code = self._code()
        first = self.client.post("/api/v1/devices/pair", json={"code": code.lower()[:5] + "-" + code[5:]})
        self.assertEqual(first.status_code, 201)
        token = first.get_json()["token"]
        self.assertTrue(token.startswith("msk_"))
        self.assertIsNone(ApiDevice.query.filter_by(token_hash=token).first())
        again = self.client.post("/api/v1/devices/pair", json={"code": code})
        self.assertEqual(again.status_code, 400)
        self.assertEqual(again.get_json()["error"]["code"], "invalid_code")

    def test_expired_code_and_disabled_user_cannot_pair(self):
        r = self.client.post("/api/v1/devices/pair", json={"code": self._code(expired=True)})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/v1/devices/pair", json={"code": self._code(user_id=self.ids["off"])})
        self.assertEqual(r.status_code, 400)

    def test_requests_need_a_live_token(self):
        self.assertEqual(self.client.get("/api/v1/stories").status_code, 401)
        self.assertEqual(self._get("/api/v1/stories", "msk_nope").status_code, 401)
        token = self._pair()
        self.assertEqual(self._get("/api/v1/me", token).status_code, 200)
        r = self.client.delete("/api/v1/me/devices/current", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self._get("/api/v1/me", token).status_code, 401)

    def test_disabling_the_user_cuts_off_their_devices(self):
        from aggregator import db
        from aggregator.models import User
        token = self._pair()
        user = User.query.get(self.ids["reader"])
        user.is_active = False
        db.session.commit()
        try:
            self.assertEqual(self._get("/api/v1/me", token).status_code, 401)
        finally:
            user.is_active = True
            db.session.commit()

    def test_content_endpoints(self):
        token = self._pair()
        latest = self._get("/api/v1/editions/latest", token).get_json()
        self.assertEqual(latest["stories"][0]["headline"], "Senate passes budget")
        card = latest["stories"][0]
        self.assertEqual(card["coverage"], {"left": 1, "center": 0, "right": 1})
        self.assertEqual(card["section"], "US Politics")
        self.assertEqual(card["dek"], "The Senate passed it.")
        self.assertTrue(card["updated_at"].endswith("Z"))

        detail = self._get(f"/api/v1/stories/{self.ids['story']}", token).get_json()
        self.assertEqual(len(detail["articles"]["left"]), 1)
        self.assertEqual(detail["opinion"]["right"][0]["stance"], "Too much spending")
        body = str(detail)
        for internal in ("scrape_status", "secret reason", "grouping", "embedding"):
            self.assertNotIn(internal, body)

        article = self._get(f"/api/v1/articles/{self.ids['a1']}", token).get_json()
        self.assertIsNone(article["content_html"])

        topics = self._get("/api/v1/topics", token).get_json()["items"]
        self.assertEqual([t["name"] for t in topics], ["US Politics"])
        self.assertEqual(len(self._get("/api/v1/editions", token).get_json()["items"]), 1)
        self.assertEqual(len(self._get("/api/v1/opinion", token).get_json()["items"]), 1)
        hits = self._get("/api/v1/search?q=budget", token).get_json()["items"]
        self.assertEqual(hits[0]["id"], self.ids["story"])

    def test_full_text_only_when_allowed(self):
        from aggregator import db
        from aggregator.api import settings
        token = self._pair()
        settings.save({"full_text": True})
        db.session.commit()
        try:
            html = self._get(f"/api/v1/articles/{self.ids['a1']}", token).get_json()["content_html"]
            self.assertIn("Full scraped text", html)
            self.assertNotIn("<script", html)
        finally:
            settings.save({"full_text": False})
            db.session.commit()

    def test_stories_feed_filters_and_pages(self):
        token = self._pair()
        both = self._get("/api/v1/stories?single=true&limit=1", token).get_json()
        self.assertEqual(len(both["items"]), 1)
        self.assertIsNotNone(both["next_cursor"])
        rest = self._get(f"/api/v1/stories?single=true&limit=1&cursor={both['next_cursor']}", token).get_json()
        self.assertEqual(len(rest["items"]), 1)
        self.assertIsNone(rest["next_cursor"])
        multi = self._get("/api/v1/stories?single=false", token).get_json()["items"]
        self.assertEqual([s["id"] for s in multi], [self.ids["story"]])
        self.assertEqual(self._get("/api/v1/stories?topic=Nope", token).status_code, 404)

    def test_bad_parameters_are_rejected(self):
        token = self._pair()
        for path in ("/api/v1/stories?limit=500", "/api/v1/stories?range=1y", "/api/v1/stories?sort=new",
                     "/api/v1/stories?cursor=garbage", "/api/v1/search?q=" + "x" * 201, "/api/v1/search"):
            r = self._get(path, token)
            self.assertEqual(r.status_code, 400, path)
            self.assertEqual(r.get_json()["error"]["code"], "bad_request")

    def test_preferences_round_trip(self):
        token = self._pair()
        headers = {"Authorization": f"Bearer {token}"}
        r = self.client.put("/api/v1/me/preferences", json={"range": "24h", "hide_single": True}, headers=headers)
        self.assertEqual(r.get_json(), {"start_page": "headlines", "range": "24h", "hide_single": True})
        r = self.client.put("/api/v1/me/preferences", json={"scrape_status": "failed"}, headers=headers)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._get("/api/v1/me/preferences", token).get_json()["range"], "24h")

    def test_etag_gives_304(self):
        token = self._pair()
        first = self._get("/api/v1/topics", token)
        etag = first.headers["ETag"]
        again = self._get("/api/v1/topics", token, **{"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)

    def test_pairing_attempts_are_limited(self):
        from aggregator import db
        from aggregator.api import settings
        settings.save({"pair_per_ip_per_hour": 3})
        db.session.commit()
        try:
            codes = [self.client.post("/api/v1/devices/pair", json={"code": "WRONGCODE2"}).status_code for _ in range(4)]
            self.assertEqual(codes, [400, 400, 400, 429])
        finally:
            settings.save({"pair_per_ip_per_hour": 10})
            db.session.commit()

    def test_rate_limit_and_switch(self):
        from aggregator import db
        from aggregator.api import settings
        token = self._pair()
        settings.save({"per_device_per_minute": 2})
        db.session.commit()
        try:
            codes = [self._get("/api/v1/topics", token).status_code for _ in range(3)]
            self.assertEqual(codes[-1], 429)
        finally:
            settings.save({"per_device_per_minute": 120})
            db.session.commit()
        settings.save({"enabled": False})
        db.session.commit()
        try:
            self.assertEqual(self._get("/api/v1/topics", token).status_code, 503)
            self.assertEqual(self.client.get("/api/v1/health").status_code, 200)
        finally:
            settings.save({"enabled": True})
            db.session.commit()

    def test_unknown_paths_are_json_404(self):
        r = self.client.get("/admin/tools")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
