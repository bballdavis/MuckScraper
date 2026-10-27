"""Which article's photo leads a story (cards on the site, image_url in
/api/v1).

It used to be the newest article with a photo, so one off-angle piece
arriving late (a Washington reaction story with a Trump photo on a Brazil
election) took over the card. Now each article with a photo is scored on
how well its title matches the story:

- headline: share of the story headline's words in the article's title.
- central: how many of the story's other titles share the article's title
  words. A title with words nobody else uses ("Trump") scores low.
- slug: words of the headline in the photo's file name, which outlets
  often build from the caption ("lula-brazil-election.jpg").
- shared: the same photo on more than one outlet (a wire photo of the event).
- reporting: a small nudge toward outlets graded high for reporting
  (aggregator/outlet_reporting.py).

Ties go to the newest article. No database and no network: it only reads
what the card already loaded.
"""
import re
from datetime import datetime

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset("""
a an and are as at be been but by for from has have he her his how in into is it its new news not of on or
our over says said she than that the their them they this to up was we were what when who why will with
after before about amid against more most just than then there these those you your live latest update
updates report reports video watch photo photos
""".split())

_REPORTING_BONUS = {"very_high": 0.06, "high": 0.03}


def _stem(word):
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def words(text):
    """Content words of a title or file name, lightly stemmed ("wins" and
    "win" match)."""
    out = set()
    for w in _WORD.findall((text or "").lower().replace("'s", "")):
        if len(w) > 2 and w not in _STOP and not w.isdigit():
            out.add(_stem(w))
    return out


def _http(url):
    return url if isinstance(url, str) and url.startswith(("http://", "https://")) else None


def _photo_words(url):
    path = url.split("?", 1)[0].rsplit("/", 2)
    return words(" ".join(path[-2:]).replace("-", " ").replace("_", " "))


def _outlet_key(article):
    outlet = getattr(article, "outlet", None)
    return getattr(outlet, "name", None) or getattr(article, "source", None)


def _reporting(article):
    outlet = getattr(article, "outlet", None)
    if outlet is None:
        return None
    try:
        from aggregator.outlet_reporting import reporting_for
        return reporting_for(outlet.name)
    except Exception:
        return None


def scored(articles, headline):
    """[(score, article)] for the articles with a usable photo, best first."""
    articles = [a for a in articles if not getattr(a, "is_roundup", False)]
    candidates = [a for a in articles if _http(a.image_url)]
    if not candidates:
        return []

    head = words(headline)
    titles = {id(a): words(a.title) for a in articles}
    df = {}
    for ws in titles.values():
        for w in ws:
            df[w] = df.get(w, 0) + 1
    others = max(1, len(articles) - 1)

    outlets_per_photo = {}
    for a in candidates:
        outlets_per_photo.setdefault(a.image_url, set()).add(_outlet_key(a))

    ranked = []
    for a in candidates:
        ws = titles[id(a)]
        headline_share = len(ws & head) / len(head) if head else 0.0
        # Mean, over the title's words, of how many other titles use it.
        central = sum((df[w] - 1) / others for w in ws) / len(ws) if ws and len(articles) > 1 else 0.0
        slug = min(2, len(_photo_words(a.image_url) & head)) / 2 if head else 0.0
        shared = 0.1 if len(outlets_per_photo[a.image_url]) > 1 else 0.0
        score = (0.45 * headline_share + 0.4 * central + 0.1 * slug + shared
                 + _REPORTING_BONUS.get(_reporting(a), 0.0))
        ranked.append((score, a.date or datetime.min, a))
    ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return [(score, a) for score, _, a in ranked]


def lead_image(articles, headline):
    """The photo for a story's card, or None when no article has one."""
    ranked = scored(articles, headline)
    return ranked[0][1].image_url if ranked else None


def image_focus(articles, image_url):
    """{"x", "y"}: where the subject of `image_url` is, 0..1 from the left
    and top, from whichever of `articles` carries that photo
    (news_fetcher/image_focus.py), or None, which clients crop around the
    center."""
    if not image_url:
        return None
    for a in articles:
        x, y = getattr(a, "image_focus_x", None), getattr(a, "image_focus_y", None)
        if a.image_url == image_url and x is not None and y is not None:
            return {"x": x, "y": y}
    return None


def object_position(focus):
    """CSS object-position for a focus: the subject sits at the same share
    of the frame as of the photo, so any crop keeps it in view."""
    if not focus:
        return "50% 50%"
    return f"{focus['x'] * 100:.0f}% {focus['y'] * 100:.0f}%"
