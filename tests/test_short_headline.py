"""Tests for the short headline the iOS small widget shows
(news_fetcher/headline_generator.shorten_headline and Story.short_headline_text).
Runs without a database or a model: the LLM call is mocked."""

import unittest
from types import SimpleNamespace
from unittest import mock

from aggregator.models import Story
from news_fetcher import headline_generator as hg

LONG = "Senate passes $95 billion Ukraine aid package after weeks of delay in the House"


def _shorten(answer, headline=LONG):
    with mock.patch.object(hg.llm_client, "is_configured", return_value=True), \
         mock.patch.object(hg, "render_prompt", return_value="prompt"), \
         mock.patch.object(hg.llm_client, "generate_text", return_value=answer):
        return hg.shorten_headline(headline)


class ShortenTests(unittest.TestCase):
    def test_short_headline_is_left_alone(self):
        with mock.patch.object(hg.llm_client, "generate_text") as call:
            self.assertIsNone(hg.shorten_headline("Senate passes Ukraine aid"))
        call.assert_not_called()

    def test_good_rewrite_is_used(self):
        self.assertEqual(_shorten('"Senate passes $95 billion Ukraine aid."'),
                         "Senate passes $95 billion Ukraine aid")

    def test_invented_number_is_rejected(self):
        self.assertIsNone(_shorten("Senate passes $96 billion Ukraine aid"))

    def test_failure_text_is_rejected(self):
        self.assertIsNone(_shorten("(Error: no headline provided)"))

    def test_rewrite_still_too_long_is_rejected(self):
        self.assertIsNone(_shorten("Senate passes $95 billion Ukraine aid package after delay"))

    def test_no_answer(self):
        self.assertIsNone(_shorten(None))


class StaleTests(unittest.TestCase):
    def _story(self, short, source):
        story = Story()
        story.title = LONG
        story.headline = None
        story.short_headline = short
        story.short_headline_source = source
        return story

    def test_current_short_headline_is_served(self):
        self.assertEqual(self._story("Senate passes aid", LONG).short_headline_text, "Senate passes aid")

    def test_short_headline_from_an_earlier_headline_is_dropped(self):
        self.assertIsNone(self._story("Senate passes aid", "An older headline").short_headline_text)

    def test_empty_short_headline(self):
        self.assertIsNone(self._story(None, LONG).short_headline_text)


if __name__ == "__main__":
    unittest.main()
