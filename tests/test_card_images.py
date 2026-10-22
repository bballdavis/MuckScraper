"""Tests for the card image fallbacks: the page's share image, outlet logos
(news_fetcher/outlet_logos.py), the API fields and the card's image box. No
network and no database."""
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from jinja2 import Environment, FileSystemLoader

from news_fetcher import outlet_logos, scraper

TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "aggregator", "templates")


class PageImageTests(unittest.TestCase):
    def test_og_image_made_absolute(self):
        html = '<head><meta property="og:image" content="/img/lead.jpg"></head>'
        self.assertEqual(scraper.extract_page_image(html, "https://www.cbsnews.com/news/a/"),
                         "https://www.cbsnews.com/img/lead.jpg")

    def test_twitter_then_json_ld(self):
        twitter = '<meta name="twitter:image" content="https://cdn.example.com/t.jpg">'
        self.assertEqual(scraper.extract_page_image(twitter, "https://example.com/a"), "https://cdn.example.com/t.jpg")
        ld = ('<script type="application/ld+json">{"@graph": [{"@type": "NewsArticle",'
              ' "image": [{"url": "https://cdn.example.com/ld.jpg"}]}]}</script>')
        self.assertEqual(scraper.extract_page_image(ld, "https://example.com/a"), "https://cdn.example.com/ld.jpg")

    def test_none_without_an_image(self):
        self.assertIsNone(scraper.extract_page_image("<p>text</p>", "https://example.com/a"))
        self.assertIsNone(scraper.extract_page_image('<meta property="og:image" content="data:x">', "https://e.com/"))

    def test_scrape_result_carries_the_share_image(self):
        page = SimpleNamespace(status_code=200, text='<meta property="og:image" content="https://e.com/i.jpg"><p>short</p>')
        with mock.patch.object(scraper, "_fetch_html", return_value=page), \
             mock.patch.object(scraper, "is_domain_blocked", return_value=False), \
             mock.patch.object(scraper, "update_retry_cache"), \
             mock.patch.object(scraper, "extract_article_html_bs4", return_value=None), \
             mock.patch.object(scraper, "build_variant_urls", return_value=[]):
            result = scraper._scrape_publisher("https://e.com/story", force=True)
        self.assertFalse(result.succeeded)
        self.assertEqual(result.image_url, "https://e.com/i.jpg")


class LogoCandidateTests(unittest.TestCase):
    HOME = """<head>
      <link rel="icon" href="/favicon-32.png" sizes="32x32">
      <link rel="icon" type="image/svg+xml" href="/logo.svg">
      <link rel="apple-touch-icon" href="/touch-120.png" sizes="120x120">
      <link rel="apple-touch-icon" href="/touch-180.png" sizes="180x180">
      <link rel="icon" href="https://cdn.cbsnews.com/icon-192.png" sizes="192x192">
      <script type="application/ld+json">{"@type": "NewsMediaOrganization",
        "logo": {"@type": "ImageObject", "url": "/brand.png"}}</script>
    </head>"""

    def test_order_and_svg_skipped(self):
        got = outlet_logos.logo_candidates(self.HOME, "https://www.cbsnews.com/")
        self.assertEqual(got, [
            "https://www.cbsnews.com/touch-180.png",
            "https://www.cbsnews.com/touch-120.png",
            "https://cdn.cbsnews.com/icon-192.png",
            "https://www.cbsnews.com/brand.png",
            "https://www.cbsnews.com/favicon-32.png",
            "https://www.cbsnews.com/apple-touch-icon.png",
            "https://www.cbsnews.com/favicon.ico",
        ])

    def test_bare_page_falls_back_to_well_known_paths(self):
        self.assertEqual(outlet_logos.logo_candidates("", "https://apnews.com/"),
                         ["https://apnews.com/apple-touch-icon.png", "https://apnews.com/favicon.ico"])

    def test_resolve_takes_the_first_real_image(self):
        def get(url, **kwargs):
            if url == "https://example.com/":
                return SimpleNamespace(status_code=200, url=url, close=lambda: None, headers={},
                                       text='<link rel="apple-touch-icon" href="/gone.png">')
            kind = "text/html" if url.endswith("gone.png") else "image/png"
            return SimpleNamespace(status_code=200, headers={"Content-Type": kind}, close=lambda: None)
        session = SimpleNamespace(get=get)
        self.assertEqual(outlet_logos.resolve_logo("https://example.com/news/x?y=1", session=session),
                         "https://example.com/apple-touch-icon.png")

    def test_blocked_site_falls_back_to_the_favicon_service(self):
        def get(url, **kwargs):
            if "google.com/s2/favicons" in url:
                return SimpleNamespace(status_code=200, headers={"Content-Type": "image/png"}, close=lambda: None)
            return SimpleNamespace(status_code=403, url=url, text="Access denied", headers={}, close=lambda: None)
        self.assertEqual(
            outlet_logos.resolve_logo("https://www.washingtonpost.com/world/x/", session=SimpleNamespace(get=get)),
            "https://www.google.com/s2/favicons?domain=washingtonpost.com&sz=128")

    def test_site_root(self):
        self.assertEqual(outlet_logos.site_root("https://www.cbsnews.com/news/a?b=1"), "https://www.cbsnews.com/")
        self.assertIsNone(outlet_logos.site_root("mailto:x@y.z"))
        self.assertIsNone(outlet_logos.site_root(None))


