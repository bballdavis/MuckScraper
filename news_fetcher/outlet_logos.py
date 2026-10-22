# news_fetcher/outlet_logos.py
"""Images for cards that have none (Philip, 2026-10-22).

A card shows, in order: its article's image, another article's image in the
same story, the outlet's logo, then the outlet's name set as a masthead. Two
passes here fill the gaps behind that chain, and both run on every fetch run:

- `retry_missing_images` fetches each recent article that has no image once
  more and reads the page's og:image (scraper.extract_page_image). New
  articles get this at ingestion from the page the scraper already fetched;
  this covers the ones whose scrape was deferred, blocked or failed early.
- `refresh_outlet_logos` resolves each outlet's icon from its home page
  (apple-touch-icon, a large rel=icon, the JSON-LD publisher logo, then
  /apple-touch-icon.png and /favicon.ico, then Google's favicon service) and
  stores the URL on the outlet, so the web cards and /api/v1 serve the same
  one. SVG is skipped: the iOS app's image views can't draw it.
"""

import json
import logging
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# A found logo is looked up again after a month; a miss after a week.
LOGO_RECHECK_DAYS = 30
MISSING_LOGO_RECHECK_DAYS = 7
LOGOS_PER_RUN = 40
# Smallest rel=icon worth showing on a card; apple-touch-icons are always big.
MIN_ICON_SIZE = 96
IMAGE_RETRY_DAYS = 3
IMAGE_RETRIES_PER_RUN = 40
TIMEOUT = 8

_ORG_TYPES = {"Organization", "NewsMediaOrganization", "Corporation", "WebSite"}


def site_root(url):
    """https://host/ for an article or outlet URL, or None."""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/"


def _icon_size(tag):
    sizes = re.findall(r"(\d+)\s*x\s*\d+", tag.get("sizes") or "", re.I)
    return max((int(s) for s in sizes), default=0)


def _is_svg(url, tag=None):
    if tag is not None and "svg" in (tag.get("type") or "").lower():
        return True
    return urlparse(url).path.lower().endswith(".svg")


def _absolute(href, base_url):
    if not href or not href.strip() or href.strip().startswith("data:"):
        return None
    url = urljoin(base_url, href.strip())
    return url if urlparse(url).scheme in ("http", "https") else None


def _json_ld_logo(soup, base_url):
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or script.get_text() or "")
        except Exception:
            continue
        items = payload if isinstance(payload, list) else [payload]
        items += [g for item in items if isinstance(item, dict) for g in item.get("@graph", []) or []]
        for item in items:
            if not isinstance(item, dict):
                continue
            kinds = item.get("@type")
            kinds = set(kinds) if isinstance(kinds, list) else {kinds}
            owners = [item] if kinds & _ORG_TYPES else []
            if isinstance(item.get("publisher"), dict):
                owners.append(item["publisher"])
            for owner in owners:
                logo = owner.get("logo")
                if isinstance(logo, dict):
                    logo = logo.get("url") or logo.get("contentUrl")
                url = _absolute(logo, base_url) if isinstance(logo, str) else None
                if url and not _is_svg(url):
                    return url
    return None


def logo_candidates(html, page_url):
    """Icon URLs from a home page, best first, without duplicates."""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        soup = BeautifulSoup("", "html.parser")

    touch, large, small = [], [], []
    for tag in soup.find_all("link", href=True):
        rels = [r.lower() for r in (tag.get("rel") or [])]
        url = _absolute(tag["href"], page_url)
        if not url or _is_svg(url, tag):
            continue
        size = _icon_size(tag)
        if any(r.startswith("apple-touch-icon") for r in rels):
            touch.append((size, url))
        elif "icon" in rels:
            (large if size >= MIN_ICON_SIZE else small).append((size, url))

    ordered = [url for _, url in sorted(touch, key=lambda t: -t[0])]
    ordered += [url for _, url in sorted(large, key=lambda t: -t[0])]
    ld_logo = _json_ld_logo(soup, page_url)
    if ld_logo:
        ordered.append(ld_logo)
    ordered += [url for _, url in sorted(small, key=lambda t: -t[0])]
    root = site_root(page_url)
    if root:
        ordered += [urljoin(root, "/apple-touch-icon.png"), urljoin(root, "/favicon.ico")]

    seen, out = set(), []
    for url in ordered:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _is_image(url, session):
    try:
        response = session.get(url, headers=_headers(), timeout=TIMEOUT, stream=True, allow_redirects=True)
        try:
            kind = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            return response.status_code == 200 and kind.startswith("image/") and "svg" not in kind
        finally:
            response.close()
    except requests.RequestException:
        return False


