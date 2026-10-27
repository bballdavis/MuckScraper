"""Tests for the iOS universal-link file (aggregator/app_links.py)."""
import os
import unittest
from unittest import mock

from flask import Flask

from aggregator import app_links


def _client():
    app = Flask(__name__)
    app_links.register(app)
    return app.test_client()


class AppLinksTests(unittest.TestCase):
    def test_off_without_app_ids(self):
        with mock.patch.dict(os.environ, {"IOS_APP_IDS": ""}):
            response = _client().get("/.well-known/apple-app-site-association")
        self.assertEqual(response.status_code, 404)

    def test_lists_app_ids_and_story_paths(self):
        with mock.patch.dict(os.environ, {"IOS_APP_IDS": "ABCDE12345.com.example.MuckScroller, X.y"}):
            client = _client()
            for path in ("/.well-known/apple-app-site-association", "/apple-app-site-association"):
                response = client.get(path)
                self.assertEqual(response.status_code, 200, path)
                self.assertEqual(response.mimetype, "application/json")
                details = response.get_json()["applinks"]["details"][0]
                self.assertEqual(details["appIDs"], ["ABCDE12345.com.example.MuckScroller", "X.y"])
                self.assertEqual(details["components"], [{"/": "/story/*"}])


if __name__ == "__main__":
    unittest.main()
