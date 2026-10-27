"""Web section pages (admin.list_articles?topic=): only the section's own
stories, and an empty section says so (aggregator/sections.py).

Needs the scratch Postgres database named by API_TEST_DATABASE_URL, like
test_api; skipped without it.
"""
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_DB, "API_TEST_DATABASE_URL not set")
class SectionPageTests(unittest.TestCase):
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
        from aggregator.models import Article, Outlet, Story, Topic, User
        now = datetime.utcnow()
        reader = User(username="reader", email="r@example.com", role="reader")
        reader.set_password("password123")
        politics = Topic(name="US Politics", sort_order=0, is_active=True)
        sports = Topic(name="Sports", sort_order=4, is_active=True)
        tech = Topic(name="Sci/Tech", sort_order=3, is_active=True)
        outlet = Outlet(name="Daily", bias_score=3.0)
        big = Story(title="Senate budget fight", headline="Senate budget fight", topics=[politics, sports])
        game = Story(title="Rovers win the cup", headline="Rovers win the cup", topics=[sports])
        db.session.add_all([reader, politics, sports, tech, outlet, big, game])
        db.session.flush()
        for i in range(4):
            db.session.add(Article(title=f"Budget {i}", url=f"https://d.example/b{i}", outlet=outlet, story=big,
                                   date=now - timedelta(hours=1), topics=[sports] if i == 0 else [politics]))
        db.session.add(Article(title="Cup final", url="https://d.example/c", outlet=outlet, story=game,
                               date=now - timedelta(hours=1), topics=[sports]))
        db.session.commit()
        cls.reader_id = reader.id

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        from aggregator import db
        db.session.remove()
        self.ctx.pop()

    def _get(self, path):
        from flask import g
        g.pop("_login_user", None)
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.reader_id)
            session["_fresh"] = True
        return client.get(path).get_data(as_text=True)

    def test_section_lists_only_its_own_stories(self):
        page = self._get("/admin/articles?topic=Sports&range=24h&show_single=true")
        self.assertIn("Rovers win the cup", page)
        self.assertNotIn("Senate budget fight", page)

    def test_empty_section_says_so(self):
        page = self._get("/admin/articles?topic=Sci/Tech&range=24h&show_single=true")
        self.assertIn("No Sci/Tech stories in the last 24 hours.", page)
        self.assertIn("Show all time", page)
        self.assertNotIn("Senate budget fight", page)
        self.assertIn("No Sci/Tech stories yet.", self._get("/admin/articles?topic=Sci/Tech&range=all&show_single=true"))


if __name__ == "__main__":
    unittest.main()
