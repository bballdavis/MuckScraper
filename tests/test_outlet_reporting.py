"""Tests for the hand-kept reporting grade (aggregator/outlet_reporting.py):
lookup, the API field and the chip. No network and no database."""
import os
import unittest
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

from aggregator import outlet_reporting as rep
from aggregator.models import Outlet

TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "aggregator", "templates")


class GradeTableTests(unittest.TestCase):
    def test_every_grade_is_a_level(self):
        self.assertTrue(rep.GRADES)
        self.assertEqual(set(rep.GRADES.values()) - set(rep.LEVELS), set())

    def test_lookup_any_case(self):
        name, grade = next(iter(rep.GRADES.items()))
        self.assertEqual(rep.reporting_for(name), grade)
        self.assertEqual(rep.reporting_for(name.upper()), grade)
        self.assertIsNone(rep.reporting_for("Some Blog Nobody Graded"))
        self.assertIsNone(rep.reporting_for(None))

    def test_feed_outlets_are_graded(self):
        for name in ("Associated Press", "Reuters", "NPR", "Fox News", "Deutsche Welle", "The Texas Tribune"):
            self.assertIn(name, rep.GRADES, name)


class ApiTests(unittest.TestCase):
    def test_outlet_reporting(self):
        from aggregator.api import serializers as ser
        ap = SimpleNamespace(id=1, name="Reuters", bias_score=3, logo_url=None, country=None, funding=None)
        self.assertEqual(ser.outlet(ap)["reporting"], rep.GRADES["Reuters"])
        self.assertIsNone(ser.outlet(None, "X")["reporting"])


class ChipTests(unittest.TestCase):
    def _render(self, outlet):
        env = Environment(loader=FileSystemLoader(TEMPLATES))
        env.globals.update(url_for=lambda *a, **k: "/", current_back=lambda: "", can=lambda r: False)
        tpl = env.from_string(
            "{% from 'story_card_macros.html' import reporting_tag with context %}{{ reporting_tag(o) }}")
        return tpl.render(o=outlet)

    def test_dots_follow_the_grade(self):
        for level, filled in zip(rep.LEVELS, (5, 4, 3, 2, 1)):
            name = next((k for k, v in rep.GRADES.items() if v == level), None)
            if name is None:
                continue
            html = self._render(Outlet(name=name))
            self.assertEqual(html.count('class="on"'), filled, level)
            self.assertIn("data-tip=", html)

    def test_grade_and_lean_share_one_pill(self):
        env = Environment(loader=FileSystemLoader(TEMPLATES))
        env.globals.update(url_for=lambda *a, **k: "/", current_back=lambda: "", can=lambda r: False)
        tpl = env.from_string(
            "{% from 'story_card_macros.html' import grade_pill, bias_tag with context %}"
            "{% call grade_pill(o) %}{{ bias_tag(3, unrated=false) }}{% endcall %}")
        graded = next(k for k in rep.GRADES)
        html = tpl.render(o=Outlet(name=graded))
        self.assertTrue(html.startswith('<span class="grade-pill">'))
        self.assertLess(html.index("reporting-tag"), html.index("bias-tag"))
        plain = tpl.render(o=Outlet(name="Some Blog Nobody Graded"))
        self.assertNotIn("grade-pill", plain)
        self.assertIn("bias-tag bias-3", plain)

    def test_ungraded_outlet_shows_nothing(self):
        self.assertEqual(self._render(Outlet(name="Some Blog Nobody Graded")).strip(), "")
        self.assertEqual(self._render(None).strip(), "")


if __name__ == "__main__":
    unittest.main()
