"""NewsBox origins and schedule amounts in client-neutral terms.

Revision ID: f9d4b2c7e1a3
Revises: e8c3a1f6d4b2
Create Date: 2026-10-22

Origins were named after the iPhone app's gestures: "swiped" is now
"requested" and "auto" is now "scheduled". A schedule's Headlines section
counted pages of 20 while topics counted stories; every section now counts
stories, so stored Headlines amounts are multiplied by 20.
"""
import json

from alembic import op
import sqlalchemy as sa

revision = 'f9d4b2c7e1a3'
down_revision = 'e8c3a1f6d4b2'
branch_labels = None
depends_on = None

HEADLINES = "Headlines"
PAGE = 20


def _rescale(factor, divide=False):
    conn = op.get_bind()
    for user_id, raw in conn.execute(sa.text("SELECT user_id, settings FROM summary_schedules")).fetchall():
        try:
            settings = json.loads(raw or "{}")
        except ValueError:
            continue
        changed = False
        for rule in settings.get("sections") or []:
            if isinstance(rule, dict) and rule.get("section") == HEADLINES and isinstance(rule.get("amount"), int):
                rule["amount"] = max(1, -(-rule["amount"] // factor)) if divide else min(rule["amount"] * factor, 100)
                changed = True
        if changed:
            conn.execute(sa.text("UPDATE summary_schedules SET settings = :s WHERE user_id = :u"),
                         {"s": json.dumps(settings), "u": user_id})


def upgrade():
    op.execute("UPDATE newsbox_items SET origin = 'requested' WHERE origin = 'swiped'")
    op.execute("UPDATE newsbox_items SET origin = 'scheduled' WHERE origin = 'auto'")
    _rescale(PAGE)


def downgrade():
    op.execute("UPDATE newsbox_items SET origin = 'swiped' WHERE origin = 'requested'")
    op.execute("UPDATE newsbox_items SET origin = 'auto' WHERE origin = 'scheduled'")
    _rescale(PAGE, divide=True)
