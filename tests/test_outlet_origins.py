"""Tests for outlet countries (aggregator/outlet_origins.py): lookup by name
and by domain, the flag and its tooltip, the API fields, and the card's chips.
No network and no database."""
import os
import unittest
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

# news_fetcher.fetch_and_store_articles builds the app on import; it never connects.
os.environ.setdefault("DATABASE_URL", "postgresql://unused/none")

from aggregator import outlet_origins as oo
from aggregator.models import Outlet

TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "aggregator", "templates")


class OriginLookupTests(unittest.TestCase):
    def test_by_name_any_case(self):
        self.assertEqual(oo.origin_for("Deutsche Welle"), ("DE", "public"))
        self.assertEqual(oo.origin_for("bbc news"), ("GB", "public"))
        self.assertEqual(oo.origin_for("CNA"), ("SG", "state"))

    def test_by_country_code_domain(self):
        self.assertEqual(oo.origin_for("Some Paper", "https://www.example.co.uk/a"), ("GB", None))
        self.assertEqual(oo.origin_for("Some Paper", "https://news.example.de/a"), ("DE", None))

    def test_us_and_generic_domains_have_no_country(self):
        self.assertEqual(oo.origin_for("Fox News", "https://www.foxnews.com/a"), (None, None))
        self.assertEqual(oo.origin_for("Startup", "https://startup.io/a"), (None, None))
        self.assertEqual(oo.origin_for("Video", "https://example.tv/a"), (None, None))
        self.assertEqual(oo.origin_for(None, None), (None, None))

    def test_is_international(self):
        self.assertTrue(oo.is_international("DE"))
        self.assertFalse(oo.is_international("US"))
        self.assertFalse(oo.is_international(None))


class OutletPropertyTests(unittest.TestCase):
    def test_flag_and_label(self):
        outlet = Outlet(name="Deutsche Welle", country="DE", funding="public")
        self.assertEqual(outlet.flag, "\U0001F1E9\U0001F1EA")
        self.assertEqual(outlet.origin_label, "Germany · Public broadcaster")
        self.assertEqual(Outlet(name="Daily Maverick", country="ZA").origin_label, "South Africa")

    def test_us_outlet_has_no_flag(self):
        self.assertIsNone(Outlet(name="NPR").flag)
        self.assertIsNone(Outlet(name="NPR", country="US").origin_label)


class SourceNameTests(unittest.TestCase):
    def test_parent_domain_override(self):
        from news_fetcher.fetch_and_store_articles.outlets import source_name_from_url
        self.assertEqual(source_name_from_url("https://www.abc.net.au/news/x"), "ABC News (Australia)")
        self.assertEqual(source_name_from_url("https://amp.dw.com/en/x"), "Deutsche Welle")
        self.assertIsNone(source_name_from_url("https://example.com/x"))

    def test_australian_abc_is_not_merged_into_abc_news(self):
        from news_fetcher.fetch_and_store_articles.outlets import normalize_source_name
        self.assertEqual(normalize_source_name("ABC News (Australia)"), "ABC News (Australia)")
        self.assertEqual(normalize_source_name("ABC News: Top Stories"), "ABC News")


class ApiTests(unittest.TestCase):
    def test_outlet_country_and_funding(self):
        from aggregator.api import serializers as ser
        dw = SimpleNamespace(id=1, name="Deutsche Welle", bias_score=None, logo_url=None, country="DE", funding="public")
        out = ser.outlet(dw)
        self.assertEqual((out["leaning"], out["country"], out["funding"]), ("unrated", "DE", "public"))
        npr = SimpleNamespace(id=2, name="NPR", bias_score=2, logo_url=None, country="US", funding=None)
        self.assertIsNone(ser.outlet(npr)["country"])
        self.assertIsNone(ser.outlet(None, "X")["country"])


class CardTests(unittest.TestCase):
    def _env(self):
        env = Environment(loader=FileSystemLoader(TEMPLATES))
        env.globals.update(url_for=lambda *a, **k: "/", current_back=lambda: "", can=lambda r: False)
        return env

    def test_flag_has_a_tooltip(self):
        tpl = self._env().from_string(
            "{% from 'story_card_macros.html' import outlet_flag with context %}{{ outlet_flag(o) }}")
        html = tpl.render(o=Outlet(name="CBC News", country="CA", funding="public"))
        self.assertIn('data-tip="Canada · Public broadcaster"', html)
        self.assertEqual(tpl.render(o=Outlet(name="NPR")).strip(), "")

    def test_unrated_international_outlet_reads_international(self):
        tpl = self._env().from_string(
            "{% from 'story_card_macros.html' import outlet_bias_tag with context %}{{ outlet_bias_tag(o) }}")
        self.assertIn("International", tpl.render(o=Outlet(name="DW", country="DE")))
        self.assertIn("Lean Left", tpl.render(o=Outlet(name="BBC News", country="GB", bias_score=2)))
        self.assertIn("Unrated", tpl.render(o=Outlet(name="Somewhere")))

    def test_bias_line_counts_international_articles(self):
        tpl = self._env().from_string(
            "{% from 'story_card_macros.html' import bias_line with context %}{{ bias_line(s) }}")
        dw = Outlet(name="DW", country="DE")
        npr = Outlet(name="NPR", bias_score=2)
        story = SimpleNamespace(left_bias_count=1, center_bias_count=0, right_bias_count=0, articles=[
            SimpleNamespace(bias_score=None, outlet=dw), SimpleNamespace(bias_score=None, outlet=dw),
            SimpleNamespace(bias_score=2, outlet=npr)])
        html = tpl.render(s=story)
        self.assertIn("international 2", html)
        self.assertIn('class="seg-i"', html)


if __name__ == "__main__":
    unittest.main()