def _headers():
    from news_fetcher.scraper import HEADERS_DEFAULT
    return HEADERS_DEFAULT


def resolve_logo(site_url, session=None):
    """The first icon of `site_url`'s home page that really is an image."""
    session = session or requests
    root = site_root(site_url)
    if not root:
        return None
    html, page_url = "", root
    try:
        response = session.get(root, headers=_headers(), timeout=TIMEOUT)
        if response.status_code == 200:
            html, page_url = response.text, response.url or root
    except requests.RequestException as exc:
        logger.info("  [Logos] Home page failed for %s: %s", root, exc)
    for candidate in logo_candidates(html, page_url) + [favicon_service_url(root)]:
        if _is_image(candidate, session):
            return candidate
    return None


def favicon_service_url(site_url):
    """Google's favicon service for the site: the last resort when the
    publisher's own server turns the lookup away, as bot walls do (the
    Washington Post's cards had no logo on 2026-10-22). It serves a PNG up to
    128px, and 404s for a site it has no icon for."""
    host = urlparse(site_url).netloc.lower().removeprefix("www.")
    return f"https://www.google.com/s2/favicons?domain={host}&sz=128"


def _outlet_site(outlet):
    if site_root(outlet.url):
        return outlet.url
    newest = max((a for a in outlet.articles if site_root(a.url)), key=lambda a: a.date or datetime.min, default=None)
    return newest.url if newest else None


def outlets_due(now=None):
    from aggregator.models import Outlet
    now = now or datetime.utcnow()
    found_before = now - timedelta(days=LOGO_RECHECK_DAYS)
    missing_before = now - timedelta(days=MISSING_LOGO_RECHECK_DAYS)
    return (
        Outlet.query
        .filter(
            (Outlet.logo_checked_at.is_(None))
            | ((Outlet.logo_url.isnot(None)) & (Outlet.logo_checked_at < found_before))
            | ((Outlet.logo_url.is_(None)) & (Outlet.logo_checked_at < missing_before))
        )
        .order_by(Outlet.logo_checked_at.asc().nullsfirst(), Outlet.id)
        .limit(LOGOS_PER_RUN)
        .all()
    )


def refresh_outlet_logos(now=None, session=None, resolve=resolve_logo):
    """Look up the logo of each outlet that is due. One lookup per domain."""
    from aggregator import db
    now = now or datetime.utcnow()
    stats = {"checked": 0, "found": 0, "missing": 0}
    by_domain = {}
    for outlet in outlets_due(now):
        site = _outlet_site(outlet)
        domain = urlparse(site).netloc.lower().removeprefix("www.") if site else None
        if domain and domain not in by_domain:
            by_domain[domain] = resolve(site, session=session)
        # A failed recheck keeps the logo already found; the cards fall back
        # to the placeholder if it ever stops loading.
        outlet.logo_url = (by_domain.get(domain) if domain else None) or outlet.logo_url
        outlet.logo_checked_at = now
        stats["checked"] += 1
        stats["found" if outlet.logo_url else "missing"] += 1
    db.session.commit()
    logger.info("  [Logos] %s outlets checked, %s logos found", stats["checked"], stats["found"])
    return stats


def articles_missing_images(now=None):
    from aggregator.models import Article
    now = now or datetime.utcnow()
    return (
        Article.query
        .filter(
            (Article.image_url.is_(None)) | (Article.image_url == ""),
            Article.image_checked_at.is_(None),
            Article.date >= now - timedelta(days=IMAGE_RETRY_DAYS),
        )
        .order_by(Article.date.desc())
        .limit(IMAGE_RETRIES_PER_RUN)
        .all()
    )


def retry_missing_images(now=None, fetch=None):
    """Read the og:image of each recent article that has no image, once."""
    from aggregator import db
    from news_fetcher import scraper
    now = now or datetime.utcnow()
    fetch = fetch or scraper._fetch_html
    stats = {"checked": 0, "found": 0}
    for article in articles_missing_images(now):
        article.image_checked_at = now
        stats["checked"] += 1
        if article.is_roundup or scraper.is_domain_blocked(article.url) or scraper.should_skip(article.url):
            continue
        try:
            response = fetch(article.url)
        except Exception as exc:
            logger.info("  [Images] Page fetch failed for %s: %s", (article.url or "")[:60], exc)
            continue
        image = scraper.extract_page_image(response.text, article.url)
        if image:
            article.image_url = image
            stats["found"] += 1
    db.session.commit()
    logger.info("  [Images] %s imageless articles retried, %s images found", stats["checked"], stats["found"])
    return stats


def refresh_card_images():
    """The scheduler's step: article images first, then outlet logos."""
    return {"article_images": retry_missing_images(), "outlet_logos": refresh_outlet_logos()}
