"""Opinion & Editorials page (admin.oped_list): the section pages' top bar,
search and filters, compact column cards, Latest / By story, and the top
bar's live column results (admin.search_suggest?scope=oped).

Needs the scratch Postgres database named by API_TEST_DATABASE_URL, like
test_api; skipped without it.
"""
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_DB, "API_TEST_DATABASE_URL not set")
class OpedPageTests(unittest.TestCase):
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
            cls._seed()

    @classmethod
    def _seed(cls):
        from aggregator import db
        from aggregator.models import Article, OpinionPiece, Outlet, Story, User
        now = datetime.utcnow()
        reader = User(username="reader", email="r@example.com", role="reader")
        reader.set_password("password123")
        left = Outlet(name="Left Daily", bias_score=1.0, logo_url="https://l.example/logo.png")
        right = Outlet(name="Right Times", bias_score=5.0)
        story = Story(title="Tariff vote", headline="Senate passes the tariff bill")
        db.session.add_all([reader, left, right, story])
        db.session.flush()
        for i, outlet in enumerate([left, right]):
            db.session.add(Article(title=f"Tariff {i}", url=f"https://n.example/{i}", outlet=outlet,
                                   story=story, date=now - timedelta(hours=2)))
        db.session.add_all([
            OpinionPiece(title="Tariffs will hurt families", url="https://l.example/op1", source="Left Daily",
                         outlet=left, bias_score=1.0, date=now - timedelta(hours=1), story=story,
                         stance="Against the tariff bill", content="<p>Prices go up.</p>"),
            OpinionPiece(title="A win for workers", url="https://r.example/op2", source="Right Times",
                         outlet=right, bias_score=5.0, date=now - timedelta(hours=3), story=story),
            OpinionPiece(title="Old column on zoning", url="https://r.example/op3", source="Right Times",
                         outlet=right, bias_score=5.0, date=now - timedelta(days=20)),
        ])
        db.session.commit()
        cls.reader_id = reader.id

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        from aggregator import db
        db.session.remove()
        self.ctx.pop()

    def _client(self):
        from flask import g
        g.pop("_login_user", None)
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.reader_id)
            session["_fresh"] = True
        return client

    def _get(self, path):
        response = self._client().get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)

    def test_latest_uses_section_chrome_and_compact_cards(self):
        page = self._get("/admin/oped?range=7d")
        self.assertIn('<span class="ft-section">Opinion &amp; Editorials</span>', page)
        self.assertIn('data-scope="oped"', page)
        self.assertIn('id="feed-filters"', page)
        # The same compact card as a one-article story on All Stories.
        self.assertIn('<div class="story compact" id="oped-', page)
        self.assertIn("Tariffs will hurt families", page)
        self.assertIn("/admin/oped/", page)
        self.assertIn("Open on the publisher", page)
        self.assertIn("Open the news story", page)
        self.assertNotIn("breaking-chip", page)
        self.assertNotIn("Old column on zoning", page)
        self.assertIn("2 columns", page)

    def test_search_and_leaning_filter(self):
        page = self._get("/admin/oped?range=all&q=workers")
        self.assertIn("A win for workers", page)
        self.assertNotIn("Tariffs will hurt families", page)
        self.assertIn("matching “workers”", page)
        page = self._get("/admin/oped?range=all&leaning=left")
        self.assertIn("Tariffs will hurt families", page)
        self.assertNotIn("A win for workers", page)
        self.assertIn("Left outlets", page)

    def test_by_story_uses_section_story_cards(self):
        page = self._get("/admin/oped?view=story&range=7d")
        self.assertIn('<div class="story compact" id="story-', page)
        self.assertIn('class="card-bias"', page)
        self.assertIn("Senate passes the tariff", page)
        self.assertIn("2 columns", page)
        self.assertIn("1 story with columns", page)

    def test_live_results_are_columns(self):
        data = self._client().get("/admin/search/suggest?q=tariff&scope=oped&range=7d").get_json()
        self.assertEqual([o["title"] for o in data["opeds"]], ["Tariffs will hurt families"])
        self.assertEqual(data["opeds"][0]["lean"], "Left")
        self.assertEqual(data["oped_total"], 1)
        self.assertIn("/admin/oped?", data["all_url"])


if __name__ == "__main__":
    unittest.main()
