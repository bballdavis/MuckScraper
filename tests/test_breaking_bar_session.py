"""The Breaking News bar is a partial that Headlines re-fetches every two minutes
(headlines_feed.html, refreshBreaking). A request without a session must not
get the login page back as a 200 page, because Headlines would insert it into
the story list. It gets a 401 instead, and Headlines sends the browser to the
login page itself.

Runs against the scratch Postgres database named by API_TEST_DATABASE_URL,
like tests/test_web_headline_views.py; skipped without it. Its tables are
dropped and recreated.
"""
import os
import unittest
from unittest import mock

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the Breaking bar session tests")
class BreakingBarSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from aggregator import create_app, db
        env = {"DATABASE_URL": TEST_DB, "SECRET_KEY": "test-secret"}
        with mock.patch.dict(os.environ, env):
            cls.app = create_app()
        cls.app.config.update(WTF_CSRF_ENABLED=False, TESTING=True, PUBLIC_READ_ACCESS=False)
        with cls.app.app_context():
            db.drop_all()
            db.create_all()
            from aggregator.models import User
            user = User(username="reader", email="r@example.com", role="reader")
            user.set_password("password123")
            db.session.add(user)
            db.session.commit()
            cls.user_id = user.id

    def setUp(self):
        self.app.config["PUBLIC_READ_ACCESS"] = False
        self.client = self.app.test_client()

    def _sign_in(self, **session_extra):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.user_id)
            session["_fresh"] = True
            session.update(session_extra)

    def test_signed_out_gets_401_not_the_login_page(self):
        response = self.client.get("/headlines/breaking")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("Location", response.headers)
        self.assertNotIn(b"<form", response.data)

    def test_expired_oidc_session_gets_401(self):
        # An OIDC session past session_lifetime_seconds is cleared before the
        # view runs (aggregator/oidc.py expire_oidc_session).
        self._sign_in(auth_method="oidc", oidc_identity="abc", oidc_expires_at=1)
        response = self.client.get("/headlines/breaking")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(b"<form", response.data)

    def test_signed_in_reader_still_gets_the_bar(self):
        self._sign_in()
        response = self.client.get("/headlines/breaking")
        self.assertEqual(response.status_code, 200)

    def test_public_read_access_needs_no_session(self):
        self.app.config["PUBLIC_READ_ACCESS"] = True
        response = self.client.get("/headlines/breaking")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
