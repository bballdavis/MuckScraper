"""The one Blocklist (Settings › Blocklist): every rule that keeps an article
out, or keeps its text from being scraped, in one list with a reason.

Two tables sit underneath and keep doing different things:

- IngestionBlock: "Don't collect". A URL or headline containing the pattern
  is refused before anything is stored.
- ScrapeBlocklist: "Collect, don't scrape". The article is stored and counts
  toward its story, but its page isn't scraped (hard paywalls by hand, and
  domains blocked on their own after repeated bad scrapes; see
  news_fetcher/scraper.py).

The scraper's retry back-offs are not rules and live on Scrape health.
"""
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from sqlalchemy import func

from aggregator.models import Article, IngestionBlock, IngestionBlockHit, ScrapeBlocklist, db

EFFECT_SKIP = "skip"          # don't collect
EFFECT_NO_SCRAPE = "noscrape"  # collect, don't scrape

ORIGIN_SEEDED = "seeded"
ORIGIN_HAND = "hand"
ORIGIN_AUTO = "auto"

CAUGHT_DAYS = 7

BLOCK_KIND_SOURCE = "source"
BLOCK_KIND_TITLE = "title_keyword"


def record_block_hits(hits, today=None):
    """Add {block id: articles stopped} to today's counts. Doesn't commit."""
    if not hits:
        return
    from sqlalchemy.dialects.postgresql import insert
    today = today or date.today()
    for block_id, count in hits.items():
        stmt = insert(IngestionBlockHit).values(block_id=block_id, day=today, count=count)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_ingestion_block_hit_day",
            set_={"count": IngestionBlockHit.count + stmt.excluded.count},
        )
        db.session.execute(stmt)


def _domain(url):
    netloc = urlparse(url or "").netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _seeded():
    from aggregator import seed_defaults
    ingestion = {("source", p.lower()) for p, _ in seed_defaults.SEED_BLOCK_SOURCES}
    ingestion |= {("title_keyword", p.lower()) for p, _ in seed_defaults.SEED_BLOCK_TITLE_KEYWORDS}
    scrape = {d for d, _ in seed_defaults.SEED_SCRAPE_BLOCKLIST}
    return ingestion, scrape


def blocklist_rows(now=None):
    """Every rule as a plain dict for the Blocklist page, newest activity first
    within each effect: id, table ("ingestion" / "scrape"), effect, match
    ("url" / "title" / "domain"), pattern, reason, origin, active, caught (in
    CAUGHT_DAYS days), and for automatic scrape blocks hits / expires_at."""
    from news_fetcher.scraper import BLOCKLIST_MIN_HIT_THRESHOLD
    now = now or datetime.utcnow()
    since = now - timedelta(days=CAUGHT_DAYS)
    seeded_ingestion, seeded_scrape = _seeded()

    caught = dict(
        db.session.query(IngestionBlockHit.block_id, func.sum(IngestionBlockHit.count))
        .filter(IngestionBlockHit.day >= since.date())
        .group_by(IngestionBlockHit.block_id)
    )
    rows = []
    for block in IngestionBlock.query.all():
        rows.append({
            "id": block.id,
            "table": "ingestion",
            "effect": EFFECT_SKIP,
            "match": "url" if block.kind == "source" else "title",
            "pattern": block.pattern,
            "reason": block.note or "",
            "origin": ORIGIN_SEEDED if (block.kind, block.pattern) in seeded_ingestion else ORIGIN_HAND,
            "active": block.is_active,
            "caught": int(caught.get(block.id) or 0),
            "added_at": block.added_at,
        })

    blocked_by_domain = {}
    for (url,) in (db.session.query(Article.url)
                   .filter(Article.scrape_status == "blocked", Article.fetched_at >= since)):
        domain = _domain(url)
        blocked_by_domain[domain] = blocked_by_domain.get(domain, 0) + 1
    for entry in ScrapeBlocklist.query.all():
        if entry.is_permanent:
            origin = ORIGIN_SEEDED if entry.domain in seeded_scrape else ORIGIN_HAND
            active = True
        else:
            origin = ORIGIN_AUTO
            active = ((entry.hit_count or 0) >= BLOCKLIST_MIN_HIT_THRESHOLD
                      and (not entry.expires_at or entry.expires_at > now))
        caught_here = sum(n for d, n in blocked_by_domain.items()
                          if d == entry.domain or d.endswith("." + entry.domain))
        rows.append({
            "id": entry.id,
            "table": "scrape",
            "effect": EFFECT_NO_SCRAPE,
            "match": "domain",
            "pattern": entry.domain,
            "reason": entry.reason or "",
            "origin": origin,
            "active": active,
            "caught": caught_here,
            "added_at": entry.added_at,
            "hits": entry.hit_count,
            "hit_threshold": BLOCKLIST_MIN_HIT_THRESHOLD,
            "expires_at": entry.expires_at,
        })
    rows.sort(key=lambda r: (r["effect"] != EFFECT_NO_SCRAPE, not r["active"], -r["caught"], r["pattern"]))
    return rows


class IngestionBlocks:
    """The active ingestion blocks for one batch, counting which rule
    stopped each article so the Blocklist page can say what every rule
    caught (blocklist_rows()). Ingestion and the Breaking poller
    (news_fetcher/breaking_feed.py) both use it. Patterns are lowercased
    once here, as in get_ingestion_blocks()."""

    def __init__(self):
        self.sources, self.title_keywords = [], []
        for row in IngestionBlock.query.filter_by(is_active=True).all():
            pattern = (row.pattern or "").strip().lower()
            if not pattern:
                continue
            if row.kind == BLOCK_KIND_SOURCE:
                self.sources.append((pattern, row.id))
            elif row.kind == BLOCK_KIND_TITLE:
                self.title_keywords.append((pattern, row.id))
        self.hits = {}

    def _match(self, rules, text, count):
        text = (text or "").lower()
        for pattern, block_id in rules:
            if pattern in text:
                if count:
                    self.hits[block_id] = self.hits.get(block_id, 0) + 1
                return True
        return False

    def blocks_url(self, url, count=True):
        return self._match(self.sources, url, count)

    def blocks_title(self, title, count=True):
        return self._match(self.title_keywords, title, count)

    def save_hits(self):
        """Add this batch's hits to today's counts. Doesn't commit."""
        record_block_hits(self.hits)
        self.hits = {}
