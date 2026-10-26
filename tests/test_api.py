"""Tests for the API (aggregator/api/, aggregator/api_app.py).

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


class HeadlineWhyTests(unittest.TestCase):
    """The phone's short reason, which sits beside the L/C/R pill."""

    def _entry(self, recent=0, gap=None, opeds=None):
        from types import SimpleNamespace
        from aggregator.headline_views import Entry
        return Entry(1, SimpleNamespace(recent=recent), 0.0, gap, opeds)

    def test_short_why(self):
        from aggregator.headline_views import short_why
        self.assertEqual(short_why("trending", self._entry(recent=4)), "4 outlets in 6h")
        self.assertEqual(short_why("trending", self._entry(recent=1)), "1 outlet in 6h")
        self.assertIsNone(short_why("trending", self._entry(recent=0)))
        self.assertIsNone(short_why("latest", self._entry(recent=4)))
        self.assertEqual(short_why("controversial", self._entry(gap=0.2, opeds=(2, True))), "High gap · 2 op-eds")
        self.assertEqual(short_why("controversial", self._entry(gap=0.1, opeds=(1, False))), "Moderate gap · 1 op-ed")
        self.assertEqual(short_why("controversial", self._entry(opeds=(0, False))), "Both sides")


class ScheduleTests(unittest.TestCase):
    def test_next_run_follows_days_and_time_zone(self):
        from aggregator.newsbox import clean_schedule, next_run
        settings = clean_schedule({
            "is_enabled": True, "timezone": "America/New_York",
            "run_times": [{"id": "a", "time": "07:00", "days": [1, 2, 3, 4, 5], "is_enabled": True},
                          {"id": "b", "time": "18:30", "days": [6], "is_enabled": False}]})
        # Friday 2026-10-23 12:00 UTC is 08:00 in New York: next is Monday 07:00 EDT.
        self.assertEqual(next_run(settings, datetime(2026, 10, 23, 12, 0)), datetime(2026, 10, 26, 11, 0))
        # Thursday 10:00 UTC is 06:00 in New York: Thursday 07:00 EDT the same day.
        self.assertEqual(next_run(settings, datetime(2026, 10, 22, 10, 0)), datetime(2026, 10, 22, 11, 0))
        self.assertIsNone(next_run({**settings, "is_enabled": False}, datetime(2026, 10, 22)))

    def test_clean_schedule_rejects_junk(self):
        from aggregator.newsbox import ScheduleError, clean_schedule
        for junk in ([], {"depth": "everything"}, {"minimum_sources": True},
                     {"run_times": [{"id": "a", "time": "7:00", "days": [1], "is_enabled": True}]},
                     {"run_times": [{"id": "a", "time": "07:00", "days": [8], "is_enabled": True}]},
                     {"sections": [{"section": "World", "is_enabled": True, "amount": 101}]}):
            with self.assertRaises(ScheduleError):
                clean_schedule(junk)

    def test_every_section_amount_is_a_story_count(self):
        from aggregator.newsbox import clean_schedule
        settings = clean_schedule({"sections": [{"section": "Headlines", "is_enabled": True, "amount": 25},
                                                {"section": "World", "is_enabled": True, "amount": 25}]})
        self.assertEqual([s["amount"] for s in settings["sections"]], [25, 25])


class SafeFetchTests(unittest.TestCase):
    def test_private_and_odd_urls_are_refused(self):
        from aggregator.safe_fetch import BlockedURL, check_url
        for url in ("http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data", "http://10.1.2.3/",
                    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "ftp://example.com/", "file:///etc/passwd",
                    "http://user:pw@example.com/", "http://example.com:8080/", "http://localhost/"):
            with self.assertRaises(BlockedURL, msg=url):
                check_url(url)

    def test_public_host_is_allowed(self):
        from aggregator.safe_fetch import check_url
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            self.assertEqual(check_url("https://example.com/a.jpg").hostname, "example.com")
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0)),
                                                            (2, 1, 6, "", ("192.168.1.7", 0))]):
            from aggregator.safe_fetch import BlockedURL
            with self.assertRaises(BlockedURL):
                check_url("https://rebind.example/")


