"""Read state for signed-in readers (aggregator/reads.py): mark a story or
article read or unread, and the Recently Read page. Its own blueprint
because these routes write; public.py's pages only read. Headlines, the
story page and the article page look the state up themselves.
"""
from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user, login_required

from aggregator import db, reads

reading = Blueprint("reading", __name__)


def _read_payload(kind, item_id):
    row = reads.get(current_user.id, kind, item_id)
    return {"kind": kind, "id": item_id, "is_read": row is not None,
            "read_at": row.read_at.isoformat() + "Z" if row else None}


@reading.route("/read/<kind>/<int:item_id>", methods=["POST", "DELETE"])
@login_required
def toggle_read(kind, item_id):
    """Mark a story or article read (POST) or unread (DELETE) for the
    signed-in reader: the reading-glasses buttons on Headlines, the story
    and article pages, and Recently Read."""
    if kind not in reads.KINDS:
        abort(404)
    if request.method == "POST":
        if reads.mark(current_user.id, kind, item_id) is None:
            abort(404)
    else:
        reads.unmark(current_user.id, kind, item_id)
    db.session.commit()
    return jsonify(_read_payload(kind, item_id))


@reading.route("/read")
@login_required
def recently_read():
    """Everything the reader marked read, newest first, with a way to mark
    each one unread. ?q= searches titles and outlet names."""
    per_page = 50
    page = max(1, request.args.get("page", 1, type=int) or 1)
    q = (request.args.get("q") or "").strip()[:200]
    total = reads.count(current_user.id, q=q)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    return render_template(
        "recently_read.html",
        items=reads.recent(current_user.id, offset=(page - 1) * per_page, limit=per_page, q=q),
        q=q,
        total=total,
        page=page,
        total_pages=total_pages,
        active_nav="read",
    )
