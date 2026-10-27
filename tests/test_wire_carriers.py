"""Tests for Reuters copy read from licensed carriers (news_fetcher/wire_carriers.py),
how scrape_article() reads a carrier page, and how ingestion files it.

The page samples follow the markup seen on 2026-10-27 (Yahoo Finance's
folded body, yahoo.com/news's body blocks, The Globe and Mail's credit
line). The ingestion tests run against the scratch Postgres database named
by API_TEST_DATABASE_URL, like tests/test_nyt_api.py, and are skipped
without it.
"""

import json
import os
import unittest
from datetime import datetime
from unittest import mock

from news_fetcher import scraper, wire_carriers

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")

YAHOO_URL = "https://finance.yahoo.com/news/oil-prices-down-almost-1-232445179.html"
YAHOO_NEWS_URL = "https://www.yahoo.com/news/world/articles/top-officials-mecca-pact-countries-032019592.html"
GLOBE_URL = "https://www.theglobeandmail.com/investing/article-trading-on-brazils-stock-exchange-halted/"
REUTERS_URL = "https://www.reuters.com/business/energy/oil-prices-down-almost-1-2026-10-26/"

ABOVE = [f"Paragraph {n} above the fold, long enough to read as the story text itself." for n in range(1, 4)]
BELOW = [f"Paragraph {n} below the fold that readability used to leave out entirely." for n in range(4, 7)]


def _jsonld(provider="Reuters", author="By Siddharth Cavale"):
    return json.dumps({
        "@context": "https://schema.org", "@type": "NewsArticle",
        "author": {"@type": "Person", "name": author},
        "provider": {"@type": "Organization", "name": provider,
                     "logo": {"@type": "ImageObject", "url": "https://s.yimg.com/reuters.png"}},
        "publisher": {"@type": "Organization", "name": "Yahoo! Finance"},
    })


def yahoo_finance_page(provider="Reuters", dateline=True, logo=True):
    lead = "NEW YORK, Oct 26 (Reuters) - Brent crude futures finished lower on Monday." if dateline \
        else "NEW YORK, Oct 26 - Brent crude futures finished lower on Monday."
    above = "".join(f'<p class="text-block paragraph yf-1abc">{p}</p>' for p in [lead] + ABOVE)
    below = "".join(f'<p class="text-block paragraph yf-1abc">{p}</p>' for p in BELOW)
    return f"""<html><head><script type="application/ld+json">{_jsonld(provider)}</script></head><body>
      {'<img class="provider-logo" alt="Reuters" src="r.png">' if logo else ''}
      <article data-testid="article-content-wrapper">
        <div class="body yf-x" data-testid="article-body">
          <div class="bodyItems-wrapper">
            <p class="text-block paragraph">By Siddharth Cavale</p>{above}
            <div data-testid="inarticle-ad"><p>Advertisement</p></div>
            <div id="sda-inbody"><p>Sponsored</p></div>
          </div>
          <div class="readmore"><button>Story Continues</button><p>Story continues</p></div>
          <div class="read-more-wrapper" data-testid="read-more" style="display: none">{below}
            <p>(Reporting by Jane Doe; Editing by John Roe)</p></div>
          <div class="view-cmts" data-testid="view-comments"><button>View comments</button></div>
        </div>
      </article>
      <section><p>Recommended Stories</p></section></body></html>"""


def yahoo_news_page():
    paragraphs = ["KARACHI/CAIRO, Oct 26 (Reuters) - Top officials met in Tehran on Monday."] + ABOVE + BELOW
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return f"""<html><body><img class="dark:hidden" alt="Reuters"><img class="dark:block" alt="Reuters">
      <div data-article-body="true" data-body-type="bodyblocks" class="text-lg">
        <p>By Jane​ Doe</p>{body}</div></body></html>"""


def globe_page(credit="Reuters"):
    paragraphs = ["Trading on Brazil's stock exchange was halted on Monday after a 15% fall."] + ABOVE + BELOW
    return f"""<html><head><script type="application/ld+json">
      {json.dumps({"@type": "NewsArticle", "author": [{"@type": "Person", "name": "Jane Doe"}]})}
      </script></head><body>
      <div class="c-article-meta__bylines">Jane Doe</div>
      <div class="c-article-meta__creditlines"><span class="c-creditline">{credit}</span></div>
      <article>{"".join(f"<p>{p}</p>" for p in paragraphs)}</article></body></html>"""


