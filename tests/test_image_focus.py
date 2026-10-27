"""Tests for photo focal points (news_fetcher/image_focus.py) and how the
API and the web cards use them. No network and no database."""
import io
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import numpy as np
from jinja2 import Environment, FileSystemLoader
from PIL import Image

from aggregator.story_image import image_focus, object_position
from news_fetcher import image_focus as focus

TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "aggregator", "templates")
SKIN = (224, 172, 140)


def _scene(w, h, face_at, face_r=0.08):
    """A grey street with some texture and one face-colored disc."""
    rng = np.random.default_rng(1)
    img = np.full((h, w, 3), 110.0) + rng.normal(0, 6, (h, w, 1))
    yy, xx = np.mgrid[0:h, 0:w]
    fx, fy = face_at[0] * w, face_at[1] * h
    disc = (xx - fx) ** 2 + (yy - fy) ** 2 <= (face_r * min(w, h)) ** 2
    img[disc] = SKIN
    return np.clip(img, 0, 255).astype(np.uint8)


def _jpeg(pixels):
    out = io.BytesIO()
    Image.fromarray(pixels).save(out, "JPEG", quality=85)
    return out.getvalue()


class FocusTests(unittest.TestCase):
    def assertNear(self, got, want, tol=0.08):
        self.assertIsNotNone(got)
        self.assertLess(abs(got[0] - want[0]), tol, got)
        self.assertLess(abs(got[1] - want[1]), tol, got)

    def test_face_near_the_top_of_a_tall_photo(self):
        self.assertNear(focus.focus_of_pixels(_scene(60, 100, (0.5, 0.15))), (0.5, 0.15))

    def test_face_off_to_one_side(self):
        self.assertNear(focus.focus_of_pixels(_scene(120, 70, (0.22, 0.4))), (0.22, 0.4))

    def test_flat_photo_keeps_the_center(self):
        self.assertIsNone(focus.focus_of_pixels(np.full((60, 90, 3), 128, dtype=np.uint8)))

    def test_from_jpeg_bytes(self):
        self.assertNear(focus.focus_of_bytes(_jpeg(_scene(600, 900, (0.6, 0.2)))), (0.6, 0.2))

    def test_unreadable_bytes(self):
        self.assertIsNone(focus.focus_of_bytes(b"not an image"))


class RefreshTests(unittest.TestCase):
    def test_each_photo_downloaded_once_and_marked_checked(self):
        now = datetime(2026, 10, 27)
        shared = "https://ap.example/p.jpg"
        articles = [SimpleNamespace(image_url=shared, image_focus_at=None),
                    SimpleNamespace(image_url=shared, image_focus_at=None),
                    SimpleNamespace(image_url="https://e.example/broken.jpg", image_focus_at=None),
                    SimpleNamespace(image_url="data:image/png;base64,x", image_focus_at=None)]
        photo = _jpeg(_scene(300, 200, (0.3, 0.3)))
        download = mock.Mock(side_effect=lambda url: photo if url == shared else None)
        fake_db = SimpleNamespace(session=mock.Mock())
        with mock.patch.object(focus, "articles_needing_focus", return_value=articles), \
             mock.patch("aggregator.db", fake_db):
            stats = focus.refresh_image_focus(now=now, download=download)
        self.assertEqual(download.call_count, 2)
        self.assertEqual(stats, {"checked": 2, "found": 1})
        self.assertTrue(all(a.image_focus_at == now for a in articles))
        self.assertEqual((articles[0].image_focus_x, articles[0].image_focus_y),
                         (articles[1].image_focus_x, articles[1].image_focus_y))
        self.assertAlmostEqual(articles[0].image_focus_x, 0.3, delta=0.08)
        self.assertIsNone(articles[2].image_focus_x)
        fake_db.session.commit.assert_called_once()


def _article(image, x=None, y=None, roundup=False):
    return SimpleNamespace(image_url=image, image_focus_x=x, image_focus_y=y, is_roundup=roundup,
                           outlet=None, source="AP", date=datetime(2026, 10, 27), title="")


class UseTests(unittest.TestCase):
    def test_focus_from_whichever_article_has_the_photo(self):
        articles = [_article("https://a/p.jpg"), _article("https://b/q.jpg", 0.2, 0.1), _article("https://a/p.jpg", 0.7, 0.25)]
        self.assertEqual(image_focus(articles, "https://a/p.jpg"), {"x": 0.7, "y": 0.25})
        self.assertIsNone(image_focus(articles, "https://c/r.jpg"))
        self.assertIsNone(image_focus(articles, None))

    def test_object_position(self):
        self.assertEqual(object_position({"x": 0.5, "y": 0.123}), "50% 12%")
        self.assertEqual(object_position(None), "50% 50%")

    def test_article_card_sends_focus(self):
        from aggregator.api import serializers as ser
        self.assertEqual(ser._article_focus(_article("https://a/p.jpg", 0.4, 0.2)), {"x": 0.4, "y": 0.2})
        self.assertIsNone(ser._article_focus(_article("https://a/p.jpg")))
        self.assertIsNone(ser._article_focus(_article("https://a/p.jpg", 0.4, 0.2, roundup=True)))

    def test_card_crops_around_the_focus(self):
        env = Environment(loader=FileSystemLoader(TEMPLATES))
        env.globals.update(url_for=lambda *a, **k: "/", current_back=lambda: "", can=lambda r: False)
        env.filters["object_position"] = object_position
        tpl = env.from_string("{% from 'story_card_macros.html' import card_image with context %}{{ card_image(story) }}")
        story = SimpleNamespace(articles=[], display_articles=[], lead_image_url="https://a/p.jpg",
                                lead_image_focus={"x": 0.5, "y": 0.18})
        self.assertIn('style="object-position: 50% 18%"', tpl.render(story=story))
        story.lead_image_focus = None
        self.assertIn('style="object-position: 50% 50%"', tpl.render(story=story))


if __name__ == "__main__":
    unittest.main()
