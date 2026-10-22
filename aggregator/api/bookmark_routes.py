"""Bookmarks: the app's saved stories, kept as versioned time capsules
(aggregator/bookmarks.py). Saving or capturing again needs the "summarize"
scope (it queues background work) and counts against the per-user daily
limit; reading, downloading and deleting need only "read".
"""
import io
import os
import zipfile

from flask import g, jsonify, send_file

from aggregator import bookmarks, db, newsbox
from aggregator.models import Bookmark, BookmarkSnapshot, Story

from . import api, serializers as ser
from .auth import require_token
from .errors import ApiError
from .params import bad, integer, json_body, query_args
from .ratelimit import check_bookmark_snapshots
from .routes import _story_load

# A zip export is built in memory; past this, ask for one version at a time.
MAX_EXPORT_BYTES = 200 * 1024 * 1024


def _usage():
    cap = bookmarks.quota_bytes(g.user.id)
    return {"used_bytes": bookmarks.used_bytes(g.user.id), "quota_bytes": cap}


def _own(bookmark_id):
    """The user's bookmark by its own id: the story it saved may be gone."""
    bookmark = Bookmark.query.filter_by(user_id=g.user.id, id=bookmark_id).first()
    if bookmark is None:
        raise ApiError(404, "not_found", "No such bookmark.")
    return bookmark


def _own_version(bookmark_id, version):
    snapshot = BookmarkSnapshot.query.filter_by(bookmark_id=_own(bookmark_id).id, version=version).first()
    if snapshot is None:
        raise ApiError(404, "not_found", "No such version.")
    return snapshot


@api.route("/me/bookmarks")
@require_token()
def list_bookmarks():
    query_args()
    rows = Bookmark.query.filter_by(user_id=g.user.id).order_by(Bookmark.created_at.desc(), Bookmark.id.desc()).all()
    return jsonify({"items": [ser.bookmark(b) for b in rows], **_usage()})


@api.route("/me/bookmarks", methods=["POST"])
@require_token("summarize")
def save_bookmark():
    """Bookmark a story ({"story_id"}): keeps a new time-capsule version and
    sends the story to NewsBox as saved. 202: the version is being written.
    Saving a story already bookmarked adds a version."""
    story_id = json_body(story_id=integer(1, 2**31 - 1))["story_id"]
    if story_id is None:
        raise bad("story_id is required.")
    story = Story.query.options(*_story_load()).get(story_id)
    if story is None:
        raise ApiError(404, "not_found", "Not found.")
    existing = Bookmark.query.filter_by(user_id=g.user.id, story_id=story_id).first()
    if existing is not None and bookmarks.in_flight(existing):
        return jsonify(ser.bookmark(existing, versions=True)), 202
    check_bookmark_snapshots(g.user)
    bookmark, _ = bookmarks.save(g.user.id, story)
    newsbox.queue_story(g.user.id, story, "saved")
    db.session.commit()
    return jsonify(ser.bookmark(bookmark, versions=True)), 202


@api.route("/me/bookmarks/<int:bookmark_id>")
@require_token()
def get_bookmark(bookmark_id):
    query_args()
    return jsonify(ser.bookmark(_own(bookmark_id), versions=True))


@api.route("/me/bookmarks/<int:bookmark_id>", methods=["DELETE"])
@require_token()
def delete_bookmark(bookmark_id):
    bookmarks.delete(_own(bookmark_id))
    db.session.commit()
    return "", 204


@api.route("/me/bookmarks/<int:bookmark_id>/versions/<int:version>")
@require_token()
def get_bookmark_version(bookmark_id, version):
    """The saved time capsule. Pictures and captures are named
    "assets/<name>": fetch them from .../versions/{version}/assets/{name}."""
    query_args()
    snapshot = _own_version(bookmark_id, version)
    if snapshot.status != "ready":
        raise ApiError(409, "not_ready", "This version isn't ready.")
    try:
        return jsonify(bookmarks.read_snapshot(snapshot))
    except (OSError, ValueError):
        raise ApiError(410, "gone", "This version's files are missing.")


@api.route("/me/bookmarks/<int:bookmark_id>/versions/<int:version>/assets/<name>")
@require_token()
def get_bookmark_asset(bookmark_id, version, name):
    query_args()
    snapshot = _own_version(bookmark_id, version)
    path = bookmarks.asset_path(snapshot, name)
    if path is None or not os.path.isfile(path):
        raise ApiError(404, "not_found", "No such file.")
    return send_file(path, mimetype=bookmarks.asset_type(name), max_age=0, conditional=True,
                     download_name=name)


@api.route("/me/bookmarks/<int:bookmark_id>/export")
@require_token()
def export_bookmark(bookmark_id):
    """Every ready version as one zip: v<n>/snapshot.json and v<n>/assets/."""
    query_args()
    bookmark = _own(bookmark_id)
    ready = [s for s in bookmark.snapshots if s.status == "ready"]
    if not ready:
        raise ApiError(409, "not_ready", "No version is ready yet.")
    if sum(s.size_bytes for s in ready) > MAX_EXPORT_BYTES:
        raise ApiError(413, "too_large", "Too large to export at once; download versions one at a time.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for snapshot in ready:
            root = bookmarks.version_dir(snapshot)
            for folder, _, files in os.walk(root):
                for filename in files:
                    if filename.endswith(".tmp"):
                        continue
                    full = os.path.join(folder, filename)
                    archive.write(full, os.path.join(f"v{snapshot.version}", os.path.relpath(full, root)))
    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True,
                     download_name=f"muckscraper-bookmark-{bookmark_id}.zip")


@api.route("/me/bookmarks/<int:bookmark_id>/versions", methods=["POST"])
@require_token("summarize")
def capture_again(bookmark_id):
    """Keep a new version of the story as it stands now."""
    bookmark = _own(bookmark_id)
    if bookmark.story_id is None or Story.query.get(bookmark.story_id) is None:
        raise ApiError(409, "story_gone", "The story no longer exists; the saved versions are kept.")
    if bookmarks.in_flight(bookmark):
        raise ApiError(409, "busy", "A version is still being written.")
    check_bookmark_snapshots(g.user)
    bookmarks.new_version(bookmark)
    db.session.commit()
    return jsonify(ser.bookmark(bookmark, versions=True)), 202
