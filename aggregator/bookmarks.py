"""Bookmarks as time capsules for the MuckScroller app.

Saving a story keeps a copy of everything about it as it stood: the story's
summary, multi-source analysis and opinion roundup, its coverage by leaning
with each outlet's bias, every article's full text, summary and analysis,
the linked columns, every picture, and (from the capture container) a PDF
and screenshot of each publisher's page. Each save or "capture again" is a
new version, so a story that changes or regroups later doesn't change what
was saved.

A version is a directory on disk, BOOKMARK_STORAGE_DIR/<user>/<bookmark>/v<n>/:

    snapshot.json   what was saved, with pictures pointing at assets/<name>
    assets/         the pictures, page PDFs and screenshots

Two jobs fill it: "bookmark_snapshot" (the scheduler's worker: text and
pictures) and then "bookmark_capture" (the capture container's worker:
pages, in aggregator/capture.py). Every byte counts against the user's
storage cap from Settings › Mobile API; a version that would go over it
stops and says so.
"""
import hashlib
import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta

from bs4 import BeautifulSoup
from sqlalchemy import func

from aggregator import jobs
from aggregator.html_safety import sanitize_html
from aggregator.models import (AppSetting, Bookmark, BookmarkSnapshot, OpinionPiece, Story, db)
from aggregator.safe_fetch import BlockedURL, fetch

logger = logging.getLogger(__name__)

SNAPSHOT_JOB = "bookmark_snapshot"
CAPTURE_JOB = "bookmark_capture"
DELETE_JOB = "bookmark_delete"
JOB_PRIORITY = 3
FORMAT = "muckscraper-time-capsule"
FORMAT_VERSION = 1

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES = 200
# Pages captured per version: the story's articles, newest first.
MAX_CAPTURES = 25
# The capture container writes this every minute; without a recent beat,
# page captures are marked unavailable instead of waiting forever.
HEARTBEAT_KEY = "capture_worker_heartbeat"
HEARTBEAT_FRESH = timedelta(minutes=5)

IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp", "image/avif": "avif"}
ASSET_TYPES = {**{ext: kind for kind, ext in IMAGE_TYPES.items()}, "pdf": "application/pdf"}
ASSET_NAME = re.compile(r"^[0-9a-f]{20}(?:-(?:page|shot))?\.(jpg|png|gif|webp|avif|pdf)$")


class OverQuota(Exception):
    """Saving more would take the user past their storage cap."""


# Storage ------------------------------------------------------------------

def storage_root():
    return os.environ.get("BOOKMARK_STORAGE_DIR", "/data/bookmarks")


def version_dir(snapshot):
    """The snapshot's directory, built only from ids, never from input."""
    b = snapshot.bookmark
    return os.path.join(storage_root(), str(int(b.user_id)), str(int(b.id)), f"v{int(snapshot.version)}")


def asset_path(snapshot, name):
    """The file for asset `name`, or None when the name isn't one we write."""
    if not isinstance(name, str) or not ASSET_NAME.match(name):
        return None
    return os.path.join(version_dir(snapshot), "assets", name)


def asset_type(name):
    return ASSET_TYPES[name.rsplit(".", 1)[1]]


def read_snapshot(snapshot):
    with open(os.path.join(version_dir(snapshot), "snapshot.json"), encoding="utf-8") as f:
        return json.load(f)


