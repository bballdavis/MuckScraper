"""Read state: the stories and articles the user marked read, shared with
the web app (aggregator/reads.py). A story marked read here is hidden from
Headlines in the browser, and the other way round. Story cards in editions,
/stories and /stories/{id} carry `is_read`; articles from /articles/{id}.
"""
from flask import g, jsonify

from aggregator import db, reads

from . import api, serializers as ser
from .auth import require_token
from .errors import ApiError
from .params import MAX_PAGE_SIZE, cursor, enum, integer, query_args
from .routes import DEFAULT_PAGE_SIZE, _page

# URL segment -> reads kind.
_KINDS = {"stories": "story", "articles": "article"}


def _kind(segment):
    if segment not in _KINDS:
        raise ApiError(404, "not_found", "Not found.")
    return _KINDS[segment]


@api.route("/me/read")
@require_token()
def get_read():
    """Everything the user marked read, newest first."""
    args = query_args(kind=enum(*reads.KINDS), cursor=cursor, limit=integer(1, MAX_PAGE_SIZE))
    offset, limit = args["cursor"] or 0, args["limit"] or DEFAULT_PAGE_SIZE
    rows = reads.recent(g.user.id, offset=offset, limit=limit + 1, kind=args["kind"])
    return jsonify(_page([ser.read_item(r) for r in rows], offset, limit))


@api.route("/me/read/<segment>/<int:item_id>", methods=["PUT"])
@require_token()
def mark_read(segment, item_id):
    """Mark a story or article read. Doing it again moves it to the top."""
    query_args()
    row = reads.mark(g.user.id, _kind(segment), item_id)
    if row is None:
        raise ApiError(404, "not_found", "Not found.")
    db.session.commit()
    return jsonify(ser.read_item(row))


@api.route("/me/read/<segment>/<int:item_id>", methods=["DELETE"])
@require_token()
def mark_unread(segment, item_id):
    """Mark it unread again (204 whether or not it was read)."""
    query_args()
    reads.unmark(g.user.id, _kind(segment), item_id)
    db.session.commit()
    return "", 204

