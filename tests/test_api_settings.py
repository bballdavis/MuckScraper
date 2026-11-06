"""Tests for the app's settings pages (aggregator/api/settings_pages/).

Like tests/test_api.py, these run against a scratch Postgres database named
by API_TEST_DATABASE_URL, whose tables are dropped and recreated, and are
skipped without it.
"""
import os
import unittest
from unittest import mock

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


def _api_app(database_url):
    with mock.patch.dict(os.environ, {"DATABASE_URL": database_url, "API_PROXY_HOPS": "0"}):
        from aggregator.api_app import create_api_app
        return create_api_app()


class SchemaTests(unittest.TestCase):
    def test_fields_leave_out_empty_keys(self):
        from aggregator.api.settings_pages import schema as s
        self.assertEqual(s.toggle("on", "On", 1), {"key": "on", "label": "On", "type": "toggle", "value": True})
        self.assertEqual(s.choice("c", "C", 45, [(0, "Off"), (45, "45s")])["choices"],
                         [{"value": "0", "label": "Off"}, {"value": "45", "label": "45s"}])
        secret = s.secret("K", "Key", True, "abcd")
        self.assertNotIn("value", secret)
        self.assertEqual(secret["secret"], {"saved": True, "hint": "abcd"})

    def test_a_section_with_fields_saves_by_form_unless_told(self):
        from aggregator.api.settings_pages import schema as s
        self.assertEqual(s.section("a", fields=[s.text("t", "T")])["save"], "form")
        self.assertNotIn("save", s.section("a", facts=[s.fact("x", 1)]))


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to a scratch Postgres database")
class SettingsPagesTests(unittest.TestCase):
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
                                           default_text="Summarize {combined}", current_text="Summarize {combined}")])
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
        code, _ = create_pairing_code(User.query.get(self.ids[user]), list(scopes), "test")
        db.session.commit()
        r = self.client.post("/api/v1/devices/pair", json={"code": code, "device_name": "Phone"})
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()["token"]

    def _h(self, token):
        return {"Authorization": f"Bearer {token}"}

    def _get(self, path, token, status=200):
        r = self.client.get(f"/api/v1{path}", headers=self._h(token))
        self.assertEqual(r.status_code, status, (path, r.get_json()))
        return r.get_json()

    def _patch(self, key, token, values, section=None, status=200):
        body = {"values": values, **({"section": section} if section else {})}
        r = self.client.patch(f"/api/v1/settings/{key}", json=body, headers=self._h(token))
        self.assertEqual(r.status_code, status, (key, r.get_json()))
        return r.get_json()

    def _act(self, key, name, token, values=None, status=200):
        r = self.client.post(f"/api/v1/settings/{key}/actions/{name}", json={"values": values or {}},
                             headers=self._h(token))
        self.assertEqual(r.status_code, status, (key, name, r.get_json()))
        return r.get_json()

    @staticmethod
    def _section(page, key):
        return next(s for s in page["sections"] if s["key"] == key)

    # --- who sees what -------------------------------------------------------------------

    def test_a_reader_sees_only_their_account(self):
        token = self._token("reader", ("read", "summarize"))
        index = self._get("/settings", token)
        self.assertEqual([g["key"] for g in index["groups"]], ["account"])
        self.assertFalse(index["admin"])
        self.assertFalse(index["admin_available"])
        self._get("/settings/users", token, 403)
        self._patch("api", token, {"full_text": True}, "settings", 403)

    def test_an_admin_without_the_scope_is_told_how_to_get_it(self):
        token = self._token("admin", ("read", "summarize"))
        index = self._get("/settings", token)
        self.assertTrue(index["admin_available"])
        self.assertEqual([g["key"] for g in index["groups"]], ["account"])
        self._get("/settings/users", token, 403)

    def test_the_scope_stops_working_once_the_user_is_no_longer_an_admin(self):
        from aggregator import db
        from aggregator.models import User
        user = User(username="former", email="f@example.com", role="admin")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()
        self.ids["former"] = user.id
        token = self._token("former")
        self._get("/settings/users", token)
        user.set_role("reader")
        db.session.commit()
        self._get("/settings/users", token, 403)

    def test_every_page_and_row_page_opens(self):
        """Walk every listed page and every row's page from it."""
        token = self._token()
        index = self._get("/settings", token)
        self.assertEqual([g["key"] for g in index["groups"]], ["account", "general", "sources", "ai", "pipeline"])
        seen, queue = set(), [p["key"] for g in index["groups"] for p in g["pages"]]
        while queue:
            key = queue.pop()
            if key in seen:
                continue
            seen.add(key)
            page = self._get(f"/settings/{key}", token)
            self.assertEqual(page["key"], key)
            for section in page["sections"]:
                for item in section.get("items", []):
                    if item.get("page"):
                        queue.append(item["page"])
                for f in section.get("fields", []):
                    self.assertIn(f["type"], ("toggle", "choice", "multichoice", "text", "url", "email", "number",
                                              "decimal", "secret", "password", "multiline"))
                    if f["type"] == "secret":
                        self.assertNotIn("value", f)
        self.assertGreaterEqual(len(seen), 17)
        self._get("/settings/nothing-here", token, 404)
        self._get("/settings/users/999999", token, 404)

    # --- account -------------------------------------------------------------------------

    def test_email_change_needs_the_current_password(self):
        token = self._token("reader", ("read",))
        r = self._patch("profile", token, {"email": "new@example.com", "current_password": "wrong"}, "email", 422)
        self.assertEqual(r["error"]["field"], "current_password")
        r = self._patch("profile", token, {"email": "new@example.com", "current_password": "password123"}, "email")
        self.assertEqual(r["message"], "Email address updated.")
        email = next(f for f in self._section(r["page"], "email")["fields"] if f["key"] == "email")
        self.assertEqual(email["value"], "new@example.com")

    def test_password_change_checks_the_new_one(self):
        token = self._token("reader", ("read",))
        r = self._patch("profile", token, {"current_password": "password123", "new_password": "short",
                                           "confirm_password": "short"}, "password", 422)
        self.assertEqual(r["error"]["field"], "new_password")

    def test_feed_defaults_and_reading_delay(self):
        token = self._token("reader", ("read",))
        page = self._patch("feed-defaults", token, {"range": "24h"})["page"]
        self.assertEqual(next(f for f in page["sections"][0]["fields"] if f["key"] == "range")["value"], "24h")
        self._patch("feed-defaults", token, {"range": "forever"}, status=422)
        r = self.client.put("/api/v1/me/preferences", json={"auto_read_seconds": 120}, headers=self._h(token))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["auto_read_seconds"], 120)
        self.assertEqual(r.get_json()["range"], "24h")
        r = self.client.put("/api/v1/me/preferences", json={"auto_read_seconds": 7}, headers=self._h(token))
        self.assertEqual(r.status_code, 400)

    def test_boosting_an_outlet(self):
        token = self._token("reader", ("read",))
        page = self._act("boosted", "add", token, {"name": "The Kept Times"})["page"]
        self.assertIn("The Kept Times", [i["id"] for i in page["sections"][0]["items"]])
        page = self._act("boosted", "remove", token, {"id": "The Kept Times"})["page"]
        self.assertNotIn("The Kept Times", [i["id"] for i in page["sections"][0]["items"]])

    # --- admin ---------------------------------------------------------------------------

    def test_users_add_edit_and_the_admin_guards(self):
        token = self._token()
        r = self._act("users", "add", token, {"username": "newbie", "email": "n@example.com", "role": "reader",
                                              "password": "password123"})
        self.assertTrue(r["open"].startswith("users/"))
        key = r["open"]
        page = self._patch(key, token, {"email": "n@example.com", "role": "scraper", "is_active": True}, "account")
        role = next(f for f in self._section(page["page"], "account")["fields"] if f["key"] == "role")
        self.assertEqual(role["value"], "scraper")
        self._act("users", "add", token, {"username": "newbie", "email": "x@example.com", "role": "reader",
                                          "password": "password123"}, 422)
        me = f"users/{self.ids['admin']}"
        r = self._patch(me, token, {"email": "boss@example.com", "role": "reader", "is_active": True}, "account", 422)
        self.assertIn("own role", r["error"]["message"])
        self._act(me, "delete", token, status=422)
        self.assertTrue(self._act(key, "delete", token)["close"])
        self._get(f"/settings/{key}", token, 404)

    def test_a_pairing_code_with_admin_settings_only_for_an_admin(self):
        token = self._token()
        r = self._act("api", "pair", token, {"user_id": str(self.ids["reader"]), "admin": True}, 422)
        self.assertEqual(r["error"]["field"], "admin")
        secret = self._act("api", "pair", token, {"user_id": str(self.ids["admin"]), "admin": True})["secret"]
        self.assertTrue(secret["link"].startswith("muckscroller://pair?code="))
        r = self.client.post("/api/v1/devices/pair", json={"code": secret["value"], "device_name": "Second"})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertIn("admin", r.get_json()["device"]["scopes"])

    def test_api_limits_are_checked(self):
        token = self._token()
        r = self._patch("api", token, {"per_device_per_minute": 0}, "limits", 422)
        self.assertEqual(r["error"]["field"], "per_device_per_minute")
        page = self._patch("api", token, {"per_device_per_minute": 500}, "limits")["page"]
        limit = next(f for f in self._section(page, "limits")["fields"] if f["key"] == "per_device_per_minute")
        self.assertEqual(limit["value"], 500)

    def test_sections_add_switch_and_reorder(self):
        from aggregator.models import Topic
        token = self._token()
        page = self._act("sections", "add", token, {"name": "Science", "display_name": "Science & Tech"})["page"]
        items = page["sections"][0]["items"]
        science = next(i for i in items if i["title"] == "Science & Tech")
        self._patch(science["page"], token, {"active": False})
        self.assertFalse(Topic.query.get(int(science["id"])).is_active)
        ids = [i["id"] for i in reversed(items)]
        page = self._act("sections", "reorder", token, {"ids": ids})["page"]
        self.assertEqual([i["id"] for i in page["sections"][0]["items"]], ids)
        self._act("sections", "reorder", token, {"ids": ids[:1]}, 422)

    def test_feeds_keep_their_page_when_roles_change(self):
        token = self._token()
        r = self._act("sources", "add", token, {"url": "https://feeds.example.com/rss", "label": "Example",
                                                "roles": ["general", "op_ed"]})
        key = r["open"]
        r = self._patch(key, token, {"roles": ["op_ed"]}, "edit")
        self.assertEqual(r["page"]["key"], key)
        r = self._patch(key, token, {"roles": ["left_enrichment"]}, "edit")
        roles = next(f for f in self._section(r["page"], "edit")["fields"] if f["key"] == "roles")
        self.assertEqual(roles["value"], ["left_enrichment"])
        self._act("sources", "add", token, {"url": "https://feeds.example.com/rss"}, 422)
        self.assertTrue(self._act(key, "delete", token)["close"])

    def test_keys_are_write_only(self):
        token = self._token()
        self._patch("source-keys/newsapi", token, {"NEWS_API_KEY": "abcdefgh12345678"}, "settings")
        page = self._get("/settings/source-keys/newsapi", token)
        key = next(f for f in self._section(page, "settings")["fields"] if f["key"] == "NEWS_API_KEY")
        self.assertEqual(key["secret"]["hint"], "5678")
        self.assertNotIn("abcdefgh12345678", str(page))
        self._patch("source-keys/newsapi", token, {"NEWS_API_KEY": "has spaces"}, "settings", 422)
        self._patch("source-keys/newsapi", token, {"NEWS_API_KEY": ""}, "settings")
        page = self._get("/settings/source-keys/newsapi", token)
        key = next(f for f in self._section(page, "settings")["fields"] if f["key"] == "NEWS_API_KEY")
        self.assertFalse(key["secret"]["saved"])

    def test_run_schedule(self):
        token = self._token()
        self._act("schedule", "add", token, {"hour": "7", "run_full_pipeline": True})
        self._act("schedule", "add", token, {"hour": "7"}, 422)
        page = self._get("/settings/schedule", token)
        run = page["sections"][0]["items"][0]
        self.assertEqual(run["title"], "7:00 AM")
        self._patch(run["page"], token, {"run_full_pipeline": False})
        self.assertTrue(self._act(run["page"], "delete", token)["close"])

    def test_blocklist_rules(self):
        from aggregator.models import IngestionBlock
        token = self._token()
        page = self._act("blocklist", "add", token, {"effect": "skip", "match": "title", "pattern": "Casino",
                                                     "reason": "Ads"})["page"]
        rule = next(i for i in page["sections"][0]["items"] if i["title"] == "casino")
        self._patch(rule["page"], token, {"active": False})
        self.assertFalse(IngestionBlock.query.get(int(rule["id"].split("-")[1])).is_active)
        self._act("blocklist", "add", token, {"effect": "skip", "match": "title", "pattern": "casino",
                                              "reason": "Again"}, 422)

    def test_prompts_save_and_reset(self):
        token = self._token()
        page = self._patch("prompts/story_summary", token, {"current_text": "Sum up {combined} briefly"})["page"]
        self.assertEqual(page["sections"][0]["fields"][0]["value"], "Sum up {combined} briefly")
        page = self._act("prompts/story_summary", "reset", token)["page"]
        self.assertEqual(page["sections"][0]["fields"][0]["value"], "Summarize {combined}")
