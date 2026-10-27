"""Sections, Sources and the Blocklist (Settings › Sources group): the source
model in aggregator/sources.py, feed health, block hit counts, ingestion's use
of a source's sections, the classifier's section list, and the pages.

The DB tests need the scratch Postgres database named by
API_TEST_DATABASE_URL, like test_api; they are skipped without it.
"""
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


class ClassifierSectionTests(unittest.TestCase):
    def test_categories_list_carries_descriptions_and_leaves_out_other(self):
        from news_fetcher.topic_classifier import categories_list
        text = categories_list(["Sports", "Health", "Other"], {"Sports": "Games and leagues", "Health": ""})
        self.assertEqual(text, "- Sports: Games and leagues\n- Health")

    def test_match_reads_the_name_before_a_colon(self):
        from news_fetcher.topic_classifier import match_topics
        valid = ["US Politics", "International News", "Other"]
        # An echoed description mentioning "another country" is not Other.
        answer = "US Politics: any US government action toward another country\nInternational News"
        self.assertEqual(match_topics(answer, valid), ["US Politics", "International News"])
        self.assertEqual(match_topics("- sports\n", ["Sports"]), ["Sports"])
        self.assertEqual(match_topics("Nothing here", ["Sports"]), [])


@unittest.skipUnless(TEST_DB, "API_TEST_DATABASE_URL not set")
class SourcesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from aggregator import create_app, db
        env = {"DATABASE_URL": TEST_DB, "SECRET_KEY": "test-secret"}
        with mock.patch.dict(os.environ, env):
            cls.app = create_app()
            # Builds its own app at import, from DATABASE_URL.
            import news_fetcher.fetch_and_store_articles  # noqa: F401
        cls.app.config.update(WTF_CSRF_ENABLED=False, TESTING=True)
        with cls.app.app_context():
            db.drop_all()
            db.create_all()
            from aggregator.models import User
            from aggregator.seed_defaults import seed_defaults
            seed_defaults()
            admin = User(username="admin", email="a@example.com", role="admin")
            admin.set_password("password123")
            db.session.add(admin)
            db.session.commit()
            cls.admin_id = admin.id

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        from aggregator import db
        db.session.rollback()
        db.session.remove()
        self.ctx.pop()

    def client(self):
        from flask import g
        g.pop("_login_user", None)
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.admin_id)
            session["_fresh"] = True
        return client

    # --- defaults ---------------------------------------------------------------

    def test_defaults_put_new_sections_before_other(self):
        from aggregator.models import Topic
        names = [t.name for t in Topic.query.order_by(Topic.sort_order)]
        self.assertEqual(names[-3:], ["Health", "Culture", "Other"])
        sci = Topic.query.filter_by(name="Sci/Tech").one()
        self.assertEqual(sci.label, "Science & Technology")
        self.assertTrue(sci.description)

    def test_defaults_tag_section_feeds_and_searches(self):
        from aggregator.models import IngestionBlock, ScheduledFetch
        from aggregator.sources import sections_by_feed
        by_feed = sections_by_feed()
        self.assertEqual([t.name for t in by_feed["https://feeds.bbci.co.uk/sport/rss.xml"]], ["Sports"])
        self.assertEqual([t.name for t in by_feed["https://variety.com/feed/"]], ["Culture"])
        self.assertNotIn("https://feeds.bbci.co.uk/news/rss.xml", by_feed)
        sports = ScheduledFetch.query.filter_by(label="Sports").one()
        self.assertEqual(sports.topic.name, "Sports")
        self.assertTrue(sports.known_outlets_only)
        self.assertFalse(IngestionBlock.query.filter_by(kind="title_keyword", pattern="signs with").one().is_active)
        self.assertTrue(IngestionBlock.query.filter_by(kind="title_keyword", pattern="nintendo").one().is_active)

    # --- ingestion ------------------------------------------------------------------

    def test_ingest_sections_prefers_the_feed_then_the_search(self):
        from aggregator.sources import IngestSections
        feed = IngestSections("Global News")
        self.assertEqual(feed.sections_for({"feed_url": "https://techcrunch.com/feed/"}), ["Sci/Tech"])
        self.assertEqual(feed.sections_for({"feed_url": "https://feeds.bbci.co.uk/news/rss.xml"}), [])
        self.assertFalse(feed.known_outlets_only)
        search = IngestSections("Sports")
        self.assertEqual(search.sections_for({}), ["Sports"])
        self.assertTrue(search.known_outlets_only)

    def _store(self, articles, label="Global News", classify=("US News",)):
        from news_fetcher.fetch_and_store_articles import ingestion
        from news_fetcher.scraper import ScrapeResult
        result = ScrapeResult(content=None, status="skipped", method=None, failure_reason=None, http_status=None)
        with mock.patch.object(ingestion.llm_client, "get_embeddings", side_effect=lambda texts: [None] * len(texts)), \
             mock.patch.object(ingestion, "scrape_article", return_value=result), \
             mock.patch.object(ingestion, "get_allsides_score", return_value=None), \
             mock.patch.object(ingestion, "get_outlet_bias_from_llm", return_value=None), \
             mock.patch.object(ingestion, "classify_article", return_value=list(classify)) as classify_mock:
            metrics = ingestion.store_articles(articles, label, provider="test")
        return metrics, classify_mock

    def test_a_section_feed_tags_its_articles_without_the_classifier(self):
        from aggregator.models import Article
        url = "https://www.cbssports.com/nfl/news/rovers-win/"
        metrics, classify = self._store([{
            "title": "Rovers win the cup in extra time", "content": "", "url": url,
            "source_name": "CBS Sports", "feed_url": "https://www.cbssports.com/rss/headlines/",
        }])
        self.assertEqual(metrics["stored"], 1)
        classify.assert_not_called()
        self.assertEqual([t.name for t in Article.query.filter_by(url=url).one().topics], ["Sports"])

    def test_a_general_feed_is_classified(self):
        from aggregator.models import Article
        url = "https://www.bbc.co.uk/news/articles/flood-warning"
        _, classify = self._store([{
            "title": "Flood warnings issued across the valley", "content": "", "url": url,
            "source_name": "BBC News", "feed_url": "https://feeds.bbci.co.uk/news/rss.xml",
        }])
        classify.assert_called_once()
        self.assertEqual([t.name for t in Article.query.filter_by(url=url).one().topics], ["US News"])

    def test_known_outlets_only_skips_new_outlets(self):
        from aggregator.models import Outlet
        metrics, _ = self._store([{
            "title": "Hot take on the playoffs from a site nobody knows", "content": "",
            "url": "https://fringe-sports-blog.example/take", "source_name": "Fringe Sports Blog",
        }], label="Sports")
        self.assertEqual(metrics["skipped"]["unknown_outlet"], 1)
        self.assertIsNone(Outlet.query.filter_by(name="Fringe Sports Blog").first())

    def test_ingestion_blocks_count_what_they_catch(self):
        from aggregator.blocklist import blocklist_rows
        from aggregator.models import IngestionBlock
        block = IngestionBlock.query.filter_by(kind="title_keyword", pattern="nintendo").one()
        metrics, _ = self._store([
            {"title": "Nintendo announces a new console", "content": "",
             "url": "https://games.example/nintendo-1", "source_name": "Games"},
            {"title": "Nintendo shares climb", "content": "",
             "url": "https://games.example/nintendo-2", "source_name": "Games"},
        ])
        self.assertEqual(metrics["skipped"]["blocked_title"], 2)
        row = next(r for r in blocklist_rows() if r["table"] == "ingestion" and r["id"] == block.id)
        self.assertEqual(row["caught"], 2)
        self.assertEqual(row["origin"], "seeded")

    # --- feed health -------------------------------------------------------------------

    def test_feed_health_needs_attention_after_three_bad_reads(self):
        from aggregator import db, feed_health
        from aggregator.models import RssFeed
        url = "https://www.cbc.ca/webfeed/rss/rss-topstories"
        for _ in range(2):
            feed_health.record_feed_read(url, feed_health.STATUS_EMPTY, items=0)
        feed = RssFeed.query.filter_by(url=url).first()
        self.assertEqual(feed_health.health_of(feed), "warning")
        feed_health.record_feed_read(url, feed_health.STATUS_EMPTY, items=0)
        # Written on its own connection, so reload the row.
        db.session.refresh(feed)
        self.assertEqual(feed_health.health_of(feed), "bad")
        self.assertIn("3 bad reads in a row", feed_health.health_note(feed))
        feed_health.record_feed_read(url, feed_health.STATUS_OK, items=12)
        db.session.refresh(feed)
        self.assertEqual(feed_health.health_of(feed), "good")
        self.assertEqual(feed.failures_in_row, 0)

    # --- pages --------------------------------------------------------------------------

    def test_settings_pages_render(self):
        client = self.client()
        for path, text in [("/admin/sections", "Culture &amp; Entertainment"),
                           ("/admin/sources", "News API searches"),
                           ("/admin/blocklist", "Collect, don&#39;t scrape"),
                           ("/admin/scrape-health", "Scrape health"),
                           ("/admin/scheduled-fetches", "Only outlets already stored")]:
            page = client.get(path)
            self.assertEqual(page.status_code, 200, path)
            self.assertIn(text, page.get_data(as_text=True), path)
        self.assertEqual(client.get("/admin/ingestion-blocks/keywords").status_code, 302)

    def test_sidebar_uses_display_names(self):
        page = self.client().get("/admin/sections").get_data(as_text=True)
        self.assertIn('<h2 class="nav-heading">Sections</h2>', page)
        self.assertIn('<span class="full-text">Business &amp; Finance</span>', page)

    def test_reorder_sections(self):
        from aggregator.models import Topic
        topics = Topic.query.order_by(Topic.sort_order).all()
        ids = [t.id for t in reversed(topics)]
        response = self.client().post("/admin/sections/reorder", json={"ids": ids})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([t.id for t in Topic.query.order_by(Topic.sort_order)], ids)
        self.assertEqual(self.client().post("/admin/sections/reorder", json={"ids": ids[:-1]}).status_code, 400)
        self.client().post("/admin/sections/reorder", json={"ids": [t.id for t in topics]})

    def test_editing_a_feed_sets_roles_and_sections(self):
        from aggregator.models import RssFeed, Topic
        from aggregator.sources import sections_by_feed
        url = "https://www.propublica.org/feeds/propublica/main"
        feed = RssFeed.query.filter_by(url=url).one()
        health = Topic.query.filter_by(name="Health").one()
        self.client().post(f"/admin/sources/feeds/{feed.id}/update", data={
            "label": "ProPublica", "roles": ["general", "left_enrichment"], "sections": [str(health.id)],
        })
        self.assertEqual(sorted(r.bucket for r in RssFeed.query.filter_by(url=url)), ["general", "left_enrichment"])
        self.assertTrue(all(r.enabled for r in RssFeed.query.filter_by(url=url)))
        self.assertEqual([t.name for t in sections_by_feed()[url]], ["Health"])
        self.client().post(f"/admin/sources/feeds/{feed.id}/update", data={"label": "ProPublica", "roles": ["general"]})
        self.assertEqual([r.bucket for r in RssFeed.query.filter_by(url=url)], ["general"])
        self.assertNotIn(url, sections_by_feed())

    def test_adding_rules_needs_a_reason(self):
        from aggregator.models import IngestionBlock, ScrapeBlocklist
        client = self.client()
        client.post("/admin/blocklist/add", data={"effect": "skip", "match": "title", "pattern": "Sponsored", "reason": ""})
        self.assertIsNone(IngestionBlock.query.filter_by(pattern="sponsored").first())
        client.post("/admin/blocklist/add", data={"effect": "skip", "match": "title", "pattern": "Sponsored", "reason": "Ads"})
        self.assertEqual(IngestionBlock.query.filter_by(pattern="sponsored").one().kind, "title_keyword")
        client.post("/admin/blocklist/add", data={"effect": "noscrape", "pattern": "https://www.paywalled.example/x",
                                                  "reason": "Hard paywall"})
        entry = ScrapeBlocklist.query.filter_by(domain="paywalled.example").one()
        self.assertTrue(entry.is_permanent)


if __name__ == "__main__":
    unittest.main()
