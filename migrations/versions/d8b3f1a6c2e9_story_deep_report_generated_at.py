"""Add stories.deep_report_generated_at.

Revision ID: d8b3f1a6c2e9
Revises: c5d2e8a1f7b4
Create Date: 2026-10-25

The story page offers Regenerate for the summary or the multi-source
analysis only when sources have arrived since it was written. The summary already has summary_generated_at; this is the
analysis's. Existing rows stay NULL and fall back to summary_generated_at.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd8b3f1a6c2e9'
down_revision = 'c5d2e8a1f7b4'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    # create_app() runs db.create_all(), so the column can already exist.
    if "deep_report_generated_at" not in {c["name"] for c in inspector.get_columns("stories")}:
        op.add_column("stories", sa.Column("deep_report_generated_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("stories", "deep_report_generated_at")