def _write_json(directory, data):
    path = os.path.join(directory, "snapshot.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.chmod(tmp, 0o664)
    os.replace(tmp, path)
    return os.path.getsize(path)


def _bookmark_dir_for(user_id, bookmark_id):
    return os.path.join(storage_root(), str(int(user_id)), str(int(bookmark_id)))


def remove_files(payload):
    """Job handler: delete a removed bookmark's files. Runs in the
    scheduler, which mounts the storage read-write (the API can't)."""
    shutil.rmtree(_bookmark_dir_for(payload["user_id"], payload["bookmark_id"]), ignore_errors=True)


def _open_up(directory):
    """Let the capture container (another user) add files to the version."""
    for path in (directory, os.path.join(directory, "assets")):
        os.chmod(path, 0o777)


# Caps -----------------------------------------------------------------------

def quota_bytes(user_id):
    """The user's storage cap in bytes, or None for unlimited."""
    from aggregator.api import settings
    config = settings.load()
    override = config["bookmark_quota_overrides"].get(str(user_id))
    if override is not None:
        return None if override == 0 else override * 1024 * 1024
    default = config["bookmark_quota_mb"]
    return None if default is None else default * 1024 * 1024


def used_bytes(user_id, exclude_snapshot_id=None):
    query = (db.session.query(func.coalesce(func.sum(BookmarkSnapshot.size_bytes), 0))
             .join(Bookmark).filter(Bookmark.user_id == user_id))
    if exclude_snapshot_id is not None:
        query = query.filter(BookmarkSnapshot.id != exclude_snapshot_id)
    return int(query.scalar() or 0)


class Budget:
    """Bytes a version may still write: the cap minus everything else the
    user keeps, minus what this version has written so far."""

    def __init__(self, user_id, snapshot_id, already=0):
        cap = quota_bytes(user_id)
        self.left = None if cap is None else cap - used_bytes(user_id, snapshot_id) - already

    def take(self, size):
        if self.left is not None:
            if size > self.left:
                raise OverQuota()
            self.left -= size


# Saving -----------------------------------------------------------------------

def save(user_id, story):
    """Bookmark `story` for the user (or find the bookmark) and queue a new
    version. Returns (bookmark, snapshot). Caller commits."""
    bookmark = Bookmark.query.filter_by(user_id=user_id, story_id=story.id).first()
    if bookmark is None:
        bookmark = Bookmark(user_id=user_id, story_id=story.id, title=(story.display_headline or story.title)[:500])
        db.session.add(bookmark)
        db.session.flush()
    return bookmark, new_version(bookmark)


def new_version(bookmark):
    """Queue a fresh snapshot of a bookmark whose story still exists."""
    last = db.session.query(func.max(BookmarkSnapshot.version)).filter_by(bookmark_id=bookmark.id).scalar() or 0
    snapshot = BookmarkSnapshot(bookmark_id=bookmark.id, version=last + 1, created_at=datetime.utcnow())
    db.session.add(snapshot)
    db.session.flush()
    jobs.enqueue(SNAPSHOT_JOB, {"snapshot_id": snapshot.id}, priority=JOB_PRIORITY)
    return snapshot


def in_flight(bookmark):
    return any(s.status in ("pending", "building") or s.captures in ("running",) for s in bookmark.snapshots)


# Building a version -------------------------------------------------------------

def _paragraphs(text):
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _story_part(story, pieces):
    from aggregator.api import serializers as ser
    out = ser.story_detail(story, pieces)
    out.update({
        "summary_generated_at": ser.iso(story.summary_generated_at),
        "headline_is_ai": bool(story.has_ai_headline),
        "original_title": story.title,
    })
    return out


def _article_part(article):
    from aggregator.api import serializers as ser
    out = ser.article_detail(article, full_text=True)
    outlet = article.outlet
    out.update({
        "bias_score": article.bias_score,
        "outlet_bias_score": outlet.bias_score if outlet else None,
        "fetched_at": ser.iso(article.fetched_at),
        "image": None,
    })
    return out


def _opinion_part(piece):
    from aggregator.api import serializers as ser
    out = ser.opinion_piece(piece)
    out.update({
        "bias_score": piece.bias_score,
        "teaser_html": sanitize_html(piece.content) if piece.content else None,
        "full_text_html": sanitize_html(piece.full_text) if piece.full_text else None,
        "image_url": piece.image_url if (piece.image_url or "").startswith(("http://", "https://")) else None,
        "image": None,
    })
    return out


class _Images:
    """Downloads each picture once into assets/ and remembers where it went."""

    def __init__(self, assets_dir, budget):
        self.dir = assets_dir
        self.budget = budget
        self.by_url = {}
        self.records = {}
        self.failed = 0

    def get(self, url):
        """'assets/<name>' for the picture at `url`, or None if it can't be kept."""
        if not url or not url.startswith(("http://", "https://")):
            return None
        if url in self.by_url:
            return self.by_url[url]
        self.by_url[url] = None
        if len(self.records) >= MAX_IMAGES:
            return None
        try:
            body, kind, _ = fetch(url, MAX_IMAGE_BYTES, accept_prefix="image/")
        except (BlockedURL, ValueError, OSError) as e:
            logger.info("[Bookmarks] Skipped picture %s: %s", url[:200], e)
            self.failed += 1
            return None
        except Exception as e:  # requests' own errors: timeouts, TLS, resets
            logger.info("[Bookmarks] Skipped picture %s: %s", url[:200], e)
            self.failed += 1
            return None
        ext = IMAGE_TYPES.get(kind)
        if ext is None:
            self.failed += 1
            return None
        self.budget.take(len(body))
        name = f"{hashlib.sha256(url.encode()).hexdigest()[:20]}.{ext}"
        with open(os.path.join(self.dir, name), "wb") as f:
            f.write(body)
        self.records[name] = {"source_url": url, "content_type": kind, "bytes": len(body), "kind": "image"}
        self.by_url[url] = f"assets/{name}"
        return self.by_url[url]

    def rewrite_html(self, html):
        """The HTML with each <img> pointing at its saved copy (or removed)."""
        if not html:
            return html
        soup = BeautifulSoup(html, "html.parser")
        for img in soup.find_all("img"):
            local = self.get(img.get("src"))
            if local:
                img["src"] = local
                img["data-source-url"] = img.get("data-source-url") or ""
            else:
                img.decompose()
        for img in soup.find_all("img"):
            if not img.get("data-source-url"):
                del img["data-source-url"]
        return str(soup)


def build_snapshot(payload):
    """Job handler: write a version's text and pictures, then hand the page
    captures to the capture container."""
    snapshot = BookmarkSnapshot.query.get(payload["snapshot_id"])
    if snapshot is None:
        return
    bookmark = snapshot.bookmark
    story = Story.query.get(bookmark.story_id) if bookmark.story_id else None
    if story is None:
        raise RuntimeError("The story no longer exists; earlier versions are kept.")
    snapshot.status = "building"
    db.session.commit()

    directory = version_dir(snapshot)
    shutil.rmtree(directory, ignore_errors=True)
    os.makedirs(os.path.join(directory, "assets"))
    _open_up(directory)
    budget = Budget(bookmark.user_id, snapshot.id)
    images = _Images(os.path.join(directory, "assets"), budget)
    pieces = OpinionPiece.query.filter_by(story_id=story.id).all()
    now = datetime.utcnow()
    try:
        data = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "version": snapshot.version,
            "captured_at": now.replace(microsecond=0).isoformat() + "Z",
            "bookmark": {"story_id": story.id, "saved_at": bookmark.created_at.replace(microsecond=0).isoformat() + "Z"},
            "story": _story_part(story, pieces),
            "articles": [],
            "opinion": [],
            "captures": [],
            "assets": {},
        }
        data["story"]["image"] = images.get(data["story"].get("image_url"))
        for article in sorted(story.articles, key=lambda a: a.date or datetime.min, reverse=True):
            part = _article_part(article)
            part["image"] = images.get(part.get("image_url"))
            part["content_html"] = images.rewrite_html(part.get("content_html"))
            data["articles"].append(part)
        for piece in pieces:
            part = _opinion_part(piece)
            part["image"] = images.get(part["image_url"])
            for field in ("teaser_html", "full_text_html"):
                part[field] = images.rewrite_html(part[field])
            data["opinion"].append(part)
        data["assets"] = images.records
        data["pictures_skipped"] = images.failed
        json_size = len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())
        budget.take(json_size)
    except OverQuota:
        shutil.rmtree(directory, ignore_errors=True)
        snapshot.status, snapshot.captures, snapshot.size_bytes = "over_quota", "over_quota", 0
        snapshot.error = "This would go over your storage limit."
        snapshot.finished_at = datetime.utcnow()
        db.session.commit()
        return

    size = _write_json(directory, data) + sum(r["bytes"] for r in images.records.values())
    snapshot.size_bytes = size
    snapshot.asset_count = len(images.records)
    snapshot.status = "ready"
    snapshot.finished_at = datetime.utcnow()
    if capture_available():
        snapshot.captures = "pending"
        db.session.commit()
        jobs.enqueue(CAPTURE_JOB, {"snapshot_id": snapshot.id}, priority=JOB_PRIORITY)
    else:
        snapshot.captures = "unavailable"
        db.session.commit()


