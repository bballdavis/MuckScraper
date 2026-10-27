"""Tests for the NYT APIs (news_fetcher/nyt_api.py) and how scrape_article()
uses them for paywalled nytimes.com articles.

The request and parsing checks fake the request ledger. The ledger, wire
and ingestion tests run against the scratch Postgres database named by
API_TEST_DATABASE_URL, like tests/test_breaking.py, and are skipped without
it. Its tables are dropped and recreated.
"""

import itertools
import os
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

import dataclasses

from news_fetcher import api_budget, nyt_api, scraper

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")
NYT_URL = "https://www.nytimes.com/2026/10/21/us/politics/example.html"


class _FakeResponse:
    def __init__(self, status_code=200, docs=None, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {"response": {"docs": docs or []}}

    def json(self):
        return self._body


def _doc(url=NYT_URL, **fields):
    return {"web_url": url, **fields}


def _wire_item(n, section="U.S.", published=None, **fields):
    return {"url": f"https://www.nytimes.com/2026/10/27/us/story-{n}.html", "title": f"Story {n}",
            "abstract": f"Abstract {n}.", "section": section, "subsection": "", "item_type": "Article",
            "byline": "By A Reporter", "published_date": published or "2026-10-27T08:00:00-04:00",
            "multimedia": [{"url": f"https://static01.nyt.com/{n}-small.jpg", "width": 75, "height": 75},
                           {"url": f"https://static01.nyt.com/{n}-large.jpg", "width": 2048, "height": 1365}],
            **fields}


class _Base(unittest.TestCase):
    def setUp(self):
        nyt_api._cache.clear()
        nyt_api._state.update(filter_field=None, wire_page_size=None)
        api_budget._rejected.clear()
        self.settings = {"NYT_API_KEY": "test-key"}
        self.calls = []
        self._patchers = [
            mock.patch.object(api_budget, "_setting",
                              side_effect=lambda name, default="": self.settings.get(name, default)),
            mock.patch.object(nyt_api, "PROVIDER", dataclasses.replace(nyt_api.PROVIDER, min_interval_seconds=0)),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self._patchers):
            patcher.stop()


class NytApiTests(_Base):
    """Requests and parsing, with an in-memory ledger."""

    def setUp(self):
        super().setUp()
        ids = itertools.count(1)

        def take(p, kind):
            if len(self.calls) >= nyt_api.daily_limit() - nyt_api.RESERVES.get(kind, 0):
                return None
            self.calls.append(kind)
            return next(ids)

        for patcher in (mock.patch.object(api_budget, "take_slot", side_effect=take),
                        mock.patch.object(api_budget, "record_result"),
                        mock.patch.object(nyt_api, "_stored_paragraphs", return_value=None)):
            patcher.start()
            self._patchers.append(patcher)

    def test_no_key_means_no_request(self):
        self.settings["NYT_API_KEY"] = ""
        with mock.patch.object(api_budget.requests, "get") as get:
            self.assertIsNone(nyt_api.lookup_article(NYT_URL))
        get.assert_not_called()

    def test_other_domains_are_ignored(self):
        with mock.patch.object(api_budget.requests, "get") as get:
            self.assertIsNone(nyt_api.lookup_article("https://www.wsj.com/articles/x"))
            self.assertIsNone(nyt_api.lookup_article("https://notnytimes.com/a"))
        get.assert_not_called()

    def test_returns_abstract_and_lead_paragraph_without_repeats(self):
        doc = _doc(abstract="The abstract.", lead_paragraph="The opening paragraph.",
                   snippet="The abstract.")
        with mock.patch.object(api_budget.requests, "get", return_value=_FakeResponse(docs=[doc])):
            result = nyt_api.lookup_article(NYT_URL + "?smid=share")
        self.assertEqual(result, ["The abstract.", "The opening paragraph."])
        self.assertEqual(self.calls, ["lookup"])

    def test_a_stored_wire_item_answers_without_a_request(self):
        with mock.patch.object(nyt_api, "_stored_paragraphs", return_value=["A.", "Lead."]), \
                mock.patch.object(api_budget.requests, "get") as get:
            self.assertEqual(nyt_api.lookup_article(NYT_URL), ["A.", "Lead."])
        get.assert_not_called()

    def test_falls_back_to_web_url_filter_and_remembers_it(self):
        responses = [_FakeResponse(status_code=400), _FakeResponse(docs=[_doc(abstract="A.")])]
        with mock.patch.object(api_budget.requests, "get", side_effect=responses) as get:
            self.assertEqual(nyt_api.lookup_article(NYT_URL), ["A."])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(nyt_api._state["filter_field"], "web_url")

    def test_result_is_cached(self):
        with mock.patch.object(api_budget.requests, "get",
                               return_value=_FakeResponse(docs=[_doc(abstract="A.")])) as get:
            nyt_api.lookup_article(NYT_URL)
            nyt_api.lookup_article(NYT_URL)
        get.assert_called_once()

    def test_spent_budget_stops_requests_and_is_not_cached(self):
        self.settings["NYT_API_DAILY_LIMIT"] = "0"
        with mock.patch.object(api_budget.requests, "get") as get:
            self.assertIsNone(nyt_api.lookup_article(NYT_URL))
        get.assert_not_called()
        self.assertNotIn(NYT_URL, nyt_api._cache)

    def test_limit_setting_never_goes_past_the_keys_own(self):
        self.settings["NYT_API_DAILY_LIMIT"] = "5000"
        self.assertEqual(nyt_api.daily_limit(), 500)
        self.settings["NYT_API_DAILY_LIMIT"] = "300"
        self.assertEqual(nyt_api.daily_limit(), 300)

    def test_rejected_key_stops_further_requests(self):
        with mock.patch.object(api_budget.requests, "get",
                               return_value=_FakeResponse(status_code=401)) as get:
            self.assertIsNone(nyt_api.lookup_article(NYT_URL))
            self.assertIsNone(nyt_api.lookup_article(NYT_URL.replace("example", "other")))
        get.assert_called_once()

    def test_saving_a_new_key_lifts_the_rejection(self):
        with mock.patch.object(api_budget.requests, "get",
                               return_value=_FakeResponse(status_code=403)):
            nyt_api.lookup_article(NYT_URL)
        self.assertFalse(nyt_api.enabled())
        self.settings["NYT_API_KEY"] = "new-key"
        with mock.patch.object(api_budget.requests, "get",
                               return_value=_FakeResponse(docs=[_doc(abstract="A.")])) as get:
            self.assertEqual(nyt_api.lookup_article(NYT_URL), ["A."])
        self.assertEqual(get.call_args.kwargs["params"]["api-key"], "new-key")

    def test_errors_never_carry_the_key(self):
        error = api_budget.requests.ConnectionError(f"Max retries exceeded with url: /x?api-key=test-key")
        with mock.patch.object(api_budget.requests, "get", side_effect=error), \
                self.assertRaises(RuntimeError) as raised:
            nyt_api._get("lookup", nyt_api.ARTICLE_SEARCH_URL, {}, "test-key", len)
        self.assertNotIn("test-key", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        with mock.patch.object(api_budget.requests, "get", return_value=_FakeResponse(status_code=500)), \
                self.assertRaises(RuntimeError) as raised:
            nyt_api._get("lookup", nyt_api.ARTICLE_SEARCH_URL, {}, "test-key", len)
        self.assertNotIn("test-key", str(raised.exception))

    def test_ten_urls_share_one_search(self):
        urls = [f"https://www.nytimes.com/2026/10/27/us/s{n}.html" for n in range(10)]
        docs = [_doc(u, abstract=f"A{n}.", lead_paragraph=f"L{n}.") for n, u in enumerate(urls)]
        with mock.patch.object(api_budget.requests, "get", return_value=_FakeResponse(docs=docs)) as get:
            found = nyt_api._docs_for("search", urls, "test-key")
        get.assert_called_once()
        self.assertEqual(len(found), 10)
        self.assertIn('"' + urls[9] + '"', get.call_args.kwargs["params"]["fq"])

    def test_reserves_keep_room_for_the_other_kinds(self):
        self.settings["NYT_API_DAILY_LIMIT"] = "200"
        self.calls.extend(["wire"] * 50)  # 150 left, the search reserve
        self.assertIsNone(api_budget.take_slot(nyt_api.PROVIDER, "search"))
        self.assertIsNotNone(api_budget.take_slot(nyt_api.PROVIDER, "wire"))
        self.calls.extend(["wire"] * 124)  # 25 left, the wire reserve
        self.assertIsNone(api_budget.take_slot(nyt_api.PROVIDER, "wire"))
        self.assertIsNotNone(api_budget.take_slot(nyt_api.PROVIDER, "lookup"))

    def test_wire_row_parsing(self):
        now = datetime(2026, 10, 27, 13, 0)
        row = nyt_api._wire_row(_wire_item(1, url="https://www.nytimes.com/2026/10/27/us/story-1.html?smid=x"), now)
        self.assertEqual(row["url"], "https://www.nytimes.com/2026/10/27/us/story-1.html")
        self.assertEqual(row["published_at"], datetime(2026, 10, 27, 12, 0))
        self.assertTrue(row["image_url"].endswith("1-large.jpg"))
        self.assertIsNone(nyt_api._wire_row(_wire_item(2, url="https://example.com/x"), now))
        self.assertTrue(nyt_api.is_news_item(row))
        self.assertFalse(nyt_api.is_news_item({**row, "section": "Crosswords & Games"}))
        self.assertFalse(nyt_api.is_news_item({**row, "section": "Opinion"}))
        self.assertFalse(nyt_api.is_news_item({**row, "item_type": "Video"}))

    def test_schedule_is_busier_at_the_news_peaks(self):
        # 12:00 UTC is 8am in New York on 2026-10-27 (EDT).
        at = lambda hour: datetime(2026, 10, 27, hour, 0)  # noqa: E731
        self.assertEqual(nyt_api.wire_interval(at(12)), 3)   # 8am
        self.assertEqual(nyt_api.wire_interval(at(17)), 6)   # 1pm
        self.assertEqual(nyt_api.wire_interval(at(22)), 3)   # 6pm
        self.assertEqual(nyt_api.wire_interval(at(7)), 20)   # 3am

    def test_planned_wire_polls_fit_the_budget(self):
        start = datetime(2026, 10, 27, 4, 0)  # local midnight
        polls, t = 0, start
        while t < start + timedelta(days=1):
            polls += 1
            t += timedelta(minutes=nyt_api.wire_interval(t))
        self.assertLess(polls, nyt_api.DAILY_CAP - nyt_api.RESERVES["search"])


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


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the NYT ledger and wire tests")
class NytDatabaseTests(_Base):
    @classmethod
    def setUpClass(cls):
        env = {"DATABASE_URL": TEST_DB, "SECRET_KEY": "test-secret"}
        with mock.patch.dict(os.environ, env):
            from aggregator import create_app, db
            cls.app = create_app()
        with cls.app.app_context():
            db.drop_all()
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        # The app reads saved settings from this database; later tests expect
        # the environment alone, already applied.
        from news_fetcher import llm_client, saved_settings
        saved_settings.set_source(None)
        llm_client._sync_config()

    def setUp(self):
        super().setUp()
        from aggregator import db
        self.ctx = self.app.app_context()
        self.ctx.push()
        for table in ("source_api_calls", "nyt_wire_items", "feed_entries", "app_settings"):
            db.session.execute(db.text(f"DELETE FROM {table}"))
        db.session.commit()

    def tearDown(self):
        from aggregator import db
        db.session.remove()
        self.ctx.pop()
        super().tearDown()

    def test_ledger_counts_the_last_24_hours_across_restarts(self):
        from aggregator import db
        from aggregator.models import SourceApiCall
        now = datetime.utcnow()
        db.session.add_all([SourceApiCall(provider="nyt", kind="wire", called_at=now - timedelta(hours=25))] +
                           [SourceApiCall(provider="nyt", kind="wire", called_at=now - timedelta(hours=1)) for _ in range(3)])
        db.session.commit()
        self.settings["NYT_API_DAILY_LIMIT"] = "4"
        self.assertEqual(nyt_api.window_usage()[0], 3)
        self.assertIsNotNone(api_budget.take_slot(nyt_api.PROVIDER, "lookup"))
        self.assertIsNone(api_budget.take_slot(nyt_api.PROVIDER, "lookup"))
        self.assertEqual(SourceApiCall.query.count(), 5)

    def test_a_429_pauses_every_kind(self):
        from aggregator import db
        from aggregator.models import SourceApiCall
        db.session.add(SourceApiCall(provider="nyt", kind="wire", called_at=datetime.utcnow(), http_status=429))
        db.session.commit()
        self.assertIsNone(api_budget.take_slot(nyt_api.PROVIDER, "lookup"))

    def test_wire_poll_stores_new_items_and_breaking_headlines(self):
        from aggregator.models import FeedEntry, SourceApiCall, NytWireItem
        now = datetime(2026, 10, 27, 13, 0)
        page = [_wire_item(n) for n in range(3)] + [_wire_item(9, section="Food")]
        body = {"num_results": 4, "results": page}
        with mock.patch.object(api_budget.requests, "get", return_value=_FakeResponse(body=body)) as get:
            metrics = nyt_api.poll_wire(now)
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs["params"]["limit"], nyt_api.WIRE_PAGE_SIZE)
        self.assertEqual(metrics["new"], 4)
        self.assertEqual(NytWireItem.query.count(), 4)
        self.assertEqual(FeedEntry.query.filter_by(source_name="New York Times").count(), 3)
        self.assertEqual(SourceApiCall.query.one().items, 4)
        # Polled again with nothing new: one request, nothing added.
        with mock.patch.object(api_budget.requests, "get", return_value=_FakeResponse(body=body)):
            self.assertEqual(nyt_api.poll_wire(now)["new"], 0)

    def test_wire_pages_back_only_while_every_item_is_new(self):
        size = nyt_api.WIRE_FALLBACK_PAGE_SIZE
        nyt_api._state["wire_page_size"] = size
        pages = [{"results": [_wire_item(p * size + n) for n in range(size)]} for p in range(3)]
        with mock.patch.object(api_budget.requests, "get",
                               side_effect=[_FakeResponse(body=b) for b in pages]) as get:
            nyt_api.poll_wire(datetime(2026, 10, 27, 13, 0))
        self.assertEqual(get.call_count, nyt_api.WIRE_MAX_PAGES)
        self.assertEqual([c.kwargs["params"]["offset"] for c in get.call_args_list], [0, size, 2 * size])

    def test_a_refused_page_size_falls_back(self):
        responses = [_FakeResponse(status_code=400), _FakeResponse(body={"results": [_wire_item(1)]})]
        with mock.patch.object(api_budget.requests, "get", side_effect=responses) as get:
            nyt_api.poll_wire(datetime(2026, 10, 27, 13, 0))
        self.assertEqual(get.call_args.kwargs["params"]["limit"], nyt_api.WIRE_FALLBACK_PAGE_SIZE)

    def test_poll_waits_for_its_interval(self):
        from aggregator import db
        from aggregator.models import SourceApiCall
        now = datetime(2026, 10, 27, 12, 0)  # 8am in New York: every 3 minutes
        db.session.add(SourceApiCall(provider="nyt", kind="wire", called_at=now - timedelta(minutes=1)))
        db.session.commit()
        with mock.patch.object(nyt_api, "poll_wire") as poll, \
                mock.patch.object(api_budget, "window_usage", return_value=(1, None, None)):
            self.assertIsNone(nyt_api.poll_wire_if_due(now))
            poll.assert_not_called()
            nyt_api.poll_wire_if_due(now + timedelta(minutes=2, seconds=40))
            poll.assert_called_once()

    def test_lead_paragraphs_come_ten_to_a_request(self):
        from aggregator import db
        from aggregator.models import NytWireItem
        now = datetime.utcnow()
        rows = [nyt_api._wire_row(_wire_item(n, published=(now - timedelta(hours=1)).isoformat() + "Z"), now)
                for n in range(12)]
        db.session.add_all(NytWireItem(**r) for r in rows)
        db.session.commit()
        docs = lambda urls: [_doc(u, lead_paragraph=f"Lead for {u[-12:]}") for u in urls]  # noqa: E731

        def get(url, params, timeout, **_):
            urls = [u.strip('"') for u in params["fq"].split("(", 1)[1].rstrip(")").split('" "')]
            return _FakeResponse(docs=docs(urls))

        with mock.patch.object(api_budget.requests, "get", side_effect=get) as fake:
            self.assertEqual(nyt_api.search_lead_paragraphs(now), 12)
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(NytWireItem.query.filter(NytWireItem.lead_paragraph.isnot(None)).count(), 12)
        self.assertEqual(nyt_api.lookup_article(rows[0]["url"])[1], f"Lead for {rows[0]['url'][-12:]}")

    def test_no_lead_paragraphs_pauses_the_searches(self):
        from aggregator import db
        from aggregator.models import NytWireItem
        now = datetime.utcnow()
        rows = [nyt_api._wire_row(_wire_item(n, published=(now - timedelta(hours=1)).isoformat() + "Z"), now)
                for n in range(12)]
        db.session.add_all(NytWireItem(**r) for r in rows)
        db.session.commit()
        bare = _FakeResponse(docs=[_doc(r["url"], abstract="A.") for r in rows[:10]])
        with mock.patch.object(api_budget.requests, "get", return_value=bare) as fake:
            nyt_api.search_lead_paragraphs(now)
            nyt_api.search_lead_paragraphs(now)
        fake.assert_called_once()

    def test_ingest_stores_news_items_once(self):
        from aggregator import db
        from aggregator.models import NytWireItem
        now = datetime.utcnow()
        recent = (now - timedelta(hours=2)).isoformat() + "Z"
        rows = [nyt_api._wire_row(_wire_item(n, published=recent), now) for n in range(2)]
        rows.append(nyt_api._wire_row(_wire_item(5, section="Style", published=recent), now))
        rows.append(nyt_api._wire_row(_wire_item(6, published=(now - timedelta(days=3)).isoformat() + "Z"), now))
        db.session.add_all(NytWireItem(**r) for r in rows)
        db.session.commit()
        stored = []
        # A stand-in for the ingestion package, which makes its own app at import.
        ingestion = types.SimpleNamespace(
            store_articles=lambda a, t, provider: stored.extend(a) or {"input_articles": len(a)})
        with mock.patch.object(nyt_api, "search_lead_paragraphs", return_value=0), \
                mock.patch.dict("sys.modules", {"news_fetcher.fetch_and_store_articles": ingestion}):
            nyt_api.ingest_wire_items(now)
            nyt_api.ingest_wire_items(now)
        self.assertEqual(sorted(a["title"] for a in stored), ["Story 0", "Story 1"])
        self.assertEqual(stored[0]["source_name"], "New York Times")


if __name__ == "__main__":
    unittest.main()