class CarrierNameTests(unittest.TestCase):
    def test_most_specific_domain_wins(self):
        self.assertEqual(wire_carriers.carrier_name(YAHOO_URL), "Yahoo Finance")
        self.assertEqual(wire_carriers.carrier_name(YAHOO_NEWS_URL), "Yahoo News")
        self.assertEqual(wire_carriers.carrier_name("https://kfgo.com/2026/10/26/x/"), "KFGO")
        self.assertEqual(wire_carriers.carrier_name(GLOBE_URL), "The Globe and Mail")

    def test_other_sites_are_not_carriers(self):
        for url in (REUTERS_URL, "https://www.straitstimes.com/world/x", "https://www.msn.com/en-us/news/x", None):
            self.assertIsNone(wire_carriers.carrier_name(url))

    def test_carrier_feeds_are_known(self):
        for url, _label in wire_carriers.CARRIER_FEEDS:
            self.assertTrue(wire_carriers.is_carrier_feed(url))
        self.assertFalse(wire_carriers.is_carrier_feed(
            "https://news.google.com/rss/search?q=site:reuters.com+when:1d&hl=en-US&gl=US&ceid=US:en"))


class CreditTests(unittest.TestCase):
    def test_dateline_at_the_top_credits_reuters(self):
        content = "<div><p>LONDON, Oct 26 (Reuters) - Something happened.</p></div>"
        self.assertTrue(wire_carriers.credits_wire(None, content))

    def test_reuters_quoted_further_down_does_not(self):
        content = "<div>" + "<p>Staff story text that goes on for a while.</p>" * 12 + \
            "<p>LONDON, Oct 26 (Reuters) - quoted.</p></div>"
        self.assertFalse(wire_carriers.credits_wire("<html></html>", content))

    def test_jsonld_provider_credits_reuters(self):
        page = yahoo_finance_page(dateline=False, logo=False)
        self.assertTrue(wire_carriers.credits_wire(page, "<p>No dateline.</p>"))

    def test_yahoo_provider_logo_credits_reuters(self):
        page = yahoo_finance_page(provider="Bloomberg", dateline=False, logo=True)
        self.assertTrue(wire_carriers.credits_wire(page, "<p>No dateline.</p>"))

    def test_other_provider_does_not(self):
        page = yahoo_finance_page(provider="Bloomberg", dateline=False, logo=False)
        self.assertFalse(wire_carriers.credits_wire(page, "<p>No dateline.</p>"))

    def test_globe_credit_line(self):
        self.assertTrue(wire_carriers.credits_wire(globe_page("Reuters"), "<p>Text.</p>"))
        self.assertFalse(wire_carriers.credits_wire(globe_page("The Globe and Mail"), "<p>Text.</p>"))


class ExtractBodyTests(unittest.TestCase):
    def test_yahoo_finance_keeps_the_text_below_the_fold(self):
        html = wire_carriers.extract_body(yahoo_finance_page(), YAHOO_URL)
        for paragraph in ABOVE + BELOW:
            self.assertIn(paragraph, html)
        self.assertIn("(Reporting by Jane Doe; Editing by John Roe)", html)
        self.assertTrue(html.startswith("<div><p>NEW YORK, Oct 26 (Reuters)"), html[:80])
        for junk in ("By Siddharth Cavale", "Advertisement", "Sponsored", "Story continues",
                     "View comments", "Recommended Stories"):
            self.assertNotIn(junk, html)

    def test_yahoo_news_body_blocks(self):
        html = wire_carriers.extract_body(yahoo_news_page(), YAHOO_NEWS_URL)
        self.assertTrue(html.startswith("<div><p>KARACHI/CAIRO"), html[:80])
        self.assertIn(BELOW[-1], html)
        self.assertNotIn("​", html)

    def test_other_carriers_use_the_regular_extraction(self):
        self.assertIsNone(wire_carriers.extract_body(globe_page(), GLOBE_URL))
        self.assertIsNone(wire_carriers.extract_body(yahoo_finance_page(), REUTERS_URL))

    def test_page_without_the_body_falls_through(self):
        self.assertIsNone(wire_carriers.extract_body("<html><p>Nothing here.</p></html>", YAHOO_URL))