def fail_snapshot(payload, message):
    snapshot = BookmarkSnapshot.query.get(payload.get("snapshot_id"))
    if snapshot is None:
        return
    snapshot.status, snapshot.captures = "failed", "failed"
    snapshot.error = (message or "Failed.")[:300]
    snapshot.finished_at = datetime.utcnow()
    db.session.commit()


def capture_available(now=None):
    row = AppSetting.query.get(HEARTBEAT_KEY)
    if row is None or not row.value:
        return False
    try:
        beat = datetime.fromisoformat(row.value)
    except ValueError:
        return False
    return (now or datetime.utcnow()) - beat < HEARTBEAT_FRESH


def beat(now=None):
    """The capture container says it is alive. Commits."""
    row = AppSetting.query.get(HEARTBEAT_KEY)
    if row is None:
        row = AppSetting(key=HEARTBEAT_KEY)
        db.session.add(row)
    row.value = (now or datetime.utcnow()).isoformat()
    db.session.commit()


def capture_targets(data):
    """(article id, url) pairs to capture: the saved articles with a public
    link, newest first, at most MAX_CAPTURES."""
    targets = []
    for article in data.get("articles", []):
        url = article.get("url")
        if url and url.startswith(("http://", "https://")):
            targets.append((article.get("id"), url))
    return targets[:MAX_CAPTURES]


