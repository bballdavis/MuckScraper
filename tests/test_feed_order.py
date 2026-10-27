"""Feed scan order (aggregator/feed_order.py): reliability, alphabetical,
date added, and the feed's outlet remembered for its grade."""
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_DB, "API_TEST_DATABASE_URL not set")
class FeedOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from aggregator import create_app, db
        env = {"DATABASE_URL": TEST_DB, "SECRET_KEY": "test-secret"}
        with mock.patch.dict(os.environ, env):
            cls.app = create_app()
            # fetch_and_store_articles builds its own app from DATABASE_URL on import.
            import news_fetcher.rss_fetcher  # noqa: F401
        cls.app.config.update(WTF_CSRF_ENABLED=False, TESTING=True)
        with cls.app.app_context():
            db.drop_all()
            db.create_all()
            from aggregator.models import RssFeed, User
            admin = User(username="admin", email="a@example.com", role="admin")
            admin.set_password("password123")
            now = datetime.utcnow()
            db.session.add_all([
                admin,
                # Oldest first: an ungraded feed, a Mixed one, then Reuters (Very high) by its outlet.
                RssFeed(url="https://zeta.example/feed", bucket="general", added_at=now - timedelta(days=3)),
                RssFeed(url="https://nypost.com/feed/", bucket="general", label="New York Post", added_at=now - timedelta(days=2)),
                RssFeed(url="https://news.google.com/rss/search?q=site:reuters.com", bucket="general",
                        source_name="Reuters", added_at=now - timedelta(days=1)),
                RssFeed(url="https://feeds.npr.org/1001/rss.xml", bucket="general", added_at=now),
            ])
            db.session.commit()
            cls.admin_id = admin.id

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        from aggregator import db
        from aggregator.models import AppSetting
        AppSetting.query.delete()
        db.session.commit()
        db.session.remove()
        self.ctx.pop()

    def client(self):
        from flask import g
        g.pop("_login_user", None)
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.admin_id)
        return client

    def test_orders(self):
        from news_fetcher.rss_fetcher import _active_feed_urls
        reliability = _active_feed_urls("general")
        self.assertEqual(reliability[0], "https://news.google.com/rss/search?q=site:reuters.com")
        self.assertEqual(reliability[-2:], ["https://feeds.npr.org/1001/rss.xml", "https://zeta.example/feed"])

        self.client().post("/admin/rss-feeds/order", data={"order": "added"})
        self.assertEqual(_active_feed_urls("general")[0], "https://zeta.example/feed")
        self.client().post("/admin/rss-feeds/order", data={"order": "alphabetical"})
        self.assertEqual(_active_feed_urls("general")[:2],
                         ["https://feeds.npr.org/1001/rss.xml", "https://nypost.com/feed/"])
        self.client().post("/admin/rss-feeds/order", data={"order": "bogus"})
        from aggregator import feed_order
        self.assertEqual(feed_order.current(), "alphabetical")

    def test_outlet_remembered_after_a_fetch(self):
        from aggregator import db
        from aggregator.models import RssFeed
        from news_fetcher import rss_fetcher
        url = "https://feeds.npr.org/1001/rss.xml"
        articles = [{"source_name": "NPR Topics: News"}, {"source_name": "NPR Topics: News"}]
        with mock.patch.object(rss_fetcher, "fetch_feed", return_value=("NPR Topics: News", articles)):
            rss_fetcher._fetch_with_fallback(url)
        db.session.expire_all()
        feed = RssFeed.query.filter_by(url=url).one()
        self.assertEqual(feed.source_name, "NPR")
        self.assertEqual(feed.reporting, "high")
        self.assertEqual(rss_fetcher._active_feed_urls("general")[1], url)

        html = self.client().get("/admin/rss-feeds").get_data(as_text=True)
        self.assertIn("Most reliable first", html)
        self.assertLess(html.index("site:reuters.com"), html.index("zeta.example"))
        feed.source_name = None
        db.session.commit()


if __name__ == "__main__":
    unittest.main()
