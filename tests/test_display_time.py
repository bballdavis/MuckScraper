import unittest
from datetime import datetime, timedelta

from flask import Flask

from aggregator.display_time import relative_time


class RelativeTimeTests(unittest.TestCase):
    """The server writes the same "3 hours ago" text that
    static/js/local_time.js would, so the line doesn't change once the script
    runs."""

    def test_matches_local_time_js(self):
        now = datetime(2026, 10, 27, 14, 0)
        cases = [
            (timedelta(seconds=20), "Just now"),
            (timedelta(minutes=1), "1 minute ago"),
            (timedelta(minutes=42), "42 minutes ago"),
            (timedelta(hours=1, minutes=5), "1 hour ago"),
            (timedelta(hours=23), "23 hours ago"),
            (timedelta(hours=30), "Yesterday"),
            (timedelta(days=3, hours=2), "3 days ago"),
        ]
        for ago, text in cases:
            self.assertEqual(relative_time(now - ago, now=now), text)
        self.assertEqual(relative_time(None), "")

    def test_older_than_a_week_is_the_date_in_the_display_zone(self):
        app = Flask(__name__)
        app.config["DISPLAY_TIMEZONE"] = "America/New_York"
        with app.app_context():
            # 02:00 UTC on Oct 10 is still Oct 9 in New York.
            self.assertEqual(relative_time(datetime(2026, 10, 10, 2, 0), now=datetime(2026, 10, 27)), "Oct 9, 2026")


if __name__ == "__main__":
    unittest.main()