def add_capture(snapshot, data, article_id, url, pdf_bytes, png_bytes, budget):
    """Store one page's PDF and screenshot in the version. Returns the record."""
    stem = hashlib.sha256(url.encode()).hexdigest()[:20]
    record = {"article_id": article_id, "url": url, "pdf": None, "screenshot": None,
              "captured_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z"}
    for body, suffix, kind, field in ((pdf_bytes, "page.pdf", "application/pdf", "pdf"),
                                      (png_bytes, "shot.jpg", "image/jpeg", "screenshot")):
        if not body:
            continue
        budget.take(len(body))
        name = f"{stem}-{suffix}"
        with open(os.path.join(version_dir(snapshot), "assets", name), "wb") as f:
            f.write(body)
        data["assets"][name] = {"source_url": url, "content_type": kind, "bytes": len(body),
                                "kind": "page_pdf" if field == "pdf" else "page_screenshot"}
        record[field] = f"assets/{name}"
    data["captures"].append(record)
    return record


def finish_captures(snapshot, data, state):
    """Write the updated snapshot.json and the version's new size. Commits."""
    directory = version_dir(snapshot)
    size = _write_json(directory, data) + sum(r["bytes"] for r in data["assets"].values())
    snapshot.size_bytes = size
    snapshot.asset_count = len(data["assets"])
    snapshot.captures = state
    snapshot.finished_at = datetime.utcnow()
    db.session.commit()


def fail_capture(payload, message):
    snapshot = BookmarkSnapshot.query.get(payload.get("snapshot_id"))
    if snapshot is None:
        return
    snapshot.captures = "failed"
    snapshot.error = (message or "Failed.")[:300]
    db.session.commit()


def delete(bookmark):
    """Remove a bookmark and its versions, and queue removing their files.
    Commits."""
    payload = {"user_id": bookmark.user_id, "bookmark_id": bookmark.id}
    db.session.delete(bookmark)
    db.session.flush()
    jobs.enqueue(DELETE_JOB, payload, priority=JOB_PRIORITY)


jobs.register(SNAPSHOT_JOB, build_snapshot, fail_snapshot)
jobs.register(DELETE_JOB, remove_files)