class ParamTests(unittest.TestCase):
    def test_cursor_round_trip_and_rejects_junk(self):
        from aggregator.api.errors import ApiError
        from aggregator.api.params import cursor, encode_cursor
        self.assertEqual(cursor(encode_cursor(40)), 40)
        for junk in ("x", "eyJvIjogLTF9", "e30"):
            with self.assertRaises(ApiError):
                cursor(junk)


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the API end-to-end tests")
class PairingLinkTests(unittest.TestCase):
    def test_link_carries_code_and_both_addresses_encoded(self):
        from urllib.parse import parse_qs, urlsplit
        from aggregator.blueprints.admin.api_access import pairing_link
        link = pairing_link("ABCDE23456", "https://api.example.com/x?a=1&b=2", "https://news.example.com")
        parts = urlsplit(link)
        self.assertEqual((parts.scheme, parts.netloc), ("muckscroller", "pair"))
        self.assertEqual(parse_qs(parts.query), {
            "code": ["ABCDE23456"], "server": ["https://api.example.com/x?a=1&b=2"],
            "site": ["https://news.example.com"],
        })

    def test_empty_addresses_are_left_out(self):
        from aggregator.blueprints.admin.api_access import pairing_link
        self.assertEqual(pairing_link("ABCDE23456", "", ""), "muckscroller://pair?code=ABCDE23456")


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

    def _code(self, user_id=None, expired=False, scopes=("read",)):
        from aggregator import db
        from aggregator.api.auth import create_pairing_code
        from aggregator.models import User
        code, row = create_pairing_code(User.query.get(user_id or self.ids["reader"]), list(scopes), "test")
        if expired:
            row.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        return code

    def _pair(self, name="Test phone", scopes=("read",)):
        r = self.client.post("/api/v1/devices/pair", json={"code": self._code(scopes=scopes), "device_name": name})
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

    def test_api_key_works_like_a_paired_device_once_keys_are_on(self):
        from datetime import timedelta
        from aggregator import db
        from aggregator.api import settings
        from aggregator.api.auth import create_api_key
        from aggregator.models import User
        reader = User.query.get(self.ids["reader"])
        token, key = create_api_key(reader, "Home dashboard", ["read"], timedelta(days=30))
        db.session.commit()
        self.assertTrue(token.startswith("msk_"))
        off = self._get("/api/v1/me", token)
        self.assertEqual((off.status_code, off.get_json()["error"]["code"]), (401, "api_keys_off"))
        settings.save({"api_keys": True})
        db.session.commit()
        try:
            me = self._get("/api/v1/me", token).get_json()
            self.assertEqual((me["device"]["kind"], me["device"]["name"]), ("key", "Home dashboard"))
            self.assertEqual(self._get("/api/v1/stories", token).status_code, 200)
            r = self.client.post(f"/api/v1/stories/{self.ids['story']}/summarize", json={},
                                 headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(r.status_code, 403)
            forever, key = create_api_key(reader, "Cron job", ["read", "summarize"])
            db.session.commit()
            self.assertIsNone(key.expires_at)
            self.assertEqual(self._get("/api/v1/me", forever).status_code, 200)
        finally:
            settings.save({"api_keys": False})
            db.session.commit()
        # Turning keys off again stops them; paired devices are unaffected.
        self.assertEqual(self._get("/api/v1/me", token).status_code, 401)
        self.assertEqual(self._get("/api/v1/me", self._pair()).status_code, 200)

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
        self.assertEqual(card["coverage"], {"left": 1, "center": 0, "right": 1, "international": 0})
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

    def test_headline_views(self):
        token = self._pair()
        body = self._get("/api/v1/headlines", token).get_json()
        self.assertEqual(body["view"], "trending")
        self.assertEqual([v["key"] for v in body["views"]], ["trending", "latest", "controversial"])
        # Two outlets at most in the seed: below Trending's three, and not
        # two on each side for Controversial.
        self.assertEqual(body["items"], [])
        self.assertEqual(body["hidden_read"], 0)

        latest = self._get("/api/v1/headlines?view=latest", token).get_json()
        self.assertEqual({c["id"] for c in latest["items"]}, {self.ids["story"], self.ids["lone"]})
        self.assertEqual(latest["views"][1]["count"], 2)
        card = next(c for c in latest["items"] if c["id"] == self.ids["story"])
        self.assertEqual(card["coverage"]["left"], 1)
        self.assertIn(card["group"], ("Last hour", "Last 6 hours"))
        self.assertIsNone(card["why"])
        self.assertTrue(card["newest_at"].endswith("Z"))

        page = self._get("/api/v1/headlines?view=latest&limit=1", token).get_json()
        self.assertEqual(len(page["items"]), 1)
        self.assertIsNotNone(page["next_cursor"])

        headers = {"Authorization": f"Bearer {token}"}
        self.client.put(f"/api/v1/me/read/stories/{self.ids['story']}", headers=headers)
        try:
            latest = self._get("/api/v1/headlines?view=latest", token).get_json()
            self.assertEqual([c["id"] for c in latest["items"]], [self.ids["lone"]])
            self.assertEqual(latest["hidden_read"], 1)
        finally:
            self.client.delete(f"/api/v1/me/read/stories/{self.ids['story']}", headers=headers)
        self.assertEqual(self._get("/api/v1/headlines?view=hot", token).status_code, 400)

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
            settings.save({"per_device_per_minute": 300})
            db.session.commit()
        settings.save({"enabled": False})
        db.session.commit()
        try:
            self.assertEqual(self._get("/api/v1/topics", token).status_code, 503)
            self.assertEqual(self.client.get("/api/v1/health").status_code, 200)
        finally:
            settings.save({"enabled": True})
            db.session.commit()

    def _clear_newsbox(self):
        from aggregator import db
        from aggregator.models import Job, NewsBoxItem, Story, StorySkip, SummarySchedule
        NewsBoxItem.query.delete()
        StorySkip.query.delete()
        SummarySchedule.query.delete()
        Job.query.delete()
        Story.query.get(self.ids["story"]).deep_report = "The story: long report."
        lone = Story.query.get(self.ids["lone"])
        lone.summary = None
        db.session.commit()

    def _send(self, method, path, token, **kwargs):
        return self.client.open(path, method=method, headers={"Authorization": f"Bearer {token}"}, **kwargs)

    def test_summarize_needs_its_scope(self):
        self._clear_newsbox()
        token = self._pair()
        r = self._send("POST", f"/api/v1/stories/{self.ids['story']}/summarize", token, json={})
        self.assertEqual(r.status_code, 403)
        r = self._send("PUT", "/api/v1/me/schedules", token, json={"is_enabled": True})
        self.assertEqual(r.status_code, 403)

    def test_summarized_story_is_ready_at_once(self):
        self._clear_newsbox()
        token = self._pair(scopes=("read", "summarize"))
        r = self._send("POST", f"/api/v1/stories/{self.ids['story']}/summarize", token,
                       json={"depth": "summary_analysis", "origin": "saved"})
        self.assertEqual(r.status_code, 200, r.get_json())
        item = r.get_json()
        self.assertEqual((item["state"], item["origin"], item["is_unread"]), ("ready", "saved", True))
        self.assertTrue(item["story"]["has_analysis"])
        self.assertTrue(item["story"]["has_ai_headline"])
        box = self._get("/api/v1/me/newsbox", token).get_json()
        self.assertEqual(box["unread_count"], 1)
        self.assertEqual(self._send("POST", "/api/v1/me/newsbox/read", token).get_json(), {"updated": 1})
        self.assertEqual(self._get("/api/v1/me/newsbox", token).get_json()["unread_count"], 0)
        r = self._send("PATCH", f"/api/v1/me/newsbox/{self.ids['story']}", token, json={"is_unread": True})
        self.assertTrue(r.get_json()["is_unread"])
        self.assertEqual(self._send("DELETE", f"/api/v1/me/newsbox/{self.ids['story']}", token).status_code, 204)
        self.assertEqual(self._get("/api/v1/me/newsbox", token).get_json()["items"], [])

    def _restore_summary(self, story_id, summary):
        from aggregator import db
        from aggregator.models import Story
        with self.app.app_context():
            Story.query.get(story_id).summary = summary
            db.session.commit()

    def test_analysis_depth_alone_and_combined(self):
        from aggregator import db, newsbox
        from aggregator.models import Story
        self._clear_newsbox()
        story = Story.query.get(self.ids["story"])
        kept = story.summary
        story.summary, story.deep_report = None, "The story: long report."
        db.session.commit()
        self.addCleanup(self._restore_summary, self.ids["story"], kept)
        token = self._pair(scopes=("read", "summarize"))
        path = f"/api/v1/stories/{self.ids['story']}/summarize"
        # The analysis is there, so asking for it alone is ready at once.
        r = self._send("POST", path, token, json={"depth": "analysis"})
        self.assertEqual((r.status_code, r.get_json()["state"]), (200, "ready"))
        # Asking for the summary as well needs a job.
        r = self._send("POST", path, token, json={"depth": "summary"})
        self.assertEqual((r.status_code, r.get_json()["state"]), (202, "pending"))
        self.assertEqual(newsbox._deeper("summary", "analysis"), "summary_analysis")
        self.assertTrue(newsbox._covers("summary_analysis", "analysis"))
        self.assertFalse(newsbox._covers("summary", "analysis"))
        # A one-article story has no analysis to write: the summary stands in.
        lone = Story.query.get(self.ids["lone"])
        self.assertEqual(newsbox._needs(lone, "analysis"), {"summary"})
        self._clear_newsbox()

    def test_unsummarized_story_queues_one_job_and_turns_ready(self):
        from aggregator import db, jobs
        from aggregator.models import Job, Story
        self._clear_newsbox()
        token = self._pair(scopes=("read", "summarize"))
        path = f"/api/v1/stories/{self.ids['lone']}/summarize"
        first = self._send("POST", path, token, json={})
        self.assertEqual(first.status_code, 202, first.get_json())
        self.assertEqual((first.get_json()["state"], first.get_json()["is_unread"]), ("pending", False))
        self.assertEqual(self._send("POST", path, token, json={}).status_code, 202)
        self.assertEqual(Job.query.filter_by(kind="newsbox_summary").count(), 1)

        def write_summary(story):
            return "The big picture: Written."
        with mock.patch("news_fetcher.summarizer.check_ollama_status", return_value=True), \
                mock.patch("news_fetcher.summarizer.summarize_story", side_effect=write_summary):
            self.assertTrue(jobs.run_next_job())
        item = self._get("/api/v1/me/newsbox?state=ready", token).get_json()["items"][0]
        self.assertEqual(item["story"]["id"], self.ids["lone"])
        self.assertTrue(item["is_unread"])
        self.assertTrue(Story.query.get(self.ids["lone"]).summary)
        db.session.commit()

    def test_failed_job_marks_items_failed(self):
        from aggregator import jobs
        self._clear_newsbox()
        token = self._pair(scopes=("read", "summarize"))
        self._send("POST", f"/api/v1/stories/{self.ids['lone']}/summarize", token, json={})
        with mock.patch("news_fetcher.summarizer.check_ollama_status", return_value=False):
            jobs.run_next_job()
        item = self._get("/api/v1/me/newsbox", token).get_json()["items"][0]
        self.assertEqual(item["state"], "failed")
        self.assertIn("offline", item["error"])

    def test_summaries_per_day_are_limited(self):
        from aggregator import db
        from aggregator.api import settings
        self._clear_newsbox()
        token = self._pair(scopes=("read", "summarize"))
        settings.save({"summaries_per_user_per_day": 1})
        db.session.commit()
        try:
            ok = self._send("POST", f"/api/v1/stories/{self.ids['story']}/summarize", token, json={})
            self.assertEqual(ok.status_code, 200)
            # Asking again for a story already in NewsBox doesn't count.
            again = self._send("POST", f"/api/v1/stories/{self.ids['story']}/summarize", token, json={})
            self.assertEqual(again.status_code, 200)
            over = self._send("POST", f"/api/v1/stories/{self.ids['lone']}/summarize", token, json={})
            self.assertEqual(over.status_code, 429)
        finally:
            settings.save({"summaries_per_user_per_day": 100})
            db.session.commit()

    def test_schedule_round_trip_and_run(self):
        self._clear_newsbox()
        token = self._pair(scopes=("read", "summarize"))
        self.assertFalse(self._get("/api/v1/me/schedules", token).get_json()["is_enabled"])
        schedule = {
            "is_enabled": True, "timezone": "America/New_York", "depth": "summary",
            "run_times": [{"id": "morning", "time": "07:00", "days": [1, 2, 3, 4, 5, 6, 7], "is_enabled": True}],
            "sections": [{"section": "Headlines", "is_enabled": True, "amount": 1},
                         {"section": "US Politics", "is_enabled": True, "amount": 3}],
            "skip_already_summarized": False, "minimum_sources": None, "notify_when_done": True,
            "next_run_at": None,
        }
        r = self._send("PUT", "/api/v1/me/schedules", token, json=schedule)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIsNotNone(r.get_json()["next_run_at"])
        for bad_value in ({"run_times": [{"id": "x", "time": "25:00", "days": [1], "is_enabled": True}]},
                          {"sections": [{"section": "Headlines", "is_enabled": True, "amount": 101}]},
                          {"timezone": "Mars/Base"}, {"surprise": 1}):
            r = self._send("PUT", "/api/v1/me/schedules", token, json={**schedule, **bad_value})
            self.assertEqual(r.status_code, 400, bad_value)
        run = self._send("POST", "/api/v1/me/schedules/run", token)
        self.assertEqual(run.status_code, 202, run.get_json())
        self.assertEqual(run.get_json()["added"], 1)
        item = self._get("/api/v1/me/newsbox?origin=scheduled", token).get_json()["items"][0]
        self.assertEqual((item["state"], item["run_id"]), ("ready", run.get_json()["run_id"]))

    def test_due_schedules_run_and_move_on(self):
        from aggregator import db, newsbox
        from aggregator.models import NewsBoxItem, SummarySchedule
        self._clear_newsbox()
        settings, row = newsbox.save_schedule(self.ids["reader"], {
            "is_enabled": True, "skip_already_summarized": False,
            "run_times": [{"id": "a", "time": "07:00", "days": [1, 2, 3, 4, 5, 6, 7], "is_enabled": True}],
            "sections": [{"section": "Headlines", "is_enabled": True, "amount": 1}]})
        row.next_run_at = datetime.utcnow() - timedelta(minutes=1)
        db.session.commit()
        self.assertEqual(newsbox.run_due_schedules(), 1)
        self.assertEqual(NewsBoxItem.query.filter_by(user_id=self.ids["reader"], origin="scheduled").count(), 1)
        self.assertGreater(SummarySchedule.query.get(self.ids["reader"]).next_run_at, datetime.utcnow())
        self.assertEqual(newsbox.run_due_schedules(), 0)

    def test_swipe_deck_and_skips(self):
        from aggregator import db, newsbox
        from aggregator.models import Story
        self._clear_newsbox()
        token = self._pair(scopes=("read", "summarize"))
        story, lone = self.ids["story"], self.ids["lone"]
        deck = lambda: [(c["id"], c["tier"]) for c in self._get("/api/v1/me/deck", token).get_json()["items"]]
        # The budget story has both parts; the lone story has no summary yet.
        self.assertEqual(deck(), [(lone, "more")])
        # A headline missing its analysis comes first.
        Story.query.get(story).deep_report = None
        db.session.commit()
        self.assertEqual(deck(), [(story, "headlines"), (lone, "more")])
        # A one-article story is done once summarized: it has no analysis to write.
        Story.query.get(lone).summary = "The big picture: alone."
        db.session.commit()
        self.assertEqual(deck(), [(story, "headlines")])
        # Queued stories leave the deck.
        self._send("POST", f"/api/v1/stories/{story}/summarize", token, json={"depth": "summary_analysis"})
        self.assertEqual(deck(), [])
        self._clear_newsbox()
        Story.query.get(story).deep_report = None
        db.session.commit()

        r = self._send("PUT", f"/api/v1/me/skips/{story}", token)
        self.assertEqual((r.status_code, r.get_json()["story_id"]), (200, story))
        self.assertEqual(self._send("PUT", f"/api/v1/me/skips/{story}", token).status_code, 200)
        self.assertEqual(self._send("PUT", "/api/v1/me/skips/999999", token).status_code, 404)
        self.assertEqual([i["story_id"] for i in self._get("/api/v1/me/skips", token).get_json()["items"]], [story])
        self.assertTrue(self._get(f"/api/v1/stories/{story}", token).get_json()["is_skipped"])
        self.assertNotIn(story, [sid for sid, _ in deck()])
        # Scheduled runs pass over it; asking by hand still works.
        settings, _ = newsbox.load_schedule(self.ids["reader"])
        settings = {**settings, "skip_already_summarized": False,
                    "sections": [{"section": "Headlines", "is_enabled": True, "amount": 5}]}
        self.assertNotIn(story, [s.id for s in newsbox.pick_stories(self.ids["reader"], settings)])
        r = self._send("POST", f"/api/v1/stories/{story}/summarize", token, json={"depth": "summary"})
        self.assertIn(r.status_code, (200, 202))
        self._send("DELETE", f"/api/v1/me/newsbox/{story}", token)

        self.assertEqual(self._send("DELETE", f"/api/v1/me/skips/{story}", token).status_code, 204)
        self.assertEqual(self._send("DELETE", f"/api/v1/me/skips/{story}", token).status_code, 204)
        self.assertFalse(self._get(f"/api/v1/stories/{story}", token).get_json()["is_skipped"])
        self.assertIn(story, [s.id for s in newsbox.pick_stories(self.ids["reader"], settings)])
        self._clear_newsbox()

    def test_article_summary_and_lean_rating(self):
        from unittest import mock
        from aggregator import article_jobs, db
        from aggregator.models import Article, Job, Story
        Job.query.delete()
        a2 = Article.query.filter_by(url="https://right.example/a2").first()
        kept = (a2.content, a2.summary, a2.bias_score)
        a2.content, a2.summary = "<p>The budget passed, critics said, with too much spending.</p>", None
        db.session.commit()
        reader = self._pair()
        token = self._pair(scopes=("read", "summarize"))
        path = f"/api/v1/articles/{a2.id}"

        self.assertEqual(self._send("POST", path + "/summarize", reader).status_code, 403)
        self.assertEqual(self._send("POST", path + "/rate-bias", reader).status_code, 403)
        # One that already has a summary is answered at once.
        r = self._send("POST", f"/api/v1/articles/{self.ids['a1']}/summarize", token)
        self.assertEqual((r.status_code, r.get_json()["summary"]), (200, "Senate passed the budget."))
        # One MuckScraper couldn't read has nothing to summarize.
        lone = Article.query.filter_by(url="https://left.example/a3").first()
        r = self._send("POST", f"/api/v1/articles/{lone.id}/summarize", token)
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (409, "no_text"))

        r = self._send("POST", path + "/summarize", token)
        self.assertEqual(r.status_code, 202)
        self.assertTrue(r.get_json()["is_summarizing"])
        self.assertEqual(self._send("POST", path + "/summarize", token).status_code, 202)
        self.assertEqual(Job.query.filter_by(kind=article_jobs.SUMMARY_JOB).count(), 1)
        r = self._send("POST", path + "/rate-bias", token)
        self.assertEqual(r.status_code, 202)
        body = self._get(path, token).get_json()
        self.assertEqual((body["is_summarizing"], body["is_rating_bias"], body["leaning"]), (True, True, "right"))

        with mock.patch("news_fetcher.summarizer.check_ollama_status", return_value=True), \
                mock.patch("news_fetcher.summarizer.summarize_article", return_value="Critics called it costly."):
            article_jobs.run_summary({"article_id": a2.id})
        with mock.patch("news_fetcher.outlet_bias_llm.get_article_lean_from_llm", return_value=(1, "Only critics quoted.")):
            article_jobs.run_bias({"article_id": a2.id})
        Job.query.delete()
        db.session.commit()
        body = self._get(path, token).get_json()
        self.assertEqual((body["summary"], body["leaning"], body["is_summarizing"]),
                         ("Critics called it costly.", "left", False))
        self.assertEqual((body["lean_source"], body["lean_reason"]), ("ai", "Only critics quoted."))
        # The story's counts now take the article's own lean.
        story = self._get(f"/api/v1/stories/{self.ids['story']}", token).get_json()
        self.assertEqual((story["coverage"]["left"], story["coverage"]["right"]), (2, 0))

        a2 = Article.query.get(a2.id)
        a2.content, a2.summary, a2.bias_score = kept
        a2.bias_source = None
        db.session.commit()

    def test_unrated_outlets_abroad_are_international(self):
        from aggregator import db
        from aggregator.models import Outlet
        right = Outlet.query.filter_by(name="Right Times").first()
        kept = (right.bias_score, right.country, right.funding)
        right.bias_score, right.country, right.funding = None, "GB", "public"
        db.session.commit()
        try:
            token = self._pair()
            story = self._get(f"/api/v1/stories/{self.ids['story']}", token).get_json()
            self.assertEqual(story["coverage"], {"left": 1, "center": 0, "right": 0, "international": 1})
            self.assertEqual(story["articles"]["right"], [])
            self.assertEqual(story["articles"]["unrated"], [])
            intl = story["articles"]["international"][0]
            self.assertEqual((intl["leaning"], intl["outlet"]["country"], intl["outlet"]["funding"]),
                             ("unrated", "GB", "public"))
            left = story["articles"]["left"][0]["outlet"]
            self.assertEqual((left["country"], left["funding"]), (None, None))
        finally:
            right = Outlet.query.get(right.id)
            right.bias_score, right.country, right.funding = kept
            db.session.commit()

    def _bookmark_env(self):
        import tempfile
        from aggregator import db
        from aggregator.models import Article, Bookmark, Job
        Bookmark.query.delete()
        Job.query.delete()
        a1 = Article.query.get(self.ids["a1"])
        a1.content = '<p>Full scraped text</p><img src="https://left.example/inline.png"><img src="http://10.0.0.5/x.png">'
        db.session.commit()
        self.addCleanup(self._restore_a1)
        storage = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"BOOKMARK_STORAGE_DIR": storage})
        patcher.start()
        self.addCleanup(patcher.stop)

        def fake_fetch(url, max_bytes, accept_prefix=None, timeout=15):
            from aggregator.safe_fetch import BlockedURL
            if "10.0.0.5" in url:
                raise BlockedURL("private")
            return b"\x89PNG fake " + url.encode(), "image/png", url
        fetch_patch = mock.patch("aggregator.bookmarks.fetch", side_effect=fake_fetch)
        fetch_patch.start()
        self.addCleanup(fetch_patch.stop)
        return storage

    def _restore_a1(self):
        from aggregator import db
        from aggregator.models import Article
        # Cleanups run after tearDown has popped the test's app context.
        with self.app.app_context():
            Article.query.get(self.ids["a1"]).content = "<p>Full scraped text</p><script>x()</script>"
            db.session.commit()

    def _run_jobs(self, kinds=None):
        from aggregator import jobs
        import aggregator.bookmarks  # noqa: F401  registers the handlers
        while jobs.run_next_job(kinds=kinds, exclude=None if kinds else jobs.BROWSER_KINDS):
            pass

    def test_bookmark_keeps_a_time_capsule(self):
        import io
        import zipfile
        storage = self._bookmark_env()
        token = self._pair(scopes=("read", "summarize"))
        r = self._send("POST", "/api/v1/me/bookmarks", token, json={"story_id": self.ids["story"]})
        self.assertEqual(r.status_code, 202, r.get_json())
        bookmark_id = r.get_json()["id"]
        self._run_jobs()
        listing = self._get("/api/v1/me/bookmarks", token).get_json()
        self.assertIsNone(listing["quota_bytes"])
        latest = listing["items"][0]["latest"]
        self.assertEqual((latest["status"], latest["captures"]), ("ready", "unavailable"))
        self.assertGreater(listing["used_bytes"], 0)

        capsule = self._get(f"/api/v1/me/bookmarks/{bookmark_id}/versions/1", token).get_json()
        self.assertEqual(capsule["format"], "muckscraper-time-capsule")
        self.assertEqual(capsule["story"]["headline"], "Senate passes budget")
        self.assertEqual(capsule["story"]["summary"][0], "The big picture: The Senate passed it.")
        self.assertTrue(capsule["story"]["deep_report"])
        self.assertEqual(capsule["opinion"][0]["stance"], "Too much spending")
        left = next(a for a in capsule["articles"] if a["id"] == self.ids["a1"])
        self.assertIn("Full scraped text", left["content_html"])
        self.assertIn('src="assets/', left["content_html"])
        self.assertNotIn("10.0.0.5", left["content_html"])
        self.assertTrue(left["image"].startswith("assets/"))
        self.assertEqual(left["outlet_bias_score"], 1.5)
        self.assertEqual(capsule["pictures_skipped"], 1)
        name = left["image"].split("/", 1)[1]
        asset = self._get(f"/api/v1/me/bookmarks/{bookmark_id}/versions/1/assets/{name}", token)
        self.assertEqual(asset.status_code, 200)
        self.assertTrue(asset.data.startswith(b"\x89PNG"))
        for bad_name in ("..%2F..%2Fetc%2Fpasswd", "snapshot.json", "abc.svg"):
            self.assertEqual(self._get(f"/api/v1/me/bookmarks/{bookmark_id}/versions/1/assets/{bad_name}", token).status_code, 404)

        again = self._send("POST", f"/api/v1/me/bookmarks/{bookmark_id}/versions", token)
        self.assertEqual(again.status_code, 202)
        self._run_jobs()
        self.assertEqual(self._get(f"/api/v1/me/bookmarks/{bookmark_id}", token).get_json()["version_count"], 2)
        export = self._get(f"/api/v1/me/bookmarks/{bookmark_id}/export", token)
        names = zipfile.ZipFile(io.BytesIO(export.data)).namelist()
        self.assertIn("v1/snapshot.json", names)
        self.assertIn("v2/snapshot.json", names)

        # Saving also sends the story to NewsBox.
        box = self._get("/api/v1/me/newsbox?origin=saved", token).get_json()["items"]
        self.assertEqual(box[0]["story"]["id"], self.ids["story"])

        self.assertEqual(self._send("DELETE", f"/api/v1/me/bookmarks/{bookmark_id}", token).status_code, 204)
        self._run_jobs()
        self.assertEqual(os.listdir(storage), [str(self.ids["reader"])])
        self.assertEqual(os.listdir(os.path.join(storage, str(self.ids["reader"]))), [])

    def test_other_users_bookmarks_are_invisible(self):
        from aggregator import db
        from aggregator.models import Bookmark
        self._bookmark_env()
        mine = Bookmark(user_id=self.ids["off"], story_id=self.ids["story"], title="Not yours")
        db.session.add(mine)
        db.session.commit()
        token = self._pair()
        self.assertEqual(self._get(f"/api/v1/me/bookmarks/{mine.id}", token).status_code, 404)
        self.assertEqual(self._get("/api/v1/me/bookmarks", token).get_json()["items"], [])

    def test_bookmark_over_the_storage_limit_stops(self):
        from aggregator import db
        from aggregator.api import settings
        self._bookmark_env()
        token = self._pair(scopes=("read", "summarize"))
        settings.save({"bookmark_quota_overrides": {str(self.ids["reader"]): 0}, "bookmark_quota_mb": 1})
        db.session.commit()
        try:
            # The override of 0 means unlimited for this user, whatever the default.
            self.assertIsNone(self._get("/api/v1/me/bookmarks", token).get_json()["quota_bytes"])
            settings.save({**settings.load(fresh=True), "bookmark_quota_overrides": {}})
            db.session.commit()
            with mock.patch("aggregator.bookmarks.quota_bytes", return_value=100):
                self._send("POST", "/api/v1/me/bookmarks", token, json={"story_id": self.ids["story"]})
                self._run_jobs()
            latest = self._get("/api/v1/me/bookmarks", token).get_json()["items"][0]["latest"]
            self.assertEqual(latest["status"], "over_quota")
            self.assertEqual(latest["size_bytes"], 0)
        finally:
            settings.save({**settings.load(fresh=True), "bookmark_quota_mb": None, "bookmark_quota_overrides": {}})
            db.session.commit()

    def test_capture_adds_pages_to_the_version(self):
        from aggregator import bookmarks, capture, db
        from aggregator.models import BookmarkSnapshot
        self._bookmark_env()
        token = self._pair(scopes=("read", "summarize"))
        with mock.patch("aggregator.bookmarks.capture_available", return_value=True):
            r = self._send("POST", "/api/v1/me/bookmarks", token, json={"story_id": self.ids["story"]})
            self._run_jobs()
        bookmark_id = r.get_json()["id"]
        self.assertEqual(self._get("/api/v1/me/bookmarks", token).get_json()["items"][0]["latest"]["captures"], "pending")

        class FakeBrowser:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_capture(browser, url):
            if "right.example" in url:
                raise RuntimeError("Timeout")
            return b"%PDF-1.7 fake", b"\xff\xd8 fake jpeg"
        with mock.patch.object(capture, "capture_page", side_effect=fake_capture):
            from aggregator import jobs
            jobs.register(bookmarks.CAPTURE_JOB, lambda p: capture.run_capture(p, FakeBrowser), bookmarks.fail_capture)
            self._run_jobs(kinds=(bookmarks.CAPTURE_JOB,))
        snapshot = BookmarkSnapshot.query.filter_by(version=1).order_by(BookmarkSnapshot.id.desc()).first()
        db.session.refresh(snapshot)
        self.assertEqual(snapshot.captures, "partial")
        capsule = self._get(f"/api/v1/me/bookmarks/{bookmark_id}/versions/1", token).get_json()
        done = [c for c in capsule["captures"] if c["pdf"]]
        self.assertEqual(len(done), 1)
        pdf = self._get(f"/api/v1/me/bookmarks/{bookmark_id}/versions/1/{done[0]['pdf']}", token)
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")

    def test_unknown_paths_are_json_404(self):
        r = self.client.get("/admin/tools")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