class _Response:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class ScrapeTests(unittest.TestCase):
    def _scrape(self, url, page):
        with mock.patch.object(scraper, "is_domain_blocked", return_value=False), \
                mock.patch.object(scraper, "get_retry_deferral", return_value=(None, None)), \
                mock.patch.object(scraper, "update_retry_cache"), \
                mock.patch.object(scraper, "add_to_blocklist"), \
                mock.patch.object(scraper, "_fetch_html", return_value=_Response(page)):
            return scraper.scrape_article(url)

    def test_yahoo_page_is_read_whole_and_credited(self):
        result = self._scrape(YAHOO_URL, yahoo_finance_page())
        self.assertEqual(result.status, scraper.SCRAPE_STATUS_SUCCESS)
        self.assertEqual(result.method, "bs4_carrier")
        self.assertIn(BELOW[-1], result.content)
        self.assertTrue(result.credits_wire)

    def test_globe_staff_story_is_not_credited(self):
        result = self._scrape(GLOBE_URL, globe_page("The Globe and Mail"))
        self.assertTrue(result.content)
        self.assertFalse(result.credits_wire)

    def test_other_sites_leave_credit_unset(self):
        result = self._scrape("https://www.bbc.com/news/articles/x", globe_page())
        self.assertIsNone(result.credits_wire)


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the ingestion tests")
class IngestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {"DATABASE_URL": TEST_DB, "SECRET_KEY": "test-secret"}
        with mock.patch.dict(os.environ, env):
            from aggregator import create_app, db
            cls.app = create_app()
            with cls.app.app_context():
                db.drop_all()
                db.create_all()
            from news_fetcher.fetch_and_store_articles import ingestion
        cls.ingestion = ingestion

    def setUp(self):
        from aggregator import db
        self.ctx = self.app.app_context()
        self.ctx.push()
        for table in ("article_topics", "story_topics", "articles", "stories", "outlets"):
            db.session.execute(db.text(f"DELETE FROM {table}"))
        db.session.commit()
        self._patchers = [
            mock.patch.object(self.ingestion, "get_embedding", return_value=[0.1] * 768),
            mock.patch.object(self.ingestion, "classify_article", return_value=[]),
            mock.patch.object(self.ingestion, "get_allsides_score", return_value=3),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self):
        from aggregator import db
        for patcher in reversed(self._patchers):
            patcher.stop()
        db.session.remove()
        self.ctx.pop()

    def _scrape(self, page_for_url):
        def scrape(url, fallback_content=None, force=False):
            page = page_for_url.get(url)
            if page is None:
                return scraper.ScrapeResult(content=None, status=scraper.SCRAPE_STATUS_BLOCKED,
                                            method="blocklist", failure_reason="domain_blocked")
            content = wire_carriers.extract_body(page, url) or "<div><p>" + "Text. " * 120 + "</p></div>"
            credited = wire_carriers.credits_wire(page, content) if wire_carriers.carrier_name(url) else None
            return scraper.ScrapeResult(content=content, status=scraper.SCRAPE_STATUS_SUCCESS,
                                        method="bs4_carrier", http_status=200, credits_wire=credited)
        return mock.patch.object(self.ingestion, "scrape_article", side_effect=scrape)

    def _entry(self, url, source_name, title="Oil prices down almost 1%", **extra):
        return {"title": title, "content": "", "url": url, "source_name": source_name,
                "published_at": datetime.utcnow(), "image_url": None, **extra}

    def test_carrier_copy_fills_the_stored_reuters_headline(self):
        from aggregator.models import Article
        with self._scrape({YAHOO_URL: yahoo_finance_page()}):
            self.ingestion.store_articles([self._entry(REUTERS_URL, "Reuters")], "Global News")
            metrics = self.ingestion.store_articles(
                [self._entry(YAHOO_URL, "Yahoo Finance", wire_only=True)], "Global News")
        self.assertEqual(metrics["skipped"]["wire_text_filled"], 1)
        articles = Article.query.all()
        self.assertEqual(len(articles), 1)
        article = articles[0]
        self.assertEqual(article.url, REUTERS_URL)
        self.assertEqual(article.source, "Reuters")
        self.assertIn(BELOW[-1], article.content)
        self.assertEqual((article.text_via, article.text_via_url), ("Yahoo Finance", YAHOO_URL))
        self.assertEqual(article.scrape_status, scraper.SCRAPE_STATUS_SUCCESS)

    def test_carrier_copy_without_a_headline_is_stored_as_reuters(self):
        from aggregator.models import Article
        with self._scrape({YAHOO_URL: yahoo_finance_page()}):
            self.ingestion.store_articles([self._entry(YAHOO_URL, "Yahoo Finance", wire_only=True)], "Global News")
        article = Article.query.one()
        self.assertEqual(article.source, "Reuters")
        self.assertEqual(article.url, YAHOO_URL)
        self.assertEqual(article.text_via, "Yahoo Finance")
        self.assertEqual(article.outlet.name, "Reuters")

    def test_uncredited_page_from_a_carrier_feed_is_dropped(self):
        from aggregator.models import Article
        with self._scrape({GLOBE_URL: globe_page("The Globe and Mail")}):
            metrics = self.ingestion.store_articles(
                [self._entry(GLOBE_URL, "The Globe and Mail", wire_only=True)], "Global News")
        self.assertEqual(metrics["skipped"]["not_wire_copy"], 1)
        self.assertEqual(Article.query.count(), 0)

    def test_uncredited_carrier_page_from_another_feed_is_kept_as_is(self):
        from aggregator.models import Article
        with self._scrape({GLOBE_URL: globe_page("The Globe and Mail")}):
            self.ingestion.store_articles([self._entry(GLOBE_URL, "The Globe and Mail")], "Global News")
        article = Article.query.one()
        self.assertEqual(article.source, "The Globe and Mail")
        self.assertIsNone(article.text_via)


if __name__ == "__main__":
    unittest.main()
