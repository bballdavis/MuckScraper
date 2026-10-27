# news_fetcher/wire_carriers.py
"""Reuters stories read from sites licensed to republish them (2026-10-27).

reuters.com answers every fetch with 401 and its robots.txt disallows the
article paths, and Reuters' terms forbid scraping. So Reuters is headline-only
(a permanent scrape_blocklist row, like the hard paywalls): its headlines
still come from the Google News `site:reuters.com` feed and count in a
story's coverage, and nothing fetches reuters.com.

The full wire text is read where Reuters licenses it to be republished
instead. Checked from a desktop on 2026-10-27 (plain fetch, our browser
headers, robots.txt allowing the article paths):

  - Yahoo Finance (finance.yahoo.com and yahoo.com/news): the full wire,
    "(Reuters) -" dateline, no consent wall from a US address.
  - KFGO (kfgo.com): a radio station site that carries the whole Reuters
    wire and nothing else, cleanly.
  - The Globe and Mail: Reuters copy mixed with its own staff stories, so
    the credit is checked per article.

Left out: usnews.com (every fetch stalled past 20 s), investing.com and
marketscreener.com (403, robots.txt too), euronews.com (406), msn.com (pages
render client-side, a fetch gets an empty shell), straitstimes.com (rewrites
the wire without crediting Reuters), news.yahoo.com regional editions (mostly
not Reuters), devdiscourse.com (roundups and template junk). WKZO carries
the same wire as KFGO, so one of the two is enough.

A carrier page that credits Reuters is stored as a Reuters article with
`text_via` naming the carrier. When the Reuters headline is already stored
with no text, the carrier's text fills that article instead, so the story
keeps one Reuters article that links to reuters.com. A page from a carrier
feed that does not credit Reuters is dropped.
"""

import json
import re
from html import escape
from urllib.parse import urlparse

from bs4 import BeautifulSoup

WIRE_OUTLET = "Reuters"

# Domain (and its subdomains) -> the name shown as "via ...".
CARRIERS = {
    "finance.yahoo.com": "Yahoo Finance",
    "yahoo.com": "Yahoo News",
    "kfgo.com": "KFGO",
    "theglobeandmail.com": "The Globe and Mail",
}

# Google News searches for each carrier's Reuters stories. Searching
# yahoo.com/news returned finance.yahoo.com URLs anyway, so one Yahoo feed
# covers both.
CARRIER_FEEDS = [
    ("https://news.google.com/rss/search?q=Reuters+site:finance.yahoo.com+when:1d&hl=en-US&gl=US&ceid=US:en",
     "Reuters via Yahoo Finance"),
    ("https://news.google.com/rss/search?q=site:kfgo.com+when:1d&hl=en-US&gl=US&ceid=US:en",
     "Reuters via KFGO"),
    ("https://news.google.com/rss/search?q=Reuters+site:theglobeandmail.com+when:1d&hl=en-US&gl=US&ceid=US:en",
     "Reuters via The Globe and Mail"),
]
CARRIER_FEED_URLS = {url for url, _ in CARRIER_FEEDS}

# A Reuters article with less text than this is a stored headline the
# carrier's copy can fill (story_view.has_good_original's floor).
MIN_TEXT_CHARS = 500

# "WASHINGTON, Oct 26 (Reuters) -" opens a wire story; only looked for near
# the top, so a staff story that quotes Reuters further down isn't taken.
DATELINE_RE = re.compile(r"\(Reuters\)\s*[-–—]", re.I)
DATELINE_WINDOW = 400


def _domain(url):
    netloc = urlparse(url or "").netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def carrier_name(url):
    """The carrier's display name when `url` is on a carrier site, else None.
    The most specific domain wins (finance.yahoo.com before yahoo.com)."""
    parts = _domain(url).split(".")
    for i in range(len(parts) - 1):
        name = CARRIERS.get(".".join(parts[i:]))
        if name:
            return name
    return None


def is_carrier_feed(feed_url):
    return feed_url in CARRIER_FEED_URLS


def _text(html):
    if not html:
        return ""
    return re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" ")).strip()


def _jsonld_items(payload):
    if isinstance(payload, list):
        for item in payload:
            yield from _jsonld_items(item)
    elif isinstance(payload, dict):
        yield payload
        yield from _jsonld_items(payload.get("@graph") or [])


def _names(value):
    """Every "name" in a JSON-LD author/provider value (a string, an object
    or a list of either)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [value.get("name") or ""]
    if isinstance(value, list):
        return [name for item in value for name in _names(item)]
    return []


def credits_wire(page_html, content_html=None):
    """True when the carrier page credits Reuters for the story.

    Any one of: the "(Reuters) -" dateline at the top of the text; Reuters
    as the JSON-LD author or provider (Yahoo's `provider.name`); Yahoo's
    provider logo (`img[alt="Reuters"]`); The Globe and Mail's credit line
    (`span.c-creditline` reading "Reuters"; its JSON-LD names only the
    reporters).
    """
    text = _text(content_html)
    if DATELINE_RE.search(text[:DATELINE_WINDOW]):
        return True
    if not page_html:
        return False
    soup = BeautifulSoup(page_html, "html.parser")
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or script.get_text() or "")
        except ValueError:
            continue
        for item in _jsonld_items(payload):
            for key in ("provider", "author", "sourceOrganization"):
                if any(name.strip().lower() == "reuters" for name in _names(item.get(key))):
                    return True
    if soup.find("img", alt=re.compile(r"^\s*reuters\s*$", re.I)):
        return True
    for credit in soup.select(".c-article-meta__creditlines .c-creditline, span.c-creditline"):
        if credit.get_text(strip=True).lower() == "reuters":
            return True
    return False


# Yahoo's article body. finance.yahoo.com folds the story behind "Story
# continues": the rest of the text is in the same HTML, in a hidden
# div.read-more-wrapper, and readability kept only the part above the fold
# (2,616 of 4,177 characters on 2026-10-27). yahoo.com/news has no fold.
YAHOO_BODY_SELECTORS = ('div.body[data-testid="article-body"]', 'div[data-article-body="true"]')
# Exact test ids: a loose [data-testid*=ad] also matches "read-more" and
# drops the whole fold.
YAHOO_DROP_SELECTORS = ('script', 'style', 'figure', 'aside', 'button', 'noscript', 'svg',
                        '[data-testid="inarticle-ad"]', '[id^="sda-"]', 'div.readmore')
ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\ufeff]")
BYLINE_PARAGRAPH_RE = re.compile(r"^By\s+\S.{0,80}$")


def extract_body(html, url):
    """The article text of a carrier page as HTML paragraphs, for carriers
    readability reads badly; None to let the regular extraction run."""
    if not html or carrier_name(url) not in ("Yahoo Finance", "Yahoo News"):
        return None
    soup = BeautifulSoup(html, "html.parser")
    body = None
    for selector in YAHOO_BODY_SELECTORS:
        body = soup.select_one(selector)
        if body:
            break
    if body is None:
        return None
    for selector in YAHOO_DROP_SELECTORS:
        for tag in body.select(selector):
            tag.decompose()
    paragraphs = []
    for p in body.find_all("p"):
        text = re.sub(r"\s+", " ", ZERO_WIDTH_RE.sub("", p.get_text(" "))).strip()
        if not text:
            continue
        # The byline repeated as the first paragraph ("By Jane Doe").
        if not paragraphs and BYLINE_PARAGRAPH_RE.match(text):
            continue
        paragraphs.append(text)
    if sum(len(p) for p in paragraphs) < 200:
        return None
    return "<div>" + "".join(f"<p>{escape(p)}</p>" for p in paragraphs) + "</div>"