def _outlet(name, logo=None, id=1):
    return SimpleNamespace(id=id, name=name, logo_url=logo, bias_score=3.0)


def _article(outlet, image=None, date=datetime(2026, 10, 22), roundup=False):
    return SimpleNamespace(outlet=outlet, image_url=image, date=date, is_roundup=roundup)


class SerializerTests(unittest.TestCase):
    def test_outlet_has_logo_url(self):
        from aggregator.api import serializers as ser
        self.assertEqual(ser.outlet(_outlet("CBS News", "https://cbsnews.com/i.png"))["logo_url"], "https://cbsnews.com/i.png")
        self.assertIsNone(ser.outlet(_outlet("CBS News", "javascript:x"))["logo_url"])
        self.assertIsNone(ser.outlet(None, "CBS")["logo_url"])

    def test_story_logo_is_the_newest_outlet_with_one(self):
        from aggregator.api import serializers as ser
        story = SimpleNamespace(site_articles=[
            _article(_outlet("Old", "https://old.com/i.png"), date=datetime(2026, 10, 1)),
            _article(_outlet("No logo"), date=datetime(2026, 10, 22)),
            _article(_outlet("New", "https://new.com/i.png"), date=datetime(2026, 10, 20)),
            _article(None, date=datetime(2026, 10, 21)),
        ])
        self.assertEqual(ser._story_logo(story), "https://new.com/i.png")
        self.assertIsNone(ser._story_logo(SimpleNamespace(site_articles=[_article(_outlet("x"))])))


class CardImageTemplateTests(unittest.TestCase):
    def render(self, articles):
        env = Environment(loader=FileSystemLoader(TEMPLATES))
        env.globals.update(url_for=lambda *a, **k: "/", current_back=lambda: "", can=lambda r: False)
        story = SimpleNamespace(articles=articles, display_articles=articles)
        tpl = env.from_string("{% from 'story_card_macros.html' import card_image with context %}{{ card_image(story) }}")
        return tpl.render(story=story)

    def test_photo_from_another_article(self):
        html = self.render([_article(_outlet("CBS News", "https://cbs.com/l.png")),
                            _article(_outlet("AP"), image="https://ap.com/p.jpg"),
                            _article(_outlet("Roundup"), image="https://r.com/p.jpg", roundup=True)])
        self.assertIn('src="https://ap.com/p.jpg"', html)
        self.assertNotIn("r.com", html)
        self.assertNotIn("is-fallback", html)
        # The logo stays in the page, hidden, for when the photo fails to load.
        self.assertIn('class="story-logo" src="https://cbs.com/l.png"', html)

    def test_logo_then_name_then_icon(self):
        html = self.render([_article(_outlet("AP")), _article(_outlet("CBS News", "https://cbs.com/l.png"))])
        self.assertIn("is-fallback", html)
        self.assertIn('src="https://cbs.com/l.png"', html)
        self.assertIn(">CBS News<", html)
        self.assertNotIn("story-photo", html)

        html = self.render([_article(_outlet("AP"))])
        self.assertIn(">AP<", html)
        self.assertNotIn("story-logo", html)

        html = self.render([_article(None)])
        self.assertIn("story-image-placeholder", html)


if __name__ == "__main__":
    unittest.main()
