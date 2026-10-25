"""Add article_lean_ratings and articles.bias_source for the Rate lean dialog.

Revision ID: c5d2e8a1f7b4
Revises: b7e3c1d9a4f2
Create Date: 2026-10-25

The dialog shows the AI's rating with its reason and lets a reader pick
another lean with a note; each rating is a row. articles.bias_source says
whether bias_score is the outlet's copy (NULL) or the article's own ("ai",
"reader"), so outlet re-syncs stop overwriting an article's own rating.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c5d2e8a1f7b4'
down_revision = 'b7e3c1d9a4f2'
branch_labels = None
depends_on = None

_NOW = sa.text("(now() at time zone 'utc')")


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "bias_source" not in {c["name"] for c in inspector.get_columns("articles")}:
        op.add_column("articles", sa.Column("bias_source", sa.String(16), nullable=True))
    # create_app() runs db.create_all(), so the table can already exist.
    if "article_lean_ratings" in inspector.get_table_names():
        return
    op.create_table(
        "article_lean_ratings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_NOW),
    )
    op.create_index("ix_lean_article_created", "article_lean_ratings", ["article_id", "created_at"])


def downgrade():
    op.drop_table("article_lean_ratings")
    op.drop_column("articles", "bias_source")
