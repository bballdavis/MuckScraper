"""Tests for the NYT Article Search fallback (news_fetcher/nyt_api.py) and how
scrape_article() uses it for paywalled nytimes.com articles."""

import unittest
from unittest import mock

from news_fetcher import nyt_api, scraper

NYT_URL = "https://www.nytimes.com/2026/10/21/us/politics/example.html"


class _FakeResponse:
    def __init__(self, status_code=200, docs=None):
        self.status_code = status_code
        self._docs = docs or []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {"response": {"docs": self._docs}}


def _doc(url=NYT_URL, **fields):
    return {"web_url": url, **fields}


class NytApiTests(unittest.TestCase):
    def setUp(self):
        nyt_api._cache.clear()
        nyt_api._state.update(last_call=0.0, day=None, calls_today=0, filter_field=None,
                              rejected_key=None)
        self.settings = {"NYT_API_KEY": "test-key"}
        self._patchers = [
            mock.patch.object(nyt_api, "_setting",
                              side_effect=lambda name, default="": self.settings.get(name, default)),
            mock.patch.object(nyt_api, "NYT_API_MIN_INTERVAL_SECONDS", 0),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()

    def test_no_key_means_no_request(self):
        self.settings["NYT_API_KEY"] = ""
        with mock.patch.object(nyt_api.requests, "get") as get:
            self.assertIsNone(nyt_api.lookup_article(NYT_URL))
        get.assert_not_called()

    def test_other_domains_are_ignored(self):
        with mock.patch.object(nyt_api.requests, "get") as get:
            self.assertIsNone(nyt_api.lookup_article("https://www.wsj.com/articles/x"))
            self.assertIsNone(nyt_api.lookup_article("https://notnytimes.com/a"))
        get.assert_not_called()

    def test_returns_abstract_and_lead_paragraph_without_repeats(self):
        doc = _doc(abstract="The abstract.", lead_paragraph="The opening paragraph.",
                   snippet="The abstract.")
        with mock.patch.object(nyt_api.requests, "get", return_value=_FakeResponse(docs=[doc])):
            result = nyt_api.lookup_article(NYT_URL + "?smid=share")
        self.assertEqual(result, ["The abstract.", "The opening paragraph."])

    def test_falls_back_to_web_url_filter_and_remembers_it(self):
        responses = [_FakeResponse(status_code=400), _FakeResponse(docs=[_doc(abstract="A.")])]
        with mock.patch.object(nyt_api.requests, "get", side_effect=responses) as get:
            self.assertEqual(nyt_api.lookup_article(NYT_URL), ["A."])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(nyt_api._state["filter_field"], "web_url")

    def test_result_is_cached(self):
        with mock.patch.object(nyt_api.requests, "get",
                               return_value=_FakeResponse(docs=[_doc(abstract="A.")])) as get:
            nyt_api.lookup_article(NYT_URL)
            nyt_api.lookup_article(NYT_URL)
        get.assert_called_once()

    def test_daily_budget_stops_requests_and_is_not_cached(self):
        self.settings["NYT_API_DAILY_LIMIT"] = "0"
        with mock.patch.object(nyt_api.requests, "get") as get:
            self.assertIsNone(nyt_api.lookup_article(NYT_URL))
        get.assert_not_called()
        self.assertNotIn(NYT_URL, nyt_api._cache)

    def test_rejected_key_stops_further_requests(self):
        with mock.patch.object(nyt_api.requests, "get",
                               return_value=_FakeResponse(status_code=401)) as get:
            self.assertIsNone(nyt_api.lookup_article(NYT_URL))
            self.assertIsNone(nyt_api.lookup_article(NYT_URL.replace("example", "other")))
        get.assert_called_once()

    def test_saving_a_new_key_lifts_the_rejection(self):
        with mock.patch.object(nyt_api.requests, "get",
                               return_value=_FakeResponse(status_code=403)):
            nyt_api.lookup_article(NYT_URL)
        self.assertFalse(nyt_api.enabled())
        self.settings["NYT_API_KEY"] = "new-key"
        with mock.patch.object(nyt_api.requests, "get",
                               return_value=_FakeResponse(docs=[_doc(abstract="A.")])) as get:
            self.assertEqual(nyt_api.lookup_article(NYT_URL), ["A."])
        self.assertEqual(get.call_args.kwargs["params"]["api-key"], "new-key")

    def test_without_the_key_store_the_env_var_is_used(self):
        self._patchers[0].stop()
        try:
            with mock.patch.dict("sys.modules", {"news_fetcher.saved_settings": None}), \
                    mock.patch.dict("os.environ", {"NYT_API_KEY": "env-key"}):
                self.assertEqual(nyt_api._api_key(), "env-key")
        finally:
            self._patchers[0].start()


class ScrapeArticleNytFallbackTests(unittest.TestCase):
    def _publisher(self, status, content=None):
        return mock.patch.object(
            scraper, "_scrape_publisher",
            return_value=scraper.ScrapeResult(content=content, status=status, method="bs4"),
        )

    def test_blocked_nyt_article_gets_api_text(self):
        with self._publisher(scraper.SCRAPE_STATUS_BLOCKED), \
                mock.patch.object(nyt_api, "lookup_article",
                                  return_value=["The abstract.", "The opening paragraph."]):
            result = scraper.scrape_article(NYT_URL)
        self.assertEqual(result.method, "nyt_api")
        self.assertEqual(result.status, scraper.SCRAPE_STATUS_FALLBACK)
        self.assertIn("The opening paragraph.", result.content)

    def test_longer_publisher_fallback_is_kept(self):
        long_text = "<div><p>" + "word " * 100 + "</p></div>"
        with self._publisher(scraper.SCRAPE_STATUS_FALLBACK, long_text), \
                mock.patch.object(nyt_api, "lookup_article", return_value=["Short."]):
            result = scraper.scrape_article(NYT_URL)
        self.assertEqual(result.method, "bs4")

    def test_successful_scrape_never_calls_api(self):
        with self._publisher(scraper.SCRAPE_STATUS_SUCCESS, "<p>full</p>"), \
                mock.patch.object(nyt_api, "lookup_article") as lookup:
            scraper.scrape_article(NYT_URL)
        lookup.assert_not_called()

    def test_other_outlets_never_call_api(self):
        with self._publisher(scraper.SCRAPE_STATUS_BLOCKED), \
                mock.patch.object(nyt_api, "lookup_article") as lookup:
            scraper.scrape_article("https://www.wsj.com/articles/x")
        lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
