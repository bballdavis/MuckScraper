"""Find articles that are the same text as another article in their story.

Measured over 30 days on 2026-10-11 with 8-word shingles: of 32,574 pairs of
articles from different outlets in the same story, 31,555 shared under 10%
of their text and only ~170 shared half or more. Those were the New York Post
republishing Fox News Digital (44 pairs, credited in the text) and outlets
running the same AP copy (NPR, CBS, ABC, the Post, the Guardian). Between
20% and 50% are AP-based pieces with an outlet's own reporting added, and
independent pieces that quote the same statement; those stay separate.

In a group of copies the original is the article the others credit by
outlet name ("Fox News Digital" in the Post's copy), else the earliest
published. Every other member gets copy_of_id = the original.

muckscraper.news leaves the copy out of the story; the
aggregator keeps it (see aggregator/article_copies.py).
"""
import html
import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

COPY_MIN_OVERLAP = 0.5   # share of the shorter text's 8-word shingles
SHINGLE_WORDS = 8
MIN_TEXT_CHARS = 500     # same floor as has_good_original / CLAUDE.md
MIN_SHINGLES = 30
LOOKBACK_HOURS = 48

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9]+")


def plain_text(content):
    text = html.unescape(_TAG.sub(" ", content or ""))
    return _SPACE.sub(" ", text).strip()


def shingles(text, size=SHINGLE_WORDS):
    words = _WORD.findall(text.lower().replace("’", "'"))
    return {tuple(words[i:i + size]) for i in range(len(words) - size + 1)}


def containment(a, b):
    """Share of the smaller shingle set found in the other."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def find_copies(items, min_overlap=COPY_MIN_OVERLAP):
    """items: dicts with id, outlet (a key), name (outlet display name),
    text (plain text), date. Returns {copy_id: (original_id, overlap)}.

    Pure: no DB access. Articles from the same outlet are never paired (a
    duplicate from one outlet is not a second source either way)."""
    usable = []
    for item in items:
        text = item.get("text") or ""
        if len(text) < MIN_TEXT_CHARS:
            continue
        grams = shingles(text)
        if len(grams) < MIN_SHINGLES:
            continue
        usable.append(dict(item, grams=grams))

    parent = {item["id"]: item["id"] for item in usable}

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    overlap = {}
    for i, a in enumerate(usable):
        for b in usable[i + 1:]:
            if a["outlet"] == b["outlet"]:
                continue
            c = containment(a["grams"], b["grams"])
            if c >= min_overlap:
                overlap[(a["id"], b["id"])] = overlap[(b["id"], a["id"])] = c
                parent[root(a["id"])] = root(b["id"])

    groups = {}
    for item in usable:
        groups.setdefault(root(item["id"]), []).append(item)

    result = {}
    for members in groups.values():
        if len(members) < 2:
            continue

        def credited(item):
            name = item.get("name") or ""
            return sum(
                1 for other in members
                if other["outlet"] != item["outlet"] and name and name in other["text"]
            )

        original = min(
            members,
            key=lambda m: (-credited(m), m.get("date") or datetime.max, m["id"]),
        )
        for member in members:
            if member["id"] == original["id"]:
                continue
            direct = overlap.get((member["id"], original["id"]))
            if direct is None:
                direct = max(v for (x, _), v in overlap.items() if x == member["id"])
            result[member["id"]] = (original["id"], round(direct, 3))
    return result


def _outlet_key(article):
    if article.outlet_id is not None:
        return ("id", article.outlet_id)
    return ("name", (article.source or "").strip().lower())


def mark_story_copies(story, now=None):
    """Recompute copy_of_id for every article in one story. Returns the
    list of (copy, original, overlap) newly marked."""
    now = now or datetime.utcnow()
    articles = list(story.articles)
    items = [
        {
            "id": a.id,
            "outlet": _outlet_key(a),
            "name": (a.outlet.name if a.outlet else None) or a.source,
            "text": plain_text(a.content),
            "date": a.date,
        }
        for a in articles
    ]
    copies = find_copies(items) if len(articles) > 1 else {}
    by_id = {a.id: a for a in articles}
    newly = []
    for a in articles:
        target = copies.get(a.id)
        new_id, new_overlap = target if target else (None, None)
        if a.copy_of_id != new_id and new_id is not None:
            newly.append((a, by_id[new_id], new_overlap))
        a.copy_of_id = new_id
        a.copy_overlap = new_overlap
        a.copy_checked_at = now
    return newly


def mark_recent_copies(hours=LOOKBACK_HOURS):
    """Pipeline step: check every story with an article fetched in the last
    `hours` that has articles from two or more outlets. Idempotent."""
    from aggregator.models import Story, db

    since = datetime.utcnow() - timedelta(hours=hours)
    story_ids = [row[0] for row in db.session.execute(db.text("""
        SELECT story_id FROM articles
        WHERE story_id IN (SELECT DISTINCT story_id FROM articles WHERE fetched_at >= :since)
        GROUP BY story_id
        HAVING count(DISTINCT coalesce(outlet_id::text, lower(source))) > 1
    """), {"since": since})]

    now = datetime.utcnow()
    marked = 0
    total_copies = 0
    for start in range(0, len(story_ids), 100):
        for story in Story.query.filter(Story.id.in_(story_ids[start:start + 100])).all():
            for copy, original, overlap in mark_story_copies(story, now=now):
                marked += 1
                logger.info(
                    "  [Copies] Article %s (%s) is the same report as %s (%s), %d%% shared text",
                    copy.id, copy.source, original.id, original.source, round(overlap * 100),
                )
            total_copies += sum(1 for a in story.articles if a.copy_of_id)
        db.session.commit()

    logger.info(
        "  [Copies] Checked %d stories: %d copies, %d newly marked",
        len(story_ids), total_copies, marked,
    )
    return {"status": "ok", "stories_checked": len(story_ids), "copies": total_copies, "newly_marked": marked}
