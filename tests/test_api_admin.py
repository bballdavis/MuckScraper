"""Tests for the account and admin settings resources (aggregator/api/admin/).

Like tests/test_api.py, these run against a scratch Postgres database named
by API_TEST_DATABASE_URL, whose tables are dropped and recreated, and are
skipped without it.
"""
import json
import os
import unittest
from unittest import mock

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


def _api_app(database_url):
    with mock.patch.dict(os.environ, {"DATABASE_URL": database_url, "API_PROXY_HOPS": "0"}):
        from aggregator.api_app import create_api_app
        return create_api_app()


class ValuesTests(unittest.TestCase):
    def test_checks(self):
        from aggregator.api.admin import values as v
        from aggregator.api.admin.values import Invalid
        self.assertEqual(v.get_int({"n": "7"}, "n", 0, 23), 7)
        with self.assertRaises(Invalid) as e:
            v.get_int({"n": True}, "n", 0, 23)
        self.assertEqual(e.exception.field, "n")
        with self.assertRaises(Invalid):
            v.get_str({"s": "x" * 10}, "s", 5)
        self.assertEqual(v.get_choice({"c": 45}, "c", (0, 45)), "45")
        self.assertEqual(v.choices([("a", "A"), ("b", "B", "More")]),
                         [{"value": "a", "label": "A", "detail": None}, {"value": "b", "label": "B", "detail": "More"}])


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to a scratch Postgres database")
class AdminApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("SECRET_KEY", "test-secret-key")
        from aggregator import db
        cls.app = _api_app(TEST_DB)
        with cls.app.app_context():
            db.drop_all()
            db.create_all()
            cls._seed()

    @classmethod
    def _seed(cls):
        from aggregator import db
        from aggregator.models import PromptTemplate, Topic, User
        admin = User(username="boss", email="boss@example.com", role="admin")
        admin.set_password("password123")
        reader = User(username="reader", email="r@example.com", role="reader")
        reader.set_password("password123")
        db.session.add_all([admin, reader, Topic(name="World", sort_order=0), Topic(name="Business", sort_order=1),
                            PromptTemplate(key="story_summary", description="Summarize a story",
                                           default_text="Summarize {combined}", current_text="Summarize {combined}"),
                            PromptTemplate(key="deep_report.politics", description="Politics report",
                                           default_text="Report on {combined}", current_text="Report on {combined}")])
        db.session.commit()
        cls.ids = {"admin": admin.id, "reader": reader.id}

    def setUp(self):
        from aggregator import db
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.session.execute(db.text("DELETE FROM api_rate_counters"))
        db.session.commit()

    def tearDown(self):
        from aggregator import db
        db.session.remove()
        self.ctx.pop()

    def _token(self, user="admin", scopes=("read", "summarize", "admin")):
        from aggregator import db
        from aggregator.api.auth import create_pairing_code
        from aggregator.models import User
        code, _ = create_pairing_code(db.session.get(User, self.ids[user]), list(scopes), "test")
        db.session.commit()
        r = self.client.post("/api/v1/devices/pair", json={"code": code, "device_name": "Phone"})
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()["token"]

    def _call(self, method, path, token, body=None, status=200):
        r = self.client.open(f"/api/v1{path}", method=method, json=body,
                             headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, status, (method, path, r.get_json()))
        return r.get_json()

    def _get(self, path, token, status=200):
        return self._call("GET", path, token, status=status)

    # --- who sees what -------------------------------------------------------------------

    def test_a_reader_has_an_account_and_no_admin(self):
        token = self._token("reader", ("read", "summarize"))
        account = self._get("/me/account", token)
        self.assertEqual(account["username"], "reader")
        self.assertIsNone(account["admin_settings"])
        self.assertIn({"value": "24h", "label": "24 hours", "detail": None}, account["choices"]["range"])
        r = self._get("/admin/users", token, 403)
        self.assertEqual(r["error"]["code"], "admin_scope")
        self._get("/admin/fetch", token, 403)

    def test_an_admin_without_the_scope_is_told_it_is_available(self):
        token = self._token("admin", ("read", "summarize"))
        self.assertEqual(self._get("/me/account", token)["admin_settings"], "available")
        self._get("/admin/summary", token, 403)
        self.assertEqual(self._get("/me/account", self._token())["admin_settings"], "on")

    def test_the_scope_stops_working_once_the_user_is_no_longer_an_admin(self):
        from aggregator import db
        from aggregator.models import User
        user = User(username="former", email="f@example.com", role="admin")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()
        self.ids["former"] = user.id
        token = self._token("former")
        self._get("/admin/users", token)
        user.set_role("reader")
        db.session.commit()
        self._get("/admin/users", token, 403)

    def test_every_admin_page_opens(self):
        token = self._token()
        for path in ("/admin/summary", "/admin/overview", "/admin/users", f"/admin/users/{self.ids['reader']}",
                     "/admin/api", "/admin/sign-in", "/admin/sections", "/admin/sources", "/admin/searches",
                     "/admin/source-keys", "/admin/blocklist", "/admin/scrape-health", "/admin/llm",
                     "/admin/prompts", "/admin/prompts/story_summary", "/admin/tasks", "/admin/schedule",
                     "/admin/fetch"):
            self._get(path, token)
        self._get("/admin/users/999999", token, 404)
        self._get("/admin/prompts/nothing", token, 404)

    def test_no_secret_value_is_ever_sent(self):
        token = self._token()
        for path in ("/admin/llm", "/admin/source-keys", "/admin/sign-in"):
            body = str(self._get(path, token))
            self.assertNotIn("'value': 'sk-", body)

    # --- account -------------------------------------------------------------------------

    def test_email_change_needs_the_current_password(self):
        token = self._token("reader", ("read",))
        r = self._call("PUT", "/me/account/email", token, {"email": "new@example.com", "current_password": "no"},
                       status=422)
        self.assertEqual(r["error"]["field"], "current_password")
        r = self._call("PUT", "/me/account/email", token,
                       {"email": "new@example.com", "current_password": "password123"})
        self.assertEqual(r["message"], "Email address updated.")
        self.assertEqual(r["data"]["email"], "new@example.com")

    def test_password_change_checks_the_new_one(self):
        token = self._token("reader", ("read",))
        r = self._call("PUT", "/me/account/password", token, {"current_password": "password123",
                                                               "new_password": "short", "confirm_password": "short"},
                       status=422)
        self.assertEqual(r["error"]["field"], "new_password")

    def test_preferences(self):
        token = self._token("reader", ("read",))
        r = self._call("PATCH", "/me/account/preferences", token, {"range": "24h", "auto_read_seconds": "45"})
        self.assertEqual(r["data"]["preferences"]["range"], "24h")
        self.assertEqual(r["data"]["preferences"]["auto_read_seconds"], 45)
        self._call("PATCH", "/me/account/preferences", token, {"range": "forever"}, status=422)
        self._call("PATCH", "/me/account/preferences", token, {"colour": "red"}, status=422)

    # --- general -------------------------------------------------------------------------

    def test_users_add_edit_and_the_admin_guards(self):
        token = self._token()
        r = self._call("POST", "/admin/users", token, {"username": "newbie", "email": "n@example.com",
                                                       "role": "reader", "password": "password123"})
        new_id = next(u["id"] for u in r["data"]["items"] if u["username"] == "newbie")
        r = self._call("PATCH", f"/admin/users/{new_id}", token, {"role": "scraper"})
        self.assertEqual(r["data"]["role"], "scraper")
        self._call("POST", "/admin/users", token, {"username": "newbie", "email": "x@example.com",
                                                   "role": "reader", "password": "password123"}, status=422)
        r = self._call("PATCH", f"/admin/users/{self.ids['admin']}", token, {"role": "reader"}, status=422)
        self.assertIn("own role", r["error"]["message"])
        self._call("DELETE", f"/admin/users/{self.ids['admin']}", token, status=422)
        self._call("DELETE", f"/admin/users/{new_id}", token)
        self._get(f"/admin/users/{new_id}", token, 404)

    def test_a_pairing_code_with_admin_settings_only_for_an_admin(self):
        token = self._token()
        r = self._call("POST", "/admin/api/pairing-codes", token, {"user_id": self.ids["reader"], "admin": True},
                       status=422)
        self.assertEqual(r["error"]["field"], "admin")
        code = self._call("POST", "/admin/api/pairing-codes", token, {"user_id": self.ids["admin"], "admin": True},
                          status=201)
        self.assertTrue(code["link"].startswith("muckscroller://pair?code="))
        r = self.client.post("/api/v1/devices/pair", json={"code": code["code"], "device_name": "Second"})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertIn("admin", r.get_json()["device"]["scopes"])

    def test_admin_access_switches_on_for_a_paired_device(self):
        from aggregator.models import ApiDevice
        admin_token = self._token()
        plain = self._token("admin", ("read", "summarize"))
        self._get("/admin/summary", plain, 403)
        device = ApiDevice.query.order_by(ApiDevice.id.desc()).first()
        self._call("PUT", f"/admin/api/devices/{device.id}/admin", admin_token, {"admin": True})
        self._get("/admin/summary", plain)
        reader = self._token("reader", ("read",))
        device = ApiDevice.query.order_by(ApiDevice.id.desc()).first()
        self._call("PUT", f"/admin/api/devices/{device.id}/admin", admin_token, {"admin": True}, status=422)
        del reader

    def test_api_limits_are_checked(self):
        token = self._token()
        r = self._call("PATCH", "/admin/api/settings", token, {"limits": {"per_device_per_minute": 0}}, status=422)
        self.assertEqual(r["error"]["field"], "per_device_per_minute")
        r = self._call("PATCH", "/admin/api/settings", token, {"limits": {"per_device_per_minute": 500}})
        self.assertEqual(r["data"]["settings"]["limits"]["per_device_per_minute"], 500)

    def test_oidc_needs_https_before_it_turns_on(self):
        token = self._token()
        r = self._call("PATCH", "/admin/sign-in", token, {"enabled": True, "issuer_url": "https://id.example.com",
                                                          "client_id": "muck", "redirect_uri": "http://x/cb"},
                       status=422)
        self.assertIn("HTTPS", r["error"]["message"])

    # --- sources -------------------------------------------------------------------------

    def test_sections_add_switch_and_reorder(self):
        from aggregator import db
        from aggregator.models import Topic
        token = self._token()
        r = self._call("POST", "/admin/sections", token, {"name": "Science", "display_name": "Science & Tech"})
        science = next(i for i in r["data"]["items"] if i["display_name"] == "Science & Tech")
        self._call("PATCH", f"/admin/sections/{science['id']}", token, {"is_active": False})
        self.assertFalse(db.session.get(Topic, science["id"]).is_active)
        ids = [i["id"] for i in reversed(r["data"]["items"])]
        r = self._call("PUT", "/admin/sections/order", token, {"ids": ids})
        self.assertEqual([i["id"] for i in r["data"]["items"]], ids)
        self._call("PUT", "/admin/sections/order", token, {"ids": ids[:1]}, status=422)

    def test_feeds_keep_their_roles_together(self):
        token = self._token()
        r = self._call("POST", "/admin/feeds", token, {"url": "https://feeds.example.com/rss", "label": "Example",
                                                       "roles": ["general", "op_ed"]})
        feed = next(f for f in r["data"]["feeds"] if f["url"] == "https://feeds.example.com/rss")
        self.assertEqual(sorted(feed["roles"]), ["general", "op_ed"])
        r = self._call("PATCH", f"/admin/feeds/{feed['id']}", token, {"roles": ["left_enrichment"]})
        feed = next(f for f in r["data"]["feeds"] if f["url"] == "https://feeds.example.com/rss")
        self.assertEqual(feed["roles"], ["left_enrichment"])
        self._call("POST", "/admin/feeds", token, {"url": "https://feeds.example.com/rss"}, status=422)
        self._call("POST", "/admin/feeds", token, {"url": "ftp://feeds.example.com/rss"}, status=422)
        self._call("DELETE", f"/admin/feeds/{feed['id']}", token)

    def test_searches(self):
        token = self._token()
        r = self._call("POST", "/admin/searches", token, {"label": "AI watch", "mode": "query",
                                                          "newsapi_query": "AI regulation"})
        item = next(i for i in r["data"]["items"] if i["label"] == "AI watch")
        r = self._call("PATCH", f"/admin/searches/{item['id']}", token, {"mode": "top", "newsapi_country": "us"})
        item = next(i for i in r["data"]["items"] if i["label"] == "AI watch")
        self.assertEqual(item["mode"], "top")
        self.assertIsNone(item["newsapi_query"])
        self._call("POST", "/admin/searches", token, {"label": "AI watch"}, status=422)
        self._call("DELETE", f"/admin/searches/{item['id']}", token)

    def test_keys_are_write_only(self):
        token = self._token()
        self._call("PATCH", "/admin/source-keys/newsapi", token, {"values": {"NEWS_API_KEY": "abcdefgh12345678"}})
        page = self._get("/admin/source-keys", token)
        newsapi = next(i for i in page["items"] if i["key"] == "newsapi")
        key = next(f for f in newsapi["fields"] if f["name"] == "NEWS_API_KEY")
        self.assertEqual(key["secret"]["hint"], "5678")
        self.assertNotIn("abcdefgh12345678", str(page))
        self._call("PATCH", "/admin/source-keys/newsapi", token, {"values": {"NEWS_API_KEY": "has spaces"}},
                   status=422)
        self._call("PATCH", "/admin/source-keys/newsapi", token, {"values": {"NEWS_API_KEY": ""}})
        page = self._get("/admin/source-keys", token)
        newsapi = next(i for i in page["items"] if i["key"] == "newsapi")
        self.assertFalse(next(f for f in newsapi["fields"] if f["name"] == "NEWS_API_KEY")["secret"]["saved"])

    def test_poll_schedule_bands(self):
        token = self._token()
        r = self._call("PATCH", "/admin/source-keys/gnews", token, {"schedule": [{"hour": 6, "minutes": 30},
                                                                                  {"hour": 22, "minutes": 120}]})
        gnews = next(i for i in r["data"]["items"] if i["key"] == "gnews")
        # The first band always starts at midnight.
        self.assertEqual(gnews["schedule"], [{"hour": 0, "minutes": 30}, {"hour": 22, "minutes": 120}])
        self.assertTrue(gnews["schedule_saved"])
        self._call("PATCH", "/admin/source-keys/gnews", token, {"schedule": [{"hour": 40, "minutes": 30}]},
                   status=422)
        r = self._call("PATCH", "/admin/source-keys/gnews", token, {"schedule": None})
        self.assertFalse(next(i for i in r["data"]["items"] if i["key"] == "gnews")["schedule_saved"])

    def test_blocklist_rules(self):
        from aggregator import db
        from aggregator.models import IngestionBlock
        token = self._token()
        r = self._call("POST", "/admin/blocklist", token, {"effect": "skip", "match": "title", "pattern": "Casino",
                                                           "reason": "Ads"})
        rule = next(i for i in r["data"]["items"] if i["pattern"] == "casino")
        self._call("PATCH", f"/admin/blocklist/{rule['table']}/{rule['id']}", token, {"active": False})
        self.assertFalse(db.session.get(IngestionBlock, rule["id"]).is_active)
        self._call("POST", "/admin/blocklist", token, {"effect": "skip", "match": "title", "pattern": "casino",
                                                       "reason": "Again"}, status=422)
        self._call("DELETE", f"/admin/blocklist/{rule['table']}/{rule['id']}", token)

    # --- AI ------------------------------------------------------------------------------

    def test_llm_routing_and_roles(self):
        token = self._token()
        page = self._get("/admin/llm", token)
        self.assertIn("ollama", [p["key"] for p in page["providers"]])
        r = self._call("PUT", "/admin/llm/routing", token, {"fast": "gemini"})
        self.assertEqual(r["data"]["routing"]["fast"], "gemini")
        r = self._call("PUT", "/admin/llm/roles", token, {"role": "fast", "provider": r["data"]["routing"]["main"]})
        self.assertEqual(r["data"]["routing"]["fast"], "main")
        self._call("PUT", "/admin/llm/routing", token, {"main": "nobody"}, status=422)
        r = self._call("PUT", "/admin/llm/auto-deep-analysis", token, {"enabled": True})
        self.assertTrue(r["data"]["auto_deep_analysis"])

    def test_provider_models_follow_the_environment_until_changed(self):
        token = self._token()
        r = self._call("PATCH", "/admin/llm/providers/anthropic", token,
                       {"values": {"ANTHROPIC_MODEL": "model-under-test"}})
        anthropic = next(p for p in r["data"]["providers"] if p["key"] == "anthropic")
        model = next(f for f in anthropic["fields"] if f["name"] == "ANTHROPIC_MODEL")
        self.assertEqual((model["source"], model["value"]), ("saved", "model-under-test"))
        self.assertIsNotNone(anthropic["spend"])
        self._call("PATCH", "/admin/llm/providers/anthropic", token, {"values": {"NOPE": "x"}}, status=422)
        self._call("PATCH", "/admin/llm/providers/nobody", token, {"values": {}}, status=404)

    def test_prompts_save_and_reset(self):
        token = self._token()
        r = self._call("PUT", "/admin/prompts/story_summary", token, {"text": "Sum up {combined} briefly"})
        self.assertEqual(r["data"]["text"], "Sum up {combined} briefly")
        self.assertTrue(r["data"]["edited"])
        self._call("PUT", "/admin/prompts/story_summary", token, {"text": "Sum up {nothing}"}, status=422)
        r = self._call("POST", "/admin/prompts/story_summary/reset", token)
        self.assertEqual(r["data"]["text"], "Summarize {combined}")
        self._get("/admin/prompts/deep_report.politics", token)

    # --- pipeline ------------------------------------------------------------------------

    def test_run_schedule(self):
        token = self._token()
        r = self._call("POST", "/admin/schedule", token, {"hour": "7", "full": True})
        self._call("POST", "/admin/schedule", token, {"hour": 7}, status=422)
        run = r["data"]["items"][0]
        self.assertEqual(run["label"], "7:00 AM")
        r = self._call("PATCH", f"/admin/schedule/{run['id']}", token, {"full": False})
        self.assertFalse(r["data"]["items"][0]["full"])
        self._call("DELETE", f"/admin/schedule/{run['id']}", token)

    def test_tasks_and_one_off_jobs(self):
        from aggregator.models import Job
        token = self._token()
        page = self._get("/admin/tasks", token)
        keys = [j["key"] for j in page["jobs"]]
        self.assertEqual(keys[0], "wake_ollama")
        self.assertIn("sync_allsides", keys)
        self.assertIn("merge_outlets", keys)
        r = self._call("POST", "/admin/actions/sync_allsides", token)
        self.assertIn("queued", r["message"])
        r = self._call("POST", "/admin/actions/sync_allsides", token)
        self.assertIn("already running", r["message"])
        job = Job.query.filter_by(kind="bulk_task").order_by(Job.id.desc()).first()
        self.assertEqual(json.loads(job.payload)["action"], "sync_allsides")
        self._call("POST", f"/admin/jobs/{job.id}/cancel", token)
        self._call("POST", "/admin/actions/nothing", token, status=404)
        self._call("PUT", "/admin/tasks/breaking_feeds/paused", token, {"paused": True})
        self.assertTrue(next(t for t in self._get("/admin/tasks", token)["recurring"]
                             if t["id"] == "breaking_feeds")["paused"])
        self._call("PUT", "/admin/tasks/breaking_feeds/paused", token, {"paused": False})
        self._call("POST", "/admin/tasks/breaking_feeds/run", token)

    def test_a_manual_fetch_is_queued_once(self):
        from aggregator.models import Job
        token = self._token()
        self._call("POST", "/admin/fetch", token, {"mode": "query", "label": "AI", "query": ""}, status=422)
        r = self._call("POST", "/admin/fetch", token, {"mode": "query", "label": "AI", "query": "AI regulation"})
        self.assertTrue(r["data"]["status"]["running"])
        self._call("POST", "/admin/fetch", token, {"mode": "top", "label": "Biz", "category": "business",
                                                   "gnews_category": "business"}, status=422)
        job = Job.query.filter_by(kind="manual_fetch").order_by(Job.id.desc()).first()
        self.assertEqual(json.loads(job.payload)["query"], "AI regulation")
